"""状态机组合测试 —— 随机轨迹下的不变量（有限执行模型）。

用固定种子生成 Attempt 回执序列、租约过期与迟到回执的交错，
每一步之后检查全局不变量。失败时输出种子与事件序列，可复现。

不变量：
S1 账本恒等：reserved = Σ(reserved+uncertain)，committed = Σ(committed)，
   且 reserved、committed 非负、两者之和不超过额度。
S2 committed 单调不减。
S3 CONFIRMED_SUCCEEDED 一旦到达不再离开。
S4 无新增 Attempt 时，离开 CONFIRMED_NOT_APPLIED 必须有冲突标记；正常重试须有新 Attempt。
S5 确认成功后不再产生新的外部调用。
S6 授权占用次数不超过 use_limit。
"""
from __future__ import annotations

import random
import unittest
from unittest.mock import patch

from asg import (AsgError, Authorizer, Gateway, Ledger, WriteService,
                 compute_operation_digest, connect, init_schema)
from asg.gateway import Connector, ConnectorContract
from asg.ids import uuid7

WS = "ws-sm"
AGENT = {"kind": "agent", "id": "agent-1"}
APPROVER = {"kind": "human", "id": "approver-1"}
GW = {"kind": "service", "id": "effect-gateway"}
DEST = "cluster-a/job-queue-1"
FAR = "2030-01-01T00:00:00Z"
LIMIT = 1000.0
AMOUNT = 60.0

OUTCOMES = ["CONFIRMED_SUCCEEDED", "CONFIRMED_NOT_APPLIED", "OUTCOME_UNKNOWN"]


class RandomRemote(Connector):
    """行为由种子驱动的连接器：调用结果与查询可见性都随机，但同键结果稳定。"""

    def __init__(self, rng: random.Random, mode: str):
        super().__init__(ConnectorContract(mode=mode, supports_job_query=True,
                                           final_query_guarantee=False))
        self.rng = rng
        self.mode = mode
        self.store: dict[str, dict] = {}
        self.calls = 0
        self.job = "job-" + uuid7()

    def call(self, op, args, key):
        self.calls += 1
        key = key or self.job
        if key not in self.store:
            self.store[key] = {"outcome": self.rng.choice(OUTCOMES),
                               "external_job_id": self.job, "clause": "random"}
        return dict(self.store[key])

    def query_by_key(self, key):
        if self.rng.random() < 0.4:  # 暂时不可见
            return None
        return dict(self.store[key]) if key in self.store else None

    def query_job(self, job):
        if self.rng.random() < 0.4:
            return None
        stored = next(iter(self.store.values()), None)
        return dict(stored) if stored else None


def check_invariants(conn, ledger, state_history, calls_at_success, remote) -> list[str]:
    """返回违反的不变量描述列表。"""
    bad = []
    rows = conn.execute("SELECT state, amount FROM reservation").fetchall()
    reserved = sum(r["amount"] for r in rows if r["state"] in ("reserved", "uncertain"))
    committed = sum(r["amount"] for r in rows if r["state"] == "committed")
    bal = ledger.balance("ws:compute.gpu")
    if abs(bal["reserved"] - reserved) > 1e-6 or abs(bal["committed"] - committed) > 1e-6:
        bad.append(f"S1 账本不一致 ledger={bal['reserved']}/{bal['committed']} "
                   f"rows={reserved}/{committed}")
    if bal["reserved"] < -1e-6 or bal["committed"] < -1e-6 \
            or bal["reserved"] + bal["committed"] > LIMIT + 1e-6:
        bad.append(f"S1 额度越界 {bal}")
    if len(state_history) >= 2 and bal["committed"] + 1e-6 < state_history[-2][1]:
        bad.append("S2 committed 下降")
    states = [entry[0] for entry in state_history]
    if "CONFIRMED_SUCCEEDED" in states:
        first = states.index("CONFIRMED_SUCCEEDED")
        if any(s != "CONFIRMED_SUCCEEDED" for s in states[first:]):
            bad.append(f"S3 成功态被离开 {states}")
        if remote.calls != calls_at_success:
            bad.append(f"S5 成功后仍有外部调用 {calls_at_success}->{remote.calls}")
    if len(states) >= 2 and states[-2] == "CONFIRMED_NOT_APPLIED" \
            and states[-1] != "CONFIRMED_NOT_APPLIED":
        reason = conn.execute("SELECT unresolved_reason FROM effect_state").fetchone()
        new_attempt = state_history[-1][2] > state_history[-2][2]
        if not new_attempt and (not reason or "conflict" not in (reason["unresolved_reason"] or "")):
            bad.append(f"S4 未生效态无冲突标记地离开 -> {states[-1]}")
    g = conn.execute("SELECT uses_consumed, use_limit FROM grant_record").fetchone()
    if g and g["uses_consumed"] > g["use_limit"]:
        bad.append(f"S6 授权占用超限 {g['uses_consumed']}/{g['use_limit']}")
    return bad


def run_trajectory(seed: int, steps: int = 12, coverage: dict | None = None) -> list[str]:
    rng = random.Random(seed)
    conn = connect()
    try:
        return _run_trajectory(conn, rng, seed, steps, coverage)
    finally:
        conn.close()


def _run_trajectory(conn, rng, seed, steps, coverage):
    init_schema(conn)
    svc = WriteService(conn)
    ledger = Ledger(conn)
    auth = Authorizer(conn, svc)
    gw = Gateway(conn, svc, auth, principal=GW)
    ledger.ensure_budget("ws:compute.gpu", LIMIT)

    mode = rng.choice(["KEYED", "QUERY_ONLY"])
    contract = {"mode": mode, "retry_policy": {"max": rng.choice([1, 2])}}
    if mode == "KEYED":
        contract.update({"key": "idem-" + uuid7(), "key_scope": "c", "key_expiry": FAR})
    payload = {
        "intent_id": "intent-" + uuid7(), "task_revision_id": "t",
        "operation": "compute.job.submit", "connector_identity": "c",
        "connector_version": "1", "executor_principal": AGENT, "target_ids": [DEST],
        "arguments_ref": {"arguments": {"n": seed}}, "artifact_revision_ids": [],
        "precondition_refs": [], "idempotency_contract": contract,
    }
    g = auth.issue_grant(WS, APPROVER, AGENT, compute_operation_digest(payload),
                         "提交", FAR, [DEST], use_limit=rng.choice([1, 3]), expected_head=0)
    try:
        gw.prepare_effect(WS, payload, 1, "req-" + uuid7(), g["grant_id"],
                          [{"resource_key": "ws:compute.gpu", "amount": AMOUNT}])
    except AsgError as error:
        return [f"seed={seed} prepare failed: {error.code}: {error}"]
    iid = payload["intent_id"]
    remote = RandomRemote(rng, mode)

    history: list[tuple] = []
    calls_at_success = None
    events = ["run", "run", "expire", "due", "late"]
    trace = []
    for _ in range(steps):
        event = rng.choice(events)
        trace.append(event)
        try:
            if event == "expire":
                conn.execute("UPDATE effect_outbox SET claim_expires_at='2000-01-01T00:00:00Z'"
                             " WHERE intent_id=?", (iid,))
            elif event == "due":
                conn.execute("UPDATE effect_outbox SET next_retry_at='2000-01-01T00:00:00Z'"
                             " WHERE intent_id=?", (iid,))
            elif event == "late":
                att = conn.execute(
                    "SELECT attempt_id FROM attempt WHERE intent_id=? ORDER BY rowid DESC LIMIT 1",
                    (iid,)).fetchone()
                if att:
                    gw.record_response(WS, att["attempt_id"],
                                       {"outcome": rng.choice(OUTCOMES),
                                        "external_job_id": remote.job, "clause": "late"})
            else:
                gw.run_once(iid, remote)
        except AsgError as error:
            # Only modelled lease/conflict/reconciliation rejections are allowed.
            if error.code not in {"E201", "E603", "E604", "E605", "E606"}:
                return [f"seed={seed} mode={mode} trace={trace} unexpected: {error.code}: {error}"]
        if coverage is not None:
            coverage["steps"] = coverage.get("steps", 0) + 1
        eff = conn.execute("SELECT state FROM effect_state WHERE intent_id=?",
                           (iid,)).fetchone()
        bal = ledger.balance("ws:compute.gpu")
        attempts = conn.execute("SELECT COUNT(*) FROM attempt WHERE intent_id=?", (iid,)).fetchone()[0]
        history.append((eff["state"] if eff else None, bal["committed"], attempts))
        if eff and eff["state"] == "CONFIRMED_SUCCEEDED" and calls_at_success is None:
            calls_at_success = remote.calls
        bad = check_invariants(conn, ledger, history, calls_at_success, remote)
        if bad:
            return [f"seed={seed} mode={mode} trace={trace}"] + bad
    if coverage is not None:
        coverage["trajectories"] = coverage.get("trajectories", 0) + 1
        coverage[mode] = coverage.get(mode, 0) + 1
        coverage["calls"] = coverage.get("calls", 0) + remote.calls
    return []


class TestRandomTrajectories(unittest.TestCase):
    def test_invariants_over_random_trajectories(self):
        failures, coverage = [], {}
        for seed in range(60):
            failures.extend(run_trajectory(seed, coverage=coverage))
        self.assertFalse(failures, "\n".join(failures[:12]))
        self.assertEqual(coverage["trajectories"], 60)
        self.assertEqual(coverage["steps"], 720)
        self.assertGreater(coverage["KEYED"], 0)
        self.assertGreater(coverage["QUERY_ONLY"], 0)
        self.assertGreater(coverage["calls"], 0)

    def test_setup_failure_cannot_pass_vacuously(self):
        with patch.object(Gateway, "prepare_effect", side_effect=AsgError("E101", "injected")):
            self.assertIn("prepare failed", run_trajectory(0)[0])

    def test_unexpected_event_error_is_reported(self):
        with patch.object(Gateway, "run_once", side_effect=AsgError("E101", "injected")):
            self.assertIn("unexpected: E101", run_trajectory(0, steps=100)[0])


if __name__ == "__main__":
    unittest.main()

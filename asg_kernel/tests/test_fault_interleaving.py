"""故障交错轨迹 —— 阶段 D 准入前的第三轮补充验收。

轨迹：
1. 租约过期与旧 worker 迟到回执交错：新 worker 已按对账确认成功并结算后，
   旧 worker 的迟到回执不得改变终态、不得重复外部效果。
2. 查询暂不可见后得到确定结果：UNKNOWN 期间只对账不重发，
   最终性结果到达后消解为确认成功并结算。
3. purge 后历史投影：回放到删除前的序号，正文仍不可读（墓碑跨快照生效）。
"""
from __future__ import annotations

import unittest

from asg import (AsgError, Authorizer, Gateway, Ledger, Projection, Purger,
                 WriteService, compute_operation_digest, connect, init_schema)
from asg.gateway import Connector, ConnectorContract
from asg.ids import uuid7

WS = "ws-f"
AGENT = {"kind": "agent", "id": "agent-1"}
APPROVER = {"kind": "human", "id": "approver-1"}
GW = {"kind": "service", "id": "effect-gateway"}
DEST = "cluster-a/job-queue-1"
FAR = "2030-01-01T00:00:00Z"


def stack():
    conn = connect()
    init_schema(conn)
    svc = WriteService(conn)
    ledger = Ledger(conn)
    auth = Authorizer(conn, svc)
    gw = Gateway(conn, svc, auth, principal=GW)
    ledger.ensure_budget("ws:compute.gpu", 100)
    return conn, svc, ledger, auth, gw


def head(conn):
    r = conn.execute("SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?",
                     (WS,)).fetchone()
    return r["head_sequence"] if r else 0


def due(conn, intent_id):
    conn.execute("UPDATE effect_outbox SET next_retry_at='2000-01-01T00:00:00Z'"
                 " WHERE intent_id=?", (intent_id,))


def prepare(conn, auth, gw, payload):
    h = head(conn)
    g = auth.issue_grant(WS, APPROVER, AGENT, compute_operation_digest(payload),
                         "提交", FAR, [DEST], expected_head=h)
    return gw.prepare_effect(WS, payload, h + 1, "req-" + uuid7(), g["grant_id"],
                             [{"resource_key": "ws:compute.gpu", "amount": 60}])


def intent(mode="KEYED", precond=None):
    contract = {"mode": mode, "retry_policy": {"max": 1}}
    if mode == "KEYED":
        contract.update({"key": "idem-" + uuid7(), "key_scope": "compute.cluster",
                         "key_expiry": FAR})
    return {
        "intent_id": "intent-" + uuid7(), "task_revision_id": "task-1",
        "operation": "compute.job.submit", "connector_identity": "compute.cluster",
        "connector_version": "1.2", "executor_principal": AGENT, "target_ids": [DEST],
        "arguments_ref": {"arguments": {"image": "exp:v1"}}, "artifact_revision_ids": [],
        "precondition_refs": precond or [], "idempotency_contract": contract,
    }


class KeyedRemote(Connector):
    def __init__(self):
        super().__init__(ConnectorContract(mode="KEYED", supports_job_query=True,
                                           final_query_guarantee=True))
        self.store: dict[str, dict] = {}
        self.calls = 0

    def call(self, op, args, key):
        self.calls += 1
        self.store[key] = {"outcome": "CONFIRMED_SUCCEEDED",
                           "external_job_id": "job-" + str(self.calls), "clause": "keyed ok"}
        return self.store[key]

    def query_by_key(self, key):
        return self.store.get(key)


class QueryOnlyRemote(Connector):
    """前 invisible_rounds 次查询返回不可见，之后返回确定成功。"""

    def __init__(self, invisible_rounds: int):
        super().__init__(ConnectorContract(mode="QUERY_ONLY", supports_job_query=True,
                                           final_query_guarantee=False))
        self.left = invisible_rounds
        self.calls = 0
        self.queries = 0

    def call(self, op, args, key):
        self.calls += 1
        return {"outcome": "OUTCOME_UNKNOWN", "external_job_id": "job-q",
                "clause": "query-only: 结果未知"}

    def query_job(self, job):
        self.queries += 1
        if self.left > 0:
            self.left -= 1
            return None
        return {"outcome": "CONFIRMED_SUCCEEDED", "external_job_id": job,
                "clause": "query-only: 最终可见，确认成功"}


class TestLeaseExpiryLateReceipt(unittest.TestCase):
    """租约过期与旧 worker 迟到回执交错。"""

    def test_late_receipt_after_reconcile_keeps_terminal_state(self):
        conn, svc, ledger, auth, gw = stack()
        self.addCleanup(conn.close)
        remote = KeyedRemote()
        p = intent()
        r = prepare(conn, auth, gw, p)

        # worker A：记录尝试并完成外部调用，但在持久化回执前失去租约
        cl = gw.claim(r["intent_id"])
        ctx = gw.begin_attempt(WS, r["intent_id"], cl["token"])
        resp = remote.call(ctx["operation"], ctx["arguments"], ctx["idempotency_key"])
        conn.execute("UPDATE effect_outbox SET claim_expires_at='2000-01-01T00:00:00Z'"
                     " WHERE intent_id=?", (r["intent_id"],))

        # worker B：对账见到远端成功，确认并结算（不再重发）
        out = gw.run_once(r["intent_id"], remote)
        self.assertEqual(out["state"], "CONFIRMED_SUCCEEDED")
        self.assertEqual(remote.calls, 1)

        # worker A 的迟到回执：终态不变、预算不重复结算、无新的外部效果
        state = gw.record_response(WS, ctx["attempt_id"], resp)
        self.assertEqual(state, "CONFIRMED_SUCCEEDED")
        self.assertEqual(gw.effect_status(WS, r["intent_id"])["state"], "CONFIRMED_SUCCEEDED")
        bal = ledger.balance("ws:compute.gpu")
        self.assertEqual(bal["committed"], 60)
        self.assertEqual(bal["reserved"], 0)
        self.assertEqual(remote.calls, 1)
        ob = conn.execute("SELECT state FROM effect_outbox WHERE intent_id=?",
                          (r["intent_id"],)).fetchone()
        self.assertEqual(ob["state"], "DONE")


class TestQueryBecomesVisible(unittest.TestCase):
    """查询暂不可见（E606 维持 UNKNOWN），随后得到确定结果并结算。"""

    def test_unknown_resolves_when_query_becomes_final(self):
        conn, svc, ledger, auth, gw = stack()
        self.addCleanup(conn.close)
        remote = QueryOnlyRemote(invisible_rounds=2)
        p = intent(mode="QUERY_ONLY")
        r = prepare(conn, auth, gw, p)

        first = gw.run_once(r["intent_id"], remote)
        self.assertEqual(first["state"], "OUTCOME_UNKNOWN")
        self.assertEqual(remote.calls, 1)

        # 两次对账均不可见：维持 UNKNOWN，不产生新的外部调用
        for _ in range(2):
            due(conn, r["intent_id"])
            out = gw.run_once(r["intent_id"], remote)
            self.assertEqual(out["state"], "OUTCOME_UNKNOWN")
            self.assertEqual(out.get("reason"), "E606")
        self.assertEqual(remote.calls, 1)
        self.assertEqual(ledger.balance("ws:compute.gpu")["reserved"], 60)

        # 查询转为确定成功：消解未知并结算，仍无重复外部效果
        due(conn, r["intent_id"])
        final = gw.run_once(r["intent_id"], remote)
        self.assertEqual(final["state"], "CONFIRMED_SUCCEEDED")
        self.assertEqual(remote.calls, 1)
        self.assertEqual(ledger.balance("ws:compute.gpu")["committed"], 60)
        self.assertEqual(ledger.balance("ws:compute.gpu")["reserved"], 0)


class TestHistoricalReadAfterPurge(unittest.TestCase):
    """purge 后回放到删除前的历史序号，正文仍不可读。"""

    def test_replay_before_purge_does_not_revive(self):
        conn, svc, _, _, _ = stack()
        self.addCleanup(conn.close)
        obs = svc.observe(WS, {
            "source": {"connector_identity": "web.fetch@v2",
                       "url_or_handle": "https://secret.example/old",
                       "content_version": "etag:1"},
            "modality": "text", "span": {"offset": 0, "length": 16},
            "acquired_at": "2026-10-24T09:00:00Z",
            "acquisition_method": {"tool": "fetch", "tool_version": "1",
                                   "parameters_digest": "sha256:0"},
        }, head(conn), "req-" + uuid7(), AGENT)
        rid = obs["record_ids"][0]
        before = obs["sequence"]

        purger = Purger(conn, svc)
        res = purger.retire_or_purge(WS, [rid], head(conn), "req-" + uuid7(),
                                     AGENT, mode="purge")
        self.assertGreater(res["sequence"], before)

        # 历史回放（删除前序号）可重建投影结构，但读取门控仍然拒绝正文
        replayed = Projection(conn).replay(WS, upto_sequence=before)
        self.assertIn(f"{WS}:{obs['record_ids'] and conn.execute('SELECT object_id FROM record WHERE record_id=?', (rid,)).fetchone()['object_id']}",
                      replayed["object_heads"])
        with self.assertRaises(AsgError) as cm:
            purger.read_record(WS, rid)
        self.assertEqual(cm.exception.code, "E802")
        # 墓碑在回放之后依然有效（回放不撤销删除域）
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM tombstone WHERE record_id=?", (rid,)).fetchone())


class ConflictingRemote(Connector):
    """对账返回与真实调用相反的结果，用于构造回执冲突。"""

    def __init__(self, query_outcome: str):
        super().__init__(ConnectorContract(mode="KEYED", supports_job_query=True,
                                           final_query_guarantee=True))
        self.query_outcome = query_outcome
        self.calls = 0

    def call(self, op, args, key):
        self.calls += 1
        return {"outcome": "CONFIRMED_SUCCEEDED", "external_job_id": "job-c",
                "clause": "keyed: 调用返回成功"}

    def query_by_key(self, key):
        return {"outcome": self.query_outcome, "external_job_id": "job-c",
                "clause": "keyed: 对账返回 " + self.query_outcome}


class TestReconcileConflict(unittest.TestCase):
    """对账结果与已落账回执相互矛盾：保留冲突，不覆盖已确认成功。"""

    def test_contradictory_query_keeps_success_and_flags_conflict(self):
        conn, svc, ledger, auth, gw = stack()
        self.addCleanup(conn.close)
        remote = ConflictingRemote(query_outcome="CONFIRMED_NOT_APPLIED")
        p = intent()
        r = prepare(conn, auth, gw, p)

        cl = gw.claim(r["intent_id"])
        ctx = gw.begin_attempt(WS, r["intent_id"], cl["token"])
        resp = remote.call(ctx["operation"], ctx["arguments"], ctx["idempotency_key"])
        # 旧 worker 先把成功回执落账
        state = gw.record_response(WS, ctx["attempt_id"], resp)
        self.assertEqual(state, "CONFIRMED_SUCCEEDED")

        # 新 worker 对账得到“未生效”：不得把已确认成功改写，且显式保留冲突
        conn.execute("UPDATE effect_outbox SET state='PENDING',"
                     " next_retry_at='2000-01-01T00:00:00Z' WHERE intent_id=?",
                     (r["intent_id"],))
        with self.assertRaises(AsgError) as cm:
            gw.run_once(r["intent_id"], remote)
        self.assertEqual(cm.exception.code, "E605")
        eff = gw.effect_status(WS, r["intent_id"])
        self.assertEqual(eff["state"], "CONFIRMED_SUCCEEDED")
        self.assertIn("receipt_conflict", eff["unresolved_reason"] or "")
        self.assertEqual(ledger.balance("ws:compute.gpu")["committed"], 60)
        self.assertEqual(remote.calls, 1)


class TestNotAppliedThenLateSuccess(unittest.TestCase):
    """先确认未生效并释放预留，随后迟到成功回执：不得静默覆盖，转入冲突保留。"""

    def test_late_success_after_not_applied_is_conflict(self):
        conn, svc, ledger, auth, gw = stack()
        self.addCleanup(conn.close)
        remote = ConflictingRemote(query_outcome="CONFIRMED_NOT_APPLIED")
        p = intent()
        r = prepare(conn, auth, gw, p)

        # 对账直接确认未生效（没有真实外呼回执）
        cl = gw.claim(r["intent_id"])
        ctx = gw.begin_attempt(WS, r["intent_id"], cl["token"])
        conn.execute("UPDATE effect_outbox SET claim_expires_at='2000-01-01T00:00:00Z'"
                     " WHERE intent_id=?", (r["intent_id"],))
        # Deliberate contradictory trusted receipts test, NOT evidence that a
        # mere negative key query closes future sends (round-seven contract).
        state = gw.record_response(WS, ctx["attempt_id"],
                                   {"outcome": "CONFIRMED_NOT_APPLIED", "clause": "injected final rejection"})
        self.assertEqual(state, "CONFIRMED_NOT_APPLIED")
        self.assertEqual(ledger.balance("ws:compute.gpu")["reserved"], 0)

        # 迟到的成功回执到达：确定性证据互相矛盾，保留冲突并不结算成功
        state = gw.record_response(WS, ctx["attempt_id"],
                                   {"outcome": "CONFIRMED_SUCCEEDED",
                                    "external_job_id": "job-c", "clause": "迟到成功"})
        self.assertNotEqual(gw.effect_status(WS, r["intent_id"])["state"],
                            "CONFIRMED_SUCCEEDED")
        self.assertEqual(ledger.balance("ws:compute.gpu")["committed"], 0)


if __name__ == "__main__":
    unittest.main()

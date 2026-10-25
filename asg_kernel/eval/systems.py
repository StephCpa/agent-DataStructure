"""阶段 D 第一层的两个被测系统（说明书 13.1 节：确定性记录 + 假工具）。

- AsgSystem：包装 asg 内核。
- BaselineSystem：消息列表 + 事实就地覆盖 + 操作名称级授权 + 盲目重试的 outbox
  + 软删除。这是说明书第 3 章候选表里“消息数组加摘要”与“成熟工作流加
  SQL 版本和 outbox”的最小组合，故意保留这些方案被 ASG 针对的缺陷：
  新消息覆盖旧事实、授权不绑定内容、未知结果直接重发、删除可被旧副本复活、
  预算先读后写。
"""
from __future__ import annotations

from asg import (AsgError, Authorizer, Gateway, Ledger, Projection, Purger,
                 WriteService, compute_operation_digest, connect, evaluate_support,
                 init_schema, justify, process_queue)
from asg.gateway import Connector, ConnectorContract
from asg.ids import uuid7

WS = "ws-eval"
AGENT = {"kind": "agent", "id": "agent-eval"}
APPROVER = {"kind": "human", "id": "approver-eval"}
GW = {"kind": "service", "id": "effect-gateway"}
DEST = "cluster-a/job-queue-1"
FAR = "2030-01-01T00:00:00Z"
BUDGET_KEY = "ws:compute.gpu"


class EvalRemote(Connector):
    """带幂等键记忆的假远端：同键返回原结果，调用次数可观察。"""

    def __init__(self):
        super().__init__(ConnectorContract(mode="KEYED", supports_job_query=True,
                                           final_query_guarantee=True))
        self.calls = 0
        self.store: dict[str, dict] = {}

    def call(self, operation, arguments, idempotency_key):
        self.calls += 1
        key = idempotency_key or f"nokey-{self.calls}"
        if key not in self.store:
            self.store[key] = {"outcome": "CONFIRMED_SUCCEEDED",
                               "external_job_id": "job", "clause": "eval remote"}
        return dict(self.store[key])

    def query_by_key(self, key):
        hit = self.store.get(key)
        return dict(hit) if hit else None


def _obs_payload(url: str) -> dict:
    return {
        "source": {"connector_identity": "web.fetch@v2", "url_or_handle": url,
                   "content_version": "etag:1"},
        "modality": "text", "span": {"offset": 0, "length": 64},
        "acquired_at": "2026-10-24T09:00:00Z",
        "acquisition_method": {"tool": "fetch", "tool_version": "1",
                               "parameters_digest": "sha256:0"},
    }


class AsgSystem:
    def __init__(self, remote: EvalRemote):
        self.remote = remote
        self.conn = connect()
        init_schema(self.conn)
        self.svc = WriteService(self.conn)
        self.ledger = Ledger(self.conn)
        self.auth = Authorizer(self.conn, self.svc)
        self.gw = Gateway(self.conn, self.svc, self.auth, principal=GW)
        self.purger = Purger(self.conn, self.svc)
        self.ledger.ensure_budget(BUDGET_KEY, 100)
        self._grants: dict[str, str] = {}
        self._intent: str | None = None

    def _head(self) -> int:
        row = self.conn.execute(
            "SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?", (WS,)
        ).fetchone()
        return row["head_sequence"] if row else 0

    def observe(self, url: str) -> str:
        res = self.svc.observe(WS, _obs_payload(url), self._head(), "req-" + uuid7(), AGENT)
        return res["record_ids"][0]

    def claim(self, text: str, evidence_ids: list[str]) -> str:
        res = self.svc.claim(WS, {
            "proposition": {"subject_entity": "e", "predicate": "p", "object": text},
            "scope": {"dataset": "d"}, "proposer": AGENT,
        }, self._head(), "req-" + uuid7(), AGENT, object_id="claim-" + uuid7())
        cid = res["record_ids"][0]
        if evidence_ids:
            justify(self.svc, WS, cid, [[e] for e in evidence_ids], expected_head=self._head())
        return cid

    def contradict(self, claim_id: str, evidence_id: str) -> None:
        justify(self.svc, WS, claim_id, [], refute_groups=[[evidence_id]],
                expected_head=self._head())

    def withdraw(self, record_id: str) -> None:
        self.svc.invalidate(WS, record_id, "withdrawn", self._head(), "req-" + uuid7(), AGENT)
        process_queue(self.conn, WS)

    def support_state(self, claim_id: str) -> str:
        return evaluate_support(self.conn, WS, claim_id)["four_state"]

    def _payload(self, content: str, preconditions: list[str]) -> dict:
        return {
            "intent_id": "intent-" + uuid7(), "task_revision_id": "t",
            "operation": "compute.job.submit", "connector_identity": "compute.cluster",
            "connector_version": "1.2", "executor_principal": AGENT, "target_ids": [DEST],
            "arguments_ref": {"arguments": {"config": content}}, "artifact_revision_ids": [],
            "precondition_refs": list(preconditions),
            "idempotency_contract": {"mode": "KEYED", "key": "idem-" + uuid7(),
                                     "key_scope": "compute.cluster", "key_expiry": FAR,
                                     "retry_policy": {"max": 1}},
        }

    def grant(self, content: str) -> str:
        payload = self._payload(content, [])
        g = self.auth.issue_grant(WS, APPROVER, AGENT, compute_operation_digest(payload),
                                  "提交", FAR, [DEST], expected_head=self._head())
        self._grants[g["grant_id"]] = content
        return g["grant_id"]

    def act(self, grant_id: str, content: str, preconditions: list[str]) -> bool:
        payload = self._payload(content, preconditions)
        try:
            res = self.gw.prepare_effect(WS, payload, self._head(), "req-" + uuid7(),
                                         grant_id, [{"resource_key": BUDGET_KEY, "amount": 10}])
        except AsgError:
            return False
        self._intent = res["intent_id"]
        try:
            self.gw.run_once(self._intent, self.remote)
        except AsgError:
            return False
        return True

    def start_and_crash(self, grant_id: str, content: str) -> None:
        """外部调用已成功，但回执持久化前崩溃（租约过期）。"""
        payload = self._payload(content, [])
        res = self.gw.prepare_effect(WS, payload, self._head(), "req-" + uuid7(),
                                     grant_id, [{"resource_key": BUDGET_KEY, "amount": 10}])
        self._intent = res["intent_id"]
        claimed = self.gw.claim(self._intent)
        ctx = self.gw.begin_attempt(WS, self._intent, claimed["token"])
        self.remote.call(ctx["operation"], ctx["arguments"], ctx["idempotency_key"])
        self.conn.execute(
            "UPDATE effect_outbox SET claim_expires_at='2000-01-01T00:00:00Z'"
            " WHERE intent_id=?", (self._intent,))

    def recover(self) -> str:
        out = self.gw.run_once(self._intent, self.remote)
        return out["state"] if out else "NONE"

    def task(self, goal: str, depends_on: str) -> None:
        task_obj = "task-" + uuid7()
        self.svc.submit_proposal(WS, [{
            "workspace_id": WS, "object_id": task_obj, "kind": "TASK_REVISION",
            "schema_version": 1, "authenticated_writer": AGENT, "policy_domain": "default",
            "payload": {"task_object_id": task_obj, "goal": goal, "acceptance_refs": ["a"]},
            "typed_refs": [{"role": "depends_on", "target_record_id": depends_on}],
            "prior_revision_ids": [],
        }], self._head(), "req-" + uuid7(), principal=AGENT)

    def purge(self, record_id: str) -> None:
        self.purger.retire_or_purge(WS, [record_id], self._head(), "req-" + uuid7(),
                                    AGENT, mode="purge")

    def read_blocked(self, record_id: str) -> bool:
        try:
            self.purger.read_record(WS, record_id)
            return False
        except AsgError as e:
            return e.code == "E802"

    def reobserve_blocked(self, url: str) -> bool:
        try:
            self.observe(url)
            return False
        except AsgError as e:
            return e.code == "E802"

    def snapshot(self) -> None:
        return None  # 账本行锁使快照读无法绕过额度检查

    def reserve(self, amount: float) -> bool:
        try:
            self.ledger.reserve(WS, "intent-" + uuid7(),
                                [{"resource_key": BUDGET_KEY, "amount": amount}])
            return True
        except AsgError:
            return False

    def replay_consistent(self) -> bool:
        cmp = Projection(self.conn).compare_with_live(WS)
        return cmp["heads_match"] and cmp["tasks_match"]

    def close(self) -> None:
        self.conn.close()


class BaselineSystem:
    """朴素基线：新消息覆盖旧事实，授权只认操作名，未知结果直接重发，删除只是标记。"""

    def __init__(self, remote: EvalRemote):
        self.remote = remote
        self.messages: dict[str, str] = {}
        self.facts: dict[str, str] = {}          # claim -> SUPPORTED / OVERWRITTEN
        self.grants: dict[str, str] = {}         # grant -> 操作名（不绑定内容）
        self.pending = False
        self.limit = 100.0
        self.used = 0.0
        self._snap: float | None = None
        self.purged: set[str] = set()
        self.archive: dict[str, str] = {}        # 软删除后仍保留的副本

    def observe(self, url: str) -> str:
        mid = "m-" + uuid7()
        self.messages[mid] = url
        return mid

    def claim(self, text: str, evidence_ids: list[str]) -> str:
        cid = "c-" + uuid7()
        self.facts[cid] = "SUPPORTED"            # 抽取后只留结论，不留证据链
        return cid

    def contradict(self, claim_id: str, evidence_id: str) -> None:
        self.facts[claim_id] = "OVERWRITTEN"     # 较新的消息直接覆盖

    def withdraw(self, record_id: str) -> None:
        self.messages.pop(record_id, None)       # 删消息，但已抽取的事实不动

    def support_state(self, claim_id: str) -> str:
        return self.facts.get(claim_id, "NONE")

    def grant(self, content: str) -> str:
        gid = "g-" + uuid7()
        self.grants[gid] = "compute.job.submit"  # 只记录操作名
        return gid

    def act(self, grant_id: str, content: str, preconditions: list[str]) -> bool:
        return grant_id in self.grants           # 不核对内容，不检查前提

    def start_and_crash(self, grant_id: str, content: str) -> None:
        self.remote.call("compute.job.submit", {"config": content}, None)
        self.pending = True                      # 没有结果记录，只知道“还没确认”

    def recover(self) -> str:
        if self.pending:                         # 盲目重发
            self.remote.call("compute.job.submit", {}, None)
            self.pending = False
        return "RETRIED"

    def task(self, goal: str, depends_on: str) -> None:
        return None                              # 任务状态就地保存，无依赖记录

    def purge(self, record_id: str) -> None:
        self.archive[record_id] = self.messages.get(record_id, "")
        self.purged.add(record_id)

    def read_blocked(self, record_id: str) -> bool:
        return record_id not in self.archive     # 旧副本仍在，读得到

    def reobserve_blocked(self, url: str) -> bool:
        self.observe(url)                        # 重新采集不受限制
        return False

    def snapshot(self) -> None:
        self._snap = self.limit - self.used      # 两个 Agent 读到同一个旧余额

    def reserve(self, amount: float) -> bool:
        view = self._snap if self._snap is not None else self.limit - self.used
        if view < amount:
            return False
        self.used += amount                      # 都按旧余额判断，双双成功
        return True

    def replay_consistent(self) -> bool:
        return False                             # 没有可回放的历史

    def close(self) -> None:
        return None

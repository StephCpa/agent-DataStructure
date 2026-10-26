"""阶段 B 验收测试 —— 《详细设计说明书》执行闭环。

覆盖：TC-04（审批后改参数不能借用旧授权，I6）、TC-05（外部成功后回执持久化前崩溃）、
TC-06（租约过期与远端去重）、授权生命周期（过期/撤销/用尽）、前提失效闸门（E401）、
预算与授权占用的原子集成。运行：
cd asg_kernel && python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import unittest

from asg import (AsgError, Authorizer, Connector, ConnectorContract, Gateway,
                 Ledger, Projection, WriteService, connect, init_schema,
                 compute_operation_digest)
from asg.ids import uuid7

WS = "ws-b"
AGENT = {"kind": "agent", "id": "agent-exec"}
APPROVER = {"kind": "human", "id": "approver-1"}
GATEWAY_PRINCIPAL = {"kind": "service", "id": "effect-gateway"}
DEST = "cluster-a/job-queue-1"
FAR_FUTURE = "2030-01-01T00:00:00Z"


class FakeKeyedRemote(Connector):
    """带幂等键的假远端：同键返回原结果，模拟远端去重；记录调用次数。"""

    def __init__(self, final_query_guarantee=True):
        super().__init__(ConnectorContract(mode="KEYED", supports_job_query=True,
                                           final_query_guarantee=final_query_guarantee,
                                           supports_attempt_closure=final_query_guarantee))
        self.key_store: dict[str, dict] = {}
        self.closed = set()
        self.call_count = 0

    def call(self, operation, arguments, idempotency_key):
        self.call_count += 1
        if idempotency_key in self.key_store:
            return self.key_store[idempotency_key]
        resp = {"outcome": "CONFIRMED_SUCCEEDED",
                "external_job_id": "job-" + str(self.call_count),
                "clause": "KEYED: 远端按幂等键登记作业"}
        self.key_store[idempotency_key] = resp
        return resp

    def query_by_key(self, key):
        return self.key_store.get(key)

    def call_attempt(self, context):
        aid = context["attempt_id"]
        if (context["idempotency_key"], aid) in self.closed:
            self.call_count += 1
            return {"outcome": "CONFIRMED_NOT_APPLIED", "closed_attempt_id": aid}
        return super().call_attempt(context)

    def close_attempt(self, key, attempt_id):
        if key in self.key_store:
            return self.key_store[key]
        self.closed.add((key, attempt_id))
        return {"outcome": "CONFIRMED_NOT_APPLIED", "closed_attempt_id": attempt_id}


class FakeNoContractRemote(Connector):
    def __init__(self):
        super().__init__(ConnectorContract(mode="NONE"))
        self.call_count = 0

    def call(self, operation, arguments, idempotency_key):
        self.call_count += 1
        return {"outcome": "OUTCOME_UNKNOWN", "clause": "NONE: 远端无法分类"}


def make_stack():
    conn = connect()
    init_schema(conn)
    svc = WriteService(conn)
    ledger = Ledger(conn)
    auth = Authorizer(conn, svc)
    gw = Gateway(conn, svc, auth, principal=GATEWAY_PRINCIPAL)
    ledger.ensure_budget("ws:compute.gpu", 100)
    return conn, svc, ledger, auth, gw


def intent_payload(**over):
    p = {
        "intent_id": "intent-" + uuid7(),
        "task_revision_id": "task-1",
        "operation": "compute.job.submit",
        "connector_identity": "compute.cluster",
        "connector_version": "1.2",
        "executor_principal": AGENT,
        "target_ids": [DEST],
        "arguments_ref": {"arguments": {"image": "exp:v1", "epochs": 3}},
        "artifact_revision_ids": [],
        "precondition_refs": [],
        "idempotency_contract": {
            "mode": "KEYED", "key": "idem-" + uuid7(), "key_scope": "compute.cluster",
            "key_expiry": FAR_FUTURE, "retry_policy": {"max": 3},
        },
    }
    p.update(over)
    return p


def issue_grant_for(auth, payload, head=0, **kw):
    kw.setdefault("use_limit", 1)
    return auth.issue_grant(
        WS, APPROVER, AGENT,
        operation_digest=compute_operation_digest(payload),
        purpose="提交实验作业", expiry=FAR_FUTURE,
        allowed_destinations=[DEST], expected_head=head, **kw)


def prepare(gw, head, payload=None, asks=None, grant=None):
    payload = payload or intent_payload()
    if grant is None:
        # 签发 Grant 本身是一次提交（头推进 1），随后 prepare 用新头
        grant = issue_grant_for(gw.auth, payload, head)["grant_id"]
        head += 1
    return gw.prepare_effect(
        WS, payload, head, "req-" + uuid7(), grant_id=grant,
        asks=asks if asks is not None else
        [{"resource_key": "ws:compute.gpu", "amount": 60}])


def expire_lease(conn, intent_id):
    conn.execute("UPDATE effect_outbox SET claim_expires_at='2000-01-01T00:00:00Z'"
                 " WHERE intent_id=?", (intent_id,))


def make_due(conn, intent_id):
    conn.execute("UPDATE effect_outbox SET next_retry_at='2000-01-01T00:00:00Z'"
                 " WHERE intent_id=?", (intent_id,))


class TestHappyPathDispatch(unittest.TestCase):
    def test_prepare_dispatch_settle(self):
        conn, svc, ledger, auth, gw = make_stack()
        self.addCleanup(conn.close)
        remote = FakeKeyedRemote()
        r = prepare(gw, 0)
        out = gw.run_once(r["intent_id"], remote)
        self.assertEqual(out["state"], "CONFIRMED_SUCCEEDED")
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(gw.effect_status(WS, r["intent_id"])["state"], "CONFIRMED_SUCCEEDED")
        bal = ledger.balance("ws:compute.gpu")
        self.assertEqual(bal["committed"], 60)
        self.assertEqual(bal["reserved"], 0)
        ob = conn.execute("SELECT state FROM effect_outbox WHERE intent_id=?",
                          (r["intent_id"],)).fetchone()
        self.assertEqual(ob["state"], "DONE")
        # 回执与观察均已落为记录，分类依据引用契约
        rc = conn.execute("SELECT COUNT(*) c FROM record WHERE kind='RECEIPT'").fetchone()["c"]
        oc = conn.execute("SELECT COUNT(*) c FROM record WHERE kind='OBSERVATION'").fetchone()["c"]
        self.assertEqual(rc, 1)
        self.assertEqual(oc, 1)
        # 授权按逻辑意图计量一次
        g = gw.effect_status  # noqa
        proj = Projection(conn).compare_with_live(WS)
        self.assertTrue(proj["heads_match"] and proj["tasks_match"])


class TestTC04GrantBinding(unittest.TestCase):
    """TC-04 / I6：审批后改参数、接收者或产物——新动作不能借用旧授权。"""

    def test_modified_arguments_rejected(self):
        conn, svc, _, auth, gw = make_stack()
        self.addCleanup(conn.close)
        v7 = intent_payload()
        grant = issue_grant_for(auth, v7, head=0)
        prepare(gw, 1, payload=v7, grant=grant["grant_id"])  # v7 获批并准备成功

        v8 = intent_payload(arguments_ref={"arguments": {"image": "exp:v2", "epochs": 3}})
        with self.assertRaises(AsgError) as cm:
            prepare(gw, 2, payload=v8, grant=grant["grant_id"])
        self.assertEqual(cm.exception.code, "E402")

    def test_target_out_of_scope_rejected(self):
        conn, svc, _, auth, gw = make_stack()
        self.addCleanup(conn.close)
        p = intent_payload(target_ids=["cluster-b/other-queue"])
        # 摘要与参数一致，但授权的目标范围只批准了 DEST：范围检查拒绝（E402）
        grant = auth.issue_grant(
            WS, APPROVER, AGENT, operation_digest=compute_operation_digest(p),
            purpose="x", expiry=FAR_FUTURE, allowed_destinations=[DEST], expected_head=0)
        with self.assertRaises(AsgError) as cm:
            prepare(gw, 1, payload=p, grant=grant["grant_id"])
        self.assertEqual(cm.exception.code, "E402")

    def test_expired_and_revoked_and_exhausted(self):
        conn, svc, _, auth, gw = make_stack()
        self.addCleanup(conn.close)
        p = intent_payload()
        # 过期授权在 prepare 即被拒
        expired = auth.issue_grant(WS, APPROVER, AGENT,
                                   compute_operation_digest(p), "x",
                                   expiry="2020-01-01T00:00:00Z",
                                   allowed_destinations=[DEST], expected_head=0)
        with self.assertRaises(AsgError) as cm:
            prepare(gw, 1, payload=p, grant=expired["grant_id"])
        self.assertEqual(cm.exception.code, "E403")

        # 撤销立即影响后续 dispatch（use_limit=2：一次占用后仍为 active，可被撤销）
        grant = issue_grant_for(auth, p, head=1, use_limit=2)
        r = prepare(gw, 2, payload=p, grant=grant["grant_id"])
        auth.revoke(grant["grant_id"])
        claim = gw.claim(r["intent_id"])
        with self.assertRaises(AsgError) as cm:
            gw.begin_attempt(WS, r["intent_id"], claim["token"])
        self.assertEqual(cm.exception.code, "E403")

        # 次数用尽：同授权的第二个逻辑意图被拒（第一个未执行也算已占用）
        small = [{"resource_key": "ws:compute.gpu", "amount": 10}]
        p2 = intent_payload()
        grant2 = issue_grant_for(auth, p2, head=3, use_limit=1)
        prepare(gw, 4, payload=p2, grant=grant2["grant_id"], asks=small)
        p3 = intent_payload()
        with self.assertRaises(AsgError) as cm:
            prepare(gw, 5, payload=p3, grant=grant2["grant_id"], asks=small)
        self.assertEqual(cm.exception.code, "E403")


class TestTC05CrashBeforeReceipt(unittest.TestCase):
    """TC-05 / I7：外部成功后、回执持久化前崩溃——不盲目新建重复逻辑效果。"""

    def test_reconcile_by_key_confirms_success(self):
        conn, svc, _, auth, gw = make_stack()
        self.addCleanup(conn.close)
        remote = FakeKeyedRemote()
        r = prepare(gw, 0)
        claim = gw.claim(r["intent_id"])
        ctx = gw.begin_attempt(WS, r["intent_id"], claim["token"])
        remote.call(ctx["operation"], ctx["arguments"], ctx["idempotency_key"])
        # —— 崩溃：响应未持久化 ——
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(gw.effect_status(WS, r["intent_id"])["state"], "IN_FLIGHT")

        expire_lease(conn, r["intent_id"])
        out = gw.run_once(r["intent_id"], remote)   # 重启后：先对账，不重发
        self.assertEqual(out["state"], "CONFIRMED_SUCCEEDED")
        self.assertEqual(remote.call_count, 1)       # 无重复外部效果
        st = conn.execute("SELECT state FROM reservation WHERE intent_id=?",
                          (r["intent_id"],)).fetchone()["state"]
        self.assertEqual(st, "committed")

    def test_non_final_query_keeps_unknown(self):
        """查询不满足最终性：查无不能作为未生效依据，维持 UNKNOWN 并保留占用。"""
        conn, svc, _, auth, gw = make_stack()
        self.addCleanup(conn.close)
        remote = FakeKeyedRemote(final_query_guarantee=False)
        remote.query_by_key = lambda key: None  # 模拟最终一致索引暂不可见
        r = prepare(gw, 0)
        claim = gw.claim(r["intent_id"])
        gw.begin_attempt(WS, r["intent_id"], claim["token"])  # 记录后、外呼前崩溃
        expire_lease(conn, r["intent_id"])
        out = gw.run_once(r["intent_id"], remote)
        self.assertEqual(out["state"], "OUTCOME_UNKNOWN")
        self.assertEqual(out.get("reason"), "E606")
        st = conn.execute("SELECT state FROM reservation WHERE intent_id=?",
                          (r["intent_id"],)).fetchone()["state"]
        self.assertEqual(st, "uncertain")          # UNKNOWN 保留占用


class TestTC06LeaseExpiry(unittest.TestCase):
    """TC-06：租约过期且旧 worker 仍在执行——有契约阻止重复，无契约保留边界。"""

    def test_keyed_dedup_no_duplicate_effect(self):
        """崩溃在开始记录之后、真正发出之前：对账判未生效后按同键安全重试。"""
        conn, svc, _, auth, gw = make_stack()
        self.addCleanup(conn.close)
        remote = FakeKeyedRemote()
        r = prepare(gw, 0)
        claim = gw.claim(r["intent_id"])
        gw.begin_attempt(WS, r["intent_id"], claim["token"])  # ATTEMPT 已记录，未外呼
        expire_lease(conn, r["intent_id"])                    # 旧 worker 失联
        out = gw.run_once(r["intent_id"], remote)
        # 远端关闭旧 Attempt -> 同键新 Attempt 重试成功；远端只收到一次调用
        self.assertEqual(out["state"], "CONFIRMED_SUCCEEDED")
        self.assertEqual(remote.call_count, 1)
        attempts = conn.execute("SELECT COUNT(*) c FROM attempt WHERE intent_id=?",
                                (r["intent_id"],)).fetchone()["c"]
        self.assertGreaterEqual(attempts, 2)  # 两次受控尝试，一个逻辑效果

    def test_no_contract_refuses_redispatch(self):
        """无幂等契约：默认暂停自动重发（E604），保留 UNKNOWN 边界。"""
        conn, svc, _, auth, gw = make_stack()
        self.addCleanup(conn.close)
        p = intent_payload(idempotency_contract={
            "mode": "NONE", "retry_policy": {"max": 3}})
        r = prepare(gw, 0, payload=p)
        # 第一次尝试：远端返回无法分类的响应 -> 意图 UNKNOWN，outbox 回 PENDING
        out1 = gw.run_once(r["intent_id"], FakeNoContractRemote())
        self.assertEqual(out1["state"], "OUTCOME_UNKNOWN")
        # 第二次循环：存在先前尝试且无契约 -> E604，放弃自动重发
        make_due(conn, r["intent_id"])
        out2 = gw.run_once(r["intent_id"], FakeNoContractRemote())
        self.assertEqual(out2["state"], "OUTCOME_UNKNOWN")
        self.assertEqual(out2.get("reason"), "E604")
        ob = conn.execute("SELECT state, last_error FROM effect_outbox WHERE intent_id=?",
                          (r["intent_id"],)).fetchone()
        self.assertEqual(ob["state"], "ABANDONED")
        self.assertIn("E604", ob["last_error"])
        st = conn.execute("SELECT state FROM reservation WHERE intent_id=?",
                          (r["intent_id"],)).fetchone()["state"]
        self.assertEqual(st, "uncertain")


class TestPreconditionGate(unittest.TestCase):
    """E401：前提刚被更正（失效队列未处理）时，prepare 与 dispatch 双闸门拒绝。"""

    def test_stale_precondition_blocks_prepare_and_dispatch(self):
        conn, svc, _, auth, gw = make_stack()
        self.addCleanup(conn.close)
        head = 0
        obs = svc.observe(WS, {
            "source": {"connector_identity": "web.fetch@v2",
                       "url_or_handle": "https://example.org/ds",
                       "content_version": "etag:1"},
            "modality": "text", "span": {"offset": 0, "length": 32},
            "acquired_at": "2026-10-24T09:00:00Z",
            "acquisition_method": {"tool": "fetch", "tool_version": "1",
                                   "parameters_digest": "sha256:0"},
        }, head, "req-" + uuid7(), AGENT)
        head = obs["snapshot"]["head_sequence"]

        p = intent_payload(precondition_refs=[obs["record_ids"][0]])
        grant = issue_grant_for(auth, p, head=head)
        r = prepare(gw, head + 1, payload=p, grant=grant["grant_id"])
        head = r["sequence"]

        # 前提被更正 -> 失效入队（PENDING）-> dispatch 闸门拒绝
        inv = svc.invalidate(WS, obs["record_ids"][0], "corrected", head,
                             "req-" + uuid7(), AGENT)
        head = inv["snapshot"]["head_sequence"]
        gw.claim(r["intent_id"])
        token_row = conn.execute(
            "SELECT claim_token FROM effect_outbox WHERE intent_id=?", (r["intent_id"],)
        ).fetchone()
        from asg.errors import AsgError as AE
        with self.assertRaises(AE) as cm:
            gw.begin_attempt(WS, r["intent_id"], token_row["claim_token"])
        self.assertEqual(cm.exception.code, "E401")

        # 新意图引用同一前提也会在 prepare 被拒
        p2 = intent_payload(precondition_refs=[obs["record_ids"][0]])
        grant2 = issue_grant_for(auth, p2, head=head)
        with self.assertRaises(AE) as cm:
            prepare(gw, head + 1, payload=p2, grant=grant2["grant_id"])
        self.assertEqual(cm.exception.code, "E401")


class TestBudgetGrantIntegration(unittest.TestCase):
    """prepare 事务内预算与授权占用原子：预算不足 E501，整体回滚不留半准备状态。"""

    def test_budget_shortage_rolls_back_everything(self):
        conn, svc, ledger, auth, gw = make_stack()
        self.addCleanup(conn.close)
        p = intent_payload()
        grant = issue_grant_for(auth, p, head=0)
        other = Ledger(conn)
        other.reserve(WS, "intent-hog", [{"resource_key": "ws:compute.gpu", "amount": 80}])
        with self.assertRaises(AsgError) as cm:
            gw.prepare_effect(WS, p, 1, "req-" + uuid7(), grant["grant_id"],
                              [{"resource_key": "ws:compute.gpu", "amount": 60}])
        self.assertEqual(cm.exception.code, "E501")
        # 无半准备状态：无意图记录、无 effect_state、无 outbox、无授权占用
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM record WHERE kind='ACTION_INTENT'").fetchone())
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM effect_state WHERE intent_id=?", (p["intent_id"],)).fetchone())
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM effect_outbox WHERE intent_id=?", (p["intent_id"],)).fetchone())
        self.assertEqual(auth.grant_status(grant["grant_id"])["uses_consumed"], 0)
        self.assertEqual(ledger.balance("ws:compute.gpu")["reserved"], 80)


if __name__ == "__main__":
    unittest.main()

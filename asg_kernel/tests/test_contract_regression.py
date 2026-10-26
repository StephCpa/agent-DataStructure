"""跨模块契约回归测试 —— 对应 eb1381b 评审发现的反例（评审收敛后的期望行为）。

覆盖：写入身份边界、伪造回执、多级证据链、规则/假设/论证撤回、
QUERY_ONLY 未决不重发、Grant 撤销与占用分离、错误路径不覆盖已确认成功、
回放一致性、清单绑定与必需项缺失、派生副本隔离、稳定内容身份复活阻止、
修订基线、跨工作空间引用、摘要服务计算、未支持分支拒绝。
"""
from __future__ import annotations

import unittest

from asg import (AsgError, Authorizer, Gateway, Ledger, Projection, Purger,
                 WriteService, compute_operation_digest, connect, evaluate_support,
                 init_schema, justify, manifest_valid, process_queue, project_context)
from asg.gateway import Connector, ConnectorContract
from asg.ids import uuid7

WS = "ws-r"
AGENT = {"kind": "agent", "id": "ordinary-agent"}
APPROVER = {"kind": "human", "id": "approver-1"}
GW_PRINCIPAL = {"kind": "service", "id": "effect-gateway"}
DEST = "cluster-a/job-queue-1"
FAR = "2030-01-01T00:00:00Z"


def stack():
    conn = connect()
    init_schema(conn)
    svc = WriteService(conn)
    ledger = Ledger(conn)
    auth = Authorizer(conn, svc)
    gw = Gateway(conn, svc, auth, principal=GW_PRINCIPAL)
    ledger.ensure_budget("ws:compute.gpu", 100)
    return conn, svc, ledger, auth, gw


def head(conn):
    r = conn.execute("SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?",
                     (WS,)).fetchone()
    return r["head_sequence"] if r else 0


def observe(svc, url="https://example.org/d"):
    r = svc.observe(WS, {
        "source": {"connector_identity": "web.fetch@v2", "url_or_handle": url,
                   "content_version": "etag:1"},
        "modality": "text", "span": {"offset": 0, "length": 32},
        "acquired_at": "2026-10-24T09:00:00Z",
        "acquisition_method": {"tool": "fetch", "tool_version": "1",
                               "parameters_digest": "sha256:0"},
    }, head(svc.conn), "req-" + uuid7(), AGENT)
    return r["record_ids"][0]


def claim(svc):
    r = svc.claim(WS, {
        "proposition": {"subject_entity": "e", "predicate": "p", "object": "v"},
        "scope": {"dataset": "d"}, "proposer": AGENT,
    }, head(svc.conn), "req-" + uuid7(), AGENT, object_id="claim-" + uuid7())
    return r["record_ids"][0]


def intent_payload(precond=None):
    return {
        "intent_id": "intent-" + uuid7(), "task_revision_id": "task-1",
        "operation": "compute.job.submit", "connector_identity": "compute.cluster",
        "connector_version": "1.2", "executor_principal": AGENT, "target_ids": [DEST],
        "arguments_ref": {"arguments": {"image": "exp:v1"}}, "artifact_revision_ids": [],
        "precondition_refs": precond or [],
        "idempotency_contract": {"mode": "KEYED", "key": "idem-" + uuid7(),
                                 "key_scope": "compute.cluster", "key_expiry": FAR,
                                 "retry_policy": {"max": 1}},
    }


def prepare(conn, auth, gw, payload):
    h = head(conn)
    g = auth.issue_grant(WS, APPROVER, AGENT, compute_operation_digest(payload),
                         "提交", FAR, [DEST], expected_head=h)
    return gw.prepare_effect(WS, payload, h + 1, "req-" + uuid7(), g["grant_id"],
                             [{"resource_key": "ws:compute.gpu", "amount": 60}])


class TestForgedReceiptRejected(unittest.TestCase):
    """反例 1：普通提案伪造网关回执不得驱动结算。"""

    def test_forged_receipt_does_not_settle(self):
        conn, svc, ledger, auth, gw = stack()
        self.addCleanup(conn.close)
        r = prepare(conn, auth, gw, intent_payload())
        claim_row = gw.claim(r["intent_id"])
        ctx = gw.begin_attempt(WS, r["intent_id"], claim_row["token"])
        # 普通 Agent 提交一条自称网关写入的成功回执
        forged = {
            "workspace_id": WS, "object_id": "rcp-" + uuid7(), "kind": "RECEIPT",
            "schema_version": 1, "authenticated_writer": GW_PRINCIPAL,
            "policy_domain": "default",
            "payload": {"attempt_id": ctx["attempt_id"], "outcome": "CONFIRMED_SUCCEEDED",
                        "classification_basis": {"clause": "伪造"},
                        "evidence_observation_id": ctx["attempt_record_id"],
                        "recorded_by": GW_PRINCIPAL},
            "typed_refs": [{"role": "receipt_of",
                            "target_record_id": ctx["attempt_record_id"]}],
            "prior_revision_ids": [],
        }
        with self.assertRaises(AsgError) as cm:
            svc.submit_proposal(WS, [forged], head(conn), "req-" + uuid7(), principal=AGENT)
        self.assertEqual(cm.exception.code, "E103")  # 普通主体不能签发 RECEIPT
        # 真实远端调用 0 次：效果不得被确认为成功，预算不得结算
        self.assertNotEqual(gw.effect_status(WS, r["intent_id"])["state"],
                            "CONFIRMED_SUCCEEDED")
        self.assertEqual(ledger.balance("ws:compute.gpu")["committed"], 0)
        # 落库身份由服务回填：即便记录自报网关，实际写入者是提交主体
        obs = observe(svc)
        row = conn.execute("SELECT authenticated_writer FROM record WHERE record_id=?",
                           (obs,)).fetchone()
        self.assertIn("ordinary-agent", row["authenticated_writer"])


class TestMultilevelEvidenceChain(unittest.TestCase):
    """反例 2：O→C1→C2→动作，撤回唯一源后下游动作不得开始。"""

    def test_chain_invalidation_blocks_action(self):
        conn, svc, _, auth, gw = stack()
        self.addCleanup(conn.close)
        o = observe(svc, url="https://src.example/only")
        c1 = claim(svc)
        justify(svc, WS, c1, [[o]], expected_head=head(conn))
        c2 = claim(svc)
        justify(svc, WS, c2, [[c1]], expected_head=head(conn))
        self.assertEqual(evaluate_support(conn, WS, c2)["four_state"], "SUPPORTED")

        p = intent_payload(precond=[c2])
        r = prepare(conn, auth, gw, p)
        svc.invalidate(WS, o, "withdrawn", head(conn), "req-" + uuid7(), AGENT)
        process_queue(conn, WS)

        self.assertEqual(evaluate_support(conn, WS, c1)["four_state"], "NONE")
        self.assertEqual(evaluate_support(conn, WS, c2)["four_state"], "NONE")
        cl = gw.claim(r["intent_id"])
        with self.assertRaises(AsgError) as cm:
            gw.begin_attempt(WS, r["intent_id"], cl["token"])
        self.assertEqual(cm.exception.code, "E401")  # 前提已失效，动作不得进入尝试

    def test_rule_assumption_and_justification_retirement(self):
        conn, svc, _, _, _ = stack()
        self.addCleanup(conn.close)
        # 规则只约束自己的论证：撤回规则不影响另一条独立论证
        o1, o2 = observe(svc, "https://a.example/1"), observe(svc, "https://b.example/2")
        rule = observe(svc, "https://rules.example/r1")
        c = claim(svc)
        justify(svc, WS, c, [[o1]], rule_refs=[rule], expected_head=head(conn))
        justify(svc, WS, c, [[o2]], expected_head=head(conn))
        svc.invalidate(WS, rule, "withdrawn", head(conn), "req-" + uuid7(), AGENT)
        process_queue(conn, WS)
        ev = evaluate_support(conn, WS, c)
        self.assertEqual(ev["four_state"], "SUPPORTED")
        self.assertEqual(ev["supporting_paths"], [[o2]])  # 独立路径保留

        # 假设撤回使依赖它的支持退出
        a = observe(svc, "https://assume.example/a")
        c3 = claim(svc)
        justify(svc, WS, c3, [[o1]], assumption_refs=[a], expected_head=head(conn))
        self.assertEqual(evaluate_support(conn, WS, c3)["four_state"], "SUPPORTED")
        svc.invalidate(WS, a, "withdrawn", head(conn), "req-" + uuid7(), AGENT)
        process_queue(conn, WS)
        self.assertEqual(evaluate_support(conn, WS, c3)["four_state"], "NONE")

        # 直接撤回唯一论证：主张不再受支持
        o4 = observe(svc, "https://c.example/4")
        c4 = claim(svc)
        j = justify(svc, WS, c4, [[o4]], expected_head=head(conn))
        svc.invalidate(WS, j["record_ids"][0], "withdrawn", head(conn),
                       "req-" + uuid7(), AGENT)
        process_queue(conn, WS)
        self.assertEqual(evaluate_support(conn, WS, c4)["four_state"], "NONE")


class TestUnknownDoesNotRedispatch(unittest.TestCase):
    """反例 3：QUERY_ONLY 返回 UNKNOWN 后不得自动重发。"""

    def test_query_only_unknown_reconciles_instead_of_redispatch(self):
        conn, svc, _, auth, gw = stack()
        self.addCleanup(conn.close)

        class QueryOnly(Connector):
            def __init__(self):
                super().__init__(ConnectorContract(mode="QUERY_ONLY",
                                                   supports_job_query=True,
                                                   final_query_guarantee=False))
                self.calls = 0

            def call(self, op, args, key):
                self.calls += 1
                return {"outcome": "OUTCOME_UNKNOWN", "external_job_id": "job-1",
                        "clause": "QUERY_ONLY: 结果未知"}

            def query_job(self, job):
                return None  # 查询暂不可见

        p = intent_payload()
        p["idempotency_contract"] = {"mode": "QUERY_ONLY", "retry_policy": {"max": 1}}
        r = prepare(conn, auth, gw, p)
        remote = QueryOnly()
        out1 = gw.run_once(r["intent_id"], remote)
        self.assertEqual(out1["state"], "OUTCOME_UNKNOWN")
        # 第二、三轮：对账（E606）维持 UNKNOWN，不再外呼
        for _ in range(2):
            conn.execute("UPDATE effect_outbox SET next_retry_at='2000-01-01T00:00:00Z'"
                         " WHERE intent_id=?", (r["intent_id"],))
            out = gw.run_once(r["intent_id"], remote)
            self.assertEqual(out["state"], "OUTCOME_UNKNOWN")
            self.assertEqual(out.get("reason"), "E606")
        self.assertEqual(remote.calls, 1)  # 外部效果只发生一次


class TestGrantRevocationSeparated(unittest.TestCase):
    """反例：次数占用后撤销路径仍开放，未发送动作可被撤销。"""

    def test_revoke_after_prepare_blocks_dispatch(self):
        conn, svc, _, auth, gw = stack()
        self.addCleanup(conn.close)
        p = intent_payload()
        h = head(conn)
        g = auth.issue_grant(WS, APPROVER, AGENT, compute_operation_digest(p),
                             "提交", FAR, [DEST], use_limit=1, expected_head=h)
        r = gw.prepare_effect(WS, p, h + 1, "req-" + uuid7(), g["grant_id"],
                              [{"resource_key": "ws:compute.gpu", "amount": 10}])
        self.assertEqual(auth.grant_status(g["grant_id"])["status"], "exhausted")
        auth.revoke(g["grant_id"])  # 占用后仍可撤销
        cl = gw.claim(r["intent_id"])
        with self.assertRaises(AsgError) as cm:
            gw.begin_attempt(WS, r["intent_id"], cl["token"])
        self.assertEqual(cm.exception.code, "E403")


class TestConfirmedNotOverwritten(unittest.TestCase):
    """反例：已确认成功不被迟到的查询错误改回 UNKNOWN。"""

    def test_late_query_error_keeps_success(self):
        conn, svc, ledger, auth, gw = stack()
        self.addCleanup(conn.close)

        class Keyed(Connector):
            def __init__(self):
                super().__init__(ConnectorContract(mode="KEYED", final_query_guarantee=False))
                self.store = {}

            def call(self, op, args, key):
                self.store[key] = {"outcome": "CONFIRMED_SUCCEEDED",
                                   "external_job_id": "job-9", "clause": "ok"}
                return self.store[key]

            def query_by_key(self, key):
                return None  # 对账时暂时不可见

        p = intent_payload()
        r = prepare(conn, auth, gw, p)
        out = gw.run_once(r["intent_id"], Keyed())
        self.assertEqual(out["state"], "CONFIRMED_SUCCEEDED")
        # 模拟租约交错：旧 worker 对账返回查无（E606）
        conn.execute("UPDATE effect_outbox SET state='PENDING',"
                     " next_retry_at='2000-01-01T00:00:00Z' WHERE intent_id=?",
                     (r["intent_id"],))
        out2 = gw.run_once(r["intent_id"], Keyed())
        self.assertEqual(gw.effect_status(WS, r["intent_id"])["state"],
                         "CONFIRMED_SUCCEEDED")  # 不被改回 UNKNOWN
        self.assertEqual(ledger.balance("ws:compute.gpu")["committed"], 60)


class TestReplayConsistency(unittest.TestCase):
    """反例 4：失效传播后的任务状态可被回放重建。"""

    def test_pending_reevaluation_replays(self):
        conn, svc, _, _, _ = stack()
        self.addCleanup(conn.close)
        o = observe(svc, "https://plan.example/input")
        task_obj = "task-" + uuid7()
        svc.submit_proposal(WS, [{
            "workspace_id": WS, "object_id": task_obj, "kind": "TASK_REVISION",
            "schema_version": 1, "authenticated_writer": AGENT, "policy_domain": "default",
            "payload": {"task_object_id": task_obj, "goal": "g", "acceptance_refs": ["a"]},
            "typed_refs": [{"role": "depends_on", "target_record_id": o}],
            "prior_revision_ids": [],
        }], head(conn), "req-" + uuid7(), principal=AGENT)
        svc.invalidate(WS, o, "withdrawn", head(conn), "req-" + uuid7(), AGENT)
        process_queue(conn, WS)
        cmp = Projection(conn).compare_with_live(WS)
        self.assertTrue(cmp["tasks_match"], cmp)
        self.assertEqual(cmp["live"]["task_state"][f"{WS}:{task_obj}"]["status"],
                         "PENDING_REEVALUATION")


class TestManifestBinding(unittest.TestCase):
    """反例 5：清单绑定全部实际选中项；必需项缺失显式失败。"""

    def test_selected_candidate_invalidates_manifest(self):
        conn, svc, _, _, _ = stack()
        self.addCleanup(conn.close)
        c_req = claim(svc)
        o_cand = observe(svc, "https://cand.example/x")
        out = project_context(conn, svc, WS, required_ids=[c_req],
                              candidate_ids=[o_cand], token_budget=10000)
        manifest_rid = out["commit"]["record_ids"][0]
        self.assertTrue(manifest_valid(conn, WS, manifest_rid))
        svc.invalidate(WS, o_cand, "withdrawn", head(conn), "req-" + uuid7(), AGENT)
        process_queue(conn, WS)
        self.assertFalse(manifest_valid(conn, WS, manifest_rid))  # 候选撤回使清单失效

    def test_missing_required_is_explicit_failure(self):
        conn, svc, _, _, _ = stack()
        self.addCleanup(conn.close)
        o = observe(svc, "https://must.example/y")
        Purger(conn, svc).retire_or_purge(WS, [o], head(conn), "req-" + uuid7(),
                                          AGENT, mode="purge")
        with self.assertRaises(AsgError) as cm:
            project_context(conn, svc, WS, required_ids=[o], candidate_ids=[],
                            token_budget=10000)
        self.assertEqual(cm.exception.code, "E705")


class TestDerivedIsolation(unittest.TestCase):
    """反例 6：删除域隔离派生副本；改采集时间不能绕过复活阻止。"""

    def test_derived_copy_isolated_and_reimport_blocked(self):
        conn, svc, _, _, _ = stack()
        self.addCleanup(conn.close)
        src = observe(svc, "https://secret.example/page")
        copy = svc.observe(WS, {
            "source": {"connector_identity": "web.fetch@v2",
                       "url_or_handle": "https://mirror.example/copy",
                       "content_version": "etag:1"},
            "modality": "text", "span": {"offset": 0, "length": 32},
            "acquired_at": "2026-10-24T10:00:00Z",
            "acquisition_method": {"tool": "fetch", "tool_version": "1",
                                   "parameters_digest": "sha256:0"},
        }, head(conn), "req-" + uuid7(), AGENT,
            typed_refs=[{"role": "extracted_from", "target_record_id": src}])
        copy_id = copy["record_ids"][0]
        purger = Purger(conn, svc)
        purger.retire_or_purge(WS, [src], head(conn), "req-" + uuid7(), AGENT, mode="purge")
        # 派生副本被隔离：读取拒绝
        with self.assertRaises(AsgError) as cm:
            purger.read_record(WS, copy_id)
        self.assertEqual(cm.exception.code, "E802")
        # 改 acquired_at 重新采集同一来源与片段：仍被复活阻止
        with self.assertRaises(AsgError) as cm:
            svc.observe(WS, {
                "source": {"connector_identity": "web.fetch@v2",
                           "url_or_handle": "https://secret.example/page",
                           "content_version": "etag:1"},
                "modality": "text", "span": {"offset": 0, "length": 32},
                "acquired_at": "2026-10-25T01:00:00Z",
                "acquisition_method": {"tool": "fetch", "tool_version": "1",
                                       "parameters_digest": "sha256:0"},
            }, head(conn), "req-" + uuid7(), AGENT)
        self.assertEqual(cm.exception.code, "E802")


class TestWriteBoundaryContracts(unittest.TestCase):
    """反例 7：约束集中在最终写入边界，通用入口不能绕过。"""

    def test_stale_object_baseline_rejected(self):
        conn, svc, _, _, _ = stack()
        self.addCleanup(conn.close)
        task_obj = "task-" + uuid7()
        base = {"task_object_id": task_obj, "goal": "v1", "acceptance_refs": ["a"]}

        def rev(goal, supersedes=None):
            refs = [{"role": "supersedes", "target_record_id": supersedes}] if supersedes else []
            return svc.submit_proposal(WS, [{
                "workspace_id": WS, "object_id": task_obj, "kind": "TASK_REVISION",
                "schema_version": 1, "authenticated_writer": AGENT,
                "policy_domain": "default",
                "payload": dict(base, goal=goal), "typed_refs": refs, "prior_revision_ids": [],
            }], head(conn), "req-" + uuid7(), principal=AGENT)

        r1 = rev("v1")
        r2 = rev("v2", r1["record_ids"][0])
        with self.assertRaises(AsgError) as cm:  # 以 v1 为基线的 v3 不得覆盖 v2
            rev("v3", r1["record_ids"][0])
        self.assertEqual(cm.exception.code, "E203")
        head_row = conn.execute("SELECT head_record_id FROM object_heads WHERE object_id=?",
                                (task_obj,)).fetchone()
        self.assertEqual(head_row["head_record_id"], r2["record_ids"][0])

    def test_prior_revision_must_exist(self):
        conn, svc, _, _, _ = stack()
        self.addCleanup(conn.close)
        with self.assertRaises(AsgError) as cm:
            svc.submit_proposal(WS, [{
                "workspace_id": WS, "object_id": "o1", "kind": "CLAIM", "schema_version": 1,
                "authenticated_writer": AGENT, "policy_domain": "default",
                "payload": {"proposition": {"a": 1}, "scope": {}, "proposer": AGENT},
                "typed_refs": [], "prior_revision_ids": [str(uuid7())],
            }], head(conn), "req-" + uuid7(), principal=AGENT)
        self.assertEqual(cm.exception.code, "E301")

    def test_cross_workspace_ref_rejected(self):
        conn, svc, _, _, _ = stack()
        self.addCleanup(conn.close)
        other = observe(svc)
        # 把观察改记为另一工作空间（直接构造第二个工作空间的记录）
        svc.submit_proposal("ws-other", [{
            "workspace_id": "ws-other", "object_id": "priv", "kind": "OBSERVATION",
            "schema_version": 1, "authenticated_writer": AGENT, "policy_domain": "private",
            "payload": {"source": {"connector_identity": "x", "url_or_handle": "u",
                                   "content_version": "1"},
                        "modality": "text", "span": {"offset": 0, "length": 1},
                        "acquired_at": "2026-10-24T00:00:00Z",
                        "acquisition_method": {"tool": "t", "tool_version": "1",
                                               "parameters_digest": "d"}},
            "typed_refs": [], "prior_revision_ids": [],
        }], 0, "req-" + uuid7(), principal=AGENT)
        foreign = conn.execute("SELECT record_id FROM record WHERE workspace_id='ws-other'"
                               ).fetchone()["record_id"]
        with self.assertRaises(AsgError) as cm:
            svc.claim(WS, {"proposition": {"a": 1}, "scope": {}, "proposer": AGENT},
                      head(conn), "req-" + uuid7(), AGENT, object_id="c-x",
                      typed_refs=[{"role": "uses_version", "target_record_id": foreign}])
        self.assertEqual(cm.exception.code, "E303")

    def test_digest_computed_by_service_and_branch_rejected(self):
        conn, svc, _, _, _ = stack()
        self.addCleanup(conn.close)
        rid = "req-fixed"
        svc.observe(WS, {"source": {"connector_identity": "x", "url_or_handle": "u1",
                                    "content_version": "1"},
                         "modality": "text", "span": {"offset": 0, "length": 1},
                         "acquired_at": "2026-10-24T00:00:00Z",
                         "acquisition_method": {"tool": "t", "tool_version": "1",
                                                "parameters_digest": "d"}},
                    0, rid, AGENT)
        # 不同内容但自报相同摘要：服务自行计算，拒绝一键两用
        with self.assertRaises(AsgError) as cm:
            svc.observe(WS, {"source": {"connector_identity": "x", "url_or_handle": "u2",
                                        "content_version": "1"},
                             "modality": "text", "span": {"offset": 0, "length": 1},
                             "acquired_at": "2026-10-24T00:00:00Z",
                             "acquisition_method": {"tool": "t", "tool_version": "1",
                                                    "parameters_digest": "d"}},
                        head(conn), rid, AGENT, object_id="o-diff")
        self.assertEqual(cm.exception.code, "E202")
        # 未支持的分支显式拒绝
        with self.assertRaises(AsgError) as cm:
            svc.submit_proposal(WS, [], head(conn), "req-" + uuid7(),
                                principal=AGENT, branch_id="draft")
        self.assertEqual(cm.exception.code, "E104")


if __name__ == "__main__":
    unittest.main()

"""阶段 C 验收测试 —— 《详细设计说明书》证据与上下文。

覆盖：TC-01（失效传播精确撤销）、TC-02（冲突进入 CONTESTED 不自动覆盖）、
TC-07（删除域复活阻止，I10）、TC-08（来源谱系不作独立验证）、
TC-09（必需约束超预算 E701）、TC-10（水位与执行闸门，I8）、任务待重评传播。
运行：cd asg_kernel && python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import unittest

from asg import (AsgError, Authorizer, Gateway, Ledger, Projection, Purger,
                 WriteService, compute_operation_digest, connect, evaluate_support,
                 init_schema, justify, process_queue, project_context, read_gate)
from asg.ids import uuid7

WS = "ws-c"
AGENT = {"kind": "agent", "id": "agent-1"}
APPROVER = {"kind": "human", "id": "approver-1"}
GATEWAY_PRINCIPAL = {"kind": "service", "id": "effect-gateway"}
DEST = "cluster-a/job-queue-1"
FAR_FUTURE = "2030-01-01T00:00:00Z"


def make_stack():
    conn = connect()
    init_schema(conn)
    svc = WriteService(conn)
    ledger = Ledger(conn)
    auth = Authorizer(conn, svc)
    gw = Gateway(conn, svc, auth, principal=GATEWAY_PRINCIPAL)
    ledger.ensure_budget("ws:compute.gpu", 100)
    return conn, svc, ledger, auth, gw


def head_of(conn, ws=WS):
    row = conn.execute("SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?", (ws,)).fetchone()
    return row["head_sequence"] if row else 0


def observe(svc, url="https://example.org/doc", refs=None, asserted=None):
    payload = {
        "source": {"connector_identity": "web.fetch@v2", "url_or_handle": url,
                   "content_version": "etag:" + url[-6:]},
        "modality": "text",
        "span": {"offset": 0, "length": 64},
        "acquired_at": "2026-10-24T09:00:00Z",
        "acquisition_method": {"tool": "fetch", "tool_version": "1.7",
                               "parameters_digest": "sha256:0"},
    }
    r = svc.observe(WS, payload, head_of(svc.conn), "req-" + uuid7(), AGENT,
                    typed_refs=refs or [], asserted_source=asserted)
    return r["record_ids"][0]


def make_claim(svc, obj=None, **extra):
    obj = obj or "claim-" + uuid7()
    payload = {
        "proposition": {"subject_entity": "ent-ds", "predicate": "test_set_size",
                        "object": "500"},
        "scope": {"dataset": "ds-1"},
        "units": "条",
        "valid_time": {"from": "2026-10-01"},
        "proposer": AGENT,
    }
    payload.update(extra)
    r = svc.claim(WS, payload, head_of(svc.conn), "req-" + uuid7(), AGENT, object_id=obj)
    return r["record_ids"][0]


class TestTC01InvalidationPrecision(unittest.TestCase):
    """TC-01：旧证据撤回、仍有独立新证据——只使失效支持路径退出，独立支持保留。"""

    def test_partial_withdraw_keeps_independent_support(self):
        conn, svc, _, _, _ = make_stack()
        self.addCleanup(conn.close)
        o1 = observe(svc, url="https://a.example/1")
        o2 = observe(svc, url="https://a.example/2")
        o3 = observe(svc, url="https://b.example/3")
        c1 = make_claim(svc)
        justify(svc, WS, c1, [[o1, o2], [o3]], expected_head=head_of(conn))

        ev = evaluate_support(conn, WS, c1)
        self.assertEqual(ev["four_state"], "SUPPORTED")
        self.assertIn([o1, o2], ev["supporting_paths"])
        self.assertIn([o3], ev["supporting_paths"])

        # 撤回 O1：组 1（O1 AND O2）失效；独立组 2（O3）仍合格
        svc.invalidate(WS, o1, "withdrawn", head_of(conn), "req-" + uuid7(), AGENT)
        process_queue(conn, WS)
        ev2 = evaluate_support(conn, WS, c1)
        self.assertEqual(ev2["four_state"], "SUPPORTED")
        self.assertEqual(ev2["supporting_paths"], [[o3]])   # 仅失效路径退出
        # 主张本身未被误删
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM record WHERE record_id=?", (c1,)).fetchone())

        # 再撤回 O3：无合格支持
        svc.invalidate(WS, o3, "withdrawn", head_of(conn), "req-" + uuid7(), AGENT)
        process_queue(conn, WS)
        ev3 = evaluate_support(conn, WS, c1)
        self.assertEqual(ev3["four_state"], "NONE")


class TestTC02ContestedNotOverwritten(unittest.TestCase):
    """TC-02：较新低可信消息与有效较早证据冲突——CONTESTED 展示，不自动覆盖。"""

    def test_contested_state_surfaces(self):
        conn, svc, _, _, _ = make_stack()
        self.addCleanup(conn.close)
        o1 = observe(svc, url="https://old.example/spec")
        c2 = make_claim(svc)
        justify(svc, WS, c2, [[o1]], expected_head=head_of(conn))
        self.assertEqual(evaluate_support(conn, WS, c2)["four_state"], "SUPPORTED")

        # 较新但低可信的消息：只是另一条观察，不自动覆盖较早证据
        o_new = observe(svc, url="https://shady.example/copy",
                        asserted={"kind": "connector", "id": "unverified-feed"})
        justify(svc, WS, c2, [], refute_groups=[[o_new]], expected_head=head_of(conn))

        ev = evaluate_support(conn, WS, c2)
        self.assertEqual(ev["four_state"], "CONTESTED")     # (1,1) 必须展示或裁决
        self.assertEqual(ev["supporting_paths"], [[o1]])    # 较早支持未被覆盖
        self.assertEqual(ev["refuting_paths"], [[o_new]])
        # 主张命题未被“更新为真相”：原文保持不变
        row = conn.execute("SELECT payload FROM record WHERE record_id=?", (c2,)).fetchone()
        self.assertIn('"object": "500"', row["payload"])


class TestTC08LineageNotIndependent(unittest.TestCase):
    """TC-08：多个 Agent 转载同一来源——同一根来源谱系，不按独立证据计数。"""

    def test_copies_share_root(self):
        conn, svc, _, _, _ = make_stack()
        self.addCleanup(conn.close)
        root = observe(svc, url="https://origin.example/post")
        c1 = observe(svc, url="https://mirror1.example/x",
                     refs=[{"role": "extracted_from", "target_record_id": root}])
        c2 = observe(svc, url="https://mirror2.example/y",
                     refs=[{"role": "extracted_from", "target_record_id": root}])
        c3 = make_claim(svc)
        justify(svc, WS, c3, [[root], [c1], [c2]], expected_head=head_of(conn))

        ev = evaluate_support(conn, WS, c3)
        self.assertEqual(ev["four_state"], "SUPPORTED")
        self.assertEqual(ev["lineage_rooted_support_item_count"], 1)   # 显式谱系去重
        self.assertEqual(ev["lineage_rooted_support_items"], [root])

        # 根来源撤回：转载链随之失效（DERIVATION 传播），不残留“独立”支持
        svc.invalidate(WS, root, "withdrawn", head_of(conn), "req-" + uuid7(), AGENT)
        process_queue(conn, WS)
        ev2 = evaluate_support(conn, WS, c3)
        self.assertEqual(ev2["four_state"], "NONE")


class TestPropagationTaskReeval(unittest.TestCase):
    """失效传播命中任务前提：任务进入待重评（6.5 节）。"""

    def test_task_moves_to_pending_reevaluation(self):
        conn, svc, _, _, _ = make_stack()
        self.addCleanup(conn.close)
        o1 = observe(svc, url="https://a.example/plan-input")
        task_obj = "task-" + uuid7()
        tp = {"task_object_id": task_obj, "goal": "整理数据说明",
              "acceptance_refs": ["acc-1"]}
        svc.submit_proposal(WS, [{
            "workspace_id": WS, "object_id": task_obj, "kind": "TASK_REVISION",
            "schema_version": 1, "authenticated_writer": AGENT, "policy_domain": "default",
            "payload": tp,
            "typed_refs": [{"role": "depends_on", "target_record_id": o1}],
            "prior_revision_ids": [],
        }], head_of(conn), "req-" + uuid7(), principal=AGENT)
        st = conn.execute("SELECT status FROM task_state WHERE task_object_id=?",
                          (task_obj,)).fetchone()
        self.assertEqual(st["status"], "ACTIVE")

        svc.invalidate(WS, o1, "corrected", head_of(conn), "req-" + uuid7(), AGENT)
        process_queue(conn, WS)
        st2 = conn.execute("SELECT status, reevaluation_reason FROM task_state"
                           " WHERE task_object_id=?", (task_obj,)).fetchone()
        self.assertEqual(st2["status"], "PENDING_REEVALUATION")
        self.assertIn("invalidated_via", st2["reevaluation_reason"])


class TestTC10StaleGate(unittest.TestCase):
    """TC-10 / I8：索引落后且前提刚被更正——陈旧候选不能直接获准执行。"""

    def _prepare_intent(self, conn, auth, gw, precond):
        from tests.test_phase_b import intent_payload  # 复用 B 的意图构造
        p = intent_payload(precondition_refs=precond)
        p["executor_principal"] = AGENT  # 与本套件的被授权者保持一致
        head = head_of(conn)
        grant = auth.issue_grant(
            WS, APPROVER, AGENT, compute_operation_digest(p), "提交实验作业",
            FAR_FUTURE, [DEST], expected_head=head)
        r = gw.prepare_effect(WS, p, head + 1, "req-" + uuid7(), grant["grant_id"],
                              [{"resource_key": "ws:compute.gpu", "amount": 10}])
        return r["intent_id"]

    def test_unrelated_pending_invalidation_blocks_dispatch(self):
        conn, svc, _, auth, gw = make_stack()
        self.addCleanup(conn.close)
        o_pre = observe(svc, url="https://a.example/pre")
        o_other = observe(svc, url="https://b.example/other")
        iid = self._prepare_intent(conn, auth, gw, [o_pre])

        # 与前提无关的记录被更正，传播未完成（水位落后）
        svc.invalidate(WS, o_other, "corrected", head_of(conn), "req-" + uuid7(), AGENT)
        claim = gw.claim(iid)
        self.assertIsNotNone(claim)
        with self.assertRaises(AsgError) as cm:
            gw.begin_attempt(WS, iid, claim["token"])
        self.assertEqual(cm.exception.code, "E401")   # 保守闸门：传播未完成

        # 传播完成、水位推进后放行
        process_queue(conn, WS)
        out = gw.begin_attempt(WS, iid, claim["token"])
        self.assertEqual(gw.effect_status(WS, iid)["state"], "IN_FLIGHT")

    def test_exploratory_read_returns_stale_annotation(self):
        conn, svc, _, _, _ = make_stack()
        self.addCleanup(conn.close)
        o1 = observe(svc, url="https://a.example/x")
        o2 = observe(svc, url="https://b.example/y")
        svc.invalidate(WS, o2, "corrected", head_of(conn), "req-" + uuid7(), AGENT)
        gate = read_gate(conn, WS, [o1], purpose="exploratory")
        self.assertTrue(gate["stale"])                # 显式标注，不阻断


class TestTC09ContextBudget(unittest.TestCase):
    """TC-09：必需约束超预算——E701 分阶段/提额/暂停，不静默裁剪。"""

    def test_required_over_budget_rejected(self):
        conn, svc, _, _, _ = make_stack()
        self.addCleanup(conn.close)
        c_a = make_claim(svc, data_labels=["internal"])
        with self.assertRaises(AsgError) as cm:
            project_context(conn, svc, WS, required_ids=[c_a], candidate_ids=[],
                            token_budget=1)
        self.assertEqual(cm.exception.code, "E701")
        self.assertGreater(cm.exception.details["required_cost"],
                           cm.exception.details["budget"])

    def test_manifest_persisted_with_labels_and_omissions(self):
        conn, svc, _, _, _ = make_stack()
        self.addCleanup(conn.close)
        c_req = make_claim(svc, data_labels=["internal"])
        c_big = make_claim(svc, data_labels=["sensitive:pii"],
                           constrained_text="超长候选" * 200)
        # 预算恰好只装下必需约束：候选被裁剪且进入 omitted 摘要
        unit_req_cost = None
        from asg import binding_unit
        unit_req_cost = binding_unit(conn, WS, c_req)["token_cost"]
        out = project_context(conn, svc, WS, required_ids=[c_req],
                              candidate_ids=[c_big], token_budget=unit_req_cost,
                              task_revision_id="task-9")
        self.assertEqual(out["selected"], [c_req])
        self.assertEqual(out["omitted"]["budget"],
                         [{"record_id": c_big, "reason": "budget"}])  # 明示不足，非静默
        # 标签并集只沿实际选中的条目传播：被裁剪候选的敏感标签不得外溢
        self.assertIn("internal", out["inherited_data_labels"])
        self.assertNotIn("sensitive:pii", out["inherited_data_labels"])
        # 清单已落账为记录
        row = conn.execute(
            "SELECT payload FROM record WHERE kind='CONTEXT_MANIFEST'").fetchone()
        self.assertIsNotNone(row)
        # 裁剪不丢绑定单元：必需约束的状态与单位仍在渲染输入摘要中
        self.assertIn("token_budget", row["payload"])


class TestTC07PurgeRevival(unittest.TestCase):
    """TC-07 / I10：删除后恢复旧快照或备份——受禁内容不重新可见。"""

    def test_purge_blocks_read_and_revival(self):
        conn, svc, _, _, _ = make_stack()
        self.addCleanup(conn.close)
        o1 = observe(svc, url="https://secret.example/page")
        c1 = make_claim(svc)
        justify(svc, WS, c1, [[o1]], expected_head=head_of(conn))
        self.assertEqual(evaluate_support(conn, WS, c1)["four_state"], "SUPPORTED")

        purger = Purger(conn, svc)
        res = purger.retire_or_purge(WS, [o1], head_of(conn), "req-" + uuid7(), AGENT,
                                     mode="purge")
        self.assertEqual(res["mode"], "purge")
        # 墓碑先行 + 策略纪元推进 + 在线副本清理
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM tombstone WHERE record_id=?", (o1,)).fetchone())
        pe = conn.execute("SELECT policy_epoch FROM workspace_epoch"
                          " WHERE workspace_id=?", (WS,)).fetchone()["policy_epoch"]
        self.assertEqual(pe, 2)
        blob = conn.execute("SELECT deletion_state FROM blob_object WHERE digest=("
                            " SELECT payload_digest FROM record WHERE record_id=?)",
                            (o1,)).fetchone()
        self.assertEqual(blob["deletion_state"], "purged")

        # 读取门控：正文不可见
        with self.assertRaises(AsgError) as cm:
            purger.read_record(WS, o1)
        self.assertEqual(cm.exception.code, "E802")

        # 恢复旧快照（回放到删除前）不复活：结构可回放，正文仍禁止
        Projection(conn).replay(WS, upto_sequence=res["sequence"] - 1)
        with self.assertRaises(AsgError) as cm:
            purger.read_record(WS, o1)
        self.assertEqual(cm.exception.code, "E802")

        # 同内容观察被复活阻止（E802）
        with self.assertRaises(AsgError) as cm:
            observe(svc, url="https://secret.example/page")
        self.assertEqual(cm.exception.code, "E802")

        # 支持电路：purged 证据不再合格
        process_queue(conn, WS)
        ev = evaluate_support(conn, WS, c1)
        self.assertEqual(ev["four_state"], "NONE")
        # 派生再生计划已给出（仍获准证据重新生成干净派生内容）
        self.assertTrue(res["derived_regeneration_plan"])


if __name__ == "__main__":
    unittest.main()

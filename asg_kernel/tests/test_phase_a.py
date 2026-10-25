"""阶段 A 验收测试 —— 《详细设计说明书》12.2 节确定性不变量用例与 TC-03。

进入阶段 B 的条件（第 13 章）：I1–I5 无反例；TC-INV-01 至 04 与 TC-03 通过。
运行：cd asg_kernel && python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import sqlite3
import unittest
import uuid

from asg import AsgError, Ledger, Projection, WriteService, connect, init_schema
from asg.ids import uuid7

WS = "ws-test"
PRINCIPAL = {"kind": "agent", "id": "agent-1"}


def make_service(path=":memory:"):
    conn = connect(path)
    init_schema(conn)
    return conn, WriteService(conn), Ledger(conn), Projection(conn)


def obs_payload(url="https://example.org/spec"):
    return {
        "source": {"connector_identity": "web.fetch@v2", "url_or_handle": url,
                   "content_version": "etag:9f2c1a"},
        "modality": "text",
        "span": {"offset": 0, "length": 128},
        "acquired_at": "2026-10-24T09:31:07Z",
        "acquisition_method": {"tool": "fetch", "tool_version": "1.7",
                               "parameters_digest": "sha256:0" * 1},
    }


def claim_payload(predicate_obj="500"):
    return {
        "proposition": {"subject_entity": "ent-dataset-1", "predicate": "test_set_size",
                        "object": predicate_obj},
        "scope": {"dataset": "ds-1"},
        "units": "条",
        "valid_time": {"from": "2026-10-01"},
        "proposer": PRINCIPAL,
    }


def task_payload(goal="准备并提交实验作业"):
    return {"task_object_id": "task-" + uuid7(), "goal": goal,
            "acceptance_refs": ["accept-" + uuid7()]}


def submit_observation(svc, head, url="https://example.org/spec"):
    return svc.observe(WS, obs_payload(url), head, "req-" + uuid7(), PRINCIPAL)


class TestHappyPath(unittest.TestCase):
    def test_observe_claim_task_invalidate(self):
        conn, svc, _, proj = make_service()
        r1 = submit_observation(svc, 0)
        self.assertEqual(r1["sequence"], 1)
        self.assertEqual(r1["snapshot"]["head_sequence"], 1)

        claim_obj = "claim-" + uuid7()
        r2 = svc.claim(WS, claim_payload(), 1, "req-" + uuid7(), PRINCIPAL, object_id=claim_obj,
                       typed_refs=[{"role": "uses_version", "target_record_id": r1["record_ids"][0]}])
        self.assertEqual(r2["sequence"], 2)

        tp = task_payload()
        r3 = svc.submit_proposal(WS, [{
            "workspace_id": WS, "object_id": tp["task_object_id"], "kind": "TASK_REVISION",
            "schema_version": 1, "authenticated_writer": PRINCIPAL, "policy_domain": "default",
            "payload": tp, "typed_refs": [], "prior_revision_ids": [],
        }], 2, "req-" + uuid7(), principal=PRINCIPAL)
        self.assertEqual(r3["snapshot"]["head_sequence"], 3)

        r4 = svc.invalidate(WS, r1["record_ids"][0], "corrected", 3, "req-" + uuid7(), PRINCIPAL)
        self.assertEqual(r4["snapshot"]["validation_epoch"], 2)  # 1 -> 2
        queue = conn.execute("SELECT COUNT(*) c FROM invalidation_queue").fetchone()["c"]
        self.assertEqual(queue, 1)

        cmp = proj.compare_with_live(WS)
        self.assertTrue(cmp["heads_match"], cmp)
        self.assertTrue(cmp["tasks_match"], cmp)


class TestINV01Immutable(unittest.TestCase):
    """TC-INV-01：record 禁止 UPDATE/DELETE（I1）。"""

    def test_direct_mutation_rejected(self):
        conn, svc, _, _ = make_service()
        r = submit_observation(svc, 0)
        rid = r["record_ids"][0]
        with self.assertRaises(sqlite3.DatabaseError) as cm:
            conn.execute("UPDATE record SET kind='CLAIM' WHERE record_id=?", (rid,))
        self.assertIn("immutable", str(cm.exception))
        with self.assertRaises(sqlite3.DatabaseError) as cm:
            conn.execute("DELETE FROM record WHERE record_id=?", (rid,))
        self.assertIn("immutable", str(cm.exception))
        kind = conn.execute("SELECT kind FROM record WHERE record_id=?", (rid,)).fetchone()["kind"]
        self.assertEqual(kind, "OBSERVATION")


class TestINV02FutureRef(unittest.TestCase):
    """TC-INV-02：不引用未来记录（I2）；不存在引用（E301）。"""

    def test_batch_future_ref_rejected(self):
        _, svc, _, _ = make_service()
        id_a, id_b = str(uuid.uuid4()), str(uuid.uuid4())
        rec_a = {
            "workspace_id": WS, "record_id": id_a, "object_id": "obj-a", "kind": "CLAIM",
            "schema_version": 1, "authenticated_writer": PRINCIPAL, "policy_domain": "default",
            "payload": claim_payload(), "typed_refs": [
                {"role": "uses_version", "target_record_id": id_b},  # 指向同批靠后
            ], "prior_revision_ids": [],
        }
        rec_b = {
            "workspace_id": WS, "record_id": id_b, "object_id": "obj-b", "kind": "OBSERVATION",
            "schema_version": 1, "authenticated_writer": PRINCIPAL, "policy_domain": "default",
            "payload": obs_payload(), "typed_refs": [], "prior_revision_ids": [],
        }
        with self.assertRaises(AsgError) as cm:
            svc.submit_proposal(WS, [rec_a, rec_b], 0, "req-" + uuid7(), principal=PRINCIPAL)
        self.assertEqual(cm.exception.code, "E302")

    def test_nonexistent_ref_rejected(self):
        _, svc, _, _ = make_service()
        rec = {
            "workspace_id": WS, "object_id": "obj-c", "kind": "CLAIM",
            "schema_version": 1, "authenticated_writer": PRINCIPAL, "policy_domain": "default",
            "payload": claim_payload(), "typed_refs": [
                {"role": "uses_version", "target_record_id": str(uuid.uuid4())},
            ], "prior_revision_ids": [],
        }
        with self.assertRaises(AsgError) as cm:
            svc.submit_proposal(WS, [rec], 0, "req-" + uuid7(), principal=PRINCIPAL)
        self.assertEqual(cm.exception.code, "E301")

    def test_batch_prior_ref_accepted(self):
        conn, svc, _, _ = make_service()
        id_b = str(uuid.uuid4())
        rec_b = {
            "workspace_id": WS, "record_id": id_b, "object_id": "obj-b", "kind": "OBSERVATION",
            "schema_version": 1, "authenticated_writer": PRINCIPAL, "policy_domain": "default",
            "payload": obs_payload(), "typed_refs": [], "prior_revision_ids": [],
        }
        rec_a = {
            "workspace_id": WS, "object_id": "obj-a", "kind": "CLAIM",
            "schema_version": 1, "authenticated_writer": PRINCIPAL, "policy_domain": "default",
            "payload": claim_payload(), "typed_refs": [
                {"role": "uses_version", "target_record_id": id_b},
            ], "prior_revision_ids": [],
        }
        r = svc.submit_proposal(WS, [rec_b, rec_a], 0, "req-" + uuid7(), principal=PRINCIPAL)
        self.assertEqual(r["sequence"], 1)
        edges = conn.execute("SELECT COUNT(*) c FROM record_ref WHERE ref_layer='DERIVATION'").fetchone()["c"]
        self.assertEqual(edges, 1)


class TestINV03RequestIdReuse(unittest.TestCase):
    """TC-INV-03：同一 request_id 不接受不同内容（I3）；同内容幂等重放。"""

    def test_reuse_with_different_content(self):
        _, svc, _, _ = make_service()
        rid = "req-fixed-1"
        svc.observe(WS, obs_payload("https://a.example/1"), 0, rid, PRINCIPAL)
        with self.assertRaises(AsgError) as cm:
            svc.observe(WS, obs_payload("https://a.example/2"), 1, rid, PRINCIPAL)
        self.assertEqual(cm.exception.code, "E202")

    def test_identical_replay_returns_original(self):
        _, svc, _, _ = make_service()
        rid = "req-fixed-2"
        payload = obs_payload("https://a.example/3")
        obj = "obs-" + uuid7()  # 幂等重放要求请求内容完全一致：同一 object_id
        r1 = svc.observe(WS, payload, 0, rid, PRINCIPAL, object_id=obj)
        r2 = svc.observe(WS, payload, 0, rid, PRINCIPAL, object_id=obj)
        self.assertTrue(r2["replayed"])
        self.assertEqual(r1["commit_id"], r2["commit_id"])
        self.assertEqual(r2["snapshot"]["head_sequence"], 1)


class TestINV04ConcurrentRevise(unittest.TestCase):
    """TC-INV-04：并发 revise 竞争——expected_head 冲突（E201）与基线不匹配（E203）。"""

    def test_stale_expected_head_rejected(self):
        _, svc, _, _ = make_service()
        submit_observation(svc, 0)                     # seq 1
        submit_observation(svc, 1, url="https://b.example/1")  # seq 2
        with self.assertRaises(AsgError) as cm:        # 携带过期 head=0
            submit_observation(svc, 0, url="https://c.example/1")
        self.assertEqual(cm.exception.code, "E201")
        self.assertEqual(cm.exception.details["current_head"], 2)

    def test_task_baseline_mismatch_rejected(self):
        _, svc, _, _ = make_service()
        tp1 = task_payload()
        r1 = svc.submit_proposal(WS, [{
            "workspace_id": WS, "object_id": tp1["task_object_id"], "kind": "TASK_REVISION",
            "schema_version": 1, "authenticated_writer": PRINCIPAL, "policy_domain": "default",
            "payload": tp1, "typed_refs": [], "prior_revision_ids": [],
        }], 0, "req-" + uuid7(), principal=PRINCIPAL)
        v1 = r1["record_ids"][0]

        tp2 = dict(tp1, goal="修订后的目标")
        r2 = svc.revise_task(WS, tp2, 1, "req-" + uuid7(), PRINCIPAL, supersedes=v1)
        v2 = r2["record_ids"][0]

        # 基于过期基线 v1 的第三个修订：E203，并返回当前头
        with self.assertRaises(AsgError) as cm:
            svc.revise_task(WS, dict(tp1, goal="迟到的修订"), 2, "req-" + uuid7(),
                            PRINCIPAL, supersedes=v1)
        self.assertEqual(cm.exception.code, "E203")
        self.assertEqual(cm.exception.details["current"], v2)


class TestINV05ReplayDeterminism(unittest.TestCase):
    """TC-INV-05：回放重建投影与在线投影逐字段一致（I9 的确定性基础）。"""

    def test_replay_matches_live(self):
        _, svc, _, proj = make_service()
        r1 = submit_observation(svc, 0)
        claim_obj = "claim-" + uuid7()
        svc.claim(WS, claim_payload(), 1, "req-" + uuid7(), PRINCIPAL, object_id=claim_obj,
                  typed_refs=[{"role": "uses_version", "target_record_id": r1["record_ids"][0]}])
        tp = task_payload()
        rt = svc.submit_proposal(WS, [{
            "workspace_id": WS, "object_id": tp["task_object_id"], "kind": "TASK_REVISION",
            "schema_version": 1, "authenticated_writer": PRINCIPAL, "policy_domain": "default",
            "payload": tp, "typed_refs": [], "prior_revision_ids": [],
        }], 2, "req-" + uuid7(), principal=PRINCIPAL)
        svc.revise_task(WS, dict(tp, goal="第二次修订"), 3, "req-" + uuid7(),
                        PRINCIPAL, supersedes=rt["record_ids"][0])
        svc.invalidate(WS, r1["record_ids"][0], "withdrawn", 4, "req-" + uuid7(), PRINCIPAL)

        cmp = proj.compare_with_live(WS)
        self.assertTrue(cmp["heads_match"], cmp)
        self.assertTrue(cmp["tasks_match"], cmp)
        # 任务当前修订应指向第二次修订
        key = f"{WS}:{tp['task_object_id']}"
        self.assertEqual(cmp["live"]["task_state"][key]["status"], "ACTIVE")
        head = cmp["live"]["object_heads"][f"{WS}:{tp['task_object_id']}"]
        self.assertEqual(head["head_sequence"], 4)


class TestTC03Budget(unittest.TestCase):
    """TC-03：预算 100，两个 Agent 各预留 60——第二笔拒绝且失败可解释（I5）。"""

    def test_double_reserve_rejected_with_details(self):
        _, _, ledger, _ = make_service()
        ledger.ensure_budget("ws:compute.gpu", 100)
        r1 = ledger.reserve(WS, "intent-a", [{"resource_key": "ws:compute.gpu", "amount": 60}])
        self.assertEqual(len(r1), 1)
        with self.assertRaises(AsgError) as cm:
            ledger.reserve(WS, "intent-b", [{"resource_key": "ws:compute.gpu", "amount": 60}])
        self.assertEqual(cm.exception.code, "E501")
        d = cm.exception.details
        self.assertEqual(d["limit"], 100)
        self.assertEqual(d["reserved"], 60)
        self.assertEqual(d["remaining"], 40)
        self.assertEqual(d["requested"], 60)
        # 降额到 40 可以成功
        r2 = ledger.reserve(WS, "intent-b", [{"resource_key": "ws:compute.gpu", "amount": 40}])
        # 结算第一笔成功
        self.assertEqual(ledger.settle(r1[0], "SUCCEEDED"), "committed")
        bal = ledger.balance("ws:compute.gpu")
        self.assertEqual(bal["committed"], 60)
        self.assertEqual(bal["reserved"], 40)
        self.assertEqual(bal["remaining"], 0)

    def test_unknown_retains_reservation(self):
        """UNKNOWN 保留占用（6.3 节）：uncertain 期间余量不回升。"""
        _, _, ledger, _ = make_service()
        ledger.ensure_budget("ws:compute.gpu", 100)
        r1 = ledger.reserve(WS, "intent-a", [{"resource_key": "ws:compute.gpu", "amount": 60}])
        self.assertEqual(ledger.settle(r1[0], "UNKNOWN"), "uncertain")
        bal = ledger.balance("ws:compute.gpu")
        self.assertEqual(bal["reserved"], 60)   # 仍占用
        self.assertEqual(bal["remaining"], 40)
        # 后续 60 依旧不可预留
        with self.assertRaises(AsgError) as cm:
            ledger.reserve(WS, "intent-b", [{"resource_key": "ws:compute.gpu", "amount": 60}])
        self.assertEqual(cm.exception.code, "E501")
        # 对账确认未生效后释放
        self.assertEqual(ledger.settle(r1[0], "NOT_APPLIED"), "released")
        self.assertEqual(ledger.balance("ws:compute.gpu")["remaining"], 100)


if __name__ == "__main__":
    unittest.main()

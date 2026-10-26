"""Independent expected states for the round-six review counterexamples."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from asg import (AsgError, Projection, WriteService, connect, eligible, evaluate_support,
                 justify, manifest_valid, process_queue, project_context)
from asg import context
from asg.ids import digest_obj, uuid7
from eval.systems import AsgSystem, EvalRemote, WS, AGENT, _obs_payload


class TestTaskIdentity(unittest.TestCase):
    def setUp(self):
        self.s = AsgSystem(EvalRemote())
        self.addCleanup(self.s.close)

    def task_record(self, obj, task, deps=(), refs=()):
        return {"workspace_id": WS, "object_id": obj, "kind": "TASK_REVISION",
                "schema_version": 1, "authenticated_writer": AGENT, "policy_domain": "default",
                "payload": {"task_object_id": task, "goal": "review", "acceptance_refs": ["ok"],
                            "dependency_refs": list(deps)}, "typed_refs": list(refs),
                "prior_revision_ids": []}

    def submit(self, records):
        return self.s.svc.submit_proposal(WS, records, self.s._head(), uuid7(), principal=AGENT)

    def test_identity_mismatch_rolls_back_batch_and_valid_revision_recovers(self):
        c = self.s.claim("unsupported", [])
        a = self.submit([self.task_record("A", "A", [c])])["record_ids"][0]
        self.assertEqual(self.s.conn.execute("SELECT status FROM task_state").fetchone()[0],
                         "PENDING_REEVALUATION")
        before = Projection(self.s.conn).live_task_state(WS), Projection(self.s.conn).live_object_heads(WS)
        count = self.s.conn.execute("SELECT COUNT(*) FROM record").fetchone()[0]
        with self.assertRaises(AsgError) as cm:
            self.submit([self.task_record("fresh", "fresh"), self.task_record("B", "A")])
        self.assertEqual(cm.exception.code, "E101")
        self.assertEqual(self.s.conn.execute("SELECT COUNT(*) FROM record").fetchone()[0], count)
        self.assertEqual(before, (Projection(self.s.conn).live_task_state(WS),
                                  Projection(self.s.conn).live_object_heads(WS)))
        good = self.task_record("A", "A", refs=[{"role": "supersedes", "target_record_id": a}])
        new = self.submit([good])["record_ids"][0]
        row = self.s.conn.execute("SELECT t.*,r.object_id FROM task_state t JOIN record r"
                                  " ON t.current_revision=r.record_id").fetchone()
        self.assertEqual((row["task_object_id"], row["object_id"], row["current_revision"], row["status"]),
                         ("A", "A", new, "ACTIVE"))

    def test_task_own_withdrawal_is_pending_online_and_replay(self):
        rid = self.submit([self.task_record("A", "A")])["record_ids"][0]
        self.s.withdraw(rid)
        self.assertFalse(eligible(self.s.conn, WS, rid))
        self.assertEqual(Projection(self.s.conn).live_task_state(WS)[WS + ":A"]["status"], "PENDING_REEVALUATION")
        self.assertEqual(Projection(self.s.conn).replay(WS)["task_state"][WS + ":A"]["status"], "PENDING_REEVALUATION")

    def test_uses_version_checked_at_creation_and_after_withdrawal(self):
        obs = self.s.observe("https://task/dependency")
        refs = [{"role": "uses_version", "target_record_id": obs}]
        self.submit([self.task_record("A", "A", refs=refs)])
        self.s.withdraw(obs)
        self.submit([self.task_record("B", "B", refs=refs)])
        process_queue(self.s.conn, WS)
        for task in ("A", "B"):
            self.assertEqual(Projection(self.s.conn).live_task_state(WS)[WS + ":" + task]["status"], "PENDING_REEVALUATION")
            self.assertEqual(Projection(self.s.conn).replay(WS)["task_state"][WS + ":" + task]["status"], "PENDING_REEVALUATION")

    def test_own_purge_pending_and_v2_remains_frozen(self):
        rid = self.submit([self.task_record("A", "A")])["record_ids"][0]
        self.s.purge(rid)
        process_queue(self.s.conn, WS)
        self.assertFalse(eligible(self.s.conn, WS, rid))
        self.assertEqual(Projection(self.s.conn).live_task_state(WS)[WS + ":A"]["status"], "PENDING_REEVALUATION")
        self.assertEqual(Projection(self.s.conn).replay(WS, reducer_version=3)["task_state"][WS + ":A"]["status"], "PENDING_REEVALUATION")
        self.assertEqual(Projection(self.s.conn).replay(WS, reducer_version=2)["task_state"][WS + ":A"]["status"], "ACTIVE")

    def test_intermediate_claims_are_not_reported_as_independent_sources(self):
        o = self.s.observe("https://one-original")
        a, b = self.s.claim("A", [o]), self.s.claim("B", [o])
        c = self.s.claim("C", [a, b])
        result = evaluate_support(self.s.conn, WS, c)
        self.assertEqual(result["lineage_rooted_support_items"], sorted([a, b]))
        self.assertEqual(result["lineage_rooted_support_item_count"], 2)
        self.assertEqual(result["support_provenance_scope"], "direct_members_with_explicit_lineage_only")
        self.assertNotIn("independent_root_count", result)
        self.assertEqual(result["four_state"], "SUPPORTED")

    def test_initial_task_eligibility_counts_its_own_depth(self):
        parent = self.s.observe("https://depth/root")
        for index in range(8):
            parent = self.s.svc.observe(WS, dict(_obs_payload(f"https://depth/{index}"), derived_from=[parent]),
                                         self.s._head(), uuid7(), AGENT)["record_ids"][0]
        self.assertTrue(eligible(self.s.conn, WS, parent))
        task = self.submit([self.task_record("A", "A", [parent])])["record_ids"][0]
        self.assertFalse(eligible(self.s.conn, WS, task))
        self.assertEqual(Projection(self.s.conn).live_task_state(WS)[WS + ":A"]["status"], "PENDING_REEVALUATION")
        self.assertEqual(Projection(self.s.conn).replay(WS)["task_state"][WS + ":A"]["status"], "PENDING_REEVALUATION")


class TestContextSnapshot(unittest.TestCase):
    def setUp(self):
        self.s = AsgSystem(EvalRemote())
        self.addCleanup(self.s.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "context.sqlite")
        self.reader = connect(self.path)
        self.addCleanup(self.reader.close)
        self.s.conn.backup(self.reader)
        self.writer = connect(self.path)
        self.addCleanup(self.writer.close)
        self.svc = WriteService(self.reader)
        self.writer_svc = WriteService(self.writer)
        o = self.writer_svc.observe(WS, _obs_payload("https://context/source"), 0, uuid7(), AGENT)["record_ids"][0]
        self.claim = self.writer_svc.claim(WS, {"constrained_text": "C", "scope": {}, "proposer": AGENT},
                                         self.writer_svc.current_head(WS), uuid7(), AGENT, "C")["record_ids"][0]
        justify(self.writer_svc, WS, self.claim, [[o]], expected_head=self.writer_svc.current_head(WS))
        self.obs = o
        process_queue(self.reader, WS)

    def test_writer_between_units_and_snapshot_rejected_then_fresh_render_succeeds(self):
        original = context._snapshot
        called = []
        def interleave(conn, workspace):
            if not called:
                called.append(True)
                justify(self.writer_svc, WS, self.claim, [], refute_groups=[[self.obs]],
                        expected_head=self.writer_svc.current_head(WS))
            return original(conn, workspace)
        with patch.object(context, "_snapshot", side_effect=interleave):
            with self.assertRaises(AsgError) as cm:
                project_context(self.reader, self.svc, WS, [self.claim], [], 10000)
        self.assertEqual(cm.exception.code, "E702")
        self.assertFalse(self.reader.in_transaction)
        self.assertEqual(self.reader.execute("SELECT COUNT(*) FROM record WHERE kind='CONTEXT_MANIFEST'").fetchone()[0], 0)
        result = project_context(self.reader, self.svc, WS, [self.claim], [], 10000)
        rid = result["commit"]["record_ids"][0]
        p = json.loads(self.reader.execute("SELECT payload FROM record WHERE record_id=?", (rid,)).fetchone()[0])
        unit = context.binding_unit(self.reader, WS, self.claim)
        self.assertEqual(unit["four_state"], "CONTESTED")
        rendered = json.dumps({"units": [{k: v for k, v in unit.items() if k != "record_id"}],
                               "ledger": {"claim_count": 1}}, ensure_ascii=False, sort_keys=True)
        self.assertEqual(p["rendered_input_digest"], digest_obj({"rendered": rendered}))
        self.assertTrue(manifest_valid(self.reader, WS, rid))
        self.assertFalse(result["stale"])

    def test_non_head_business_write_during_render_also_requires_rebuild(self):
        original = context.deterministic_ledger_query
        before = self.writer_svc.current_head(WS)
        def interleave(conn, workspace, kind):
            count = original(conn, workspace, kind)
            self.writer.execute("UPDATE budget_ledger SET limit_amount=limit_amount+1")
            return count
        with patch.object(context, "deterministic_ledger_query", side_effect=interleave):
            with self.assertRaises(AsgError) as cm:
                project_context(self.reader, self.svc, WS, [self.claim], [], 10000)
        self.assertEqual(cm.exception.code, "E702")
        self.assertEqual(self.writer_svc.current_head(WS), before)
        self.assertFalse(self.reader.in_transaction)
        self.assertTrue(project_context(self.reader, self.svc, WS, [self.claim], [], 10000)["selected"])

    def test_rendered_count_change_invalidates_manifest_without_selected_change(self):
        result = project_context(self.reader, self.svc, WS, [self.claim], [], 10000)
        rid = result["commit"]["record_ids"][0]
        self.assertTrue(manifest_valid(self.reader, WS, rid))
        self.writer_svc.claim(WS, {"constrained_text": "another", "scope": {}, "proposer": AGENT},
                              self.writer_svc.current_head(WS), uuid7(), AGENT, "another")
        self.assertFalse(manifest_valid(self.reader, WS, rid))

import json
import unittest

from asg import AsgError, Projection, evaluate_support, justify, manifest_valid, process_queue, project_context
from asg.ids import uuid7
from asg.invalidation import read_gate
from eval.systems import AsgSystem, EvalRemote, WS, AGENT, _obs_payload


class TestArgumentVersions(unittest.TestCase):
    def setUp(self):
        self.s = AsgSystem(EvalRemote())
        self.addCleanup(self.s.close)

    def argument(self, claim, support, refute=None, obj=None, parent=None):
        return justify(self.s.svc, WS, claim, support, refute_groups=refute,
                       expected_head=self.s._head(), object_id=obj, supersedes=parent)["record_ids"][0]

    def test_frozen_six_step_version_trajectory(self):
        a, b, r = [self.s.observe(f"https://versions/{i}") for i in range(3)]
        c = self.s.claim("C", [])
        j1 = self.argument(c, [[a]], obj="JA")
        self.argument(c, [[b]], obj="JB")
        old_snapshot = self.s._head()
        states = [self.s.support_state(c)]
        j2 = self.argument(c, [], [[r]], "JA", j1)
        states.append(self.s.support_state(c))
        self.s.withdraw(b)
        states.append(self.s.support_state(c))
        self.s.withdraw(j2)
        states.append(self.s.support_state(c))
        self.argument(c, [[a]], obj="JC")
        states.append(self.s.support_state(c))
        self.assertEqual(states, ["SUPPORTED", "CONTESTED", "REFUTED", "NONE", "SUPPORTED"])
        # Historical withdrawal/version visibility differs from current admission.
        self.assertEqual(evaluate_support(self.s.conn, WS, c, as_of=old_snapshot)["four_state"], "SUPPORTED")
        self.assertTrue(self.s.act(self.s.grant("good"), "good", [c]))
        self.s.withdraw(c)
        self.assertFalse(self.s.act(self.s.grant("retired"), "retired", [c]))
        self.assertEqual(self.s.last_error, "E401")
        self.assertEqual(self.s.remote.calls, 1)

    def test_revision_target_change_does_not_resurrect_old_argument(self):
        obs = self.s.observe("https://target/source")
        c1, c2 = self.s.claim("C1", []), self.s.claim("C2", [])
        j1 = self.argument(c1, [[obs]], obj="JA")
        self.argument(c2, [[obs]], obj="JA", parent=j1)
        self.assertEqual(self.s.support_state(c1), "NONE")
        self.assertEqual(self.s.support_state(c2), "SUPPORTED")

    def record(self, c, obs, refs):
        return {"workspace_id": WS, "object_id": "J-" + uuid7(), "kind": "JUSTIFICATION",
                "schema_version": 1, "authenticated_writer": AGENT, "policy_domain": "default",
                "payload": {"target_claim_id": c, "support_groups": [[obs]], "refute_groups": []},
                "typed_refs": refs, "prior_revision_ids": []}

    def test_missing_payload_edges_are_normalized_and_propagate(self):
        obs = self.s.observe("https://refs/one")
        c = self.s.claim("C", [])
        record = self.record(c, obs, [])
        result = self.s.svc.submit_proposal(WS, [record], self.s._head(), "missing", principal=AGENT)
        edges = {(r[0], r[1]) for r in self.s.conn.execute(
            "SELECT ref_role,dst_record_id FROM record_ref WHERE src_record_id=?", (result["record_ids"][0],))}
        self.assertEqual(edges, {("supports", obs), ("targets_claim", c)})
        self.assertEqual(record["typed_refs"], [])  # caller request remains unchanged
        self.s.task("task", c)
        self.s.withdraw(obs)
        self.assertEqual(self.s.support_state(c), "NONE")
        self.assertEqual(self.s.conn.execute("SELECT status FROM task_state").fetchone()[0], "PENDING_REEVALUATION")
        self.assertTrue(self.s.replay_consistent())

    def test_conflicting_payload_edges_rejected_without_partial_write(self):
        a, b = self.s.observe("https://refs/a"), self.s.observe("https://refs/b")
        c = self.s.claim("C", [])
        h = self.s._head()
        with self.assertRaises(AsgError) as cm:
            self.s.svc.submit_proposal(WS, [self.record(c, a, [{"role": "supports", "target_record_id": b}])],
                                       h, "mismatch", principal=AGENT)
        self.assertEqual(cm.exception.code, "E101")
        self.assertEqual(self.s._head(), h)

    def test_payload_only_lineage_is_in_deletion_domain(self):
        source = self.s.observe("https://lineage/root")
        payload = dict(_obs_payload("https://lineage/copy"), extracted_from=[source])
        result = self.s.svc.observe(WS, payload, self.s._head(), "copy", AGENT)
        self.s.purge(source)
        self.assertTrue(self.s.read_blocked(result["record_ids"][0]))

    def test_parent_revision_edges_are_persisted(self):
        a = self.s.observe("https://parent/a")
        c = self.s.claim("C", [])
        j1 = self.argument(c, [[a]], obj="JA")
        j2 = self.argument(c, [], [[a]], "JA", j1)
        rows = self.s.conn.execute("SELECT ref_role FROM record_ref WHERE src_record_id=? AND dst_record_id=?",
                                   (j2, j1)).fetchall()
        self.assertEqual({r[0] for r in rows}, {"supersedes", "prior_revision"})

    def test_payload_only_cross_workspace_reference_is_rejected(self):
        foreign = self.s.svc.observe("foreign", _obs_payload("https://foreign/root"), 0, "foreign", AGENT)["record_ids"][0]
        c = self.s.claim("C", [])
        with self.assertRaises(AsgError) as cm:
            self.s.svc.submit_proposal(WS, [self.record(c, foreign, [])], self.s._head(), "cross", principal=AGENT)
        self.assertEqual(cm.exception.code, "E303")

    def test_reducer_version_is_explicit_and_legacy_replay_available(self):
        obs = self.s.observe("https://version/root")
        self.s.task("task", obs)
        version = self.s.conn.execute('SELECT reducer_version FROM "commit" ORDER BY sequence DESC LIMIT 1').fetchone()[0]
        self.assertEqual(version, 3)
        projection = Projection(self.s.conn)
        self.assertEqual(projection.replay(WS, reducer_version=1), projection.replay(WS, reducer_version=2))
        with self.assertRaises(ValueError):
            projection.replay(WS, reducer_version=4)

    def test_late_dependency_then_independent_support_and_drain_fixed_point(self):
        o = self.s.observe("https://late/retired")
        self.s.withdraw(o)
        c = self.s.claim("late C", [o])
        downstream = self.s.claim("late downstream", [c])
        self.s.task("late task", downstream)
        old = project_context(self.s.conn, self.s.svc, WS, [downstream], [], 10000)
        # No drain between dependency creation and admission.
        self.assertEqual(self.s.support_state(downstream), "NONE")
        with self.assertRaises(AsgError) as cm:
            read_gate(self.s.conn, WS, [downstream], purpose="critical")
        self.assertEqual(cm.exception.code, "E401")
        self.assertFalse(self.s.act(self.s.grant("blocked"), "blocked", [downstream]))
        self.assertEqual(self.s.remote.calls, 0)
        process_queue(self.s.conn, WS)
        self.assertEqual(self.s.support_state(downstream), "NONE")
        o2 = self.s.observe("https://late/independent")
        self.argument(c, [[o2]])
        self.assertEqual(self.s.support_state(downstream), "SUPPORTED")
        self.assertFalse(manifest_valid(self.s.conn, WS, old["commit"]["record_ids"][0]))
        # Explicit task revision is the reevaluation boundary. No direct status UPDATE.
        row = self.s.conn.execute("SELECT r.record_id,r.payload FROM task_state t JOIN record r"
                                  " ON r.record_id=t.current_revision").fetchone()
        self.s.svc.revise_task(WS, json.loads(row["payload"]), self.s._head(), "reevaluate", AGENT,
                               supersedes=row["record_id"])
        fresh = project_context(self.s.conn, self.s.svc, WS, [downstream], [], 10000)
        def semantic_state():
            return (self.s.support_state(downstream),
                    dict(self.s.conn.execute("SELECT * FROM task_state").fetchone()),
                    manifest_valid(self.s.conn, WS, fresh["commit"]["record_ids"][0]),
                    Projection(self.s.conn).replay(WS))
        process_queue(self.s.conn, WS)
        first = semantic_state()
        process_queue(self.s.conn, WS)
        self.assertEqual(semantic_state(), first)
        self.assertEqual(first[1]["status"], "ACTIVE")
        self.assertTrue(first[2])
        self.assertTrue(self.s.replay_consistent())
        self.assertTrue(self.s.act(self.s.grant("healthy"), "healthy", [downstream]))
        self.assertEqual(self.s.remote.calls, 1)

    def test_bounded_queue_does_not_advance_watermark_until_complete(self):
        from asg.invalidation import watermark
        a, b = self.s.observe("https://queue/a"), self.s.observe("https://queue/b")
        for index, rid in enumerate((a, b)):
            self.s.svc.invalidate(WS, rid, "withdrawn", self.s._head(), f"withdraw-{index}", AGENT)
        metrics = {}
        self.assertEqual(process_queue(self.s.conn, WS, max_items=1, metrics=metrics), 1)
        self.assertIsNone(watermark(self.s.conn, WS))
        self.assertGreater(metrics["records_scanned"], 0)
        self.assertGreater(metrics["closure_nodes_visited"], 0)
        process_queue(self.s.conn, WS, metrics=metrics)
        self.assertEqual(watermark(self.s.conn, WS)["applied_sequence"], self.s._head())

    def test_obsolete_argument_does_not_propagate_new_withdrawal(self):
        a, b = self.s.observe("https://obsolete/a"), self.s.observe("https://obsolete/b")
        c = self.s.claim("C", [])
        j1 = self.argument(c, [[a]], obj="JA")
        self.argument(c, [[b]], obj="JA", parent=j1)
        self.s.task("current task", c)
        ctx = project_context(self.s.conn, self.s.svc, WS, [c], [], 10000)
        self.s.withdraw(a)
        self.assertEqual(self.s.conn.execute("SELECT status FROM task_state").fetchone()[0], "ACTIVE")
        self.assertTrue(manifest_valid(self.s.conn, WS, ctx["commit"]["record_ids"][0]))
        self.assertTrue(self.s.replay_consistent())

"""Cross-module regressions and seeded evidence/deletion schedules.

The oracle tracks two independent support paths and explicit copy ancestry;
it does not call the kernel evaluator to construct expected states.
"""
import random
import json
import unittest

from asg import AsgError, manifest_valid, process_queue, project_context
from asg.ids import uuid7
from eval.systems import AsgSystem, EvalRemote, WS, AGENT, _obs_payload


class TestEvidenceDeletionInterleaving(unittest.TestCase):
    def setUp(self):
        self.system = AsgSystem(EvalRemote())
        self.addCleanup(self.system.close)
        self.conn = self.system.conn

    def chain(self):
        a = self.system.observe("https://source/a")
        b = self.system.observe("https://source/b")
        c1 = self.system.claim("first", [a, b])
        c2 = self.system.claim("second", [c1])
        self.system.task("dependent task", c2)
        manifest = project_context(self.conn, self.system.svc, WS, [c2], [], 10000)
        return a, b, c1, c2, manifest["commit"]["record_ids"][0]

    def copy(self, source, url):
        return self.system.svc.observe(
            WS, _obs_payload(url), self.system._head(), "req-" + uuid7(), AGENT,
            typed_refs=[{"role": "extracted_from", "target_record_id": source}]
        )["record_ids"][0]

    def test_multilevel_propagation_preserves_independent_support(self):
        a, b, c1, c2, manifest = self.chain()
        self.system.withdraw(a)
        self.assertEqual(self.system.support_state(c2), "SUPPORTED")
        task = self.conn.execute("SELECT status FROM task_state").fetchone()
        self.assertEqual(task["status"], "PENDING_REEVALUATION")
        self.assertFalse(manifest_valid(self.conn, WS, manifest))
        self.assertTrue(self.system.replay_consistent())
        self.system.withdraw(b)
        self.assertEqual(self.system.support_state(c2), "NONE")
        self.assertFalse(self.system.act(self.system.grant("v1"), "v1", [c2]))
        self.assertEqual(self.system.last_error, "E401")
        self.assertEqual(self.system.remote.calls, 0)

    def test_derived_dependency_replay(self):
        source = self.system.observe("https://source/root")
        copy = self.copy(source, "https://source/copy")
        self.system.task("copy dependent task", copy)
        self.system.withdraw(source)
        self.assertTrue(self.system.replay_consistent())

    def test_dependency_added_after_queue_drained_replays(self):
        source = self.system.observe("https://source/retired")
        self.system.withdraw(source)
        self.system.task("late dependency", source)
        process_queue(self.conn, WS)
        self.assertTrue(self.system.replay_consistent())
        self.assertEqual(self.conn.execute("SELECT status FROM task_state").fetchone()["status"],
                         "PENDING_REEVALUATION")

    def record(self):
        return {"workspace_id": WS, "object_id": "audit-object", "kind": "CLAIM",
                "schema_version": 1, "authenticated_writer": {"kind": "service", "id": "claimed"},
                "policy_domain": "default", "payload": {"scope": {}, "proposer": AGENT,
                "constrained_text": "v1"}, "typed_refs": [], "prior_revision_ids": []}

    def test_revision_cannot_omit_baseline(self):
        rec = self.record()
        self.system.svc.submit_proposal(WS, [rec], 0, "first", principal=AGENT)
        rec["payload"]["constrained_text"] = "v2"
        with self.assertRaises(AsgError) as cm:
            self.system.svc.submit_proposal(WS, [rec], 1, "second", principal=AGENT)
        self.assertEqual(cm.exception.code, "E203")

    def test_batch_revision_checks_advanced_head(self):
        rec = self.record()
        rec["record_id"] = uuid7()
        second = dict(rec, record_id=uuid7())
        second["typed_refs"] = [{"role": "supersedes", "target_record_id": rec["record_id"]}]
        result = self.system.svc.submit_proposal(WS, [rec, second], 0, "batch", principal=AGENT)
        self.assertEqual(len(result["record_ids"]), 2)
        third = dict(second, record_id=uuid7())
        with self.assertRaises(AsgError) as cm:
            self.system.svc.submit_proposal(WS, [third], 1, "stale", principal=AGENT)
        self.assertEqual(cm.exception.code, "E203")

    def test_replayed_request_still_rejects_unsupported_branch(self):
        rec = self.record()
        self.system.svc.submit_proposal(WS, [rec], 0, "fixed", principal=AGENT)
        with self.assertRaises(AsgError) as cm:
            self.system.svc.submit_proposal(WS, [rec], 1, "fixed", principal=AGENT, branch_id="draft")
        self.assertEqual(cm.exception.code, "E104")

    def test_declared_identity_is_saved_separately(self):
        rec = self.record()
        result = self.system.svc.submit_proposal(WS, [rec], 0, "identity", principal=AGENT)
        row = self.conn.execute("SELECT authenticated_writer, asserted_source FROM record WHERE record_id=?",
                                (result["record_ids"][0],)).fetchone()
        self.assertEqual(json.loads(row["authenticated_writer"]), AGENT)
        self.assertEqual(json.loads(row["asserted_source"]), rec["authenticated_writer"])

    def test_purge_closes_transitive_copy_domain_immediately(self):
        source = self.system.observe("https://secret/root")
        first = self.copy(source, "https://secret/first")
        second = self.copy(first, "https://secret/second")
        self.system.purge(source)
        # No queue drain: deletion read isolation must be synchronous.
        for rid in (source, first, second):
            with self.subTest(record=rid):
                self.assertTrue(self.system.read_blocked(rid))
                with self.assertRaises(AsgError) as cm:
                    project_context(self.conn, self.system.svc, WS, [rid], [], 10000)
                self.assertEqual(cm.exception.code, "E705")

    def test_purged_derived_identity_cannot_be_reimported(self):
        source = self.system.observe("https://secret/root")
        self.copy(source, "https://secret/copy")
        self.system.purge(source)
        self.assertTrue(self.system.reobserve_blocked("https://secret/copy"))

    def test_new_copy_of_purged_record_is_rejected(self):
        source = self.system.observe("https://secret/root")
        self.system.purge(source)
        with self.assertRaises(AsgError) as cm:
            self.copy(source, "https://secret/new-copy")
        self.assertEqual(cm.exception.code, "E802")

    def test_workspace_cannot_bypass_purge_read_gate(self):
        source = self.system.observe("https://secret/root")
        self.system.purge(source)
        with self.assertRaises(AsgError) as cm:
            self.system.purger.read_record("another-workspace", source)
        self.assertEqual(cm.exception.code, "E303")

    def test_omitted_candidate_does_not_invalidate_manifest(self):
        candidate = self.system.observe("https://source/omitted")
        out = project_context(self.conn, self.system.svc, WS, [], [candidate], 0)
        self.assertEqual(out["selected"], [])
        self.system.withdraw(candidate)
        self.assertTrue(manifest_valid(self.conn, WS, out["commit"]["record_ids"][0]))

    def test_retired_claim_is_not_an_action_precondition(self):
        obs = self.system.observe("https://source/root")
        claim = self.system.claim("retired conclusion", [obs])
        self.system.withdraw(claim)
        self.assertFalse(self.system.act(self.system.grant("v1"), "v1", [claim]))
        self.assertEqual(self.system.last_error, "E401")
        self.assertEqual(self.system.remote.calls, 0)

    def test_seeded_evidence_and_deletion_schedules(self):
        for seed in range(40):
            system = AsgSystem(EvalRemote())
            try:
                rng = random.Random(seed)
                roots = [system.observe(f"https://root/{i}") for i in range(2)]
                c1 = system.claim("first", roots)
                c2 = system.claim("second", [c1])
                system.task("dependent", c2)
                manifest = project_context(system.conn, system.svc, WS, [c2], [], 10000)
                mid = manifest["commit"]["record_ids"][0]
                copies = []
                for index, parent in enumerate((roots[0],)):
                    for depth in range(3):
                        parent = system.svc.observe(
                            WS, _obs_payload(f"https://copy/{index}/{depth}"),
                            system._head(), "req-" + uuid7(), AGENT,
                            typed_refs=[{"role": "derived_from", "target_record_id": parent}]
                        )["record_ids"][0]
                        copies.append(parent)
                active = [True, True]
                purged = False
                history = []
                # Every schedule includes both withdrawals, purge, drains and reads.
                events = ["withdraw0", "withdraw1", "purge0", "drain", "drain", "read"]
                rng.shuffle(events)
                for event in events:
                    history.append(event)
                    with self.subTest(seed=seed, history=list(history)):
                        if event.startswith("withdraw"):
                            index = int(event[-1])
                            system.svc.invalidate(WS, roots[index], "withdrawn", system._head(),
                                                  "req-" + uuid7(), AGENT)
                            active[index] = False
                        elif event == "purge0":
                            system.purge(roots[0])
                            active[0], purged = False, True
                        elif event == "drain":
                            process_queue(system.conn, WS)
                        expected = "SUPPORTED" if any(active) else "NONE"
                        self.assertEqual(system.support_state(c2), expected)
                        for rid in copies:
                            self.assertEqual(system.read_blocked(rid), purged)
                        if event == "drain":
                            self.assertTrue(system.replay_consistent())
                            if not all(active):
                                self.assertFalse(manifest_valid(system.conn, WS, mid))
                                task = system.conn.execute("SELECT status FROM task_state").fetchone()
                                self.assertEqual(task["status"], "PENDING_REEVALUATION")
                process_queue(system.conn, WS)
                self.assertTrue(system.replay_consistent(), f"seed={seed} events={history}")
                self.assertFalse(system.act(system.grant("v1"), "v1", [c2]))
                self.assertEqual(system.remote.calls, 0)
            finally:
                system.close()

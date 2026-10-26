"""Negative closure evidence and all-attempt release guard."""
import tempfile
from pathlib import Path
import unittest

from asg import AsgError
from asg.gateway import Connector, ConnectorContract
from eval.durable_remote import DurableRemote
from tests.test_fault_interleaving import stack, intent, prepare, WS


class TestAttemptClosure(unittest.TestCase):
    def setUp(self):
        self.conn, self.svc, self.ledger, self.auth, self.gw = stack()
        self.addCleanup(self.conn.close)
        self.payload = intent()
        self.payload["idempotency_contract"]["retry_policy"]["max"] = 3
        self.iid = prepare(self.conn, self.auth, self.gw, self.payload)["intent_id"]

    def attempt(self):
        self.conn.execute("UPDATE effect_outbox SET claim_expires_at='2000-01-01', next_retry_at='2000-01-01'")
        claim = self.gw.claim(self.iid)
        return self.gw.begin_attempt(WS, self.iid, claim["token"])

    def test_closure_missing_exact_attempt_id_keeps_reservation(self):
        self.attempt()
        class BadProof(Connector):
            def close_attempt(self, key, aid):
                return {"outcome": "CONFIRMED_NOT_APPLIED", "closed_attempt_id": "other"}
        remote = BadProof(ConnectorContract(mode="KEYED", supports_attempt_closure=True))
        with self.assertRaises(AsgError) as cm:
            self.gw.reconcile(WS, self.iid, remote)
        self.assertEqual(cm.exception.code, "E606")
        self.assertEqual(self.ledger.balance("ws:compute.gpu")["reserved"], 60)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM record WHERE kind='RECEIPT'").fetchone()[0], 0)

    def test_partial_closure_does_not_release_other_persisted_attempt(self):
        first, second = self.attempt(), self.attempt()
        class PartialProof(Connector):
            def close_attempt(self, key, aid):
                return ({"outcome": "CONFIRMED_NOT_APPLIED", "closed_attempt_id": aid}
                        if aid == first["attempt_id"] else None)
        remote = PartialProof(ConnectorContract(mode="KEYED", supports_attempt_closure=True))
        with self.assertRaises(AsgError) as cm:
            self.gw.reconcile(WS, self.iid, remote)
        self.assertEqual(cm.exception.code, "E606")
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(self.gw.effect_status(WS, self.iid)["state"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.ledger.balance("ws:compute.gpu")["reserved"], 60)

    def test_all_closed_then_new_attempt_success_and_closed_late_send_no_conflict(self):
        first, second = self.attempt(), self.attempt()
        with tempfile.TemporaryDirectory() as tmp:
            remote = DurableRemote(Path(tmp) / "remote.sqlite")
            try:
                remote.approve(first["idempotency_key"], first["operation"], first["arguments"], managed=True)
                self.assertEqual(self.gw.reconcile(WS, self.iid, remote), "CONFIRMED_NOT_APPLIED")
                self.assertEqual(self.ledger.balance("ws:compute.gpu")["reserved"], 0)
                self.assertEqual(self.gw.run_once(self.iid, remote)["state"], "CONFIRMED_SUCCEEDED")
                for old in (first, second):
                    response = remote.call_attempt(old)
                    self.assertEqual(response["outcome"], "CONFIRMED_NOT_APPLIED")
                    self.assertEqual(self.gw.record_response(WS, old["attempt_id"], response), "CONFIRMED_SUCCEEDED")
                self.assertIsNone(self.gw.effect_status(WS, self.iid)["unresolved_reason"])
                self.assertEqual(remote.metrics()["effects"], 1)
                self.assertEqual(remote.metrics()["calls"], 3)
                self.assertEqual(self.ledger.balance("ws:compute.gpu")["committed"], 60)
                self.assertEqual(self.ledger.balance("ws:compute.gpu")["reserved"], 0)
            finally:
                remote.close()

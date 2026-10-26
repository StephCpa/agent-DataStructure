"""Subprocess fixture. os._exit(86) deliberately skips connection cleanup."""
import json
import os
from pathlib import Path
import sqlite3
import sys

from asg import AsgError, Authorizer, Gateway, Ledger, Purger, WriteService, connect, init_schema
from asg.recovery import HistoricalView
from asg.ids import uuid7
from eval.durable_remote import DurableRemote
from eval.systems import WS, AGENT, APPROVER, DEST, FAR, BUDGET_KEY, _obs_payload
from asg import compute_operation_digest


def run(directory, mode):
    root = Path(directory)
    db = connect(str(root / "authority.sqlite"))
    init_schema(db)
    remote = DurableRemote(root / "remote.sqlite")
    svc = WriteService(db)
    ledger = Ledger(db)
    auth = Authorizer(db, svc)
    gateway = Gateway(db, svc, auth)
    meta_file = root / "meta.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    def head():
        return svc.current_head(WS)
    def save():
        meta_file.write_text(json.dumps(meta), encoding="utf-8")
    def state():
        return {"pid": os.getpid(), "effect": gateway.effect_status(WS, meta["intent"]),
                "balance": ledger.balance(BUDGET_KEY), "grant": auth.grant_status(meta["grant"]),
                "remote": remote.metrics(),
                "outbox": dict(db.execute("SELECT * FROM effect_outbox WHERE intent_id=?", (meta["intent"],)).fetchone()),
                "reservations": [dict(r) for r in db.execute("SELECT intent_id,amount,state FROM reservation")],
                "attempts": db.execute("SELECT COUNT(*) FROM attempt").fetchone()[0]}
    try:
        if mode in {"prepare_crash", "prepare_max1_crash"}:
            ledger.ensure_budget(BUDGET_KEY, 100)
            payload = {"intent_id": "intent-" + uuid7(), "task_revision_id": "symbolic-task",
                       "operation": "compute.job.submit", "connector_identity": "durable-test",
                       "connector_version": "1", "executor_principal": AGENT, "target_ids": [DEST],
                       "arguments_ref": {"arguments": {"logical_id": "operation-1"}},
                       "artifact_revision_ids": [], "precondition_refs": [],
                       "idempotency_contract": {"mode": "KEYED", "key": "operation-key",
                         "key_scope": "durable-test", "key_expiry": FAR, "retry_policy": {"max": 2}}}
            if mode == "prepare_max1_crash":
                payload["idempotency_contract"]["retry_policy"]["max"] = 1
            grant = auth.issue_grant(WS, APPROVER, AGENT, compute_operation_digest(payload),
                                     "test", FAR, [DEST], expected_head=head())
            gateway.prepare_effect(WS, payload, head(), "prepare", grant["grant_id"],
                                   [{"resource_key": BUDGET_KEY, "amount": 60}])
            obs = svc.observe(WS, _obs_payload("https://checkpoint/secret"), head(), "observe", AGENT)
            meta.update(intent=payload["intent_id"], grant=grant["grant_id"], payload=payload,
                        observation=obs["record_ids"][0], prepare_pid=os.getpid())
            remote.approve("operation-key", payload["operation"], payload["arguments_ref"]["arguments"], managed=True)
            with sqlite3.connect(str(root / "old.sqlite")) as old:
                db.backup(old)
            save()
            os._exit(86)
        if mode in {"effect_crash", "attempt_crash", "effect_wait", "attempt_wait"}:
            claim = gateway.claim(meta["intent"])
            context = gateway.begin_attempt(WS, meta["intent"], claim["token"])
            meta["context"] = context
            meta["old_token"] = claim["token"]
            meta["effect_pid"] = os.getpid()
            if mode not in {"attempt_crash", "attempt_wait"}:
                meta["response"] = remote.call_attempt(context)
            save()
            if mode in {"effect_wait", "attempt_wait"}:
                (root / "waiting.json").write_text(json.dumps({"pid": os.getpid()}))
                if sys.stdin.readline().strip() != "resume":
                    raise AssertionError("Old worker did not receive resume barrier")
                if mode == "attempt_wait":
                    meta["response"] = remote.call_attempt(context)
                gateway.record_response(WS, context["attempt_id"], meta["response"])
                return state()
            os._exit(86)
        if mode == "expire":
            # Deterministic test clock advance, separate from worker recovery.
            db.execute("UPDATE effect_outbox SET claim_expires_at='2000-01-01T00:00:00Z',"
                       " next_retry_at='2000-01-01T00:00:00Z'")
        elif mode == "recover":
            remote.contract.final_query_guarantee = True
            gateway.run_once(meta["intent"], remote)
        elif mode == "reconcile_crash":
            remote.contract.final_query_guarantee = True
            gateway.reconcile(WS, meta["intent"], remote)
            os._exit(86)
        elif mode == "reconcile_closed":
            remote.contract.final_query_guarantee = True
            gateway.reconcile(WS, meta["intent"], remote)
        elif mode == "recover_revoked":
            try:
                gateway.run_once(meta["intent"], remote)
                raise AssertionError("Revoked retry dispatched")
            except AsgError as error:
                assert error.code == "E403", error
        elif mode == "query_only":
            remote.contract.final_query_guarantee = True  # boolean alone must not authorize release
            remote.contract.supports_attempt_closure = False
            gateway.run_once(meta["intent"], remote)
        elif mode in {"reserve80", "reserve80_blocked"}:
            try:
                ledger.reserve(WS, "other-intent", [{"resource_key": BUDGET_KEY, "amount": 80}])
                assert mode == "reserve80", "Unclosed attempt liability was released"
            except AsgError as error:
                assert mode == "reserve80_blocked" and error.code == "E501", error
        elif mode == "invisible":
            remote.set_visibility(False)
            gateway.run_once(meta["intent"], remote)
        elif mode == "visible":
            remote.set_visibility(True)
            gateway.run_once(meta["intent"], remote)
        elif mode == "late_receipt":
            gateway.record_response(WS, meta["context"]["attempt_id"], meta["response"])
        elif mode == "stale_token":
            try:
                gateway.begin_attempt(WS, meta["intent"], meta["old_token"])
                raise AssertionError("Stale token accepted")
            except AsgError as error:
                if error.code != "E201":
                    raise
        elif mode == "revoke_purge":
            auth.revoke(meta["grant"])
            Purger(db, svc).retire_or_purge(WS, [meta["observation"]], head(), "purge", AGENT)
        elif mode == "checkpoint_view":
            old = sqlite3.connect((root / "old.sqlite").as_uri() + "?mode=ro", uri=True)
            old.row_factory = sqlite3.Row
            try:
                view = HistoricalView(old, db)
                assert view.projection(WS)["object_heads"]
                try:
                    view.read_record(WS, meta["observation"])
                    raise AssertionError("Historical body revived")
                except AsgError as error:
                    assert error.code == "E802", error
                try:
                    gateway.run_once(meta["intent"], remote)
                    raise AssertionError("Revoked checkpoint action executed")
                except AsgError as error:
                    assert error.code == "E403", error
            finally:
                old.close()
        elif mode != "inspect":
            raise ValueError(mode)
        return state()
    finally:
        db.close()
        remote.close()


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], sys.argv[2])))

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from eval.durable_remote import DurableRemote

ROOT = Path(__file__).resolve().parents[1]


class TestPersistentRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.pids = set()
        self.worker("prepare_crash", crash=True)

    def worker(self, mode, crash=False):
        result = subprocess.run([sys.executable, "-W", "error::ResourceWarning", "-m",
                                 "tests.recovery_worker", str(self.directory), mode], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=20,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 86 if crash else 0, result.stdout + result.stderr)
        if not crash:
            state = json.loads(result.stdout)
            self.pids.add(state["pid"])
            self.assertEqual(state["remote"]["unauthorized_effects"], 0)
            self.assertEqual(state["remote"]["duplicate_effects"], 0)
            return state

    def success(self, state, calls=1, attempts=1):
        self.assertEqual(state["effect"]["state"], "CONFIRMED_SUCCEEDED")
        self.assertEqual(state["balance"]["committed"], 60)
        self.assertEqual(state["balance"]["reserved"], 0)
        self.assertEqual(state["grant"]["uses_consumed"], 1)
        self.assertEqual(state["remote"]["calls"], calls)
        self.assertEqual(state["remote"]["effects"], 1)
        self.assertEqual(state["attempts"], attempts)

    def test_prepare_committed_process_exit_then_resume(self):
        before = self.worker("inspect")
        self.assertEqual(before["effect"]["state"], "PREPARED")
        self.assertEqual(before["balance"]["reserved"], 60)
        self.assertEqual(before["remote"]["calls"], 0)
        self.success(self.worker("recover"))
        self.success(self.worker("recover"))
        self.assertGreaterEqual(len(self.pids), 2)

    def test_effect_survives_worker_crash_before_receipt(self):
        self.worker("effect_crash", crash=True)
        uncertain = self.worker("inspect")
        self.assertEqual(uncertain["effect"]["state"], "IN_FLIGHT")
        self.assertEqual(uncertain["remote"]["effects"], 1)
        self.assertEqual(uncertain["balance"]["committed"], 0)
        self.worker("expire")
        recovered = self.worker("recover")
        self.success(recovered)
        self.assertEqual(recovered["remote"]["queries"], 1)
        self.success(self.worker("recover"))

    def test_attempt_without_effect_final_query_then_safe_retry(self):
        self.worker("attempt_crash", crash=True)
        self.worker("expire")
        self.success(self.worker("recover"), attempts=2)
        self.success(self.worker("recover"), attempts=2)

    def test_lease_takeover_and_old_worker_receipt(self):
        self.worker("effect_crash", crash=True)
        self.worker("expire")
        self.success(self.worker("recover"))
        self.success(self.worker("stale_token"))
        self.success(self.worker("late_receipt"))
        self.success(self.worker("recover"))

    def test_reconcile_commit_exit_before_retry_then_restart(self):
        self.worker("attempt_crash", crash=True)
        self.worker("expire")
        self.worker("reconcile_crash", crash=True)
        before = self.worker("inspect")
        self.assertEqual(before["effect"]["state"], "CONFIRMED_NOT_APPLIED")
        self.assertEqual(before["remote"]["calls"], 0)
        self.success(self.worker("recover"), attempts=2)

    def test_reconcile_commit_exit_then_revoked_grant_blocks_retry(self):
        self.worker("attempt_crash", crash=True)
        self.worker("expire")
        self.worker("reconcile_crash", crash=True)
        self.worker("revoke_purge")
        state = self.worker("recover_revoked")
        self.assertEqual(state["remote"]["calls"], 0)
        self.assertEqual(state["remote"]["effects"], 0)
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(state["balance"]["reserved"], 0)

    def test_reconcile_commit_exit_with_exhausted_attempt_budget_stays_done(self):
        # A second fresh fixture; no mutation of the immutable retry policy.
        with tempfile.TemporaryDirectory() as tmp:
            self.directory = Path(tmp)
            self.worker("prepare_max1_crash", crash=True)
            self.worker("attempt_crash", crash=True)
            self.worker("expire")
            self.worker("reconcile_crash", crash=True)
            for _ in range(2):
                state = self.worker("recover")
                self.assertEqual(state["effect"]["state"], "CONFIRMED_NOT_APPLIED")
                self.assertEqual(state["outbox"]["state"], "DONE")
                self.assertEqual(state["attempts"], 1)
                self.assertEqual(state["remote"]["calls"], 0)
                self.assertEqual(state["balance"]["reserved"], 0)

    def test_live_old_worker_submits_after_new_worker_takes_over(self):
        process = subprocess.Popen([sys.executable, "-m", "tests.recovery_worker",
                                    str(self.directory), "effect_wait"], cwd=ROOT,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8",
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            deadline = time.monotonic() + 10
            while not (self.directory / "waiting.json").exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("Old worker failed to reach the durable-effect barrier")
                time.sleep(0.02)
            old_pid = json.loads((self.directory / "waiting.json").read_text())["pid"]
            self.worker("expire")
            takeover = self.worker("recover")
            self.success(takeover)
            self.assertNotEqual(takeover["pid"], old_pid)
            output, error = process.communicate("resume\n", timeout=10)
            self.assertEqual(process.returncode, 0, error)
            late = json.loads(output)
            self.assertEqual(late["pid"], old_pid)
            self.success(late)
            self.success(self.worker("recover"))
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_unknown_is_distinct_from_external_truth_then_resolves(self):
        self.worker("effect_crash", crash=True)
        self.worker("expire")
        unknown = self.worker("invisible")
        self.assertEqual(unknown["effect"]["state"], "OUTCOME_UNKNOWN")
        self.assertEqual(unknown["balance"]["reserved"], 60)
        self.assertEqual(unknown["remote"]["effects"], 1)
        self.assertEqual(unknown["remote"]["calls"], 1)
        self.worker("expire")
        self.success(self.worker("visible"))

    def test_old_checkpoint_requires_current_deletion_and_revocation(self):
        self.worker("revoke_purge")
        state = self.worker("checkpoint_view")
        self.assertEqual(state["grant"]["status"], "revoked")
        self.assertEqual(state["remote"]["calls"], 0)
        self.assertEqual(state["remote"]["effects"], 0)
        self.assertEqual(state["balance"]["reserved"], 60)

    def predispatch_worker(self, closure):
        process = subprocess.Popen([sys.executable, "-m", "tests.recovery_worker",
                                    str(self.directory), "attempt_wait"], cwd=ROOT,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8",
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            deadline = time.monotonic() + 10
            while not (self.directory / "waiting.json").exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("Worker did not reach pre-dispatch barrier")
                time.sleep(0.02)
            old_pid = json.loads((self.directory / "waiting.json").read_text())["pid"]
            self.worker("expire")
            reconciled = self.worker("reconcile_closed" if closure else "query_only")
            self.assertEqual(reconciled["effect"]["state"],
                             "CONFIRMED_NOT_APPLIED" if closure else "OUTCOME_UNKNOWN")
            self.assertEqual(reconciled["balance"]["reserved"], 0 if closure else 60)
            self.worker("reserve80" if closure else "reserve80_blocked")
            output, error = process.communicate("resume\n", timeout=10)
            self.assertEqual(process.returncode, 0, error)
            late = json.loads(output)
            self.assertEqual(late["pid"], old_pid)
            self.assertEqual(late["remote"]["calls"], 1)
            self.assertEqual(late["remote"]["effects"], 0 if closure else 1)
            self.assertEqual(late["balance"]["committed"], 0 if closure else 60)
            self.assertEqual(late["balance"]["reserved"], 80 if closure else 0)
            self.assertEqual(late["effect"]["state"],
                             "CONFIRMED_NOT_APPLIED" if closure else "CONFIRMED_SUCCEEDED")
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_live_predispatch_worker_is_closed_before_releasing_liability(self):
        self.predispatch_worker(closure=True)

    def test_negative_query_without_closure_retains_liability_until_old_worker_succeeds(self):
        self.predispatch_worker(closure=False)


class TestRemoteMetrics(unittest.TestCase):
    def test_closed_attempt_stays_closed_across_restart_new_attempt_can_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "remote.sqlite"
            remote = DurableRemote(path)
            try:
                args = {"logical_id": "same-operation"}
                remote.approve("key", "op", args, managed=True)
                context = {"operation": "op", "arguments": args, "idempotency_key": "key", "attempt_id": "old"}
                self.assertEqual(remote.close_attempt("key", "old")["closed_attempt_id"], "old")
                remote.close()
                remote = DurableRemote(path)
                self.assertEqual(remote.call_attempt(context)["outcome"], "CONFIRMED_NOT_APPLIED")
                self.assertEqual(remote.call("op", args, "key")["outcome"], "CONFIRMED_NOT_APPLIED")
                self.assertEqual(remote.metrics()["effects"], 0)
                self.assertEqual(remote.call_attempt(dict(context, attempt_id="new"))["outcome"], "CONFIRMED_SUCCEEDED")
                self.assertEqual(remote.call_attempt(context)["outcome"], "CONFIRMED_NOT_APPLIED")
                self.assertEqual(remote.close_attempt("key", "old")["outcome"], "CONFIRMED_NOT_APPLIED")
                self.assertEqual(remote.metrics()["effects"], 1)
                self.assertEqual(remote.metrics()["duplicate_effects"], 0)
            finally:
                remote.close()

    def test_calls_effects_response_loss_and_duplicates_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote = DurableRemote(Path(tmp) / "remote.sqlite")
            try:
                args = {"logical_id": "same-operation"}
                remote.approve("key", "op", args)
                remote.set_response_loss(True)
                self.assertIsNone(remote.call("op", args, "key"))
                remote.close()
                remote = DurableRemote(Path(tmp) / "remote.sqlite")
                remote.set_response_loss(False)
                self.assertEqual(remote.call("op", args, "key")["outcome"], "CONFIRMED_SUCCEEDED")
                self.assertEqual(remote.metrics()["calls"], 2)
                self.assertEqual(remote.metrics()["effects"], 1)
                remote.call("op", args, None)
                self.assertEqual(remote.metrics()["duplicate_effects"], 1)
                self.assertEqual(remote.metrics()["unauthorized_effects"], 1)
            finally:
                remote.close()

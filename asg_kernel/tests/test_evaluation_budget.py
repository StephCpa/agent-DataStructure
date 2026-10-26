import unittest

from asg import AsgError, evaluate_support, justify
from eval.systems import AsgSystem, EvalRemote, WS


class TestEvaluationBudget(unittest.TestCase):
    def setUp(self):
        self.s = AsgSystem(EvalRemote())
        self.addCleanup(self.s.close)

    def evaluate(self, claim, **budget):
        return evaluate_support(self.s.conn, WS, claim, budget=budget)

    def hidden_refutation(self):
        obs = [self.s.observe(f"https://budget/{i}") for i in range(17)]
        claim = self.s.claim("bounded conclusion", [])
        justify(self.s.svc, WS, claim, [[o] for o in obs[:16]],
                refute_groups=[[obs[16]]], expected_head=self.s._head())
        return claim

    def test_hidden_seventeenth_refutation_blocks_action(self):
        claim = self.hidden_refutation()
        small = self.evaluate(claim)
        self.assertTrue(small["truncated"])
        self.assertEqual(self.evaluate(claim, max_paths=17)["four_state"], "CONTESTED")
        self.assertFalse(self.s.act(self.s.grant("v1"), "v1", [claim]))
        self.assertEqual(self.s.last_error, "E401")
        self.assertEqual(self.s.remote.calls, 0)

    def test_result_distinguishes_absence_from_unknown(self):
        result = self.evaluate(self.hidden_refutation())
        self.assertEqual(result["support"], "FOUND")
        self.assertEqual(result["refutation"], "UNKNOWN")
        self.assertEqual(result["evaluation_status"], "INCOMPLETE")
        self.assertIn("max_paths", result["exhausted_budget"])

    def test_child_truncation_reaches_outer_gate(self):
        inner = self.hidden_refutation()
        outer = self.s.claim("outer", [inner])
        self.assertTrue(self.evaluate(outer)["truncated"])
        self.assertFalse(self.s.act(self.s.grant("v1"), "v1", [outer]))
        self.assertEqual(self.s.remote.calls, 0)

    def test_depth_is_recursive_not_group_width(self):
        obs = [self.s.observe(f"https://wide/{i}") for i in range(10)]
        wide = self.s.claim("wide", [])
        justify(self.s.svc, WS, wide, [obs], expected_head=self.s._head())
        self.assertEqual(self.evaluate(wide, max_depth=1)["four_state"], "SUPPORTED")
        outer = self.s.claim("depth two", [wide])
        self.assertTrue(self.evaluate(outer, max_depth=0)["truncated"])
        self.assertEqual(self.evaluate(outer, max_depth=2)["four_state"], "SUPPORTED")

    def test_group_width_has_separate_limit(self):
        obs = [self.s.observe(f"https://width/{i}") for i in range(3)]
        claim = self.s.claim("wide", [])
        justify(self.s.svc, WS, claim, [obs], expected_head=self.s._head())
        result = self.evaluate(claim, max_group_width=2)
        self.assertTrue(result["truncated"])
        self.assertIn("max_group_width", result["exhausted_budget"])

    def test_recompute_budget_shared_by_children(self):
        obs = self.s.observe("https://budget/root")
        inner = self.s.claim("inner", [obs])
        outer = self.s.claim("outer", [inner])
        result = self.evaluate(outer, max_recompute=4)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["budget_used"]["recompute"], 4)
        self.assertIn("max_recompute", result["exhausted_budget"])

    def test_shared_subgraph_reuses_complete_evaluation(self):
        obs = self.s.observe("https://shared/root")
        inner = self.s.claim("inner", [obs])
        outer = self.s.claim("outer", [])
        justify(self.s.svc, WS, outer, [[inner], [inner]], expected_head=self.s._head())
        result = self.evaluate(outer, max_paths=3)
        self.assertEqual(result["four_state"], "SUPPORTED")
        self.assertEqual(result["budget_used"]["paths"], 3)
        self.assertGreater(result["budget_used"]["cache_hits"], 0)

    def test_complete_positive_case_executes(self):
        obs = self.s.observe("https://positive/root")
        claim = self.s.claim("positive", [obs])
        result = self.evaluate(claim)
        self.assertEqual(result["evaluation_status"], "COMPLETE")
        self.assertEqual(result["refutation"], "ABSENT")
        self.assertTrue(self.s.act(self.s.grant("v1"), "v1", [claim]))
        self.assertEqual(self.s.remote.calls, 1)

    def test_invalid_budget_rejected(self):
        claim = self.s.claim("empty", [])
        for budget in ({"max_paths": -1}, {"max_depth": True}, {"unknown": 1}):
            with self.assertRaises(AsgError):
                self.evaluate(claim, **budget)

    def test_dispatch_rechecks_incomplete_evaluation_after_prepare(self):
        obs = self.s.observe("https://dispatch/initial")
        claim = self.s.claim("prepared claim", [obs])
        grant = self.s.grant("v1")
        payload = self.s._payload("v1", [claim])
        result = self.s.gw.prepare_effect(WS, payload, self.s._head(), "prepare-budget", grant,
                                         [{"resource_key": "ws:compute.gpu", "amount": 10}])
        extra = [self.s.observe(f"https://dispatch/{i}") for i in range(16)]
        justify(self.s.svc, WS, claim, [[o] for o in extra[:15]], refute_groups=[[extra[15]]],
                expected_head=self.s._head())
        with self.assertRaises(AsgError) as cm:
            self.s.gw.run_once(result["intent_id"], self.s.remote)
        self.assertEqual(cm.exception.code, "E401")
        self.assertEqual(self.s.remote.calls, 0)
        self.assertEqual(self.s.ledger.balance("ws:compute.gpu")["committed"], 0)

    def test_cycle_produces_no_support_and_cannot_execute(self):
        a, b = self.s.claim("cycle A", []), self.s.claim("cycle B", [])
        justify(self.s.svc, WS, a, [[b]], expected_head=self.s._head())
        justify(self.s.svc, WS, b, [[a]], expected_head=self.s._head())
        result = self.evaluate(a)
        self.assertFalse(result["supported"])
        self.assertTrue(result["truncated"])
        self.assertIn("cycle", result["exhausted_budget"])
        self.assertFalse(self.s.act(self.s.grant("v1"), "v1", [a]))
        self.assertEqual(self.s.remote.calls, 0)

"""阶段 D 第一层：ASG 与简单基线在同一批故障场景上的对比。

场景取自说明书第 13.3 节的对抗清单。每个场景对两个系统施加相同操作序列，
断言 ASG 满足契约；基线按“消息列表 + 就地覆盖 + 名称级授权 + 盲目重试”
的固有行为失败。基线的失败是预期结果，用来说明结构带来的差异，
不是待修复缺陷。
"""
from __future__ import annotations

import unittest

from eval.systems import AsgSystem, BaselineSystem, EvalRemote


def systems():
    return AsgSystem(EvalRemote()), BaselineSystem(EvalRemote())


class TestStaleEvidenceBlocksAction(unittest.TestCase):
    """唯一证据被撤回后，依赖它的动作不得执行（TC-01 的动作侧）。"""

    def test_asg_blocks_and_baseline_executes(self):
        for system, expect_blocked in ((AsgSystem(EvalRemote()), True),
                                       (BaselineSystem(EvalRemote()), False)):
            with self.subTest(system=type(system).__name__):
                obs = system.observe("https://docs.example/spec")
                claim = system.claim("测试集 500 条", [obs])
                grant = system.grant("config-v1")
                system.withdraw(obs)
                executed = system.act(grant, "config-v1", [claim])
                self.assertEqual(executed, not expect_blocked)
                system.close()


class TestConflictNotOverwritten(unittest.TestCase):
    """较新但低可信的消息不得覆盖较早的有效证据（TC-02）。"""

    def test_asg_keeps_contested_baseline_overwrites(self):
        for system, expected in ((AsgSystem(EvalRemote()), "CONTESTED"),
                                 (BaselineSystem(EvalRemote()), "OVERWRITTEN")):
            with self.subTest(system=type(system).__name__):
                old = system.observe("https://official.example/notice")
                claim = system.claim("负责人自周一起变更", [old])
                newer = system.observe("https://unverified.example/rumor")
                system.contradict(claim, newer)
                self.assertEqual(system.support_state(claim), expected)
                system.close()


class TestGrantBindsContent(unittest.TestCase):
    """审批后修改参数，新动作不得借用旧授权（TC-04）。"""

    def test_asg_rejects_modified_content(self):
        for system, expect_allowed in ((AsgSystem(EvalRemote()), False),
                                       (BaselineSystem(EvalRemote()), True)):
            with self.subTest(system=type(system).__name__):
                grant = system.grant("config-v7")
                executed = system.act(grant, "config-v8", [])
                self.assertEqual(executed, expect_allowed)
                system.close()


class TestCrashDoesNotDuplicate(unittest.TestCase):
    """外部成功后回执持久化前崩溃，恢复不得产生重复外部效果（TC-05）。"""

    def test_asg_reconciles_baseline_retries(self):
        for system, expect_calls in ((AsgSystem(EvalRemote()), 1),
                                     (BaselineSystem(EvalRemote()), 2)):
            with self.subTest(system=type(system).__name__):
                grant = system.grant("config-v1")
                system.start_and_crash(grant, "config-v1")
                system.recover()
                self.assertEqual(system.remote.calls, expect_calls)
                system.close()


class TestBudgetNotOverspent(unittest.TestCase):
    """两个 Agent 各读到余额 100 后各申请 60，不得超额（TC-03）。"""

    def test_asg_rejects_second_baseline_accepts_both(self):
        for system, expect_both in ((AsgSystem(EvalRemote()), False),
                                    (BaselineSystem(EvalRemote()), True)):
            with self.subTest(system=type(system).__name__):
                system.snapshot()
                first = system.reserve(60)
                second = system.reserve(60)
                self.assertTrue(first)
                self.assertEqual(first and second, expect_both)
                system.close()


class TestPurgeNotRevived(unittest.TestCase):
    """删除后旧副本与重新采集都不得恢复正文（TC-07）。"""

    def test_asg_blocks_revival_baseline_keeps_copy(self):
        for system, expect_blocked in ((AsgSystem(EvalRemote()), True),
                                       (BaselineSystem(EvalRemote()), False)):
            with self.subTest(system=type(system).__name__):
                obs = system.observe("https://secret.example/page")
                system.purge(obs)
                self.assertEqual(system.read_blocked(obs), expect_blocked)
                self.assertEqual(system.reobserve_blocked("https://secret.example/page"),
                                 expect_blocked)
                system.close()


class TestReplay(unittest.TestCase):
    """撤回证据并完成传播后，在线状态与回放状态一致。"""

    def test_asg_replays_baseline_cannot(self):
        for system, expect in ((AsgSystem(EvalRemote()), True),
                               (BaselineSystem(EvalRemote()), False)):
            with self.subTest(system=type(system).__name__):
                obs = system.observe("https://plan.example/input")
                system.task("整理数据说明", obs)
                system.withdraw(obs)
                self.assertEqual(system.replay_consistent(), expect)
                system.close()


if __name__ == "__main__":
    unittest.main()

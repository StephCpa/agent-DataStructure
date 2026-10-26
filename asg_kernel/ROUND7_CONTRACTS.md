# 第七轮：恢复、资源责任、任务身份与上下文快照

日期：2026-10-27。基于第六轮 105 项源码快照继续修复；本轮 123 项测试通过，新增 18 项。
第五、六轮归档保持原样。本轮仍是 `1ff974a` 之后的未提交工作树，不代表已经推送 main。

## 对独立复核的处理

接受 R6-A / B / C / D 与两项次级问题。已复现并保存修复前的失败日志：
`review_artifacts/round7/before-fixes.txt`（五项）与 `before-closure.txt`（两项）。
测试的期望直接来自对象身份、额度责任、远端效果与渲染内容，不把在线/replay 一致单独当作正确性证据。

| 审查项 | 本轮行为 | 验收位置 |
|---|---|---|
| R6-A 对账后退出丢失重试 | 已确认未生效且持久化 outbox 要求重试、次数未耗尽时，恢复执行 dispatch 的授权、依赖、预算检查；成功和取消终态仍不重发 | `test_persistent_recovery.py`：对账提交后硬退出、撤销授权、次数耗尽三条 |
| R6-B 旧发送未关闭就释放 | 普通负查询保持 UNKNOWN 与占用；有远端逐 Attempt 关闭证据且全部可能送达尝试均未生效，才允许释放 | 同文件的存活旧 PID 调用前屏障；`test_attempt_closure.py` |
| R6-C 双重任务身份 | 注册校验要求 task_object_id == object_id，任何不符整批拒绝 E101；合法 supersedes 修订仍可恢复 ACTIVE | `test_round7_boundaries.py::TestTaskIdentity` |
| R6-D 旧摘要认证为新快照 | 一个 SQLite WAL 读取事务覆盖筛选、绑定单元、计数、渲染、snapshot、stamp 与清单提交；并发写导致提交升级失败时回滚 E702，调用方重建 | `test_round7_boundaries.py::TestContextSnapshot` |
| 任务自身失效 / uses_version | 初始验证纳入资格检查使用的依赖、共享预算和任务自身深度；传播检查任务自身资格；v3 回放重建自身撤回后的待重评 | `TestTaskIdentity`：撤回、删除、uses_version、深度边界 |
| 来源独立性误读 | 移除 independent_root_count / independent_roots，改为 lineage_rooted_support_item_count / lineage_rooted_support_items，并明确 scope | `TestTaskIdentity`：同一 O 经 A、B 支持 C |

## 远端关闭机制的确切含义

`final_query_guarantee` 仅保留为旧接口元数据，不再凭这个布尔值把查无结果转换成未生效。
键查询返回显式 NOT_APPLIED 也不能独自关闭未来发送。
连接器要支持释放路径，必须实现 `supports_attempt_closure`、`call_attempt(context)`、
`close_attempt(key, attempt_id)`：

1. dispatch 携带不可变 Attempt ID，重试创建新的 Attempt ID，但保留同一逻辑幂等键。
2. 远端把“查询既有效果”与“永久关闭指定 Attempt 的未来发送”串行化。
3. 关闭返回未生效时必须返回对应 `closed_attempt_id`；网关将该标识写入回执分类依据。
4. 一个 Attempt 已关闭，不会因新 Attempt 成功或远端重启而重新开放。
5. 所有已持久化且可能送达的 Attempt 都须有确定未生效结果；缺证据或只关闭一部分，不能释放。

持久假远端用自己的 SQLite 事务实现第 2 点，关闭表与实际效果表使用同一写锁。
托管操作键拒绝不携带 Attempt ID 的调用。普通 `call` 保留给未托管的模拟器计数对照。
关闭不改变授权和操作内容；原始参数摘要仍用于模拟器的未批准效果统计。

两种实际存活旧 PID 轨迹均已验证：

| 路径 | 接管后的原预留 | 另一笔 80 | 旧 PID 恢复后的效果 | 原操作结算 |
|---|---:|---|---:|---|
| 普通负查询，无关闭能力 | 保留 60 | E501 拒绝 | 1 | 原 60 committed |
| 远端关闭旧 Attempt | 释放 60 | 预留成功 | 0 | 原 60 保持 released |

此外，多个旧 Attempt 关闭后，新 Attempt 可正常成功；旧 Attempt 的迟到拒绝不会把该成功变成冲突。
这是对测试远端协议的验收；没有证明任意真实服务支持此机制，也没有测量真实账单超额。
主调用直接返回的最终未生效回执仍属于可信连接器契约；蓄意矛盾的可信回执测试仅检验冲突记录，
不能视作远端保证已兑现。当前仍不提供抵御恶意连接器的协议证明。

## 上下文的一致读取边界

`project_context` 自己建立 deferred WAL 事务，要求与 WriteService 共用同一连接且没有外层事务。
所有 SQLite 输入来自同一读取视图，随后在同一事务中写清单。
即使外部更新未推进 head_sequence（例如预算表更新），读取快照升级写入也不能越过该并发更新。
这种保守选择可能因无关写入返回 E702；调用方需要重建，本轮不自动无限重试。

测试在 units 已读取、snapshot 尚未读取时，由第二连接提交反证。
旧构建必须 E702 且无清单残留；新构建的摘要必须匹配 CONTESTED 单元，manifest_valid=true。
另测了不推进 head_sequence 的业务表并发写，以及清单之后增加 Claim 导致渲染计数变化的失效。
`ledger_inputs` 保存当前实际渲染的 claim_count，复用检查也核对该计数。
以后引入其他渲染输入时，必须纳入同一事务并扩展复用检查，不能只依赖主提交序号。

## 回放版本与字段兼容性

新提交与默认回放使用 reducer v3，v1/v2 函数保留历史行为，不把旧版本结果悄悄替换成新规则。
v3 拒绝任务身份错配的回放输入。任务自身的撤回/删除单独影响状态，不能被不变的依赖戳覆盖。
存量数据库未做批量自动迁移；带旧验证戳的任务需要显式修订、重评后才能使用新的完整根集合。

来源指标的改名是原型 API 变更。新字段只对直接支持项沿显式谱系链作去重；中间 Claim 的完整证明
没有展开，多个谱系父边也没有穷举。O→A/B→C 的结果可列 A、B，数量 2，不能称为两份独立佐证。
该指标不参与动作授权。证明展开、完整来源去重与其预算仍未实现。

## 测试与边界

新增 18 项：持久恢复/远端 6、关闭证据 3、任务/上下文/来源指标 9；总计 123 项。
保留原来的 60×12 执行随机轨迹与 40×6 证据/删除轨迹。
执行随机轨迹的 S4 改为：无新增 Attempt 时，离开未生效态需要冲突标记；有合法新 Attempt 的重试
允许成功。这与 R6-A 的意图/尝试区分一致，不再把每次未生效都当作意图永久终态。

旧 `FakeKeyedRemote` 补充了实际旧 Attempt 拒绝行为，保留其正常安全重试测试；原来的负查询后
注入成功回执测试改为明确注入两份矛盾可信回执，仍断言冲突及不错误结算。没有把该故障注入用例
当作关闭协议的阳性验证；关闭协议由独立磁盘远端和存活 PID 测试验证。

运行：`python -W error::ResourceWarning -m unittest discover -s tests -t . -v`。
完整包通过 `python tools/package_review.py --label round7` 生成，包含源码、逐文件哈希、原始测试日志、
修复前失败日志与 tracked.patch；tracked.patch 本身不包含未跟踪源码，完整源码目录才是审查依据。

仍未验收：成熟强基线、真实模型第二/三层、完整灾备回滚、分布式故障、真实服务关闭协议、
SQL 扫描/传播硬预算、大图性能及完整证明来源展开。123 项通过不代表完整契约已经证明。

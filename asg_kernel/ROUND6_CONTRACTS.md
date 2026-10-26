# 第六轮：求值完整性、论证版本、动态依赖与持久化恢复

日期：2026-10-26。代码基线仍为 `1ff974a`，第五、六轮均为本地工作树，未推送。
第五轮已经先行封存为 `review_artifacts/ASG_round5_complete.zip`，含完整 asg_kernel、
未跟踪文件、75 项测试原始输出、源码 SHA-256 清单和 tracked.patch。
本轮完成后的源码另行打包，不能用第六轮源码替代第五轮快照。

## 核验结论

审查提出的“16 条支持耗尽预算，反证未检查但动作仍执行”在第五轮源码上也复现。
修复后，同一输入默认返回 INCOMPLETE，关键动作 E401、零外呼；增加预算能看到 CONTESTED。
当前 105 项测试通过，其中本轮增加 30 项：预算 11、版本/依赖 11、持久恢复/模拟器 8。
测试总数仅用于核对运行覆盖；验收目标及边界以下表和契约为准。

| 验收目标 | 实现与证据 | 已测边界 |
|---|---|---|
| 截断结果不被关键动作当作完整结论 | evidence 共享预算、显式未知；read_gate / gateway 拒绝；prepare 与 dispatch 各有反例回归 | 内存 SQLite、确定性图、假连接器 |
| 论证版本、引用、晚到依赖含义一致 | 最新修订不回退、注册表规范化、依赖版本戳、reducer v2；六步版本轨迹与阳性固定点测试 | 显式引用、main 分支、记录序号快照 |
| 持久化恢复可继续执行且不重复结算 | 文件 SQLite、独立进程 os._exit、持久远端、活的旧 worker 屏障交错 | 进程退出/重开和租约时钟注入，不是电源故障或分布式数据库验收 |

## 1. 求值预算与返回契约

一次 evaluate_support 调用创建一个预算上下文，所有递归子求值共享它：

| 参数 | 默认 | 计量单位 |
|---|---:|---|
| max_paths | 16 | 支持与反证两侧、各级 Claim 的 AND 组访问总数 |
| max_depth | 8 | 根深度为 0，每条被递归检查的证据依赖增加 1；含 Claim 与来源祖先 |
| max_recompute | 512 | 一个未缓存记录检查或一个 AND 组访问各计 1 单位 |
| max_group_width | 64 | 单个 AND 组或规则/假设合取的成员数，独立于递归深度 |

预算必须为非负整数；未知参数、负数、布尔值被拒绝。递归实现额外限制 max_depth≤128。
缓存仅在本次求值内存在；完整 Claim 结果按 `(record_id, depth)` 缓存，记录检查也缓存。
命中缓存不再计 recompute，但计入 cache_hits；不同深度不能借浅层缓存绕过深度限制。
循环返回不完整，不制造自支持。exhausted_budget 中的 cycle 是未完成原因，而非耗用额度。

返回语义：

- support/refutation 分别为 FOUND、ABSENT 或 UNKNOWN。ABSENT 只有在所需检查完成后才成立。
- evaluation_status 为 COMPLETE 或 INCOMPLETE；truncated 是兼容标志。
- supported/refuted 只表示已经找到的有效见证，不能单独用作完整性判据。
- 不完整时 four_state 返回 INCOMPLETE。这是求值视图标志，不是第五种证据真值。
- budget_used 返回 paths、recompute、max_depth、cache_hits；exhausted_budget 返回具体未完成原因。

关键动作采用保守规则：依赖的 Claim 求值必须完整、有支持、无反证。
上下文探索视图同时携带 evaluation_status 和耗尽原因，不能把不完整伪装为 SUPPORTED。
预算限制逻辑求值工作，**不等于 SQL 扫描、来源根报告或端到端耗时的硬上限**。

主要用例：第 17 条隐藏反证、子求值截断传播、prepare 后新增反证的 dispatch 重验、
递归深度与组宽分离、共享 recompute、共享子图缓存、循环、合法阳性动作。

## 2. 权威引用和论证修订

JUSTIFICATION 的 object_id 是逻辑论证身份；supersedes 指向该对象当前头。
先按 `(sequence, rowid)` 选择快照内的最新修订，再检查撤回/删除/目标 Claim。
当前修订被撤回不会回退到旧版；恢复旧内容须创建显式新修订。
不同逻辑论证仍为独立 OR 路径，规则/假设仅约束所属论证。

冻结轨迹的独立预期为：SUPPORTED → CONTESTED → REFUTED → NONE → SUPPORTED；
最后直接撤回目标 Claim，动作 E401。测试还验证历史快照仍看到当时的旧修订与撤回状态。
当前墓碑始终阻止已删除内容重新参与使用。

`registry.REFERENCE_FIELDS` 声明实际 record ID 字段：

| 记录 | 规范化字段 |
|---|---|
| JUSTIFICATION | target_claim_id、support_groups、refute_groups、rule_refs、assumption_refs |
| OBSERVATION | derived_from、extracted_from、transcribed_from 列表 |
| TASK_REVISION | dependency_refs |
| ACTION_INTENT | precondition_refs |
| RETIRE_PURGE_MARK / ASSESSMENT | target.record_id / target_record_id |
| RECEIPT | evidence_observation_id |
| CONTEXT_MANIFEST | selected_items[].record_id |

遗漏的边由服务补齐；结构化载荷与对应 typed_refs 冲突则 E101，事务不留半写入。
可选的观察来源/任务依赖字段只从 typed_refs 提供时，服务同步写入规范化载荷。
引用去重、存在性、批内拓扑、工作空间检查均作用于规范化后的边。
prior_revision_ids 与 supersedes 的父版本合并，保存独立 prior_revision 边，并检查对象/类型一致。
旧 justify 将目标编码成 supports 的冗余形式被规范化为 targets_claim。

来源边、版本边和求值边保持不同用途：删除闭包仅沿三类显式来源边；
求值/失效视图忽略历史论证和 supersedes/prior_revision，不把旧版本撤回传播到健康新版本。
targets_claim 及由它导出的反向求值关系补齐论证到 Claim 的变化传播。

明确边界：现有 task_revision_id、acceptance_refs 仍允许历史脚手架的符号值；
operation、intent、job 等逻辑标识也不自动解释为 record ID。
本轮没有声称全部业务关联键都已成为严格外键，也没有重写存量不可变记录。

## 3. 晚到依赖与固定点

关键读取和动作同步求值当前依赖，包含来源祖先；不以旧队列已排空代替资格检查。
新建任务时，服务计算初始资格：无效/不完整依赖立即进入 PENDING_REEVALUATION。

任务修订和上下文清单保存服务生成的 `_validation` 依赖版本戳。版本戳包含相关记录、
当前有效论证修订和相关撤回状态，**不承担真值判断**。调用方提供的该字段会被服务覆盖。
变化后任务待重评；显式 revise_task 在新快照重新求值，健康任务可恢复 ACTIVE。
上下文按已选输入的版本戳和当前 policy_epoch 验证；新清单不会被历史标记永久污染。

固定点测试同时检查：

1. 撤回 O、排空，随后创建依赖 O 的论证、下游 Claim、任务和上下文。
2. 再次排空之前，关键读取和动作即 E401，远端调用为 0。
3. 加入独立 O2，显式重评任务，生成新清单。
4. 连续两次空排空，业务状态完全相同；任务 ACTIVE、新清单有效、动作成功。

`process_queue(metrics=...)` 返回扫描计数到调用方字典：records_scanned、refs_scanned、
closure_nodes_visited。当前仍全图扫描，包括历史根重验；这些计数不是性能收益结论。
max_items 只限制队列项，不限制图节点；达到队列项额度后不提前推进完成水位。
图级可恢复分片预算尚未实现。水位按工作空间分别保存。

新增提交声明 reducer_version=2，默认投影使用 reducer_v2。
第五轮 reducer_v1 及其关系映射冻结保留，可显式指定版本作旧语义审计。
v1 不是新契约的恢复路径；新增行为的在线/回放比较使用 v2。

## 4. 恢复范围与权威状态

| 数据 | 当前权威来源 | 本轮验证的恢复方式 |
|---|---|---|
| 不可变记录、规范化引用、提交序 | record / record_ref / commit | 文件数据库重开；用于对象头/任务投影回放 |
| 对象头、任务状态 | 可从记录和引用派生；新任务验证戳在记录内 | reducer v2 语义回放与在线比较 |
| 授权有效性与次数 | grant_record / grant_usage 的事务状态 | 数据库重开保留撤销和占用；不声称仅靠 record 重建 |
| 预算与资源责任 | budget_ledger / reservation | 重开后继续对账/执行，确认只结算一次 |
| 传输、意图与调度 | attempt / effect_state / effect_outbox | 重开、租约接管、对账和迟到回执 |
| 删除约束 | 当前 authority 的 tombstone / policy_epoch | 历史视图也必须通过当前读取门控 |
| 外部真实效果 | 独立 remote.sqlite | 与 worker 内存和 authority 数据库分别持久化 |

磁盘测试使用真实子进程，不靠重新创建一个内存对象假装重启。
os._exit(86) 故意跳过正常清理；租约到期通过单独的测试时钟注入步骤触发。
另有一个仍存活的旧 worker 在效果提交后等待屏障：新 worker 接管并对账后，
旧 worker 原进程继续提交回执；验证其 PID 未变、没有重复外呼或重复结算。

`HistoricalView` 接收只读旧 checkpoint 和当前 authority 两个连接。
它可以重建旧语义投影；读取旧正文须通过当前删除门控。动作只能在当前 authority 上运行，
因此旧检查点中的授权不能覆盖当前撤销。这是**历史视图恢复**，不是允许整库回滚。
不支持丢弃当前 authority 后直接用陈旧 SQLite 备份恢复生产执行。
电源断电、磁盘损坏、多主节点、网络分区和完整灾备合并不属于本轮验证。

## 5. 远端模拟器与独立期望

DurableRemote 将外部效果、响应丢失、查询可见性和本地意图认知分开。
持久指标包括 calls、queries、effects、key_reuses、duplicate_effects、unauthorized_effects。
测试批准集合由 fixture 单独登记；副作用身份由 logical_id 区分，模拟器不调用 ASG 的求值或 reducer。

阳性与阴性同时验证：prepare 后重启能继续；外部生效但不可见时 UNKNOWN 且保留 60；
可见后成功结算 60；同键调用两次只产生一个效果；无键重复及未批准效果计数可以实际增加。
这里的未经授权标记是模拟器对批准集合的核对，不是已完成真实服务认证的声明。

本轮没有实现成熟 SQL/outbox 强基线，也没有真实模型第二、三层结果。
正式收益比较仍需匹配保护机制的基线和开销测量，不从当前朴素对照推出优越性。

## 复跑

```text
cd asg_kernel
python -W error::ResourceWarning -m unittest discover -s tests -t . -v
```

原始输出、环境、源文件哈希与完整源码在独立核验压缩包的 verification/ 中。
本地结果不替代外部审查者复跑；本轮未推送 Git 远端。

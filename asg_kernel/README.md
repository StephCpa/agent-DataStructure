# ASG 内核实现（阶段 A + B + C）

依据《面向大模型智能体的数据结构详细设计说明书（ASG V1.0）》第 13 章实施顺序实现：
- **阶段 A（语义内核）**：记录与提交模型、串行提交协议、事务资源账本、确定性回放。
- **阶段 B（执行闭环）**：授权绑定（operation_digest）、outbox 与网关执行循环、
  UNKNOWN 对账、效果状态机聚合。
- **阶段 C（证据与上下文）**：支持分组与四态求值、失效传播与 epoch 闸门、
  ContextManifest 上下文投影、删除域与墓碑门控。

## 运行

```bash
cd asg_kernel
python -m unittest discover -s tests -t . -v
```

45 项测试全部通过（含跨模块契约回归）。阶段 A 验收：I1–I5 无反例、TC-INV-01 至 04 与 TC-03 通过
（另含 TC-INV-05）；阶段 B 验收：TC-04（I6）、TC-05（回执前崩溃，I7）、
TC-06（租约过期去重）及预算授权原子集成通过；阶段 C 验收：TC-01（失效传播
精确撤销）、TC-02（冲突 CONTESTED 不自动覆盖）、TC-07（删除复活阻止，I10）、
TC-08（来源谱系不作独立验证）、TC-09（必需约束超预算 E701）、TC-10（水位与
执行闸门，I8）及任务待重评传播通过。

## 模块与说明书章节映射

| 模块 | 职责 | 说明书章节 |
|------|------|-----------|
| `asg/registry.py` | 记录类型注册表（附录 A）、信封与载荷校验、引用角色分层 | 3.2 / 3.13 / 3.15 / 附录 A |
| `asg/write_service.py` | M1 写入服务：串行提交协议、幂等去重、批内拓扑检查、object_heads 条件更新、失效入队；observe / claim / revise_task / invalidate 便捷入口 | 5.1–5.4 / 7.1 |
| `asg/authorize.py` | M4 策略服务：operation_digest 规范化（JCS 思路 + 版本前缀）、Grant 签发/撤销/占用、prepare 与 dispatch 双检查点 | 6.4 / 9.1 / CP-2 |
| `asg/gateway.py` | M2 执行网关：prepare_effect 单事务组合（意图+预留+占用+outbox）、领取/重验证/先记录后外呼、回执持久化与聚合结算、对账（覆盖/可见性/最终性信任边界）、E603/E604 放弃路径 | 5.5 / 5.6 / 6.1 / 7.6 / 7.7 |
| `asg/ledger.py` | 预算账本与原子预留、结算（committed / released / uncertain）；reserve_in_txn/settle_in_txn 供网关同事务组合 | 7.5 / 6.3 |
| `asg/projection.py` | reducer v1 与确定性回放、在线投影比对 | 7.9 / 2.4 |
| `asg/storage.py` | SQLite 开发适配层：表结构、不可变触发器、约束 | 第 4 章 |
| `asg/errors.py` | 错误码目录 A/B 子集 | 第 10 章 |
| `asg/invalidation.py` | M3/M6 失效传播：反向依赖闭包（DERIVATION/CONTROL 边）、任务待重评、投影水位、read_gate 闸门（critical 拒绝 / exploratory 陈旧标注） | 7.3 |
| `asg/evidence.py` | M3 证据求值：OR-of-AND 支持电路、规则全局合取、四态（含 CONTESTED）、预算截断 truncated、来源谱系根去重 | 3.6 / 7.2 |
| `asg/context.py` | M3 上下文投影：绑定单元组装、必需约束预算检查（E701）、候选裁剪入 omitted 摘要、标签并集、清单落账与有效期 | 3.12 / 7.4 |
| `asg/purge.py` | M4 删除域：retire_or_purge（墓碑先行 + policy_epoch 推进 + 在线清理 + 派生再生计划）、读取门控（E802 复活阻止） | 7.8 / 9.4 |
| `asg/ids.py` | UUIDv7 标识、规范化 JSON 与 SHA-256 摘要 | 3.1 / 9.1 |

## 验收用例对照

| 用例 | 测试 | 断言要点 |
|------|------|---------|
| TC-INV-01 | `TestINV01Immutable` | record 表直接 UPDATE/DELETE 被触发器拒绝（I1） |
| TC-INV-02 | `TestINV02FutureRef` | 同批未来引用 E302、不存在引用 E301、同批前序引用合法（I2） |
| TC-INV-03 | `TestINV03RequestIdReuse` | 同键不同内容 E202；同键同内容返回原提交（I3） |
| TC-INV-04 | `TestINV04ConcurrentRevise` | 过期 expected_head → E201（附当前头）；任务基线不匹配 → E203（I4） |
| TC-INV-05 | `TestINV05ReplayDeterminism` | 回放投影与在线投影逐字段一致 |
| TC-03 | `TestTC03Budget` | 预算 100 双预留 60：第二笔 E501 且明细可解释；UNKNOWN 保留占用（I5） |
| TC-04 | `TestTC04GrantBinding` | 改参数/改目标不能借用旧授权（E402）；过期、撤销、次数用尽（E403）（I6） |
| TC-05 | `TestTC05CrashBeforeReceipt` | 外部成功后回执前崩溃：按键对账确认成功、无重复外部效果、结算 committed；查询不满足最终性维持 UNKNOWN 且保留占用（I7） |
| TC-06 | `TestTC06LeaseExpiry` | 记录后未发出即崩溃：对账判未生效后同键安全重试，远端仅一次调用；无契约时 E604 放弃自动重发并保留边界 |
| 集成 | `TestPreconditionGate` / `TestBudgetGrantIntegration` | E401 双闸门（prepare 与 dispatch）；预算不足整体回滚不留半准备状态、授权占用同步回退 |
| TC-01 | `TestTC01InvalidationPrecision` | 撤回 E1 仅使 (E1 AND E2) 路径退出，独立 E3 支持保留；再撤 E3 后转 NONE；主张未被误删 |
| TC-02 | `TestTC02ContestedNotOverwritten` | 较新低可信反证进入 CONTESTED 必须展示；较早支持路径与主张原文不被覆盖 |
| TC-07 | `TestTC07PurgeRevival` | 墓碑先行、policy_epoch 推进、blob 清理；read E802、回放旧快照不复活、同内容观察 E802、purged 证据退出求值、再生计划给出 |
| TC-08 | `TestTC08LineageNotIndependent` | 三条转载路径同一根来源，independent_root_count=1；根撤回后谱系整体失效 |
| 传播 | `TestPropagationTaskReeval` | 失效命中任务 depends_on 前提：任务进入 PENDING_REEVALUATION |
| TC-09 | `TestTC09ContextBudget` | 必需约束超预算 E701（附成本明细）；恰好装下时候选裁剪入 omitted、标签只随选中传播、清单落账 |
| TC-10 | `TestTC10StaleGate` | 无关传播未完成即阻塞 dispatch（E401）；水位推进后放行；探索读取返回显式陈旧标注 |

## 与说明书的偏差声明

1. **存储引擎**：生产目标为 PostgreSQL（说明书 4.2 节 DDL 为权威定义）。开发期以
   SQLite 落地同一 ADT：单写者用 `BEGIN IMMEDIATE` 串行化（PostgreSQL 下为可串行化
   事务），`SKIP LOCKED` 式多实例领取在 SQLite 下由单写者语义覆盖。表名 `commit`
   在 SQLite 中为保留字，DDL 以引号引用，语义不变。
2. **record_id 预分配**：说明书 3.2 节规定 record_id 由写入服务分配；实现允许调用方
   预分配合法 UUID 以支持同批前序引用（批内拓扑仍是 I2 检查对象），服务负责格式与
   唯一性校验。recorded_at 与 sequence 永远由服务分配。
3. **失效传播**：invalidate 的事务部分（标记、epoch 推进、入队）已实现；反向依赖
   闭包传播与读取闸门的完整实现属阶段 C（说明书 7.3 节异步部分）。阶段 B 的 E401
   闸门为前提直接命中检查。
4. **预留释放守卫**：释放判定由网关聚合规则构造保证（全部可能送达的尝试均确认未
   生效且无在途/未知尝试才落 CONFIRMED_NOT_APPLIED）；重试前按 outbox 的 asks 快照
   重新预留（asks_json 为簿记列，不在说明书 DDL 内，属实现细节）。
5. **租约丢失错误码**：令牌不匹配或租约过期复用 E201（冲突重试语义），未新增目录
   外错误码。
6. **PARTIAL 简化**：部分生效回执落 PARTIAL 状态并保留 uncertain 占用；逐项对账
   与逐项结算随真实复合操作连接器（阶段 D 工具模拟器）补充。
7. **放弃路径语义**：E603（键过期）/E604（无契约自动重发）先在事务内落 ABANDONED
   与 UNKNOWN（预留转 uncertain 保留）再抛错；无契约且存在 NO_RESPONSE 尝试时，
   对账以 E606 维持 UNKNOWN 并按退避延后，二者均不产生重复外部效果。
8. **task_state 状态集**：仅落 DRAFT→ACTIVE，PENDING_REEVALUATION 等转移随失效传播
   （阶段 C）启用。

### 阶段 C 补充偏差

9. **失效传播精度**：标记（invalidation_mark）承担任务重评与闸门保守阻塞；
   支持路径的精确退出由成员级资格判定（失效根 + 标记 + 墓碑）实现，
   Justification 自身被标记不等于其全部 OR 分支失效（TC-01 语义）。
10. **read_gate 保守规则**：存在任何未完成传播即阻塞带前提的关键动作
    （等待或闭包重验）；细粒度依赖戳属说明书 7.3 节的后续优化。
11. **上下文排序**：阶段 C 以记录序号代理效用（U 为待验证估计），
    分词器为 simple-v1 估算；真实检索器与排序器按 7.4 节契约接入。
12. **派生再生**：删除后给出再生计划（受影响派生记录清单），
    正文级再生随阶段 D 工具模拟器落地；差分隐私与备份物理清除不在此承诺。

## 里程碑状态（eb1381b 评审收敛后）

> 阶段 A/B/C 的主要接口与执行路径已实现，51 项测试通过（含跨模块契约回归、
> 故障交错轨迹，以及 60 条随机轨迹的状态机组合不变量检查）。
> **阶段 D 的契约前置已满足**：正式收益比较可以启动。随机轨迹覆盖的不变量为：
> 账本与预留行恒等且不越额、committed 单调不减、CONFIRMED_SUCCEEDED 不被离开、
> 离开 CONFIRMED_NOT_APPLIED 必带冲突标记、成功后不再产生外部调用、
> 授权占用不超过 use_limit。

本轮收敛（对应评审反例，回归用例见 `tests/test_contract_regression.py`）：

| 契约 | 落实位置 |
|------|---------|
| 写入身份由服务按 principal 回填；GRANT/ATTEMPT/RECEIPT 仅可信签发者可签发（E103）；回执聚合只采信可信签发 | write_service / gateway 聚合 |
| 对象级修订基线（supersedes 必须指向当前头，E203）与全局 expected_head 同时生效 | write_service |
| 父版本必须存在（E301）；引用不得跨工作空间（E303）；摘要由服务计算（E202）；未支持分支显式拒绝（E104） | write_service |
| QUERY_ONLY 的 UNKNOWN 走对账不重发；无契约自动重发 E604 放弃；已确认结果不被迟到查询错误覆盖 | gateway run_once |
| 撤销与次数占用分维度：exhausted 仍可撤销，未发送动作的 dispatch 被拒 | authorize / gateway |
| 按 Attempt 归约回执再合成意图状态；历史未知可被后续确定回执消解 | gateway 聚合 |
| 主张成员递归求值；规则/假设按论证独立合取；论证自身撤回整条路径退出 | evidence |
| 动作准入同时要求传播水位与前提语义（CLAIM 需正向支持成立） | gateway 闸门 |
| 清单绑定全部实际选中项；必需项缺失显式失败（E705） | context |
| 派生副本随源记录隔离；复活阻止按稳定内容身份（来源+片段），采集时间变化不能绕过 | purge / write_service |
| 失效传播后的任务待重评状态可由 reducer 重建，在线与回放一致 | projection |

第三轮补充（故障交错轨迹，见 `tests/test_fault_interleaving.py`，5 项）：

| 轨迹 | 验收结果 |
|------|---------|
| 租约过期后旧 worker 的成功回执迟到：新 worker 已按对账确认并结算 | 终态保持 CONFIRMED_SUCCEEDED，预算只结算一次，外部调用 1 次 |
| QUERY_ONLY 查询两次不可见后转为确定成功 | 前两轮 E606 维持 UNKNOWN 且不重发，第三次消解为确认成功并结算 |
| purge 后回放到删除前的历史序号 | 投影结构可重建，正文读取仍 E802，墓碑不被回放撤销 |
| 已确认成功后对账返回未生效 | E605 显式保留冲突，状态与已结算预算不被覆盖 |
| 先确认未生效并释放预留，随后迟到成功回执 | 不静默改为成功，committed 保持 0 |

第四轮（状态机组合，见 `tests/test_state_machine_combinations.py`）：固定种子生成
60 条轨迹、每条 12 个随机事件（run / 租约过期 / 重试到期 / 迟到回执），
连接器的调用结果与查询可见性同样由种子决定。每步之后检查六条不变量
（账本恒等与额度、committed 单调、成功态不离开、未生效态离开必带冲突标记、
成功后无新外部调用、授权占用不超限），全部通过。

## 阶段 D 第一层（已启动）

`eval/systems.py` 与 `tests/test_baseline_comparison.py` 用确定性记录和假工具，
把 ASG 与一个简单基线放在同一批故障场景上对比。基线 = 消息列表 + 事实就地覆盖
+ 操作名称级授权 + 崩溃后盲目重试 + 软删除 + 先读后写的预算判断，对应说明书
第 3 章候选表里“消息数组加摘要”与“成熟工作流加 SQL 版本和 outbox”的最小组合。

| 场景（说明书 13.3 节） | ASG | 基线 |
|---|---|---|
| 唯一证据撤回后依赖它的动作 | 拒绝执行 | 照常执行 |
| 较新低可信消息与较早证据冲突 | CONTESTED，两边保留 | 直接覆盖为 OVERWRITTEN |
| 审批后把 config-v7 改成 v8 | 拒绝（授权绑定内容） | 放行（只认操作名） |
| 外部成功后回执前崩溃再恢复 | 对账确认，外部调用 1 次 | 盲目重发，外部调用 2 次 |
| 两个 Agent 各读到余额 100 后各申请 60 | 第二笔拒绝 | 两笔都成功，超额 |
| 删除后读旧副本 / 重新采集 | 均 E802 | 旧副本仍可读，重新采集不受限 |
| 撤回后在线状态与回放对比 | 一致 | 无历史可回放 |

基线的失败是该方案的固有行为，用作对照而非待修复缺陷。这一层隔离了模型随机性；
第二层（自然语言抽取对照 oracle）与第三层（完整任务成功率与成本）尚未开始，
且组合测试仍未覆盖随机化的证据链与删除域交错。

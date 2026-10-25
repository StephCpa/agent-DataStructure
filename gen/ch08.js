// 第 8 章 并发控制与一致性 + 第 9 章 授权隐私删除 + 第 10 章 错误码
const { h1, h2, h3, body, bodies, lead, note, tbl, tblTitle, codeBlock } = require("./helpers");

function ch8() {
  const out = [];
  out.push(h1("8  并发控制与一致性设计"));

  out.push(h2("8.1  工作空间内有序提交"));
  out.push(body("MVP 为每个工作空间维护单一提交序列（7.1 节协议）：提交串行、序号无空洞、多工作空间并行。智能体读取稳定快照后在事务外思考与调用模型，提案提交时才做版本与资源检查——不在模型推理期间持有数据库锁。冲突处理策略是显式重新检查：expected_head 不匹配即拒绝并返回当前头，由调用方决定合并方式，不做静默最后写入获胜。"));

  out.push(h2("8.2  事务边界一览"));
  out.push(tblTitle("事务边界表"));
  out.push(tbl(
    ["操作", "事务内写集", "锁窗口", "事务外动作"],
    [
      ["commit_proposal", "commit、record、record_ref、object_heads、invalidation_queue", "短；仅行锁", "索引通知"],
      ["prepare_effect", "intent 记录、effect_state、reservation、grant_usage、effect_outbox", "短；可串行化", "无（摘要计算在事务前）"],
      ["execute_outbox 领取", "outbox 领取、attempt(STARTED)", "短；SKIP LOCKED", "连接器外呼"],
      ["execute_outbox 回执", "Observation、Receipt、effect_state、reservation 结算", "短", "无"],
      ["invalidate", "失效标记、epoch、队列", "短", "异步传播（7.3 节）"],
      ["purge", "tombstone、policy_epoch", "短", "在线清理、派生再生、备份门控"],
      ["project_context", "ContextManifest 写入", "短", "检索、排序、渲染、模型调用"],
      ["模型推理", "不开启事务", "无", "全部推理与工具预取"],
    ],
    [20, 40, 16, 24],
    { fontSize: 19 }
  ));

  out.push(h2("8.3  预算账本与写偏斜防范"));
  out.push(body("共享预算、库存与授权次数必须作为同一事务中的约束处理。研究设计报告给出的反例：预算 100，两个 Agent 各读到 100 并各建消耗 60 的动作，逐对象 CAS 全部成功也会超额。防范措施：预留一律走 7.5 节算法（账本行 FOR UPDATE 加余量判断），隔离级别取可串行化；快照隔离下的写偏斜由谓词锁兜底 [S19]。扩展到细粒度并发后，须显式处理多对象读集、谓词条件、写偏斜与序列化失败重试，重试不消耗 request_id 幂等配额。"));

  out.push(h2("8.4  分支 fork 与 merge 协议"));
  out.push(body("fork(snapshot) 建立分支根并共享不可变记录，新增记录仅对该分支可见；分支用于比较研究假设或候选计划。merge 生成新记录，说明采用哪些版本、哪些冲突仍未解决；语义合并不得由字符串拼接或最近时间戳代替。可合并性规则：只追加独立观察的集合可采用可合并集合思路；预算余额、审批次数与任务唯一完成等约束不能取并集，必须回到工作空间账本重新校验。外部已经发生的效果属于现实执行账本，不能通过回滚分支撤销；从分支发起实际效果仍须通过公共网关与公共资源预留。跨工作空间原子事务不属于 MVP 承诺。"));

  out.push(h2("8.5  不变量到机制的映射"));
  out.push(tblTitle("不变量实现映射（I1–I10）"));
  out.push(tbl(
    ["编号", "不变量", "实现机制", "验证方法"],
    [
      ["I1", "正常修订只追加，版本引用不悄悄改指向", "record 不可变触发器；可信写入服务", "TC-INV-01 随机 UPDATE/DELETE 注入"],
      ["I2", "不引用未来记录", "写入器验证 ID 与批内拓扑序（7.1 节）", "TC-INV-02 批内乱序与跨批未来引用"],
      ["I3", "同一 request_id 不接受不同内容", "UNIQUE(workspace, request_id) + 摘要比对", "TC-INV-03 一键两用并发"],
      ["I4", "旧版本写入不能静默覆盖当前版本", "object_heads 条件更新 + expected_head 检查", "TC-INV-04 并发 revise 竞争"],
      ["I5", "预留加已确认消耗不超额度", "可串行化事务 + 账本 CHECK（7.5 节）", "TC-05 双预留 60/预算 100"],
      ["I6", "实际动作与仍有效授权的具体内容匹配", "网关摘要匹配重验证（9.1、7.6 节）", "TC-04 审批后改参数"],
      ["I7", "超时不能直接推导未生效", "UNKNOWN 状态 + 对账信任边界（7.7 节）", "TC-06 回执前崩溃；TC-07 租约过期"],
      ["I8", "失效投影不能作为当前已验证前提", "epoch 闸门与闭包重验（7.3 节）", "TC-10 索引落后且前提被更正"],
      ["I9", "回放不重新调用外部效果", "确定性 reducer 与记录结果（7.9 节）", "TC-INV-05 回放比对投影"],
      ["I10", "当前禁止访问或删除不因旧快照失效", "墓碑门控全部读取与恢复路径（7.8 节）", "TC-08 删除后恢复旧快照"],
    ],
    [8, 30, 34, 28],
    { fontSize: 18 }
  ));
  out.push(body("可采用归纳证明维持不变量：初始空状态满足；每个允许操作在提交前检查前置条件并在一个事务内更新记录、资源与必需投影；因而每个提交后的状态继续满足结构性性质。证明范围不含：未知依赖、错误抽取、绕过网关、恶意数据库管理员、不遵守契约的外部服务；数据结构无法证明任意自然语言结论为真。"));
  return out;
}

function ch9() {
  const out = [];
  out.push(h1("9  授权、隐私与删除设计"));

  out.push(h2("9.1  operation_digest 规范化规则"));
  out.push(body("授权绑定具体操作内容，而非操作名称。规范化采用 RFC 8785 JSON Canonicalization Scheme 思路并固定 canonicalization_version；大整数、单位、时间格式与二进制文件摘要按下列附加规则编码 [S21]。摘要覆盖字段：operation、target_ids、executor_principal、connector_identity 与 connector_version、arguments（规范化后）、artifact_revision_ids 对应的确切内容摘要。规范化版本变化时旧摘要不重算，网关按 Grant 内记录的 canonicalization_version 选择校验器。"));
  out.push(tblTitle("规范化附加编码规则"));
  out.push(tbl(
    ["数据类别", "编码规则"],
    [
      ["大整数", "十进制字符串，禁用浮点与科学计数法"],
      ["时间", "RFC 3339 UTC，同一精度"],
      ["二进制与文件", "sha-256 摘要加字节长度，不内联内容"],
      ["计量与金额", "数值字符串加显式单位字段，单位参与摘要"],
      ["数组与对象键", "JCS 排序规则；数组保持语义序（顺序敏感时先标注）"],
      ["枚举", "注册表中的规范名，禁止同义词"],
    ],
    [24, 76]
  ));

  out.push(h2("9.2  权限检查点"));
  out.push(body("权限检查同时覆盖记录、关系边、搜索候选、缓存、全文提示与产物。检查点在代码中集中声明，禁止在业务分支内散落实现。"));
  out.push(tblTitle("权限检查点清单"));
  out.push(tbl(
    ["检查点", "位置", "检查内容"],
    [
      ["CP-1 提案准入", "M1 submit_proposal", "principal 可写该 kind 与 policy_domain；Grant/Receipt/回执类拒绝非可信签发者"],
      ["CP-2 授权签发与使用", "M4 + 网关", "签发者权限；委派收敛（子范围不超父）；使用时摘要匹配、目的、目标、有效期、次数"],
      ["CP-3 记录读取", "M3 query_graph", "Visibility 解析；对无权主体连证据存在性也按策略隐藏"],
      ["CP-4 检索候选过滤", "M3/M6 project_context", "候选先过访问与适用性过滤，排序只作用于已过滤集合"],
      ["CP-5 渲染与缓存", "M3 渲染缓存", "缓存键含 policy_epoch；渲染输入的复用不得跨越策略纪元"],
      ["CP-6 产物披露", "M3/M2 产物下载", "读取权限与向某目标披露数据的权限分开判定"],
      ["CP-7 名称解析", "M4 授权前", "人名、服务器名等解析为稳定身份并冻结；执行时不得悄悄重新指向另一对象 [S15]"],
    ],
    [20, 22, 58],
    { fontSize: 19 }
  ));
  out.push(body("来自网页的提示注入仍可能影响模型行为，但不会因此获得签发 Grant 或执行未经许可操作的能力——该边界依赖所有外部效果都经过网关（第 2.3 节信任边界）。"));

  out.push(h2("9.3  数据标签传播"));
  out.push(body("数据标签沿实际使用的输入传播：Observation 标签进入 ContextManifest 的 inherited_data_labels 并集，随模型输出与产物继续传播（Artifact.data_labels 只增不减）。读取敏感数据后生成的派生内容不能自动成为公开信息；披露或降级需要显式规则（策略服务的降级规则表，阶段 B 提供最小集：仅允许向更高或相等标签域流动）。"));

  out.push(h2("9.4  删除域与能力边界"));
  out.push(body("不可变指正常业务修订不能覆盖旧版本，不代表正文永远不可删除。删除域覆盖：正文、嵌入向量、摘要、完整渲染输入、附件、缓存与必要元数据。删除时序按 7.8 节执行，墓碑先于一切清理持久化。能力边界必须向产品与用户明示：仅做软删除、删除主副本或销毁一把密钥，不能自动证明备份、第三方副本及已经输出的信息全部消失；记录 ID、关系边、时间与哈希也可能泄露信息；普通权限隔离不等于差分隐私，若需要多租户统计或共享记忆的差分隐私保证，应另行定义邻接关系、发布机制与隐私预算，不能从本数据结构推出。"));

  out.push(h2("9.5  标识与摘要暴露控制"));
  out.push(body("ID 为租户范围内不可猜测标识（UUIDv7/ULID），枚举攻击不可行。哈希用于完整性与内容绑定，不公开暴露敏感低熵内容的摘要（低熵内容可在摘要上离线穷举）；对外查询接口返回的定位符一律经能力令牌包装，且能力令牌受 policy_epoch 失效约束。"));
  return out;
}

function ch10() {
  const out = [];
  out.push(h1("10  错误处理设计"));
  out.push(body("错误码全局唯一、分层分配。返回结构统一为 {error_code, message, retryable, details}：retryable 表示调用方可以原 request_id 安全重试（幂等键仍有效）；details 携带可解释信息（如预算余额与占用明细、当前 head、命中的墓碑）。下表为第一版全量目录。"));
  out.push(tblTitle("错误码目录"));
  out.push(tbl(
    ["码", "名称", "语义", "调用方处理"],
    [
      ["E101", "SCHEMA_INVALID", "载荷不符合注册表 schema", "修正后重提"],
      ["E102", "SCHEMA_VERSION", "schema 版本不可解释", "升级写入器"],
      ["E103", "KIND_NOT_ALLOWED", "principal 无权写入该 kind", "转人工或放弃"],
      ["E201", "HEAD_CONFLICT", "expected_head 不匹配", "读新快照重检查后重提"],
      ["E202", "REQUEST_ID_REUSE", "request_id 重复且摘要不同", "换新请求；一键两用属违规"],
      ["E203", "REVISE_BASELINE", "修订基线不匹配", "以返回的当前 head 重做"],
      ["E301", "REF_NOT_FOUND", "引用版本不存在", "修正引用"],
      ["E302", "FUTURE_REF", "引用未来记录（I2）", "修正批内拓扑"],
      ["E303", "REF_NOT_READABLE", "无引用目标读取权限", "放弃或申请权限"],
      ["E401", "STALE_PREREQ", "前提失效待重评（闸门）", "等待或闭包重验后重提"],
      ["E402", "GRANT_MISMATCH", "operation_digest 不匹配", "重新申请授权"],
      ["E403", "GRANT_INVALID", "授权过期、撤销或用尽", "重新申请"],
      ["E501", "BUDGET_EXCEEDED", "资源余量不足", "调整数量或等待释放"],
      ["E502", "RESERVATION_LOST", "预留已不存在或被结算", "重新 prepare"],
      ["E601", "CONNECTOR_CONTRACT_MISSING", "连接器未声明所需契约", "补契约或降级人工"],
      ["E602", "CLASSIFICATION_UNSUPPORTED", "回执分类无契约依据", "置 UNKNOWN 并对账"],
      ["E603", "IDEMPOTENCY_KEY_EXPIRED", "幂等键过期或参数改变", "不得声称重试安全"],
      ["E604", "RETRY_FORBIDDEN", "无幂等契约的自动重发", "暂停并转策略通道"],
      ["E605", "RECEIPT_CONFLICT", "迟到回执与已确认成功冲突", "保留冲突并对账"],
      ["E606", "QUERY_NOT_FINAL", "查询不满足覆盖/可见性/最终性", "维持 UNKNOWN"],
      ["E701", "CONTEXT_BUDGET", "必需约束超出 token 预算", "分阶段/提额/暂停动作"],
      ["E702", "CONTEXT_EXPIRED", "清单超期或策略纪元变更", "重新投影"],
      ["E801", "PURGE_SCOPE_UNRESOLVED", "删除范围无法解析", "显式列举范围"],
      ["E802", "REVIVAL_BLOCKED", "命中墓碑（复活阻止）", "放弃该路径"],
      ["E803", "PURGE_FORBIDDEN", "策略禁止删除该域", "转策略审批"],
    ],
    [9, 27, 38, 26],
    { fontSize: 18 }
  ));
  return out;
}

module.exports = { ch8, ch9, ch10 };

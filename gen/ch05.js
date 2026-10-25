// 第 5 章 操作接口详细设计
const { h1, h2, h3, body, bodies, lead, note, tbl, tblTitle, codeBlock, step } = require("./helpers");

function ch5() {
  const out = [];
  out.push(h1("5  操作接口详细设计"));
  out.push(body("本章细化研究设计报告第 10.1 节的九个关键操作契约。接口以进程内函数签名描述（传输层可为进程调用、HTTP 或消息，契约不变）；每个操作给出签名、前置条件、处理步骤、事务边界与错误码。错误码目录见第 10 章。"));

  out.push(h2("5.0  通用约定"));
  out.push(body("所有写操作的请求信封携带：workspace_id、branch_id、expected_head（或明确的读版本集合）、request_id、request_digest、principal 凭证。写入服务对 (workspace_id, request_id) 去重：重复请求若摘要一致，直接返回原提交结果；不一致则拒绝（E202），不允许一个幂等键用于两个请求。"));
  out.push(body("所有写操作遵循统一骨架：校验身份与权限，校验 schema 与引用，检查提交基线（expected_head），在一个数据库事务内完成记录写入、账本变更与失效队列入队，提交后返回新快照定位。模型调用、外部工具调用一律不在事务内。读取操作显式接收 as_of（sequence 截断）与 valid_at 参数，两个时间维度不得合并。"));

  // ── observe ──
  out.push(h2("5.1  observe：记录观察"));
  out.push(...codeBlock([
    "observe(ws, branch, expected_head, request, principal,",
    "        source, modality, span, content_blob, acquired_at,",
    "        acquisition_method) -> {record_id, commit_id, snapshot}",
  ], "observe 签名"));
  out.push(body("前置条件：来源身份与内容位置可记录（source 结构完整、span 与 modality 匹配）；content 已写入对象存储且摘要一致；principal 具备该采集通道的写入权限。"));
  out.push(step("1", "解析并规范化 source 与 span，按 3.4 节校验载荷；派生内容（转录、OCR）必须携带 derived_from 指回源观察。"));
  out.push(step("2", "创建 Observation 记录（candidate 语义：不直接升级为真值）。"));
  out.push(step("3", "若 span 命中已删除域（对账墓碑），拒绝写入（E802），避免删除内容经观察通道复活。"));
  out.push(step("4", "提交并触发索引工作者登记新证据（水位推进由 M6 异步完成）。"));
  out.push(body("事务边界：单事务（记录 + commit + 引用边 + blob_object 登记）。失败语义：对象存储已写而事务失败时，孤儿 blob 由清理任务按无引用回收。错误码：E101 schema 不符、E202 幂等冲突、E802 命中删除域。"));

  // ── assess ──
  out.push(h2("5.2  assess：主张评估"));
  out.push(...codeBlock([
    "assess(ws, branch, expected_head, request, principal,",
    "       claim: {proposition, entities, scope, units, valid_time},",
    "       justification?: {support_groups, refute_groups, rule_refs},",
    "       assessment?: {method, evaluator, verdict, calibration_ref})",
    "    -> {claim_id, justification_id?, assessment_id?, commit_id}",
  ], "assess 签名"));
  out.push(body("前置条件：引用的每个证据版本存在且 principal 有权读取；引用的规则记录版本存在；评估者对目标域有权限。"));
  out.push(step("1", "校验 Claim 载荷；实体引用必须指向实体注册记录，相同名称不触发自动合并。"));
  out.push(step("2", "校验 Justification：每个证据引用存在；规则引用存在；不检查证据真值（真值不是结构承诺）。"));
  out.push(step("3", "登记 CONTROL 层 supports / refutes 边，与记录同事务写入 record_ref。"));
  out.push(step("4", "若携带 Assessment，校验 evaluator 标识完整（模型评估必须带模型与版本）。"));
  out.push(step("5", "触发支持电路增量重算（第 7.2 节），目标主张进入待重算集合。"));
  out.push(body("事务边界：单事务。并发语义：多个 Agent 同时对同一主张建立 Justification 属正常并发，支持电路为可合并结构（OR 分支追加），无需互斥。错误码：E301 引用版本不存在、E302 引用未来记录、E303 无读取权限。"));

  // ── revise ──
  out.push(h2("5.3  revise：版本修订"));
  out.push(...codeBlock([
    "revise(ws, branch, expected_head, request, principal,",
    "       object_id, prior_revision_ids, new_payload) -> {record_id, commit_id}",
  ], "revise 签名"));
  out.push(body("前置条件：基线版本匹配——prior_revision_ids 必须等于该对象当前 head 的全部前序（乐观并发检查）；新载荷通过 schema 校验；typed_refs 拓扑合法。"));
  out.push(step("1", "检查 object_heads：head_record_id 的修订链与 prior_revision_ids 一致，否则返回 E203 并附当前 head，调用方重新检查后重提，不静默最后写入获胜。"));
  out.push(step("2", "写入新记录，登记 supersedes 边（DERIVATION，DAG）。"));
  out.push(step("3", "更新 object_heads（条件更新保证 I4）。"));
  out.push(step("4", "若修订使旧版本的某些主张不再被引用为前提，不做删除：旧版本仍可按 as_of 查询（当时知道什么）。"));
  out.push(body("事务边界：单事务（记录 + heads + 边）。错误码：E102 schema 版本不可解释、E203 基线不匹配、E301 引用不存在。"));

  // ── invalidate ──
  out.push(h2("5.4  invalidate：失效传播"));
  out.push(...codeBlock([
    "invalidate(ws, request, principal,",
    "           target: {record_id | object_id | payload_digest},",
    "           reason: {withdrawn | corrected | superseded | purged})",
    "    -> {validation_epoch, queue_item_ids}",
  ], "invalidate 签名"));
  out.push(body("前置条件：操作主体有权标记失效；目标可识别（记录、对象或载荷摘要之一）。"));
  out.push(step("1", "在同一事务内：写入失效标记记录，推进 workspace_epoch.validation_epoch，向 invalidation_queue 插入根项。"));
  out.push(step("2", "失效传播工作者（M3）按反向依赖闭包标记 invalidation_mark，将受影响的 Claim、TaskRevision 前提、ContextManifest 置入待重评（第 7.3 节算法）。"));
  out.push(step("3", "关键读取与动作准入发现投影落后（epoch 或 applied_sequence 落后）时执行闸门：等待、对依赖闭包重验，或返回需要重评（E401）。普通探索检索可返回显式标注陈旧的候选。"));
  out.push(body("语义边界：撤回支持不是新增反证；无法访问证据不等于从未有过证据；全局 epoch 只解决已收到的更新，外部世界尚未通知的变化依赖过期策略与主动复查，不属于本操作承诺。事务边界：标记与入队单事务，传播本身异步但带水位。"));

  // ── prepare_effect ──
  out.push(h2("5.5  prepare_effect：意图准备"));
  out.push(...codeBlock([
    "prepare_effect(ws, branch, expected_head, request, principal,",
    "               intent: ActionIntent 载荷, grant_id, reservations: [{resource_key, amount}])",
    "    -> {intent_id, outbox_id, commit_id, snapshot}",
  ], "prepare_effect 签名"));
  out.push(body("前置条件：意图冻结（operation_digest 已按 9.1 节计算）；授权合格（Grant 活跃、operation_digest 匹配、目标在 allowed_destinations 内、未过期未撤销未用尽）；资源可预留；关键证据与 validation_epoch 校验通过（precondition_refs 的闭包无失效标记）。完整伪代码由研究设计报告附录 A 给出，本节为其工程化步骤。"));
  out.push(step("1", "开启可串行化事务：验证请求身份与冻结摘要一致。"));
  out.push(step("2", "验证分支头与声明读版本（expected_head 及 precondition 声明的版本仍为当前认知基线）。"));
  out.push(step("3", "验证当前策略与签发者授权（策略服务检查点 CP-2，第 9.2 节）。"));
  out.push(step("4", "验证关键证据与验证纪元（闭包内无 invalidation_mark，workspace_epoch.validation_epoch 已被传播水位覆盖）。"));
  out.push(step("5", "原子预留预算并占用授权次数（第 7.5 节；次数按逻辑意图计量，占用于 grant_usage）。"));
  out.push(step("6", "追加 ActionIntent（PROPOSED 转 PREPARED）与 Reservation 记录。"));
  out.push(step("7", "同事务插入 effect_outbox 条目（PENDING），提交事务。"));
  out.push(body("失败语义：任一步骤失败整体回滚，不留半准备状态。错误码：E401 前提失效待重评、E402 授权不匹配、E403 授权过期或撤销、E501 资源不足、E201 基线冲突。"));

  // ── dispatch ──
  out.push(h2("5.6  dispatch / execute_outbox：网关执行"));
  out.push(...codeBlock([
    "execute_outbox(gateway_principal) -> 循环处理；单条流程：",
    "  claim(intent_id) -> revalidate(intent_id) -> record_attempt_start",
    "  -> call_connector(frozen_args, contract_key)",
    "  -> persist(observation, receipt) -> settle_or_retain(intent_id)",
  ], "execute_outbox 签名"));
  out.push(body("前置条件：网关以可信身份运行；outbox 条目可领取（state 为 PENDING，或 CLAIMED 且租约到期）。领取使用 SELECT FOR UPDATE SKIP LOCKED，多实例安全。"));
  out.push(step("1", "原子领取并重新验证当前条件：授权仍有效、前置版本未失效、删除策略未命中、预留仍持有（与准备时不同的检查点：此刻的最新性）。"));
  out.push(step("2", "在实际尝试前追加 attempt-started 记录（先记录、后调用；崩溃窗口因此可被识别为 UNKNOWN，这是保守但诚实的表示）。"));
  out.push(step("3", "以冻结参数与契约键调用连接器；调用在数据库事务外进行。"));
  out.push(step("4", "将响应保存为新 Observation，再写 Receipt（分类依据必须引用连接器契约条款）。"));
  out.push(step("5", "推进效果状态：任一尝试确认已生效则结算预留（committed）；全部可能送达的尝试均确认未生效且无在途或未知尝试则释放或按策略重试（released）；否则置 OUTCOME_UNKNOWN 并保留预留（uncertain）。"));
  out.push(body("崩溃恢复：租约到期只说明本地持有者失联，不证明远端未执行；到期条目重新领取时先对账（第 7.7 节），不盲目重发。已确认成功不得被迟到的失败或未知回执覆盖；重试回执冲突时保留冲突记录并对账（E605）。错误码：E601 连接器契约缺失、E602 分类依据不成立、E604 无幂等契约禁止自动重发。"));

  // ── resolve_effect ──
  out.push(h2("5.7  resolve_effect：对账与结算"));
  out.push(...codeBlock([
    "resolve_effect(ws, intent_id, principal,",
    "               basis: {receipt_id | query_result | manual_review})",
    "    -> {effect_state, reservation_settlement}",
  ], "resolve_effect 签名"));
  out.push(body("前置条件：存在可核查回执或对账依据。查询类依据仅在连接器契约保证查询覆盖范围、可见性与最终性时可采信为未生效证据；最终一致索引中暂时不存在仍判 UNKNOWN（E606）。"));
  out.push(step("1", "按 Attempt 聚合证据，逐项核对 PARTIAL 明细。"));
  out.push(step("2", "满足结算守卫（第 6.4 节）后推进效果状态并结算预留：成功转 committed；确认未生效转 released；未知转 uncertain 并保留。"));
  out.push(step("3", "一次尝试确认未生效不足以释放整个意图的预留；只有所有可能送达的尝试均可靠确认未生效且无在途或未知尝试，才判定意图未生效。"));

  // ── project_context ──
  out.push(h2("5.8  project_context：上下文投影"));
  out.push(...codeBlock([
    "project_context(ws, branch, task_revision_id?, query,",
    "                budget: {token_limit}, as_of?, valid_at?)",
    "    -> {context_manifest_id, rendered_input_ref, omitted_items_summary}",
  ], "project_context 签名"));
  out.push(body("前置条件：当前访问策略允许（policy_epoch 与水位闸门检查）。处理流程按第 7.4 节算法：候选检索（可带陈旧标注）、访问与适用性过滤、绑定单元组装、必需约束（M）优先装配、效用排序（U）填充预算、截断记录进 omitted_items_summary。必需约束超出预算时返回 E701，调用方必须分阶段读取、提高预算或暂停动作，不得静默丢弃关键约束。排序只能作用于已通过访问与适用性过滤的候选。LLM 的数值汇总、唯一性计数和状态迁移应调用确定性账本查询（第 7.4 节步骤 6），不让模型从片段心算。"));

  // ── retire_or_purge ──
  out.push(h2("5.9  retire_or_purge：撤回与删除"));
  out.push(...codeBlock([
    "retire_or_purge(ws, request, principal,",
    "               scope: {record_ids | object_ids | payload_digests | label_selector},",
    "               mode: {retire | purge})",
    "    -> {tombstone_ids, derived_regeneration_plan}",
  ], "retire_or_purge 签名"));
  out.push(body("前置条件：删除范围已解析（scope 可枚举）；principal 具备该 policy_domain 的删除权限。retire 仅标记失效（走 5.4 节）；purge 按第 7.8 节删除时序执行：墓碑先行、在线清理、派生内容再生、备份与恢复路径门控。保留独立证据支持某结论与保留含已删秘密的旧结论文本是两个问题：正文的删除域处理不因存在独立支持而豁免；历史回放在删除后可能返回输入不可恢复，这是明确的能力边界。错误码：E801 范围不可解析、E802 复活阻止、E803 策略禁止删除。"));

  // ── 辅助接口 ──
  out.push(h2("5.10  辅助接口"));
  out.push(tblTitle("辅助接口一览"));
  out.push(tbl(
    ["接口", "签名要点", "语义与限制"],
    [
      ["fork", "fork(ws, snapshot) -> branch_id", "建立分支根并共享不可变记录；新增记录仅本分支可见；分支用于比较方案，不复制现实"],
      ["merge", "merge(ws, branch_a, branch_b, resolution) -> commit_id", "生成新记录说明采用哪些版本、哪些冲突未解决；语义合并不得由字符串拼接或最近时间戳代替；预算、审批次数与任务唯一完成等约束不可取并集"],
      ["replay", "replay(ws, branch, upto_sequence) -> 重建投影", "确定性 reducer 按序应用；只输入已记录的模型输出、回执与审批结果，不重新调用模型或外部工具；载荷已删除时只恢复可保留状态"],
      ["reconcile", "reconcile(ws, intent_id) -> 对账结果", "按幂等键或外部作业 ID 查询远端；信任边界见 5.7 节"],
      ["query_graph", "query_graph(ws, as_of, valid_at, filters) -> 只读视图", "所有读取执行策略检查与存在性隐藏；对无权主体连证据存在性也按策略控制"],
      ["submit_proposal", "统一的提案入口", "智能体只能经此提交建议；类型检查与权限检查由代码执行，建议不等于签发"],
    ],
    [21, 31, 48],
    { fontSize: 19 }
  ));
  return out;
}

module.exports = { ch5 };

// 第 7 章 核心算法设计
const { h1, h2, h3, body, bodies, lead, note, tbl, tblTitle, codeBlock } = require("./helpers");

function ch7() {
  const out = [];
  out.push(h1("7  核心算法设计"));
  out.push(body("本章给出九个算法的伪代码。伪代码为接口语义描述，采用类 Pascal 风格，不含部署细节；连接器契约、事务隔离级别与错误处理按第 4、5、6 章执行。所有算法不得在持有数据库锁期间调用模型或外部工具。"));

  out.push(h2("7.1  串行提交协议"));
  out.push(...codeBlock([
    "procedure commit_proposal(ws, branch, proposal, expected_head):",
    "  begin serializable transaction",
    "    if exists(commit where ws, branch, request_id = proposal.request_id) then",
    "      if old.request_digest != proposal.request_digest:",
    "        reject E202  -- 一键两用",
    "      else return old_result  -- 幂等重放",
    "    head := read head_sequence(ws, branch) FOR UPDATE",
    "    if head != expected_head: reject E201  -- 冲突重新检查，非静默合并",
    "    validate schema(proposal.records)              -- 按 kind 注册表",
    "    validate refs: 目标已存在或同批前序；无未来引用（I2）",
    "    validate 批内拓扑序（DERIVATION 边构成 DAG）",
    "    seq := head + 1",
    "    insert commit(..., sequence=seq, reducer_version=RV)",
    "    insert records with recorded_at = service_clock()",
    "    insert record_ref edges",
    "    update object_heads conditionally (head_sequence < seq)  -- I4",
    "    enqueue invalidation roots if any RETIRE marks",
    "  commit",
    "  notify index workers; return snapshot(seq, epochs)",
  ], "串行提交协议"));
  out.push(body("同一工作空间内按序号串行，多工作空间并行。Agent 在确定快照上产生提案，提交时携带 expected_head；冲突时调用方在最新快照上重新检查依赖后重提。该选择简化一致性证明，代价是热点空间写入吞吐受限，扩展路径见第 11 章。"));

  out.push(h2("7.2  支持电路求值"));
  out.push(...codeBlock([
    "function evaluate_support(claim, as_of, policy) ->",
    "    {four_state, supporting_paths, refuting_paths, truncated}:",
    "  j := latest Justification targeting claim where j.sequence <= as_of",
    "  eligible(ref) :=",
    "    ref 可读(policy) and ref.sequence <= as_of",
    "    and 无 invalidation_mark 命中 ref 且 mark.epoch 未被传播水位覆盖",
    "    and ref 的 freshness/visibility/retention 检查通过",
    "  s := false; r := false; memo := {}",
    "  for group in j.support_groups:            -- OR of AND",
    "    if all(eligible(e) for e in group):",
    "      s := true; record path(group); break",
    "  for group in j.refute_groups:",
    "    if all(eligible(e) for e in group):",
    "      r := true; record path(group); break",
    "  propagate rule_refs: 规则失效 => 其所在组全部失效（递归 memo 共享子表达式）",
    "  budget := {max_paths, max_depth, max_recompute}",
    "  if budget exhausted: truncated := true  -- 返回需要进一步评估，不谎称完整",
    "  four_state := (s, r) 映射：NONE / SUPPORTED / REFUTED / CONTESTED",
  ], "支持电路求值"));
  out.push(body("求值继承真值维护与数据溯源思想，不是新的概率模型。最小支持集合的完整展开可能指数增长，因此电路采用共享子表达式的 AND/OR 结构并设置路径数、深度与重算预算；超限返回 truncated，调用方按需要进一步评估处理。CONTESTED 结果必须向读取方展示或进入裁决，不得被静默择一。"));

  out.push(h2("7.3  失效传播与读取闸门"));
  out.push(...codeBlock([
    "procedure invalidate(target, reason):            -- 事务部分",
    "  begin transaction",
    "    insert retire/purge mark record",
    "    epoch := workspace_epoch.validation_epoch + 1",
    "    update workspace_epoch set validation_epoch = epoch",
    "    insert invalidation_queue(root=target, epoch)",
    "  commit",
    "",
    "procedure propagate(worker):                     -- 异步部分",
    "  item := claim next PENDING queue item",
    "  closure := reverse_deps(root=item.root)         -- record_ref 反向闭包",
    "  for each hit in closure:",
    "    insert invalidation_mark(record=hit, epoch)",
    "    if hit 是 Claim: 标记支持电路待重算",
    "    if hit 是 TaskRevision 前提: task_state -> PENDING_REEVALUATION",
    "    if hit 是 ContextManifest: 标记清单过期",
    "  update index_watermark(applied_sequence, epoch)",
    "",
    "function read_gate(ws, needed_deps, purpose):    -- 关键读取/动作准入闸门",
    "  if watermark.epoch < ws.validation_epoch",
    "     or any mark 命中 needed_deps 未处理:",
    "    case purpose:",
    "      critical: wait or 对依赖闭包同步重验; 仍落后 -> E401 需要重评",
    "      exploratory: 返回候选并显式标注 stale",
  ], "失效传播与读取闸门"));
  out.push(body("异步索引必须带 applied_sequence 与 epoch；失效传播未完成时，旧索引命中不得当作当前已验证事实。后续优化可用细粒度依赖戳缩小无关更正造成的阻塞，第一版以闭包重验保证正确性。全局 epoch 只解决已收到的更新，不知道外部世界尚未通知的变化。"));

  out.push(h2("7.4  上下文投影"));
  out.push(...codeBlock([
    "procedure project_context(ws, task, query, budget):",
    "  check read_gate(purpose=critical if 高影响动作 else exploratory)",
    "  C := retrieve_candidates(query, watermark)      -- 可含 stale 标注",
    "  C := C filtered by 访问策略(CP-4) 与适用性(scope, valid_at, as_of)",
    "  bind := for c in C: 组装绑定单元(正文片段 + 状态 + 适用范围",
    "          + 关键来源指针 + 未解决冲突 + 否定条件 + 单位 + 时间 + 标签)",
    "  M := bind 中标记为必需约束/关键证据/风险提示的单元",
    "  cost(M) := tokenize(M, tokenizer_version)",
    "  if cost(M) > budget.token_limit:",
    "    return E701  -- 分阶段读取 / 提高预算 / 暂停动作，禁止静默裁剪",
    "  U := rank_by_utility(bind - M)                  -- 效用为待验证估计",
    "  fill remaining budget with U 顺序装入",
    "  rendered := render(M + selected, order, transforms)",
    "  persist ContextManifest(selected_items, rendered_digest,",
    "         omitted_summary, labels := 并集(inherited_data_labels))",
    "  数值汇总/计数/状态迁移: 调用确定性账本查询，不经模型心算",
    "  return manifest",
  ], "上下文投影"));
  out.push(body("摘要只是派生内容，继续指向原文；不得因摘要更短而丢掉否定条件、单位、时间或权限标签。优化问题只规定目标（预算内最大化效用并保全部必需约束），不宣称已有精确最优算法。"));

  out.push(h2("7.5  原子预留与结算"));
  out.push(...codeBlock([
    "procedure reserve(ws, intent_id, asks):  -- asks: [{resource, amount}]",
    "  begin serializable transaction",
    "    for a in asks sorted by resource_key:",
    "      l := SELECT * FROM budget_ledger WHERE resource_key = a.resource",
    "             FOR UPDATE",
    "      if l.limit - l.reserved - l.committed < a.amount:",
    "        reject E501(可解释余额与占用明细)",
    "      UPDATE budget_ledger SET reserved = reserved + a.amount",
    "      insert reservation(state=reserved, intent_id, amount)",
    "    consume grant_usage(grant, intent_id)  -- 次数按逻辑意图计量",
    "  commit",
    "",
    "procedure settle(reservation, outcome):",
    "  begin transaction",
    "    if outcome = SUCCEEDED: reserved->committed",
    "    elif outcome = NOT_APPLIED and 释放守卫成立: reserved->released",
    "    elif outcome = UNKNOWN: reserved->uncertain  -- 保留占用",
    "  commit",
  ], "原子预留与结算"));
  out.push(body("预算 100、两个 Agent 各读到 100 并各建消耗 60 的动作时，逐对象 CAS 会全部成功并超额；正确做法是本算法的账本行锁加余量判断：第一个成功后余额 40，第二个被拒绝并可解释返回。稳定快照不等于可串行化 [S19]。"));

  out.push(h2("7.6  outbox 执行循环"));
  out.push(...codeBlock([
    "procedure execute_outbox(gateway):",
    "  loop:",
    "    item := SELECT .. FROM effect_outbox",
    "            WHERE state='PENDING' OR (state='CLAIMED' AND lease_expired)",
    "            ORDER BY next_retry_at FOR UPDATE SKIP LOCKED LIMIT 1",
    "    if none: sleep(backoff); continue",
    "    claim(item, lease=now+T)                        -- 原子领取",
    "    begin transaction",
    "      revalidate: grant 活跃且摘要匹配且目标未变;",
    "        precondition 闭包无失效标记; 删除策略未命中; 预留仍持有",
    "      if revalidate 失败: state -> ABANDONED; 释放; continue",
    "      append attempt(STARTED, dispatch_digest)      -- 先记录后外呼",
    "    commit",
    "    resp := call_connector(frozen_args, contract_key)   -- 事务外",
    "    begin transaction",
    "      obs := persist Observation(resp)",
    "      receipt := persist Receipt(分类依据=契约条款, evidence=obs)",
    "      aggregate: 按 6.1 节规则推进 effect_state",
    "      settle_or_retain reservation",
    "      state -> DONE or 重排 next_retry_at",
    "    commit",
  ], "outbox 执行循环"));
  out.push(body("本地可证明的边界是网关准入的线性化时刻：外部请求一旦发出，后来的撤销无法收回；远端资源在检查与使用之间变化，不能靠本地事务消除。强条件更新需远端支持 If-Match、版本前提或同等机制 [S20]。"));

  out.push(h2("7.7  幂等与对账"));
  out.push(...codeBlock([
    "procedure reconcile(ws, intent_id):",
    "  c := connector_contract(intent.connector)",
    "  if c.mode = KEYED and key 未过期 and 参数摘要一致:",
    "    r := query(idempotency_key)  -- 远端按键返回原结果",
    "  elif c.supports_job_query and attempt.external_job_id 存在:",
    "    r := query(external_job_id)",
    "  else: 保持 UNKNOWN; 通知人工或策略通道",
    "  采信条件: 契约保证查询覆盖范围、可见性与最终性;",
    "    最终一致索引中暂时不存在 => 仍为 UNKNOWN（E606）",
    "  if r 冲突已有 CONFIRMED_SUCCEEDED: 保留冲突并记录（E605），不覆盖",
    "  resolve_effect(intent_id, basis=r)",
  ], "对账算法"));
  out.push(body("既不能查询又不幂等的操作，默认暂停自动重发（E604）。查询未找到只有在连接器保证查询覆盖范围、可见性与最终性时，才能作为未生效依据。"));

  out.push(h2("7.8  删除传播与复活阻止"));
  out.push(...codeBlock([
    "procedure purge(scope):",
    "  resolve scope -> {objects, records, payload_digests, labels}",
    "  begin transaction            -- 墓碑先行",
    "    insert tombstone(scope, policy_epoch = next)",
    "    advance workspace_epoch.policy_epoch",
    "    invalidate(scope, reason=purged)   -- 走 7.3 节传播",
    "  commit",
    "  cleanup_online: blob_object -> pending_purge -> purged;",
    "    清理索引条目、缓存、渲染输入副本",
    "  regenerate_derived: 仍获准使用的证据重新生成干净派生内容;",
    "    不得只给原文加一句来源已删除",
    "  gate_recovery: 回放与备份恢复路径读取前检查墓碑;",
    "    受禁内容不因旧快照或备份重新可见（I10）",
    "  边界: 软删除/删除主副本/销毁一把密钥不足以证明备份、第三方副本、",
    "    已输出信息全部消失; 记录 ID、关系边、时间与哈希也可能泄露信息",
  ], "删除传播时序"));

  out.push(h2("7.9  确定性回放恢复"));
  out.push(...codeBlock([
    "procedure replay(ws, branch, upto_sequence):",
    "  state := empty_projection(reducer_version 链)",
    "  for c in commits(ws, branch) where sequence <= upto_sequence:",
    "    inputs := records(c) 的已记录模型输出、工具回执与审批结果",
    "    state := apply(reducer(c.reducer_version), state, select_b(inputs))",
    "    -- 不调用模型; 不调用外部工具; 同种子也不承诺云模型输出相同",
    "  for d in 已删除载荷(closed by tombstone):",
    "    保留结构与摘要, 正文标记不可恢复   -- 只能恢复可保留状态",
    "  rebuild derived indexes from state (带水位回填)",
    "  return state",
  ], "确定性回放"));
  out.push(body("重新向模型提问属于新运行，不属于回放。回放的正确性依赖 reducer 固定版本与输入完整性；这也是 record 不可变触发器存在的理由。"));
  return out;
}

module.exports = { ch7 };

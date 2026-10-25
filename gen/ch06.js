// 第 6 章 状态机详细设计
const { h1, h2, h3, body, bodies, lead, note, tbl, tblTitle, codeBlock } = require("./helpers");

function ch6() {
  const out = [];
  out.push(h1("6  状态机详细设计"));
  out.push(body("任务与行动需要不同状态机：任务状态描述工作进展，效果状态描述外部世界的可见结果，预留状态描述资源占用，授权状态描述许可的有效性。四类状态机相互独立又通过事件关联：失效传播使任务进入待重评，回执推进效果状态，效果状态驱动预留结算，网关在每次领取时重验证授权。"));

  out.push(h2("6.1  效果状态机 EffectState"));
  out.push(body("七态外加 PARTIAL 是外部效果的最小状态集：通用 FAILED 不足以处理部分成功与错误码不明确的服务。转移表如下，守卫不满足时转移被拒绝并记录 unresolved_reason。"));
  out.push(tblTitle("EffectState 转移表"));
  out.push(tbl(
    ["当前状态", "事件", "守卫条件", "下一状态", "动作"],
    [
      ["PROPOSED", "prepare_effect 提交", "授权、资源、前提、纪元全部通过（5.5 节）", "PREPARED", "写 intent 与 outbox，原子预留"],
      ["PREPARED", "网关领取并重验证通过", "授权仍有效、前提未失效、预留持有", "IN_FLIGHT", "先写 attempt-started 再外呼"],
      ["PREPARED", "撤销请求", "尚未发出任何尝试（outbox 未领取）", "CANCELLED_BEFORE_DISPATCH", "释放预留，回收授权占用"],
      ["IN_FLIGHT", "收到回执：确认生效", "分类依据成立（契约条款）", "CONFIRMED_SUCCEEDED", "预留转 committed"],
      ["IN_FLIGHT", "收到回执：确认未生效", "该尝试可靠确认；且无其他在途/未知尝试", "CONFIRMED_NOT_APPLIED", "按策略重试（回 PREPARED）或释放预留"],
      ["IN_FLIGHT", "超时且无法判定", "无回执或回执不可采信", "OUTCOME_UNKNOWN", "预留转 uncertain 并保留，进入对账"],
      ["OUTCOME_UNKNOWN", "对账确认生效", "查询契约满足覆盖/可见性/最终性，或收到迟到回执", "CONFIRMED_SUCCEEDED", "结算 committed"],
      ["OUTCOME_UNKNOWN", "对账确认未生效", "同上且所有可能送达尝试均确认未生效", "CONFIRMED_NOT_APPLIED", "释放或按策略重试"],
      ["IN_FLIGHT / OUTCOME_UNKNOWN", "部分生效回执", "复合操作已拆分、逐项可核查", "PARTIAL", "逐项对账，逐项结算"],
      ["CONFIRMED_SUCCEEDED", "迟到回执冲突", "任何已确认成功不得被覆盖", "保持并记录冲突", "保留冲突证据，转人工或规则对账（E605）"],
    ],
    [17, 19, 28, 17, 19],
    { fontSize: 18 }
  ));
  out.push(note("回执先属于具体 Attempt，再按 Intent 聚合：一次尝试未生效不释放整个意图的预留；已确认成功不被迟到回执覆盖。"));

  out.push(h2("6.2  尝试状态机与幂等契约"));
  out.push(tblTitle("AttemptStatus 转移表"));
  out.push(tbl(
    ["当前状态", "事件", "下一状态", "说明"],
    [
      ["（新建）", "网关记录后外呼", "STARTED", "先落库再调用，崩溃窗口可识别"],
      ["STARTED", "响应持久化为 Observation + Receipt", "RESPONSE_PERSISTED", "分类依据随回执保存"],
      ["STARTED", "超时或连接失败", "NO_RESPONSE", "尝试级终态；意图层是否 UNKNOWN 由聚合规则决定"],
    ],
    [18, 34, 22, 26]
  ));
  out.push(tblTitle("幂等契约 IdempotencyContract 结构"));
  out.push(tbl(
    ["字段", "取值与规则"],
    [
      ["mode", "KEYED：远端支持幂等键，重试沿用同一逻辑意图、同一规范化参数与同一键；QUERY_ONLY：远端可查询回执但对账受信任边界约束；NONE：无幂等承诺，默认暂停自动重发（E604）"],
      ["key / key_scope / key_expiry", "键值、其服务范围（命名空间）与有效期；键已过期、参数改变或服务无承诺时，不得继续声称重试安全（E603）"],
      ["retry_policy", "重试次数上限、退避策略、是否允许跨网关实例重试"],
      ["conditional_update", "远端是否支持 If-Match、版本前提或事务；强条件更新依赖远端能力，HTTP 幂等语义不可外推到任意工具 POST [S20]"],
    ],
    [26, 74]
  ));

  out.push(h2("6.3  资源预留状态机 ReservationState"));
  out.push(tblTitle("ReservationState 转移表"));
  out.push(tbl(
    ["当前状态", "事件", "守卫条件", "下一状态"],
    [
      ["（新建）", "prepare_effect 原子预留", "账本余量充足（I5）", "reserved"],
      ["reserved", "意图进入 IN_FLIGHT 后无法判定", "效果状态转 OUTCOME_UNKNOWN", "uncertain"],
      ["uncertain", "对账确认生效", "结算依据可核查", "committed"],
      ["uncertain", "对账确认未生效", "全部可能送达尝试均确认未生效且无在途/未知尝试", "released"],
      ["reserved", "意图确认未生效或取消", "同上释放守卫；UNKNOWN 期间禁止释放", "released"],
      ["reserved", "意图确认成功", "验收前即可结算", "committed"],
    ],
    [16, 30, 36, 18],
    { fontSize: 19 }
  ));
  out.push(body("UNKNOWN 必须保留资源和授权次数的占用，不能随租约到期释放：否则第一次请求实际已成功却仍未知时，第二个动作会重复消耗同一预算。租约到期只说明本地持有者失联，不证明远端未执行 [S12]。"));

  out.push(h2("6.4  授权生命周期 GrantStatus"));
  out.push(tblTitle("GrantStatus 转移表"));
  out.push(tbl(
    ["当前状态", "事件", "守卫条件", "下一状态"],
    [
      ["（签发）", "允许的签发者创建 Grant", "operation_digest、scope、expiry、use_limit 完整；委派收敛于父授权", "active"],
      ["active", "dispatch 重验证", "now 小于 expiry 且未撤销且 uses_consumed 小于 use_limit", "保持 active"],
      ["active", "时钟越过 expiry", "由网关重验证判定", "expired"],
      ["active", "撤销调用 revocation_handle", "撤销立即影响后续 dispatch；已发出的外部请求无法收回", "revoked"],
      ["active", "占用次数达到上限", "按逻辑意图计量；重试不新增占用", "exhausted"],
    ],
    [14, 26, 42, 18],
    { fontSize: 19 }
  ));

  out.push(h2("6.5  任务状态机 TaskStatus"));
  out.push(tblTitle("TaskStatus 转移表"));
  out.push(tbl(
    ["当前状态", "事件", "守卫条件", "下一状态"],
    [
      ["DRAFT", "任务修订提交且验收谓词完整", "acceptance_refs 非空", "ACTIVE"],
      ["ACTIVE", "失效传播命中任务前提或前置任务", "invalidation_mark 命中 depends_on 或 precondition 闭包", "PENDING_REEVALUATION"],
      ["PENDING_REEVALUATION", "重评完成且前提恢复", "受影响前提重验通过或以新修订显式接受变化", "ACTIVE"],
      ["ACTIVE", "验收判定", "全部验收谓词通过（收到成功回执不等于验收通过）", "COMPLETED"],
      ["ACTIVE / PENDING_REEVALUATION", "取消", "未完成的外部意图按各自状态机处理，不因取消而虚构结果", "CANCELLED"],
    ],
    [22, 28, 36, 14],
    { fontSize: 19 }
  ));
  return out;
}

module.exports = { ch6 };

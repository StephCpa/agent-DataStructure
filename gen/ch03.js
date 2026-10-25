// 第 3 章 数据结构详细设计
const { h1, h2, h3, body, bodies, lead, note, tbl, tblTitle, codeBlock } = require("./helpers");

function ch3() {
  const out = [];
  out.push(h1("3  数据结构详细设计"));
  out.push(body("本章将研究设计报告第 4、5、6、8 章给出的记录草图细化为字段级规格。所有记录共享一个通用信封，载荷按 kind 组成类型联合；记录一经提交即不可变，正常修订以新记录加 supersedes 边表达。载荷的权威副本存于对象存储，库内保存规范化 JSONB 副本以供校验与索引，两者以内容摘要绑定。"));

  out.push(h2("3.1  标识、摘要与基本类型"));
  out.push(tblTitle("基本类型定义"));
  out.push(tbl(
    ["类型", "表示", "规则"],
    [
      ["RecordId / ObjectId / WorkspaceId / BranchId", "UUIDv7 字符串（36 字符）", "租户范围内不可猜测；UUIDv7 的时间有序性利于索引局部性；不得由模型生成后直接采信，一律由写入服务分配或校验格式与唯一性"],
      ["Digest", "SHA-256 十六进制，64 字符", "用于内容绑定与完整性校验；不对外公开敏感低熵内容的摘要（第 9.5 节）"],
      ["Timestamp", "RFC 3339 字符串，一律 UTC", "系统时间（recorded_at）由可信写入服务分配；valid_time 与 event_time 可为区间或未知"],
      ["Principal", "结构体 {kind, id}", "kind 取 service、agent、human、connector；id 为稳定身份，禁止用显示名称充当"],
      ["PolicyDomain", "枚举字符串", "记录归属的策略域，删除与访问策略按域解析"],
      ["Span", "结构体（按模态取一种）", "text:{offset,length}；pdf:{page,char_range}；image:{region_polygon}；av:{start_ms,end_ms}；table:{version,row_keys}"],
      ["Money / Quantity", "十进制字符串 + 显式单位字段", "禁止浮点数表示金额与计量；单位为绑定单元组成部分"],
      ["JsonBlob", "对象存储引用 {blob_id, digest, media_type, size}", "超过 4 KB 的载荷一律外置；库内 JSONB 仅保留规范化副本"],
    ],
    [30, 26, 44]
  ));
  out.push(note("UUIDv7 之外允许 ULID，但同一部署内必须统一，且两种均满足不可猜测性要求时优先 UUIDv7（生态成熟度）。"));

  out.push(h2("3.2  记录通用信封 Record"));
  out.push(lead("每个记录（无论何种 kind）都携带下列信封字段。字段名与语义与研究设计报告第 4 章一致，此处补充类型与约束。"));
  out.push(tblTitle("Record 通用信封字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["record_id", "RecordId", "全局唯一，由写入服务分配", "标识一个确定版本"],
      ["object_id", "ObjectId", "非空", "逻辑对象标识，跨版本稳定；实体合并后旧 object_id 仍保留"],
      ["workspace_id", "WorkspaceId", "非空", "归属工作空间"],
      ["branch_id", "BranchId", "非空，默认 main", "归属分支；分支记录对其他分支不可见"],
      ["kind", "枚举", "见附录 A 注册表", "记录类型的判别字段"],
      ["schema_version", "整数", "大于等于 1", "该 kind 载荷的 schema 版本；演进规则见 3.15 节"],
      ["authenticated_writer", "Principal", "由认证通道确定", "已认证的实际写入者，不采信载荷内自报作者"],
      ["asserted_source", "Principal 或 null", "载荷声明", "材料声称来自谁，与 authenticated_writer 分开保存"],
      ["recorded_at", "Timestamp", "由写入服务分配", "系统何时知道；与 valid_time、event_time 分离（3.9 节）"],
      ["payload_ref", "JsonBlob", "digest 必须与库内副本一致", "载荷权威副本引用"],
      ["prior_revision_ids", "RecordId 数组", "可空", "本记录修订了哪些旧版本，构成修订链"],
      ["typed_refs", "引用数组", "见 3.13 节", "每项 {role, layer, target_record_id, target_object_id, note}"],
      ["policy_domain", "PolicyDomain", "非空", "删除与访问策略解析依据"],
      ["commit_id", "CommitId", "由写入服务回填", "所属提交，入 commit 后不可变"],
    ],
    [22, 17, 24, 37],
    { fontSize: 19 }
  ));

  out.push(h2("3.3  提交 Commit 与快照 Snapshot"));
  out.push(tblTitle("Commit 字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["commit_id", "CommitId", "全局唯一", "提交标识"],
      ["workspace_id / branch_id", "ID", "联合唯一序号", "与 sequence 构成 (workspace, branch, sequence) 唯一键"],
      ["sequence", "整数", "分支内单调递增、无空洞", "由写入服务在串行协议下分配（第 8.1 节）"],
      ["previous_commit_id", "CommitId", "必须等于提交时分支头", "冲突时不静默合并，返回 E201 要求重检查"],
      ["record_ids", "RecordId 数组", "非空，批内拓扑合法", "批内允许引用同批前序记录，但不得引用未来记录（不变量 I2）"],
      ["reducer_version", "整数", "非空", "本提交引入的 reducer 版本；回放按版本切换转换逻辑"],
      ["request_id", "字符串", "(workspace, request_id) 唯一", "幂等键；重复且摘要相同返回原结果，不同则拒绝（I3）"],
      ["request_digest", "Digest", "非空", "请求内容摘要，配合 request_id 防止一键两用"],
      ["committed_at", "Timestamp", "写入服务分配", "提交时间"],
    ],
    [24, 15, 26, 35],
    { fontSize: 19 }
  ));
  out.push(tblTitle("Snapshot 字段"));
  out.push(tbl(
    ["字段", "类型", "说明"],
    [
      ["workspace_id / branch_id", "ID", "快照归属"],
      ["branch_head", "{sequence, commit_id}", "分支头位置"],
      ["reducer_version", "整数", "重建投影所需的 reducer 版本链"],
      ["validation_epoch", "整数", "失效传播纪元；读取闸门比较对象"],
      ["policy_epoch", "整数", "访问策略纪元；删除与权限变更推进"],
    ],
    [30, 26, 44]
  ));
  out.push(body("快照不是数据的另一份拷贝，而是定位符：恢复或分支时按 branch_head 截断提交历史，经 reducer 重建投影。大载荷与不可变记录在分支与快照间共享引用，不复制。"));

  out.push(h2("3.4  证据类记录 Observation"));
  out.push(body("Observation 只断言某来源在某时刻返回了某段内容，不直接升级为真值。多模态证据以 Span 精确定位；机器转录与 OCR 不是对原始观察的改写，而是一条新的派生记录，通过 extracted_from 边指回源观察。"));
  out.push(tblTitle("Observation 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["source", "结构体", "非空", "{connector_identity, fetched_via, url_or_handle, content_version}；content_version 为 ETag、版本号或内容摘要"],
      ["modality", "枚举", "text/pdf/image/audio/video/table/api_response", "证据模态，决定 span 结构"],
      ["span", "Span", "与 modality 匹配", "原始内容的精确位置；引用必须落到具体片段，不得只给整篇"],
      ["acquired_at", "Timestamp", "事件时间语义", "采集时刻（被讨论事件何时发生维度之一）"],
      ["acquisition_method", "结构体", "非空", "{tool, tool_version, parameters_digest}，采集方式本身可追溯"],
      ["content", "JsonBlob 或内联短文本", "摘要必填", "原始内容或其片段；超过阈值外置对象存储"],
      ["media_type", "字符串", "MIME 类型", "内容类型"],
      ["derived_from", "RecordId 或 null", "指向另一 Observation", "转录、OCR、格式转换等派生来源；原始采集为 null"],
      ["declared_freshness", "结构体", "可选", "来源自述的时效信息，仅作参考，不等于系统的 freshness 状态"],
    ],
    [22, 20, 22, 36],
    { fontSize: 19 }
  ));
  out.push(...codeBlock([
    "// Observation 载荷示例（PDF 引用页与片段）",
    "{",
    "  \"source\": { \"connector_identity\": \"web.fetch@v2\", \"url_or_handle\": \"https://example.org/ds-spec\",",
    "             \"content_version\": \"etag:9f2c1a\" },",
    "  \"modality\": \"pdf\",",
    "  \"span\": { \"page\": 12, \"char_range\": [880, 1264] },",
    "  \"acquired_at\": \"2026-10-20T09:31:07Z\",",
    "  \"acquisition_method\": { \"tool\": \"pdf.extractor\", \"tool_version\": \"1.7\", \"parameters_digest\": \"sha256:...\" },",
    "  \"content\": { \"blob_id\": \"b01\", \"digest\": \"sha256:...\", \"media_type\": \"application/pdf\", \"size\": 182044 },",
    "  \"derived_from\": null",
    "}",
  ], "Observation 载荷示例"));

  out.push(h2("3.5  主张类记录 Claim"));
  out.push(body("Claim 保存结构化命题或受约束的自然语言文本，并显式携带适用范围、单位与时间限定。相同姓名不得自动合并为同一实体；实体合并是独立的、可撤销的记录（kind 为 ENTITY_MERGE，见附录 A）。"));
  out.push(tblTitle("Claim 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["proposition", "结构化三元组或 constrained_text", "二选一", "结构化时为 {subject_entity, predicate, object, qualifiers}；文本时必须携带约束标签"],
      ["entity_ids", "ObjectId 数组", "可为空", "命题涉及的实体；引用实体注册记录而非名称字符串"],
      ["scope", "结构体", "非空", "适用范围：数据集、项目、租户等边界条件"],
      ["units", "字符串或 null", "数量命题必填", "计量单位，属于绑定单元，投影时不可剥离"],
      ["valid_time", "时间结构", "可为区间或未知", "命题何时适用（负责人自周一起变更的周一维度）"],
      ["event_time", "时间结构", "可选", "被讨论事件何时发生（活动计划周五举行的周五维度）"],
      ["proposer", "Principal", "非空", "提出者；与 authenticated_writer 可以不同"],
      ["candidate_status", "枚举", "candidate 或 asserted", "初始为候选；是否采信由评估与支持关系决定，不在此字段表达"],
      ["negation_conditions", "数组", "可空", "命题不成立的边界条件，属于绑定单元（第 7.4 节）"],
    ],
    [22, 22, 20, 36],
    { fontSize: 19 }
  ));

  out.push(h2("3.6  支持关系记录 Justification"));
  out.push(body("普通 supports 列表无法区分必须同时依赖与独立充分两种情形，Justification 保存 AND 组与 OR 分支组成的支持电路：组内证据为合取（缺一即失效），组间为析取（任一组合格即存在支持）。支持与反证分别建组；推理规则与验证器本身也是记录，被规则边引用并携带版本。"));
  out.push(tblTitle("Justification 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["target_claim_id", "RecordId", "指向 Claim", "被论证的主张"],
      ["support_groups", "数组（OR of AND）", "每组为 RecordId 数组", "每个 AND 组内证据全部合格时该组合格；任一组合格即存在合格支持"],
      ["refute_groups", "数组（OR of AND）", "同上", "反证分支，与支持分支独立求值"],
      ["rule_refs", "RecordId 数组", "可空", "推理规则或验证器记录及其版本；规则失效等同于全部依赖其的组失效"],
      ["assumption_refs", "RecordId 数组", "可空", "显式假设；循环主张不能凭互引用产生支持，必须有可用观察或显式假设作根"],
      ["justification_note", "字符串", "可空", "论证说明"],
    ],
    [24, 22, 20, 34],
    { fontSize: 19 }
  ));
  out.push(...codeBlock([
    "// 支持电路示例：C1 依赖 (E1 AND E2) OR E3；反证分支 E4",
    "Justification {",
    "  target_claim_id: \"rec_claim_c1\",",
    "  support_groups: [ [\"rec_obs_e1\", \"rec_obs_e2\"],   // AND 组 1",
    "                    [\"rec_obs_e3\"] ],                // AND 组 2（独立充分）",
    "  refute_groups:  [ [\"rec_obs_e4\"] ],",
    "  rule_refs:      [\"rec_rule_validator_v3\"],",
    "  assumption_refs: []",
    "}",
    "// E1 被撤回时组 1 失效；E3 仍合格则 C1 仍有支持（研究报告 5.2 节语义）",
  ], "Justification 支持电路示例"));
  out.push(body("求值语义：Eligible 仅表示在指定查询范围、as_of 序号与策略下，证据可被使用且相关验证尚有效，不表示其为真。四态结果（支持位、反证位）由第 7.2 节算法计算；数据结构只维护已声明的依赖，未记录的隐含前提造成的漏失效不属于结构承诺。"));

  out.push(h2("3.7  评估记录 Assessment"));
  out.push(tblTitle("Assessment 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["target_record_id", "RecordId", "指向 Claim 或 Justification", "被评估对象（固定版本）"],
      ["method", "字符串", "非空", "评估方法标识（人工核验、模型评估、程序校验器）"],
      ["evaluator", "结构体", "非空", "{principal, model_id?, model_version?}；模型评估必须携带模型标识与版本"],
      ["calibration_ref", "RecordId 或 null", "可选", "校准集版本引用；统计量必须可追溯其校准依据"],
      ["verdict", "枚举", "verified / contradicted / uncertain / withdrawn", "评估结论，作用于目标版本"],
      ["confidence_stat", "结构体", "可选", "模型置信度为附加统计量，不得作为准入唯一依据（研究报告 5.3 节）"],
      ["valid_window", "时间结构", "可选", "评估结论的有效期"],
    ],
    [24, 22, 20, 34],
    { fontSize: 19 }
  ));

  out.push(h2("3.8  工作类记录 TaskRevision"));
  out.push(body("任务以修订序列表达：每次目标、范围或验收条件的变化都产生新的 TaskRevision，通过 supersedes 边链接前一版本，任务循环不伪装成无环计划。任务的当前工作状态（ACTIVE、PENDING_REEVALUATION 等）是投影，存于 task_state 表，不写入记录。"));
  out.push(tblTitle("TaskRevision 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["task_object_id", "ObjectId", "跨修订稳定", "任务逻辑对象"],
      ["goal", "字符串", "非空", "目标描述"],
      ["scope", "结构体", "非空", "任务边界与资源边界"],
      ["acceptance_refs", "RecordId 数组", "非空", "验收谓词记录；任务完成必须满足其声明（第 6.5 节状态机）"],
      ["depends_on_task_refs", "RecordId 数组", "可空", "前置任务及其版本；失效传播命中前置时任务进入待重评"],
      ["artifact_requirements", "数组", "可空", "产物要求：类型、格式、验收摘要"],
      ["iteration", "整数", "非负", "迭代序号；同一任务的多次循环以新修订表达"],
      ["supersedes", "RecordId 或 null", "前一修订", "修订链；基线不匹配时 revise 操作被拒绝（E203）"],
    ],
    [26, 20, 18, 36],
    { fontSize: 19 }
  ));

  out.push(h2("3.9  动作意图 ActionIntent"));
  out.push(lead("字段与研究设计报告第 6 章一致。意图描述一次具体外部操作；同一任务可有多个意图，每个意图可有多次受控尝试。意图 ID、尝试 ID、工具请求 ID 与外部作业 ID 分开保存，任何合并都会破坏对账语义。"));
  out.push(tblTitle("ActionIntent 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["intent_id", "ID", "工作空间内唯一", "逻辑意图标识，重试沿用，不随尝试变化"],
      ["task_revision_id", "RecordId", "指向具体任务修订", "意图服务于哪个任务版本"],
      ["operation", "字符串", "连接器注册的操作名", "如 compute.job.submit、email.send"],
      ["connector_identity / connector_version", "字符串", "非空", "目标连接器身份与版本，参与 operation_digest"],
      ["executor_principal", "Principal", "非空", "执行主体；名称须在授权前解析为稳定身份，执行时不得悄悄重指向（第 9.2 节）"],
      ["target_ids", "ID 数组", "非空", "真实目标对象标识，参与 operation_digest"],
      ["arguments_ref", "JsonBlob", "摘要必填", "冻结后的参数，规范化摘要参与 operation_digest"],
      ["artifact_revision_ids", "RecordId 数组", "可空", "操作涉及的确切产物内容版本"],
      ["precondition_refs", "RecordId 数组", "可空", "前置条件：证据版本、任务修订、关键主张；dispatch 前重验证"],
      ["context_manifest_id", "RecordId", "高影响动作必填", "决策时实际看到的上下文清单"],
      ["idempotency_contract", "结构体", "见 6.2 节", "{mode, key, key_scope, key_expiry, retry_policy}"],
      ["resource_reservation_refs", "ID 数组", "可空", "原子预留引用（第 7.5 节）"],
    ],
    [26, 18, 20, 36],
    { fontSize: 19 }
  ));

  out.push(h2("3.10  授权记录 AuthorizationGrant"));
  out.push(body("授权是运行时强制执行的控制数据，不是描述性文本。Grant 只能由被允许的签发者（人工审批者或具备委派权限的服务）创建；子授权的范围不得超出父授权。批准 v7 不自动授权 v8，但仅仅出现 v8 也不使冻结为 v7 的动作失效——需要重检查的是具体策略与前置条件，由网关在 dispatch 时执行（第 7.6 节步骤 3）。"));
  out.push(tblTitle("AuthorizationGrant 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["grant_id", "ID", "全局唯一", "授权标识"],
      ["issuer / grantee", "Principal", "签发者须具备权限", "签发者与被授权主体"],
      ["parent_grant_id", "ID 或 null", "委派链", "非空时范围必须收敛于父授权（第 9.2 节检查）"],
      ["operation_digest", "Digest", "非空", "规范化操作摘要，覆盖字段见 9.1 节；网关按摘要匹配而非按名称"],
      ["scope", "结构体", "非空", "目标范围、参数约束、数据标签上限"],
      ["purpose", "字符串", "非空", "用途绑定；超出用途的使用被拒绝"],
      ["expiry", "Timestamp", "非空", "过期时刻；网关重验证包含此项"],
      ["revocation_handle", "ID", "非空", "撤销句柄；撤销立即影响后续 dispatch，不能收回已发出的外部请求"],
      ["use_limit / uses_consumed", "整数", "use_limit 大于等于 1", "按逻辑意图计量；尝试重试不额外获得授权（第 7.5 节）"],
      ["allowed_destinations", "数组", "非空", "允许的目标清单；读取权限与向某目标披露数据的权限分开声明"],
      ["policy_version", "整数", "非空", "签发时的策略版本，审计用"],
    ],
    [26, 18, 20, 36],
    { fontSize: 19 }
  ));

  out.push(h2("3.11  尝试 Attempt、回执 Receipt 与产物 Artifact"));
  out.push(tblTitle("Attempt 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["attempt_id", "ID", "全局唯一", "一次受控尝试的标识"],
      ["intent_id", "ID", "非空", "归属逻辑意图"],
      ["gateway_principal", "Principal", "仅网关可写", "执行网关实例"],
      ["started_at", "Timestamp", "网关分配", "外部调用开始前的落库时刻（先记录后调用）"],
      ["dispatch_digest", "Digest", "非空", "本次实际发出请求的规范化摘要，与 intent 冻结摘要核对"],
      ["idempotency_key_state", "结构体", "与契约匹配", "发出的键、范围、有效期；键过期后重试不得声称安全（E603）"],
      ["external_job_id", "字符串或 null", "远端返回", "外部作业标识，仅当连接器契约提供"],
      ["attempt_status", "枚举", "第 6.3 节", "STARTED / RESPONSE_PERSISTED / NO_RESPONSE"],
    ],
    [26, 18, 20, 36],
    { fontSize: 19 }
  ));
  out.push(tblTitle("Receipt 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["attempt_id", "ID", "非空", "回执先属于具体尝试，再按意图聚合"],
      ["outcome", "枚举", "第 6.1 节", "CONFIRMED_SUCCEEDED / CONFIRMED_NOT_APPLIED / OUTCOME_UNKNOWN / PARTIAL"],
      ["classification_basis", "结构体", "非空", "连接器契约条款引用：为何该响应对应该分类；无契约支撑的成功描述不得分类为确认"],
      ["evidence_observation_id", "RecordId", "非空", "原始响应先落为 Observation，回执引用之"],
      ["partial_items", "数组", "仅 PARTIAL", "复合操作拆分后的逐项结果，供逐项对账"],
      ["recorded_by", "Principal", "仅网关或可信接入", "智能体不得自行签发回执"],
    ],
    [26, 18, 20, 36],
    { fontSize: 19 }
  ));
  out.push(tblTitle("Artifact 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["artifact_object_id", "ObjectId", "跨版本稳定", "产物逻辑对象"],
      ["content_version", "整数", "非空", "内容版本；操作摘要绑定确切产物内容"],
      ["content", "JsonBlob", "摘要必填", "产物正文引用"],
      ["produced_by_attempt", "ID", "非空", "由哪次尝试产生"],
      ["data_labels", "数组", "继承并只增不减", "沿输入传播的数据标签（第 9.3 节）"],
    ],
    [26, 18, 20, 36],
    { fontSize: 19 }
  ));

  out.push(h2("3.12  上下文清单 ContextManifest"));
  out.push(body("上下文不是另一份可随意改写的真相，而是一次有契约的读取结果。ContextManifest 记录模型实际看到了什么：选中的记录与片段、顺序、变换或摘要版本、预算、检索器与策略版本。模型看见了什么必须落实到实际渲染输入（rendered_input_digest），不能只记录一条检索查询。"));
  out.push(tblTitle("ContextManifest 载荷字段"));
  out.push(tbl(
    ["字段", "类型", "约束", "说明"],
    [
      ["context_id", "ID", "全局唯一", "清单标识"],
      ["snapshot", "Snapshot 定位", "非空", "读取时的分支头、reducer 版本、两个 epoch"],
      ["task_revision_id", "RecordId", "可空", "服务于哪个任务版本"],
      ["selected_items", "数组", "非空", "{record_id, span, transform_id}：选中记录、精确片段与变换版本"],
      ["rendered_input_ref / rendered_input_digest", "JsonBlob / Digest", "渲染后必填", "实际发送给模型的完整输入及其摘要"],
      ["required_items", "RecordId 数组", "可空", "标记为必需的绑定单元；被省略时进入 omitted_items_summary"],
      ["omitted_items_summary", "结构体", "预算不足时非空", "被裁剪内容及原因；必需约束超出预算时必须分阶段、提额或暂停动作，不得静默丢弃（E701）"],
      ["token_budget / tokenizer_version", "整数 / 字符串", "非空", "预算与分词器版本"],
      ["retrieval_version", "字符串", "非空", "检索器与排序器版本"],
      ["policy_epoch", "整数", "非空", "读取时策略纪元；渲染缓存据此失效"],
      ["inherited_data_labels", "数组", "非空", "所有选中条目标签的并集，随输出传播"],
      ["expiry", "Timestamp", "非空", "清单有效期，超过后不得作为动作的 context_manifest_id"],
    ],
    [30, 18, 18, 34],
    { fontSize: 19 }
  ));

  out.push(h2("3.13  关系边类型系统 typed_refs"));
  out.push(body("三种关系不能混为一张无类型图：版本与已声明派生构成 DAG，语义关系允许成环，控制与执行关系按各自状态机验证。每条边携带来源与类型，控制边不得由相似度推导。下表为第一版注册的关系角色；扩展角色必须先登记结构限制，再允许写入。"));
  out.push(tblTitle("关系角色注册表"));
  out.push(tbl(
    ["层 layer", "角色 role", "语义", "结构限制", "允许的源与目标 kind"],
    [
      ["DERIVATION", "supersedes", "新版本替代旧版本", "DAG；指向已存在或同批前序记录", "任意 kind 到同 object_id 的旧版本"],
      ["DERIVATION", "derived_from / extracted_from / transcribed_from", "派生与转录来源", "DAG；不得指向未来记录（I2）", "Observation、Claim、Artifact 到 Observation 等"],
      ["DERIVATION", "uses_version", "声明使用了哪个确定版本", "DAG", "TaskRevision、ActionIntent、Justification 到任意记录"],
      ["SEMANTIC", "related_to / similar_to", "领域关联与相似", "允许成环；不产生支持，不触发失效", "Claim 到 Claim、实体到实体"],
      ["SEMANTIC", "same_entity_candidate", "疑似同一实体", "允许成环；合并须经 ENTITY_MERGE 记录确认", "实体记录之间"],
      ["CONTROL", "supports / refutes", "支持与反证分组（经 Justification）", "按 Justification 结构验证", "Justification 到 Observation、Claim"],
      ["CONTROL", "depends_on / precondition_of", "任务与动作前提", "命中失效传播时触发重评（第 7.3 节）", "TaskRevision、ActionIntent 到任意记录"],
      ["CONTROL", "authorized_by", "授权绑定", "摘要匹配校验（第 9.1 节）", "ActionIntent 到 Grant"],
      ["CONTROL", "attempt_of / receipt_of", "尝试与回执归属", "仅网关写入", "Attempt 到 ActionIntent；Receipt 到 Attempt"],
      ["CONTROL", "selected_into", "上下文选中", "指向 ContextManifest", "任意记录到 ContextManifest"],
      ["CONTROL", "produced", "尝试产物", "版本与摘要一致", "Attempt 到 Artifact"],
    ],
    [16, 22, 20, 22, 20],
    { fontSize: 18 }
  ));
  out.push(body("派生边表示系统记录的使用关系，不是现实世界的因果效应。A 信任 B 与 B 信任 A 可同时存在；但新版本不能把未来版本当作已使用的输入。反向依赖索引（record_refs 反向查询）是失效传播的基础，必须与正向边同事务写入。"));

  out.push(h2("3.14  状态与枚举字典"));
  out.push(tblTitle("证据与内容状态枚举"));
  out.push(tbl(
    ["枚举", "取值", "语义与规则"],
    [
      ["EvidenceFourState", "NONE / SUPPORTED / REFUTED / CONTESTED", "由支持位与反证位组合：1/1 为 CONTESTED，必须展示或裁决；不表示形式真值或概率"],
      ["Freshness", "fresh / stale", "来源或命题相对过期策略的状态；stale 不等于错误"],
      ["Visibility", "available / restricted / hidden", "按主体权限解析；对无权主体连证据存在性也按策略隐藏"],
      ["Retention", "retained / purged", "purged 表示正文已进入删除域处理；元数据按策略决定去留"],
      ["AssessmentStatus", "unassessed / verified / contradicted / withdrawn", "评估结论投影；撤回支持不是新增反证"],
    ],
    [22, 30, 48]
  ));
  out.push(tblTitle("执行与资源状态枚举"));
  out.push(tbl(
    ["枚举", "取值", "语义与规则"],
    [
      ["EffectState", "PROPOSED / PREPARED / IN_FLIGHT / OUTCOME_UNKNOWN / CONFIRMED_SUCCEEDED / CONFIRMED_NOT_APPLIED / CANCELLED_BEFORE_DISPATCH / PARTIAL", "第 6.1 节状态机；UNKNOWN 期间保留资源与授权占用"],
      ["AttemptStatus", "STARTED / RESPONSE_PERSISTED / NO_RESPONSE", "NO_RESPONSE 在超时后仍为尝试级状态，不直接判定意图失败"],
      ["ReservationState", "reserved / uncertain / committed / released", "uncertain 对应意图 OUTCOME_UNKNOWN；released 须满足释放守卫（第 6.4 节）"],
      ["GrantStatus", "active / expired / revoked / exhausted", "过期与撤销由网关重验证判定；次数用尽为 exhausted"],
      ["IdempotencyMode", "KEYED / QUERY_ONLY / NONE", "KEYED：远端支持键；QUERY_ONLY：可查询对账；NONE：默认暂停自动重发（E604）"],
      ["TaskStatus", "DRAFT / ACTIVE / PENDING_REEVALUATION / COMPLETED / CANCELLED", "COMPLETED 的守卫为验收谓词全部通过；PENDING_REEVALUATION 由失效传播触发"],
    ],
    [22, 34, 44],
    { fontSize: 19 }
  ));

  out.push(h2("3.15  schema 演进规则"));
  out.push(body("schema_version 只增不减。新增可选字段递增次版本；改变字段语义、删除字段或收紧约束必须递增主版本并同步升级 reducer，旧版本载荷在回放时按其声明版本的解释器处理。写入服务按注册表（附录 A）校验当前接受的最低与最高版本，拒绝无法解释的版本（E102）。载荷中不允许出现注册表之外的字段名，防止以扩展名义夹带未声明语义。"));
  return out;
}

module.exports = { ch3 };

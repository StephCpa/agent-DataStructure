"""记录类型注册表与 schema 校验 —— 对应《详细设计说明书》3.2/3.13/3.15 节与附录 A。

注册表是写入服务校验的唯一依据（附录 A）：新增 kind 必须先修订本表与 reducer。
阶段 A 仅启用语义内核所需的 kind；阶段 B/C 的 kind 已登记结构但标记未启用。
"""
from __future__ import annotations

from .errors import AsgError

# ── 关系角色 -> 层（3.13 节注册表） ──────────────────────
ROLE_LAYER = {
    # DERIVATION：版本与已声明派生，DAG
    "supersedes": "DERIVATION",
    "derived_from": "DERIVATION",
    "extracted_from": "DERIVATION",
    "transcribed_from": "DERIVATION",
    "uses_version": "DERIVATION",
    # SEMANTIC：领域关系，允许成环
    "related_to": "SEMANTIC",
    "similar_to": "SEMANTIC",
    "same_entity_candidate": "SEMANTIC",
    # CONTROL：控制与执行，按状态机验证
    "supports": "CONTROL",
    "refutes": "CONTROL",
    "depends_on": "CONTROL",
    "precondition_of": "CONTROL",
    "authorized_by": "CONTROL",
    "attempt_of": "CONTROL",
    "receipt_of": "CONTROL",
    "selected_into": "CONTROL",
    "produced": "CONTROL",
}

# ── 附录 A：kind 注册表 ─────────────────────────────────
# phase: A=已启用；B/C=已登记、写入服务暂拒（E103），待对应阶段启用。
# required: 载荷必填字段；每字段附简单校验器。
KIND_SPECS = {
    "OBSERVATION": {
        "phase": "A",
        "required": {
            "source": dict,
            "modality": str,
            "span": dict,
            "acquired_at": str,
            "acquisition_method": dict,
        },
        "choices": {"modality": {"text", "pdf", "image", "audio", "video", "table", "api_response"}},
        "roles": {"derived_from", "extracted_from", "transcribed_from"},
    },
    "CLAIM": {
        "phase": "A",
        "required": {"scope": dict, "proposer": dict},
        "one_of": [("proposition", "constrained_text")],
        "roles": {"supersedes", "uses_version"},
    },
    "TASK_REVISION": {
        "phase": "A",
        "required": {"task_object_id": str, "goal": str, "acceptance_refs": list},
        "roles": {"supersedes", "depends_on", "uses_version"},
    },
    "RETIRE_PURGE_MARK": {
        "phase": "A",
        "required": {"target": dict, "reason": str},
        "choices": {"reason": {"withdrawn", "corrected", "superseded", "purged"}},
        "roles": {"uses_version"},
    },
    # ── 阶段 B/C：已登记，未启用 ──
    "JUSTIFICATION": {
        "phase": "C",
        "required": {"target_claim_id": str, "support_groups": list, "refute_groups": list},
        "roles": {"supports", "refutes", "uses_version"},
    },
    "ASSESSMENT": {
        "phase": "C",
        "required": {"target_record_id": str, "method": str, "evaluator": dict, "verdict": str},
        "roles": {"uses_version"},
    },
    "ENTITY_MERGE": {"phase": "C", "required": {"decision": str}, "roles": {"supersedes"}},
    "ACTION_INTENT": {
        "phase": "B",
        "required": {"intent_id": str, "task_revision_id": str, "operation": str,
                     "connector_identity": str, "connector_version": str,
                     "executor_principal": dict, "target_ids": list,
                     "arguments_ref": dict, "idempotency_contract": dict},
        "roles": {"precondition_of", "authorized_by", "uses_version", "selected_into"},
    },
    "GRANT": {
        "phase": "B",
        "required": {"grant_id": str, "issuer": dict, "grantee": dict, "operation_digest": str,
                     "scope": dict, "purpose": str, "expiry": str, "revocation_handle": str,
                     "use_limit": int, "allowed_destinations": list, "policy_version": int},
        "roles": set(),
    },
    "ATTEMPT": {
        "phase": "B",
        "required": {"attempt_id": str, "intent_id": str, "gateway_principal": dict,
                     "started_at": str, "dispatch_digest": str},
        "roles": {"attempt_of", "produced"},
    },
    "RECEIPT": {
        "phase": "B",
        "required": {"attempt_id": str, "outcome": str, "classification_basis": dict,
                     "evidence_observation_id": str, "recorded_by": dict},
        "roles": {"receipt_of"},
    },
    "ARTIFACT": {
        "phase": "B",
        "required": {"artifact_object_id": str, "content_version": int, "content": dict,
                     "produced_by_attempt": str, "data_labels": list},
        "roles": {"supersedes"},
    },
    "CONTEXT_MANIFEST": {
        "phase": "C",
        "required": {"context_id": str, "snapshot": dict, "selected_items": list,
                     "token_budget": int, "tokenizer_version": str, "retrieval_version": str,
                     "policy_epoch": int, "inherited_data_labels": list, "expiry": str},
        "roles": {"selected_into"},
    },
    "RULE_VALIDATOR": {
        "phase": "C",
        "required": {"rule_id": str, "body_ref": dict},
        "roles": {"supersedes"},
    },
}

SUPPORTED_SCHEMA_VERSIONS = (1,)

# 当前已启用的阶段（registry gate）：A=语义内核；B=执行闭环；C=证据与上下文
ENABLED_PHASES = {"A", "B", "C"}

ENVELOPE_REQUIRED = ("workspace_id", "object_id", "kind", "schema_version",
                     "authenticated_writer", "policy_domain")


def validate_kind_phase(kind: str) -> None:
    spec = KIND_SPECS.get(kind)
    if spec is None:
        raise AsgError("E101", f"未注册的记录类型: {kind}")
    if spec["phase"] not in ENABLED_PHASES:
        raise AsgError("E103", f"记录类型 {kind} 属于阶段 {spec['phase']}，写入服务尚未启用")


def validate_record(rec: dict) -> None:
    """校验信封 + 载荷（3.2 信封、3.15 演进规则、附录 A 注册表）。"""
    for f in ENVELOPE_REQUIRED:
        if f not in rec or rec[f] in (None, ""):
            raise AsgError("E101", f"信封缺少必填字段: {f}")
    kind = rec["kind"]
    spec = KIND_SPECS.get(kind)
    if spec is None:
        raise AsgError("E101", f"未注册的记录类型: {kind}")
    if rec["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        raise AsgError("E102", f"schema_version {rec['schema_version']} 不可解释，支持 {SUPPORTED_SCHEMA_VERSIONS}")
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        raise AsgError("E101", "payload 必须为对象")
    for fname, ftype in spec["required"].items():
        if fname not in payload or payload[fname] in (None, ""):
            raise AsgError("E101", f"{kind} 载荷缺少必填字段: {fname}")
        if ftype is not None and not isinstance(payload[fname], ftype):
            raise AsgError("E101", f"{kind} 载荷字段 {fname} 类型应为 {ftype.__name__}")
    if not payload.get("acceptance_refs", True):  # 非空列表检查（acceptance_refs 须非空）
        if "acceptance_refs" in spec["required"]:
            raise AsgError("E101", "TASK_REVISION 的 acceptance_refs 不得为空")
    for pair in spec.get("one_of", []):
        if not any(payload.get(k) not in (None, "") for k in pair):
            raise AsgError("E101", f"{kind} 载荷须提供 {' 或 '.join(pair)} 之一")
    for field, allowed in spec.get("choices", {}).items():
        if payload.get(field) is not None and payload[field] not in allowed:
            raise AsgError("E101", f"{kind} 载荷字段 {field} 取值 {payload[field]!r} 不在允许集合")
    # 引用角色白名单（附录 A 允许发出的引用角色）
    allowed_roles = spec["roles"]
    for ref in rec.get("typed_refs", []):
        role = ref.get("role")
        if role not in ROLE_LAYER:
            raise AsgError("E101", f"未注册的引用角色: {role}")
        if role not in allowed_roles:
            raise AsgError("E101", f"{kind} 不允许发出引用角色 {role}")
        if "target_record_id" not in ref:
            raise AsgError("E101", "typed_refs 每项必须包含 target_record_id")

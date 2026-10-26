"""记录类型注册表与 schema 校验 —— 对应《详细设计说明书》3.2/3.13/3.15 节与附录 A。

注册表是写入服务校验的唯一依据（附录 A）：新增 kind 必须先修订本表与 reducer。
阶段 A 仅启用语义内核所需的 kind；阶段 B/C 的 kind 已登记结构但标记未启用。
"""
from __future__ import annotations

from copy import deepcopy

from .errors import AsgError

# ── 关系角色 -> 层（3.13 节注册表） ──────────────────────
ROLE_LAYER = {
    # DERIVATION：版本与已声明派生，DAG
    "supersedes": "DERIVATION",
    "prior_revision": "DERIVATION",
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
    "targets_claim": "CONTROL",
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
        "roles": {"supports", "refutes", "uses_version", "targets_claim", "supersedes"},
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
        "roles": {"receipt_of", "uses_version"},
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
    if kind == "TASK_REVISION" and payload["task_object_id"] != rec["object_id"]:
        raise AsgError("E101", "TaskRevision task_object_id must equal envelope object_id")
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
        if role not in allowed_roles and role != "prior_revision":
            raise AsgError("E101", f"{kind} 不允许发出引用角色 {role}")
        if "target_record_id" not in ref:
            raise AsgError("E101", "typed_refs 每项必须包含 target_record_id")


# Only these fields contain record IDs. Operation/job IDs and legacy symbolic
# task_revision_id/acceptance_refs are not silently interpreted as record IDs.
REFERENCE_FIELDS = {
    "JUSTIFICATION": {"target_claim_id": ("targets_claim", "one"),
                      "support_groups": ("supports", "groups"),
                      "refute_groups": ("refutes", "groups"),
                      "rule_refs": ("uses_version", "many"),
                      "assumption_refs": ("uses_version", "many")},
    "OBSERVATION": {role: (role, "many") for role in
                    ("derived_from", "extracted_from", "transcribed_from")},
    "TASK_REVISION": {"dependency_refs": ("depends_on", "many")},
    "ACTION_INTENT": {"precondition_refs": ("uses_version", "many")},
    "RETIRE_PURGE_MARK": {"target.record_id": ("uses_version", "one")},
    "ASSESSMENT": {"target_record_id": ("uses_version", "one")},
    "RECEIPT": {"evidence_observation_id": ("uses_version", "one")},
    "CONTEXT_MANIFEST": {"selected_items": ("selected_into", "items")},
}


def normalize_record(record):
    """Fill missing edges, reject conflicting edges, persist parent edges.

    Normalization is server-side and never modifies the caller's request object.
    Request idempotency remains based on the original request bytes/structure.
    """
    rec = deepcopy(record)
    validate_record(rec)
    p = rec["payload"]
    refs = rec.get("typed_refs", [])
    if rec["kind"] == "JUSTIFICATION":
        # Previous justify() encoded the target using supports. Migrate that
        # redundant representation; target is no longer a supporting member.
        target = p["target_claim_id"]
        members = {m for g in p["support_groups"] if isinstance(g, list) for m in g}
        refs = [r for r in refs if not (r["role"] == "supports"
                and r["target_record_id"] == target and target not in members)]
    expected, managed = set(), set()
    for field, (role, shape) in REFERENCE_FIELDS.get(rec["kind"], {}).items():
        value = p
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            # Optional fields may be supplied through typed_refs; persist one
            # canonical payload description as well, where no role is shared.
            if rec["kind"] in {"OBSERVATION", "TASK_REVISION", "ACTION_INTENT"}:
                value = [r["target_record_id"] for r in refs if r["role"] == role]
                p[field] = value
            else:
                value = []
        if shape == "one":
            values = [value]
        elif shape == "groups":
            if not isinstance(value, list) or any(not isinstance(g, list) or not g for g in value):
                raise AsgError("E101", f"{field} must contain nonempty AND groups")
            values = [m for group in value for m in group]
        elif shape == "items":
            if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
                raise AsgError("E101", f"Invalid {field}")
            values = [v.get("record_id") for v in value]
        else:
            values = value
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
            raise AsgError("E101", f"{field} must contain record IDs")
        managed.add(role)
        expected.update((role, v) for v in values)
    supplied = {(r["role"], r["target_record_id"]) for r in refs}
    extras = {edge for edge in supplied if edge[0] in managed} - expected
    if extras:
        raise AsgError("E101", "Payload references disagree with typed_refs", details={"extra": sorted(extras)})
    parents = rec.get("prior_revision_ids", [])
    if not isinstance(parents, list) or any(not isinstance(v, str) or not v for v in parents):
        raise AsgError("E101", "prior_revision_ids must contain record IDs")
    parents = set(parents) | {dst for role, dst in supplied if role in {"supersedes", "prior_revision"}}
    rec["prior_revision_ids"] = sorted(parents)
    edges = supplied | expected | {("prior_revision", v) for v in parents}
    rec["typed_refs"] = [{"role": role, "target_record_id": dst} for role, dst in sorted(edges)]
    validate_record(rec)
    return rec

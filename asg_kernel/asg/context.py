"""上下文投影（M3）—— 《详细设计说明书》3.12 节与 7.4 节。

上下文是有契约的读取结果：绑定单元（来源 + 否定条件 + 单位 + 时间 + 标签）不可拆散；
必需约束（M）超出预算必须分阶段、提额或暂停动作（E701），不得静默裁剪；
排序只作用于已通过访问与适用性过滤的候选；摘要类派生内容继续指向原文。
数值汇总与计数调用确定性账本查询，不经模型心算。
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone

from . import evidence
from .errors import AsgError
from .ids import digest_obj, uuid7
from .invalidation import read_gate

TOKENIZER_VERSION = "simple-v1"
RETRIEVAL_VERSION = "retrieval-v1"
ALLOWED_KINDS = {"OBSERVATION", "CLAIM", "JUSTIFICATION", "ASSESSMENT"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def tokens(text: str) -> int:
    """简单分词估算：约 4 字符一 token（tokenizer_version 标注，可替换）。"""
    return math.ceil(max(len(text), 1) / 4)


def _policy_epoch(conn, workspace_id: str) -> int:
    row = conn.execute(
        "SELECT policy_epoch FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    return row["policy_epoch"] if row else 1


def _snapshot(conn, workspace_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    return {
        "head_sequence": row["head_sequence"] if row else 0,
        "validation_epoch": row["validation_epoch"] if row else 1,
        "policy_epoch": row["policy_epoch"] if row else 1,
        "reducer_version": 1,
    }


def binding_unit(conn, workspace_id: str, record_id: str) -> dict | None:
    """绑定单元组装（7.4 节步骤 3）：正文片段 + 状态 + 关键来源指针 + 约束。"""
    row = conn.execute("SELECT * FROM record WHERE record_id=?", (record_id,)).fetchone()
    if row is None or row["kind"] not in ALLOWED_KINDS:
        return None
    if conn.execute("SELECT 1 FROM tombstone WHERE record_id=? AND mode='purge'",
                    (record_id,)).fetchone():
        return None  # 删除域内容不进入上下文
    payload = json.loads(row["payload"])
    if row["kind"] == "CLAIM":
        text = payload.get("proposition") and json.dumps(payload["proposition"], ensure_ascii=False) \
            or payload.get("constrained_text", "")
        state = evidence.evaluate_support(conn, workspace_id, record_id)
        unit = {
            "record_id": record_id, "kind": row["kind"], "text": str(text),
            "four_state": state["four_state"],
            "unresolved_conflict": state["four_state"] == "CONTESTED",
            "units_field": payload.get("units"),
            "valid_time": payload.get("valid_time"),
            "negation_conditions": payload.get("negation_conditions", []),
            "source_pointers": [p for g in state["supporting_paths"] for p in g][:4],
            "data_labels": payload.get("data_labels", []),
        }
    elif row["kind"] == "OBSERVATION":
        unit = {
            "record_id": record_id, "kind": row["kind"],
            "text": str(payload.get("source", {}).get("url_or_handle", "")),
            "span": payload.get("span"),
            "source_root": evidence.root_source(conn, record_id),
            "modality": payload.get("modality"),
            "data_labels": payload.get("data_labels", []),
        }
    else:
        unit = {"record_id": record_id, "kind": row["kind"],
                "text": json.dumps(payload, ensure_ascii=False)[:512],
                "data_labels": payload.get("data_labels", [])}
    unit["token_cost"] = tokens(json.dumps(unit, ensure_ascii=False))
    return unit


def deterministic_ledger_query(conn, workspace_id: str, kind: str) -> int:
    """确定性账本查询（7.4 节步骤 6）：计数类信息不经模型汇总。"""
    return conn.execute(
        "SELECT COUNT(*) c FROM record WHERE workspace_id=? AND kind=?", (workspace_id, kind)
    ).fetchone()["c"]


def project_context(conn, svc, workspace_id: str, required_ids: list[str],
                    candidate_ids: list[str], token_budget: int,
                    task_revision_id: str | None = None) -> dict:
    """上下文投影（7.4 节）：过滤 -> 绑定单元 -> M 预算检查 -> U 填充 -> 清单落账。"""
    # 探索检索走闸门但不阻断：显式标注陈旧
    gate = read_gate(conn, workspace_id, required_ids + candidate_ids, purpose="exploratory")

    m_units, omitted_filter = [], []
    for rid in required_ids + candidate_ids:
        unit = binding_unit(conn, workspace_id, rid)
        if unit is None:
            omitted_filter.append({"record_id": rid, "reason": "not_readable_or_applicable"})
        elif rid in required_ids:
            m_units.append(unit)
    m_cost = sum(u["token_cost"] for u in m_units)
    if m_cost > token_budget:
        raise AsgError(
            "E701", "必需约束超出 token 预算：分阶段读取、提高预算或暂停动作",
            details={"required_cost": m_cost, "budget": token_budget,
                     "required_items": [u["record_id"] for u in m_units]})

    # 效用排序仅作用于已过滤候选（阶段 C 以新近度代理效用，可替换排序器）
    cand_units = [binding_unit(conn, workspace_id, rid) for rid in candidate_ids]
    cand_units = [u for u in cand_units if u is not None]
    cand_units.sort(key=lambda u: u["record_id"], reverse=False)
    selected, used, omitted_budget = list(m_units), m_cost, []
    for u in cand_units:
        if used + u["token_cost"] <= token_budget:
            selected.append(u)
            used += u["token_cost"]
        else:
            omitted_budget.append({"record_id": u["record_id"], "reason": "budget"})

    labels = sorted({l for u in selected for l in u.get("data_labels", [])})
    snap = _snapshot(conn, workspace_id)
    rendered = json.dumps(
        {"units": [{k: v for k, v in u.items() if k != "record_id"} or u for u in selected],
         "ledger": {"claim_count":
                    deterministic_ledger_query(conn, workspace_id, "CLAIM")}},
        ensure_ascii=False, sort_keys=True)
    manifest_payload = {
        "context_id": "ctx-" + uuid7(),
        "snapshot": snap,
        "task_revision_id": task_revision_id,
        "selected_items": [{"record_id": u["record_id"], "span": u.get("span"),
                            "transform_id": "identity@1"} for u in selected],
        "rendered_input_digest": digest_obj({"rendered": rendered}),
        "required_items": list(required_ids),
        "omitted_items_summary": {"filtered": omitted_filter, "budget": omitted_budget},
        "token_budget": token_budget,
        "tokenizer_version": TOKENIZER_VERSION,
        "retrieval_version": RETRIEVAL_VERSION,
        "policy_epoch": snap["policy_epoch"],
        "inherited_data_labels": labels,
        "expiry": _now(),
        "stale_annotation": gate,
    }
    record = {
        "workspace_id": workspace_id,
        "object_id": manifest_payload["context_id"],
        "kind": "CONTEXT_MANIFEST",
        "schema_version": 1,
        "authenticated_writer": {"kind": "service", "id": "read-service"},
        "policy_domain": "default",
        "payload": manifest_payload,
        "typed_refs": [{"role": "selected_into", "target_record_id": rid}
                       for rid in required_ids],
        "prior_revision_ids": [],
    }
    head = conn.execute(
        "SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
    ).fetchone()["head_sequence"]
    result = svc.submit_proposal(workspace_id, [record], head, "req-" + uuid7())
    return {
        "context_manifest_id": manifest_payload["context_id"],
        "commit": result,
        "selected": [u["record_id"] for u in selected],
        "omitted": {"filtered": omitted_filter, "budget": omitted_budget},
        "token_used": used,
        "inherited_data_labels": labels,
        "stale": gate["stale"],
    }


def manifest_valid(conn, workspace_id: str, manifest_record_id: str) -> bool:
    """清单有效性：命中失效标记或策略纪元变更即过期（E702 语义）。"""
    row = conn.execute("SELECT payload FROM record WHERE record_id=? AND kind='CONTEXT_MANIFEST'",
                       (manifest_record_id,)).fetchone()
    if row is None:
        return False
    p = json.loads(row["payload"])
    if _policy_epoch(conn, workspace_id) != p.get("policy_epoch"):
        return False
    hit = conn.execute(
        "SELECT 1 FROM invalidation_mark WHERE workspace_id=? AND record_id=?",
        (workspace_id, manifest_record_id),
    ).fetchone()
    return hit is None

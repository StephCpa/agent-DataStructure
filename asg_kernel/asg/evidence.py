"""证据与主张求值（M3）—— 《详细设计说明书》3.6 节与 7.2 节。

支持电路：OR of AND（组内合取、组间析取），支持与反证分别建组；
规则失效等同于其参与的组全部失效；四态由（支持位，反证位）组合。
Eligible 只表示在查询范围、时间与策略下可被使用且验证尚有效，不表示为真。
最小支持集合完整展开可能指数增长：电路设路径/深度/重算预算，超限置 truncated，
不把截断结果谎称完整证明。来源谱系：转载链（derived/extracted/transcribed_from）
归于同一根来源，不按独立证据计数。
"""
from __future__ import annotations

import json
import sqlite3

from .errors import AsgError
from .ids import uuid7

DEFAULT_BUDGET = {"max_paths": 16, "max_depth": 8, "max_recompute": 512}

FOUR_STATE = {
    (False, False): "NONE",
    (True, False): "SUPPORTED",
    (False, True): "REFUTED",
    (True, True): "CONTESTED",
}

LINEAGE_ROLES = ("derived_from", "extracted_from", "transcribed_from")


def justify(svc, workspace_id: str, claim_record_id: str,
            support_groups: list[list[str]], refute_groups: list[list[str]] | None = None,
            rule_refs: list[str] | None = None, assumption_refs: list[str] | None = None,
            expected_head: int = 0, principal: dict | None = None) -> dict:
    """登记 Justification（3.6 节）。refute_groups 与 support_groups 同构：OR of AND。"""
    refute = refute_groups or []
    payload = {
        "target_claim_id": claim_record_id,
        "support_groups": support_groups,
        "refute_groups": refute,
        "rule_refs": rule_refs or [],
        "assumption_refs": assumption_refs or [],
        "justification_note": "",
    }
    refs = ([{"role": "supports", "target_record_id": m} for g in support_groups for m in g]
            + [{"role": "refutes", "target_record_id": m} for g in refute for m in g]
            + [{"role": "uses_version", "target_record_id": r} for r in (rule_refs or [])])
    record = {
        "workspace_id": workspace_id,
        "object_id": "just-" + uuid7(),
        "kind": "JUSTIFICATION",
        "schema_version": 1,
        "authenticated_writer": principal or {"kind": "service", "id": "read-service"},
        "policy_domain": "default",
        "payload": payload,
        "typed_refs": refs,
        "prior_revision_ids": [],
    }
    return svc.submit_proposal(workspace_id, [record], expected_head,
                               "req-" + uuid7(), principal=principal)


def root_source(conn: sqlite3.Connection, record_id: str) -> str:
    """来源谱系根：沿转载/转录/派生边回溯（TC-08：转述不作独立验证）。"""
    current = record_id
    seen = set()
    while current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT dst_record_id FROM record_ref WHERE src_record_id=? AND ref_role IN "
            f"{LINEAGE_ROLES} LIMIT 1", (current,),
        ).fetchone()
        if row is None:
            return current
        current = row["dst_record_id"]
    return current  # 防御：异常环路时返回当前节点


def _retired_roots(conn, workspace_id: str) -> set[str]:
    return {r["root_record_id"] for r in conn.execute(
        "SELECT root_record_id FROM invalidation_queue WHERE workspace_id=?", (workspace_id,)
    ).fetchall()}


def _marked(conn, workspace_id: str, record_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM invalidation_mark WHERE workspace_id=? AND record_id=?",
        (workspace_id, record_id),
    ).fetchone() is not None


def _purged(conn, record_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM tombstone WHERE record_id=? AND mode='purge'", (record_id,)
    ).fetchone() is not None


def eligible(conn, workspace_id: str, record_id: str, as_of: int | None = None) -> bool:
    """证据资格：版本存在于 as_of 之前，未被撤回（根失效）、未被标记、未被删除。"""
    row = conn.execute("SELECT sequence FROM record WHERE record_id=?", (record_id,)).fetchone()
    if row is None:
        return False
    if as_of is not None and row["sequence"] > as_of:
        return False
    if record_id in _retired_roots(conn, workspace_id):
        return False
    if _marked(conn, workspace_id, record_id):
        return False
    if _purged(conn, record_id):
        return False
    return True


def evaluate_support(conn, workspace_id: str, claim_record_id: str,
                     as_of: int | None = None, budget: dict | None = None) -> dict:
    """支持电路求值（7.2 节）：合并该主张的全部 Justification（OR of AND）。"""
    b = dict(DEFAULT_BUDGET)
    b.update(budget or {})
    if as_of is None:
        row = conn.execute(
            "SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        as_of = row["head_sequence"] if row else 0

    justs = conn.execute(
        "SELECT payload FROM record WHERE workspace_id=? AND kind='JUSTIFICATION'"
        " AND sequence<=? AND json_extract(payload, '$.target_claim_id')=?",
        (workspace_id, as_of, claim_record_id),
    ).fetchall()
    support_groups: list[list[str]] = []
    refute_groups: list[list[str]] = []
    rules: list[str] = []
    for j in justs:
        p = json.loads(j["payload"])
        support_groups.extend(p.get("support_groups") or [])
        refute_groups.extend(p.get("refute_groups") or [])
        rules.extend(p.get("rule_refs") or [])

    # 规则全局合取：任一规则失效，其参与的组全部失效（3.6 节）
    rules_eligible = all(eligible(conn, workspace_id, r, as_of) for r in rules) if rules else True

    def eval_groups(groups):
        ok_paths, evaluated = [], 0
        truncated = False
        for g in groups:
            if evaluated >= b["max_paths"] or len(g) > b["max_depth"]:
                # 尚有未展开的组：结果不谎称完整证明（7.2 节预算截断）
                truncated = True
                break
            evaluated += 1
            if rules_eligible and all(eligible(conn, workspace_id, m, as_of) for m in g):
                ok_paths.append(list(g))
        return ok_paths, evaluated, truncated

    supporting, sup_n, t1 = eval_groups(support_groups)
    refuting, ref_n, t2 = eval_groups(refute_groups)
    truncated = t1 or t2

    s = bool(supporting)
    r = bool(refuting)
    # 来源谱系：合格支持证据按根来源去重（TC-08）
    roots = {root_source(conn, m) for g in supporting for m in g}

    return {
        "claim": claim_record_id,
        "four_state": FOUR_STATE[(s, r)],
        "supported": s,
        "refuted": r,
        "supporting_paths": supporting,
        "refuting_paths": refuting,
        "groups_evaluated": sup_n + ref_n,
        "truncated": truncated,
        "independent_roots": sorted(roots),
        "independent_root_count": len(roots),
    }


def claim_four_state(conn, workspace_id: str, claim_record_id: str) -> str:
    return evaluate_support(conn, workspace_id, claim_record_id)["four_state"]

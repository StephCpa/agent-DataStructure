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
            + [{"role": "uses_version", "target_record_id": r} for r in (rule_refs or [])]
            # 假设与目标主张也入引用图：撤回它们时失效传播能到达本论证（7.3 节）
            + [{"role": "uses_version", "target_record_id": a} for a in (assumption_refs or [])]
            + [{"role": "supports", "target_record_id": claim_record_id}])
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


def eligible(conn, workspace_id: str, record_id: str, as_of: int | None = None,
             _stack: tuple = ()) -> bool:
    """证据资格：版本存在于 as_of 之前，未被撤回/标记/删除。

    主张成员递归求值：作为支持成员的 Claim 只有自身正向支持成立才合格
    （评审反例：O→C1→C2 撤回 O 后 C2 仍 SUPPORTED）。循环引用不凭空产生支持。
    """
    row = conn.execute("SELECT sequence, kind FROM record WHERE record_id=?",
                       (record_id,)).fetchone()
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
    if row["kind"] == "CLAIM":
        if record_id in _stack:  # 循环主张互引用不产生支持（3.6 节）
            return False
        ev = evaluate_support(conn, workspace_id, record_id, as_of, _stack=_stack + (record_id,))
        return ev["supported"] and not ev["refuted"]
    return True


def evaluate_support(conn, workspace_id: str, claim_record_id: str,
                     as_of: int | None = None, budget: dict | None = None,
                     _stack: tuple = ()) -> dict:
    """支持电路求值（7.2 节，按论证独立求值）。

    每个 Justification 是独立的逻辑论证：其规则与假设只约束自身的支持组，
    论证自身被撤回则整条路径退出。不同论证之间为析取（评审反例：
    撤回 J1 的规则不得使 J2 的独立支持消失；撤回论证自身不得继续提供支持）。
    """
    b = dict(DEFAULT_BUDGET)
    b.update(budget or {})
    if as_of is None:
        row = conn.execute(
            "SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        as_of = row["head_sequence"] if row else 0

    justs = conn.execute(
        "SELECT record_id, payload FROM record WHERE workspace_id=? AND kind='JUSTIFICATION'"
        " AND sequence<=? AND json_extract(payload, '$.target_claim_id')=?",
        (workspace_id, as_of, claim_record_id),
    ).fetchall()

    supporting, refuting, evaluated, truncated = [], [], 0, False
    for j in justs:
        p = json.loads(j["payload"])
        # 论证自身被撤回：整条路径退出。论证被标记只表示其依赖有变更，
        # 成员资格在下方逐组重算——标记本身不使全部 OR 分支失效（TC-01 语义）
        if j["record_id"] in _retired_roots(conn, workspace_id):
            continue
        # 规则与假设按论证独立合取（不跨论证合并）
        prereq_ok = all(eligible(conn, workspace_id, r, as_of, _stack)
                        for r in (p.get("rule_refs") or []) + (p.get("assumption_refs") or []))
        if not prereq_ok:
            continue
        for groups, bucket in ((p.get("support_groups") or [], supporting),
                               (p.get("refute_groups") or [], refuting)):
            for g in groups:
                if evaluated >= b["max_paths"] or len(g) > b["max_depth"]:
                    truncated = True  # 超限不谎称完整证明（7.2 节）
                    break
                evaluated += 1
                if all(eligible(conn, workspace_id, m, as_of, _stack) for m in g):
                    bucket.append(list(g))

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
        "groups_evaluated": evaluated,
        "truncated": truncated,
        "independent_roots": sorted(roots),
        "independent_root_count": len(roots),
    }


def claim_four_state(conn, workspace_id: str, claim_record_id: str) -> str:
    return evaluate_support(conn, workspace_id, claim_record_id)["four_state"]

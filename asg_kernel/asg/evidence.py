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

DEFAULT_BUDGET = {"max_paths": 16, "max_depth": 8, "max_recompute": 512, "max_group_width": 64}

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
            expected_head: int = 0, principal: dict | None = None,
            object_id: str | None = None, supersedes: str | None = None) -> dict:
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
            + [{"role": "targets_claim", "target_record_id": claim_record_id}])
    if supersedes:
        refs.append({"role": "supersedes", "target_record_id": supersedes})
    record = {
        "workspace_id": workspace_id,
        "object_id": object_id or "just-" + uuid7(),
        "kind": "JUSTIFICATION",
        "schema_version": 1,
        "authenticated_writer": principal or {"kind": "service", "id": "read-service"},
        "policy_domain": "default",
        "payload": payload,
        "typed_refs": refs,
        "prior_revision_ids": [supersedes] if supersedes else [],
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


def _retired_roots(conn, workspace_id, as_of):
    return {json.loads(r["payload"])["target"]["record_id"] for r in conn.execute(
        "SELECT payload FROM record WHERE workspace_id=? AND kind='RETIRE_PURGE_MARK'"
        " AND sequence<=?", (workspace_id, as_of))}


class _Evaluation:
    """One request, one shared budget. Cache hits cost zero recompute units.

    A unit is one uncached record inspection or one AND group visit.
    max_paths counts group visits across both polarities and all child claims.
    max_depth counts nested evidence edges below the root (root depth = 0).
    SQL enumeration/IO and provenance reporting are not wall-clock budgets.
    """
    def __init__(self, conn, workspace_id, as_of, budget):
        self.conn, self.ws = conn, workspace_id
        head = conn.execute("SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?",
                            (workspace_id,)).fetchone()
        self.as_of = as_of if as_of is not None else (head[0] if head else 0)
        self.limits = dict(DEFAULT_BUDGET)
        for key, value in (budget or {}).items():
            if key not in self.limits or type(value) is not int or value < 0:
                raise AsgError("E101", f"Invalid evaluation budget: {key}={value!r}")
            self.limits[key] = value
        if self.limits["max_depth"] > 128:
            raise AsgError("E101", "max_depth must not exceed the recursive implementation limit 128")
        self.used = {"paths": 0, "recompute": 0, "max_depth": 0, "cache_hits": 0}
        self.exhausted = set()
        self.records, self.results = {}, {}
        self.retired = _retired_roots(conn, workspace_id, self.as_of)
        # Current deletion policy always applies, even to historical evaluation.
        self.purged = {r[0] for r in conn.execute(
            "SELECT record_id FROM tombstone WHERE workspace_id=? AND mode='purge'", (workspace_id,))}

    def spend(self, name):
        if self.used[name] >= self.limits["max_" + name]:
            self.exhausted.add("max_" + name)
            return False
        self.used[name] += 1
        return True

    def record(self, rid):
        if rid in self.records:
            self.used["cache_hits"] += 1
            return self.records[rid]
        if not self.spend("recompute"):
            return None, False
        row = self.conn.execute("SELECT * FROM record WHERE record_id=? AND workspace_id=?"
                                " AND sequence<=?", (rid, self.ws, self.as_of)).fetchone()
        rec = dict(row) if row else None
        if rec:
            rec["payload"] = json.loads(rec["payload"])
        self.records[rid] = (rec, True)
        return rec, True

    def member(self, rid, depth, stack):
        if depth > self.limits["max_depth"]:
            self.exhausted.add("max_depth")
            return None
        self.used["max_depth"] = max(self.used["max_depth"], depth)
        rec, complete = self.record(rid)
        if not complete:
            return None
        if rec is None or rid in self.retired or rid in self.purged:
            return False
        if rid in stack:
            self.exhausted.add("cycle")
            return None
        if rec["kind"] == "CLAIM":
            result = self.claim(rid, depth, stack)
            if result["truncated"]:
                return None
            return result["supported"] and not result["refuted"]
        # Ancestry is checked synchronously, including before any queue drain.
        parents = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT dst_record_id FROM record_ref WHERE src_record_id=? AND ref_role IN"
            " ('derived_from','extracted_from','transcribed_from','uses_version','depends_on','selected_into')"
            " ORDER BY dst_record_id", (rid,))]
        return self.conjunction(parents, depth, stack + (rid,))

    def conjunction(self, members, depth, stack):
        if len(members) > self.limits["max_group_width"]:
            self.exhausted.add("max_group_width")
            return None
        unknown = False
        for rid in members:
            value = self.member(rid, depth + 1, stack)
            if value is False:
                return False
            unknown |= value is None
        return None if unknown else True

    def claim(self, rid, depth=0, stack=()):
        cache_key = (rid, depth)
        if cache_key in self.results:
            self.used["cache_hits"] += 1
            return self.results[cache_key]
        rec, complete = self.record(rid)
        if not complete:
            return self.result(rid, [], [], True, True, 0)
        if (rec is None or rec["kind"] != "CLAIM" or rid in self.retired or rid in self.purged):
            return self.result(rid, [], [], False, False, 0)
        if depth > self.limits["max_depth"] or rid in stack:
            self.exhausted.add("cycle" if rid in stack else "max_depth")
            return self.result(rid, [], [], True, True, 0)
        self.used["max_depth"] = max(self.used["max_depth"], depth)
        paths = [[], []]
        unknown = [False, False]
        count = 0
        # Select latest revision BEFORE testing retirement or target: never fall back.
        justs = self.conn.execute(
            "SELECT j.record_id FROM record j WHERE j.workspace_id=? AND j.kind='JUSTIFICATION'"
            " AND j.sequence<=? AND json_extract(j.payload,'$.target_claim_id')=?"
            " AND NOT EXISTS (SELECT 1 FROM record n WHERE n.workspace_id=j.workspace_id"
            " AND n.object_id=j.object_id AND n.sequence<=?"
            " AND (n.sequence>j.sequence OR (n.sequence=j.sequence AND n.rowid>j.rowid)))"
            " ORDER BY j.sequence,j.rowid", (self.ws, self.as_of, rid, self.as_of))
        for row in justs:
            jid = row[0]
            j, done = self.record(jid)
            if not done:
                unknown = [True, True]
                break
            if jid in self.retired or jid in self.purged:
                continue
            p = j["payload"]
            prereq = self.conjunction(p.get("rule_refs", []) + p.get("assumption_refs", []),
                                     depth, stack + (rid,))
            if prereq is False:
                continue
            for side, name in enumerate(("support_groups", "refute_groups")):
                for group in p.get(name, []):
                    if not self.spend("paths"):
                        unknown[side] = True
                        break
                    if not self.spend("recompute"):
                        unknown[side] = True
                        break
                    count += 1
                    value = self.conjunction(group, depth, stack + (rid,))
                    if value is True and prereq is True:
                        paths[side].append(list(group))
                    elif value is not False:
                        unknown[side] = True
        result = self.result(rid, *paths, *unknown, count)
        if not result["truncated"]:
            self.results[cache_key] = result
        return result

    @staticmethod
    def result(rid, supporting, refuting, unknown_s, unknown_r, count):
        s, r = bool(supporting), bool(refuting)
        incomplete = unknown_s or unknown_r
        return {"claim": rid, "four_state": "INCOMPLETE" if incomplete else FOUR_STATE[(s, r)],
                "supported": s, "refuted": r, "supporting_paths": supporting,
                "refuting_paths": refuting, "groups_evaluated": count,
                "support": "FOUND" if s else "UNKNOWN" if unknown_s else "ABSENT",
                "refutation": "FOUND" if r else "UNKNOWN" if unknown_r else "ABSENT",
                "evaluation_status": "INCOMPLETE" if incomplete else "COMPLETE",
                "truncated": incomplete}


def eligible(conn, workspace_id, record_id, as_of=None, _stack=()):
    evaluation = _Evaluation(conn, workspace_id, as_of, None)
    return evaluation.member(record_id, 0, _stack) is True


def eligible_new_task(conn, workspace_id, roots, as_of):
    """Same depth and shared budget as member(task), before its immutable insert."""
    evaluation = _Evaluation(conn, workspace_id, as_of, None)
    if not evaluation.spend("recompute"):  # the new task's own record inspection
        return False
    return evaluation.conjunction(roots, 0, ()) is True


def evaluate_support(conn, workspace_id, claim_record_id, as_of=None, budget=None, _stack=()):
    evaluation = _Evaluation(conn, workspace_id, as_of, budget)
    result = dict(evaluation.claim(claim_record_id, stack=_stack))
    roots = {root_source(conn, m) for group in result["supporting_paths"] for m in group}
    result.update(exhausted_budget=sorted(evaluation.exhausted), budget_used=dict(evaluation.used),
                  lineage_rooted_support_items=sorted(roots),
                  lineage_rooted_support_item_count=len(roots),
                  support_provenance_scope="direct_members_with_explicit_lineage_only")
    return result


def claim_four_state(conn, workspace_id, claim_record_id):
    return evaluate_support(conn, workspace_id, claim_record_id)["four_state"]

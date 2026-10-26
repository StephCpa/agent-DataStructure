"""Derived invalidation graph shared by live propagation and replay.

Justification -> target is an evaluation edge, not a stored future reference.
"""
import json
from collections import defaultdict, deque

# Frozen round-five role mapping; do not import the evolving registry.
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

LINEAGE_ROLES = {"derived_from", "extracted_from", "transcribed_from"}


def reverse_dependencies(records, refs, lineage_only=False):
    records = {r["record_id"]: r for r in records}
    graph = defaultdict(list)
    for ref in refs:
        src, dst, role = ref["src"], ref["dst"], ref["role"]
        if src not in records or dst not in records:
            continue
        if records[src]["workspace_id"] != records[dst]["workspace_id"]:
            continue
        propagates = (role in LINEAGE_ROLES if lineage_only
                      else ROLE_LAYER.get(role) in {"DERIVATION", "CONTROL"})
        if propagates:
            graph[dst].append((src, role))
    if not lineage_only:
        for rid, rec in records.items():
            if rec["kind"] != "JUSTIFICATION":
                continue
            p = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            target = p["target_claim_id"]
            if target in records and records[target]["workspace_id"] == rec["workspace_id"]:
                graph[rid].append((target, "justifies"))
    return graph


def closure(graph, roots):
    """Return record -> (predecessor, role), including each root exactly once."""
    reached = {rid: (rid, "root") for rid in roots}
    queue = deque(reached)
    while queue:
        parent = queue.popleft()
        for child, role in graph.get(parent, []):
            if child not in reached:
                reached[child] = (parent, role)
                queue.append(child)
    return reached


def load_graph(conn, workspace_id, lineage_only=False):
    records = [dict(r) for r in conn.execute(
        "SELECT * FROM record WHERE workspace_id=?", (workspace_id,))]
    refs = [dict(r) for r in conn.execute(
        "SELECT rr.src_record_id AS src, rr.dst_record_id AS dst, rr.ref_role AS role"
        " FROM record_ref rr JOIN record r ON r.record_id=rr.src_record_id"
        " WHERE r.workspace_id=?", (workspace_id,))]
    return reverse_dependencies(records, refs, lineage_only)

"""Derived invalidation graph shared by live propagation and replay.

Justification -> target is an evaluation edge, not a stored future reference.
"""
import json
from collections import defaultdict, deque

from .registry import ROLE_LAYER
from .ids import digest_obj

LINEAGE_ROLES = {"derived_from", "extracted_from", "transcribed_from"}


def reverse_dependencies(records, refs, lineage_only=False):
    records = {r["record_id"]: r for r in records}
    latest = {}
    for rid, rec in records.items():
        latest[(rec["workspace_id"], rec["object_id"])] = rid
    active_justs = {rid for rid, rec in records.items() if rec["kind"] == "JUSTIFICATION"
                   and latest[(rec["workspace_id"], rec["object_id"])] == rid}
    graph = defaultdict(list)
    for ref in refs:
        src, dst, role = ref["src"], ref["dst"], ref["role"]
        if src not in records or dst not in records:
            continue
        if records[src]["workspace_id"] != records[dst]["workspace_id"]:
            continue
        if not lineage_only and (role in {"supersedes", "prior_revision"}
                or (records[src]["kind"] == "JUSTIFICATION" and src not in active_justs)):
            continue
        propagates = (role in LINEAGE_ROLES if lineage_only
                      else ROLE_LAYER.get(role) in {"DERIVATION", "CONTROL"})
        if propagates:
            graph[dst].append((src, role))
    if not lineage_only:
        for rid, rec in records.items():
            if rid not in active_justs:
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


def load_graph(conn, workspace_id, lineage_only=False, metrics=None):
    records, refs = load_inputs(conn, workspace_id, metrics)
    return reverse_dependencies(records, refs, lineage_only)


def load_inputs(conn, workspace_id, metrics=None):
    records = [dict(r) for r in conn.execute(
        "SELECT * FROM record WHERE workspace_id=? ORDER BY sequence,rowid", (workspace_id,))]
    refs = [dict(r) for r in conn.execute(
        "SELECT rr.src_record_id AS src, rr.dst_record_id AS dst, rr.ref_role AS role"
        " FROM record_ref rr JOIN record r ON r.record_id=rr.src_record_id"
        " WHERE r.workspace_id=?", (workspace_id,))]
    if metrics is not None:
        metrics["records_scanned"] = metrics.get("records_scanned", 0) + len(records)
        metrics["refs_scanned"] = metrics.get("refs_scanned", 0) + len(refs)
    return records, refs


def dependency_stamp(records, refs, roots):
    """A pure version/change stamp, NOT an eligibility or truth oracle.

    Includes active argument revisions and retire events for reachable inputs.
    Historical invalidation does not change this stamp again on an empty drain.
    """
    graph = reverse_dependencies(records, refs)
    parents = defaultdict(list)
    for parent, children in graph.items():
        for child, role in children:
            parents[child].append((parent, role))
    relevant = closure(parents, roots)
    retired = set()
    by_id = {r["record_id"]: r for r in records}
    for rec in records:
        if rec["kind"] == "RETIRE_PURGE_MARK":
            p = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            retired.add(p["target"]["record_id"])
    content = []
    for rid in sorted(relevant):
        rec = by_id.get(rid)
        if rec is None:
            content.append([rid, "missing"])
        else:
            p = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            content.append([rid, digest_obj(p), rid in retired])
    return digest_obj(content)


def current_stamp(conn, workspace_id, roots, metrics=None):
    return dependency_stamp(*load_inputs(conn, workspace_id, metrics), roots)

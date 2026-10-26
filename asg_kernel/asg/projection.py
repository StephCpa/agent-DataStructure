"""投影与确定性回放（M7）—— 《详细设计说明书》7.9 节与 2.4 节映射。

reducer 为固定版本的纯函数：只消费已记录的输入，不调用模型与外部工具。
回放结果与在线投影逐字段比对即 TC-INV-05。
"""
from __future__ import annotations

import json
import sqlite3

from .dependencies import closure, reverse_dependencies, dependency_stamp


def reducer_v3(records: list[dict], refs: list[dict] | None = None) -> dict:
    """reducer 版本 3：按提交序重放 object_heads 与 task_state。

    refs 为引用边（src/role/dst）；失效标记记录（RETIRE_PURGE_MARK）经 depends_on
    边把依赖任务重建为 PENDING_REEVALUATION（评审反例：在线 PENDING_REEVALUATION、
    回放仍 ACTIVE）。在线传播与回放使用同一规则。
    """
    heads: dict[str, dict] = {}
    tasks: dict[str, dict] = {}
    record_by_id: dict[str, dict] = {}
    for rec in records:
        ws, obj, rid, seq = rec["workspace_id"], rec["object_id"], rec["record_id"], rec["sequence"]
        record_by_id[rid] = rec
        cur = heads.get((ws, obj))
        if cur is None or seq >= cur["head_sequence"]:
            heads[(ws, obj)] = {"head_record_id": rid, "head_sequence": seq}
        if rec["kind"] == "TASK_REVISION":
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            task_obj = rec["object_id"]
            if payload["task_object_id"] != task_obj:
                raise ValueError("Task identity mismatch in replay input")
            prev = tasks.get((ws, task_obj), {"status": "DRAFT"})
            status = payload.get("_validation", {}).get("status") or (
                "ACTIVE" if prev["status"] in ("DRAFT",) else prev["status"])
            tasks[(ws, task_obj)] = {"status": status, "current_revision": rid}
    # 失效标记重建任务重评：标记目标命中任务依赖闭包即进入待重评
    retired = set()
    for rec in records:
        if rec["kind"] == "RETIRE_PURGE_MARK":
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            retired.add(payload["target"]["record_id"])
    affected = closure(reverse_dependencies(records, refs or []), retired)
    for rid in affected:
        rec = record_by_id[rid]
        if rec["kind"] == "TASK_REVISION":
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            st = tasks[(rec["workspace_id"], payload["task_object_id"])]
            current = record_by_id[st["current_revision"]]
            cp = json.loads(current["payload"]) if isinstance(current["payload"], str) else current["payload"]
            if "_validation" in cp:
                continue
            if st["status"] == "ACTIVE":
                st["status"] = "PENDING_REEVALUATION"
    for st in tasks.values():
        rec = record_by_id[st["current_revision"]]
        p = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
        validation = p.get("_validation")
        if validation and (rec["record_id"] in retired or
                           validation["stamp"] != dependency_stamp(records, refs or [], validation["roots"])):
            st["status"] = "PENDING_REEVALUATION"
    return {"object_heads": {f"{k[0]}:{k[1]}": v for k, v in heads.items()},
            "task_state": {f"{k[0]}:{k[1]}": v for k, v in tasks.items()}}


def reducer_v2(records: list[dict], refs: list[dict] | None = None) -> dict:
    """reducer 版本 2：按提交序重放 object_heads 与 task_state。

    refs 为引用边（src/role/dst）；失效标记记录（RETIRE_PURGE_MARK）经 depends_on
    边把依赖任务重建为 PENDING_REEVALUATION（评审反例：在线 PENDING_REEVALUATION、
    回放仍 ACTIVE）。在线传播与回放使用同一规则。
    """
    heads: dict[str, dict] = {}
    tasks: dict[str, dict] = {}
    record_by_id: dict[str, dict] = {}
    for rec in records:
        ws, obj, rid, seq = rec["workspace_id"], rec["object_id"], rec["record_id"], rec["sequence"]
        record_by_id[rid] = rec
        cur = heads.get((ws, obj))
        if cur is None or seq >= cur["head_sequence"]:
            heads[(ws, obj)] = {"head_record_id": rid, "head_sequence": seq}
        if rec["kind"] == "TASK_REVISION":
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            task_obj = payload["task_object_id"]
            prev = tasks.get((ws, task_obj), {"status": "DRAFT"})
            status = payload.get("_validation", {}).get("status") or (
                "ACTIVE" if prev["status"] in ("DRAFT",) else prev["status"])
            tasks[(ws, task_obj)] = {"status": status, "current_revision": rid}
    # 失效标记重建任务重评：标记目标命中任务依赖闭包即进入待重评
    retired = set()
    for rec in records:
        if rec["kind"] == "RETIRE_PURGE_MARK":
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            retired.add(payload["target"]["record_id"])
    affected = closure(reverse_dependencies(records, refs or []), retired)
    for rid in affected:
        rec = record_by_id[rid]
        if rec["kind"] == "TASK_REVISION":
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            st = tasks[(rec["workspace_id"], payload["task_object_id"])]
            current = record_by_id[st["current_revision"]]
            cp = json.loads(current["payload"]) if isinstance(current["payload"], str) else current["payload"]
            if "_validation" in cp:
                continue
            if st["status"] == "ACTIVE":
                st["status"] = "PENDING_REEVALUATION"
    for st in tasks.values():
        rec = record_by_id[st["current_revision"]]
        p = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
        validation = p.get("_validation")
        if validation and validation["stamp"] != dependency_stamp(records, refs or [], validation["roots"]):
            st["status"] = "PENDING_REEVALUATION"
    return {"object_heads": {f"{k[0]}:{k[1]}": v for k, v in heads.items()},
            "task_state": {f"{k[0]}:{k[1]}": v for k, v in tasks.items()}}


def reducer_v1(records: list[dict], refs: list[dict] | None = None) -> dict:
    """reducer 版本 1：按提交序重放 object_heads 与 task_state。

    refs 为引用边（src/role/dst）；失效标记记录（RETIRE_PURGE_MARK）经 depends_on
    边把依赖任务重建为 PENDING_REEVALUATION（评审反例：在线 PENDING_REEVALUATION、
    回放仍 ACTIVE）。在线传播与回放使用同一规则。
    """
    from .legacy_dependencies_v1 import closure, reverse_dependencies

    heads: dict[str, dict] = {}
    tasks: dict[str, dict] = {}
    record_by_id: dict[str, dict] = {}
    for rec in records:
        ws, obj, rid, seq = rec["workspace_id"], rec["object_id"], rec["record_id"], rec["sequence"]
        record_by_id[rid] = rec
        cur = heads.get((ws, obj))
        if cur is None or seq >= cur["head_sequence"]:
            heads[(ws, obj)] = {"head_record_id": rid, "head_sequence": seq}
        if rec["kind"] == "TASK_REVISION":
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            task_obj = payload["task_object_id"]
            prev = tasks.get((ws, task_obj), {"status": "DRAFT"})
            status = "ACTIVE" if prev["status"] in ("DRAFT",) else prev["status"]
            tasks[(ws, task_obj)] = {"status": status, "current_revision": rid}
    # 失效标记重建任务重评：标记目标命中任务依赖闭包即进入待重评
    retired = set()
    for rec in records:
        if rec["kind"] == "RETIRE_PURGE_MARK":
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            retired.add(payload["target"]["record_id"])
    affected = closure(reverse_dependencies(records, refs or []), retired)
    for rid in affected:
        rec = record_by_id[rid]
        if rec["kind"] == "TASK_REVISION":
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            st = tasks[(rec["workspace_id"], payload["task_object_id"])]
            if st["status"] == "ACTIVE":
                st["status"] = "PENDING_REEVALUATION"
    return {"object_heads": {f"{k[0]}:{k[1]}": v for k, v in heads.items()},
            "task_state": {f"{k[0]}:{k[1]}": v for k, v in tasks.items()}}


class Projection:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def replay(self, workspace_id: str, upto_sequence: int | None = None,
               reducer_version: int = 3) -> dict:
        """确定性回放（7.9 节）：按序应用已记录输入重建投影。"""
        q = ("SELECT * FROM record WHERE workspace_id=? ORDER BY sequence, rowid")
        rows = self.conn.execute(q, (workspace_id,)).fetchall()
        recs = []
        for r in rows:
            if upto_sequence is not None and r["sequence"] > upto_sequence:
                continue
            recs.append({
                "record_id": r["record_id"], "object_id": r["object_id"],
                "workspace_id": r["workspace_id"], "kind": r["kind"],
                "payload": r["payload"], "sequence": r["sequence"],
            })
        refs = [{"src": r["src_record_id"], "role": r["ref_role"], "dst": r["dst_record_id"]}
                for r in self.conn.execute(
                    "SELECT rr.src_record_id, rr.ref_role, rr.dst_record_id FROM record_ref rr"
                    " JOIN record r ON r.record_id = rr.src_record_id"
                    " WHERE r.workspace_id=?", (workspace_id,)).fetchall()
                if upto_sequence is None or self._seq(r["src_record_id"]) <= upto_sequence]
        if reducer_version not in (1, 2, 3):
            raise ValueError("Unsupported reducer version")
        return {1: reducer_v1, 2: reducer_v2, 3: reducer_v3}[reducer_version](recs, refs)

    def _seq(self, record_id: str) -> int:
        row = self.conn.execute("SELECT sequence FROM record WHERE record_id=?",
                                (record_id,)).fetchone()
        return row["sequence"] if row else 0

    def live_object_heads(self, workspace_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT object_id, head_record_id, head_sequence FROM object_heads WHERE workspace_id=?",
            (workspace_id,),
        ).fetchall()
        return {f"{workspace_id}:{r['object_id']}":
                {"head_record_id": r["head_record_id"], "head_sequence": r["head_sequence"]}
                for r in rows}

    def live_task_state(self, workspace_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT task_object_id, status, current_revision FROM task_state WHERE workspace_id=?",
            (workspace_id,),
        ).fetchall()
        return {f"{workspace_id}:{r['task_object_id']}":
                {"status": r["status"], "current_revision": r["current_revision"]}
                for r in rows}

    def compare_with_live(self, workspace_id: str) -> dict:
        """TC-INV-05：回放投影与在线投影比对（task_state 的 DRAFT 初始态不计差异）。"""
        replayed = self.replay(workspace_id)
        live_heads = self.live_object_heads(workspace_id)
        live_tasks = self.live_task_state(workspace_id)
        return {
            "heads_match": replayed["object_heads"] == live_heads,
            "tasks_match": replayed["task_state"] == live_tasks,
            "replayed": replayed,
            "live": {"object_heads": live_heads, "task_state": live_tasks},
        }

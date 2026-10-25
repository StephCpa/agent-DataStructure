"""投影与确定性回放（M7）—— 《详细设计说明书》7.9 节与 2.4 节映射。

reducer 为固定版本的纯函数：只消费已记录的输入，不调用模型与外部工具。
回放结果与在线投影逐字段比对即 TC-INV-05。
"""
from __future__ import annotations

import json
import sqlite3


def reducer_v1(records: list[dict]) -> dict:
    """reducer 版本 1：按提交序重放 object_heads 与 task_state 的当前值。

    输入记录须已按 (sequence, 批内顺序) 排序；输出投影字典。
    """
    heads: dict[str, dict] = {}
    tasks: dict[str, dict] = {}
    for rec in records:
        ws, obj, rid, seq = rec["workspace_id"], rec["object_id"], rec["record_id"], rec["sequence"]
        cur = heads.get((ws, obj))
        if cur is None or seq >= cur["head_sequence"]:
            heads[(ws, obj)] = {"head_record_id": rid, "head_sequence": seq}
        if rec["kind"] == "TASK_REVISION":
            payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
            task_obj = payload["task_object_id"]
            prev = tasks.get((ws, task_obj), {"status": "DRAFT"})
            status = "ACTIVE" if prev["status"] in ("DRAFT",) else prev["status"]
            tasks[(ws, task_obj)] = {"status": status, "current_revision": rid}
    return {"object_heads": {f"{k[0]}:{k[1]}": v for k, v in heads.items()},
            "task_state": {f"{k[0]}:{k[1]}": v for k, v in tasks.items()}}


class Projection:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def replay(self, workspace_id: str, upto_sequence: int | None = None) -> dict:
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
        return reducer_v1(recs)

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

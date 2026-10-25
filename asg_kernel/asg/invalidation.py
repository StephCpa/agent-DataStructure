"""失效传播与读取闸门（M3/M6）—— 《详细设计说明书》7.3 节。

事务部分（标记记录、推进 validation_epoch、入队）在写入服务 invalidate 中完成；
本模块实现异步传播工作者与关键读取闸门：
- 反向依赖闭包：沿 DERIVATION 与 CONTROL 边向上游传播（SEMANTIC 边不触发失效）；
- 受影响的 Claim 标记重算、TaskRevision 进入 PENDING_REEVALUATION、清单标记过期；
- index_watermark 记录 applied_sequence 与 validation_epoch；
- read_gate：关键读取/动作准入发现投影落后时拒绝（E401）；探索检索显式标注陈旧。

边界：全局 epoch 只解决已收到的更新；外部世界尚未通知的变化依赖过期策略与主动复查。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .errors import AsgError
from .storage import write_txn

PROJECTION_INDEX = "projection"
PROPAGATING_LAYERS = ("DERIVATION", "CONTROL")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _epoch(conn, workspace_id: str) -> int:
    row = conn.execute(
        "SELECT validation_epoch FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    return row["validation_epoch"] if row else 1


def _head(conn, workspace_id: str) -> int:
    row = conn.execute(
        "SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    return row["head_sequence"] if row else 0


def process_queue(conn: sqlite3.Connection, workspace_id: str, max_items: int = 32) -> int:
    """传播工作者：逐条处理失效队列，按反向依赖闭包打标；队列清空后推进水位。"""
    processed = 0
    while processed < max_items:
        with write_txn(conn) as tx:
            item = tx.execute(
                "SELECT * FROM invalidation_queue WHERE workspace_id=? AND status='PENDING'"
                " ORDER BY item_id LIMIT 1", (workspace_id,),
            ).fetchone()
            if item is None:
                _advance_watermark(tx, workspace_id)
                break
            tx.execute("UPDATE invalidation_queue SET status='PROCESSING' WHERE item_id=?",
                       (item["item_id"],))
            _propagate_closure(tx, workspace_id, item["root_record_id"], item["epoch"])
            tx.execute("UPDATE invalidation_queue SET status='DONE' WHERE item_id=?",
                       (item["item_id"],))
            processed += 1
    return processed


def _propagate_closure(tx, workspace_id: str, root_record_id: str, epoch: int) -> int:
    """反向依赖闭包：root 自身与全部上游依赖者打标（via 为其直接依赖的下游记录）。"""
    visited = {root_record_id}
    frontier = [root_record_id]
    affected = 0
    while frontier:
        q = ", ".join("?" * len(frontier))
        rows = tx.execute(
            f"SELECT src_record_id, dst_record_id, ref_role FROM record_ref"
            f" WHERE dst_record_id IN ({q}) AND ref_layer IN {PROPAGATING_LAYERS}",
            tuple(frontier),
        ).fetchall()
        next_frontier = []
        for r in rows:
            src = r["src_record_id"]
            if src in visited:
                continue
            visited.add(src)
            next_frontier.append(src)
            tx.execute(
                "INSERT OR IGNORE INTO invalidation_mark (workspace_id, record_id,"
                " via_record_id, path_role, epoch) VALUES (?,?,?,?,?)",
                (workspace_id, src, r["dst_record_id"], r["ref_role"], epoch),
            )
            affected += 1
            kind = tx.execute("SELECT kind FROM record WHERE record_id=?", (src,)).fetchone()
            if kind is None:
                continue
            if kind["kind"] == "TASK_REVISION":
                # 任务前提命中失效：进入待重评（6.5 节）
                tx.execute(
                    "UPDATE task_state SET status='PENDING_REEVALUATION',"
                    " reevaluation_reason=? WHERE workspace_id=? AND task_object_id IN"
                    " (SELECT object_id FROM record WHERE record_id=?)"
                    " AND status IN ('DRAFT','ACTIVE')",
                    (f"invalidated_via:{root_record_id}", workspace_id, src),
                )
            # CLAIM / JUSTIFICATION / CONTEXT_MANIFEST 命中标记即可：
            # 求值按成员资格精确重算（7.2 节），清单读取按 mark 判过期
        frontier = next_frontier
    return affected


def _advance_watermark(tx, workspace_id: str) -> None:
    """队列清空后推进投影水位：applied_sequence 与 validation_epoch（4.2 节）。"""
    pending = tx.execute(
        "SELECT COUNT(*) c FROM invalidation_queue WHERE workspace_id=? AND status='PENDING'",
        (workspace_id,),
    ).fetchone()["c"]
    if pending:
        return
    tx.execute(
        "INSERT INTO index_watermark (index_name, applied_sequence, validation_epoch, updated_at)"
        " VALUES (?,?,?,?)"
        " ON CONFLICT(index_name) DO UPDATE SET applied_sequence=excluded.applied_sequence,"
        " validation_epoch=excluded.validation_epoch, updated_at=excluded.updated_at",
        (PROJECTION_INDEX, _head(tx, workspace_id), _epoch(tx, workspace_id), _now()),
    )


def watermark(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM index_watermark WHERE index_name=?", (PROJECTION_INDEX,)
    ).fetchone()
    if row is None:
        return None
    return {"applied_sequence": row["applied_sequence"],
            "validation_epoch": row["validation_epoch"]}


def read_gate(conn: sqlite3.Connection, workspace_id: str, deps: list[str],
              purpose: str = "critical") -> dict:
    """读取闸门（7.3 节）：critical 在投影落后或前提直接命中时拒绝（E401）；
    exploratory 返回显式陈旧标注，不阻断。"""
    pending_roots = [r["root_record_id"] for r in conn.execute(
        "SELECT root_record_id FROM invalidation_queue WHERE workspace_id=? AND status='PENDING'",
        (workspace_id,),
    ).fetchall()]
    hit = sorted(set(deps) & set(pending_roots)) if deps else []
    ws_epoch = _epoch(conn, workspace_id)
    wm = watermark(conn)
    wm_epoch = wm["validation_epoch"] if wm else 1

    if purpose == "critical":
        if hit:
            raise AsgError("E401", "前提命中未处理的失效根，待重评", details={"stale": hit})
        if pending_roots:
            # 保守规则：传播未完成时，关键读取不把当前投影当作已验证前提
            raise AsgError("E401", "失效传播尚未完成，关键读取被闸门阻塞（等待或闭包重验）",
                           details={"pending_roots": pending_roots[:8]})
        if ws_epoch > wm_epoch:
            raise AsgError("E401", "投影水位落后于验证纪元",
                           details={"watermark_epoch": wm_epoch, "workspace_epoch": ws_epoch})
        return {"stale": False}
    return {"stale": bool(hit or pending_roots or ws_epoch > wm_epoch),
            "reason": "invalidation_pending" if (hit or pending_roots) else "watermark_behind"
            if ws_epoch > wm_epoch else None}


def marks_on(conn: sqlite3.Connection, workspace_id: str, record_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM invalidation_mark WHERE workspace_id=? AND record_id=?",
        (workspace_id, record_id),
    ).fetchall()]

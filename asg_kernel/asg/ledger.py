"""预算账本与资源预留（M1/M2 共用）—— 《详细设计说明书》7.5 节。

I5：预留加已确认消耗不超额度。CHECK 约束是最后防线，正常路径是
写事务内先查余量再更新（研究报告 8.3 节反例：逐对象 CAS 会超额）。

reserve_in_txn / settle_in_txn 供网关在 prepare_effect 的同一事务内组合调用
（5.5 节：追加意图、预留、outbox 同一数据库事务）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .errors import AsgError
from .ids import uuid7
from .storage import write_txn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def reserve_in_txn(conn: sqlite3.Connection, workspace_id: str, intent_id: str,
                   asks: list[dict]) -> list[str]:
    """原子预留（7.5 节）：全部成功或全部失败；失败可解释（E501 附余额与占用明细）。"""
    reservation_ids = []
    for ask in sorted(asks, key=lambda a: a["resource_key"]):
        key, amount = ask["resource_key"], float(ask["amount"])
        row = conn.execute(
            "SELECT * FROM budget_ledger WHERE resource_key=?", (key,)
        ).fetchone()
        if row is None:
            raise AsgError("E501", f"预算账本不存在: {key}", details={"resource_key": key})
        remaining = row["limit_amount"] - row["reserved_amount"] - row["committed_amount"]
        if remaining < amount:
            raise AsgError(
                "E501", "资源余量不足",
                details={
                    "resource_key": key,
                    "limit": row["limit_amount"],
                    "reserved": row["reserved_amount"],
                    "committed": row["committed_amount"],
                    "remaining": remaining,
                    "requested": amount,
                },
            )
        conn.execute(
            "UPDATE budget_ledger SET reserved_amount = reserved_amount + ? WHERE resource_key=?",
            (amount, key),
        )
        rid = uuid7()
        conn.execute(
            "INSERT INTO reservation (reservation_id, workspace_id, intent_id,"
            " resource_key, amount, state, created_at) VALUES (?,?,?,?,?, 'reserved', ?)",
            (rid, workspace_id, intent_id, key, amount, _now()),
        )
        reservation_ids.append(rid)
    return reservation_ids


def settle_in_txn(conn: sqlite3.Connection, reservation_id: str, outcome: str) -> str:
    """结算（7.5 节 / 6.3 节状态机）：
    SUCCEEDED->committed；NOT_APPLIED（释放守卫成立）->released；UNKNOWN->uncertain 保留占用。
    """
    r = conn.execute(
        "SELECT * FROM reservation WHERE reservation_id=?", (reservation_id,)
    ).fetchone()
    if r is None:
        raise AsgError("E502", f"预留不存在: {reservation_id}")
    if r["state"] not in ("reserved", "uncertain"):
        raise AsgError("E502", f"预留已结算于 {r['state']}")
    if outcome == "SUCCEEDED":
        conn.execute(
            "UPDATE reservation SET state='committed', settled_at=? WHERE reservation_id=?",
            (_now(), reservation_id),
        )
        conn.execute(
            "UPDATE budget_ledger SET reserved_amount = reserved_amount - ?,"
            " committed_amount = committed_amount + ? WHERE resource_key=?",
            (r["amount"], r["amount"], r["resource_key"]),
        )
        return "committed"
    if outcome == "NOT_APPLIED":
        # 释放守卫（阶段 B 起由网关聚合规则保证：全部可能送达的尝试均确认未生效且无在途/未知尝试）
        conn.execute(
            "UPDATE reservation SET state='released', settled_at=? WHERE reservation_id=?",
            (_now(), reservation_id),
        )
        conn.execute(
            "UPDATE budget_ledger SET reserved_amount = reserved_amount - ? WHERE resource_key=?",
            (r["amount"], r["resource_key"]),
        )
        return "released"
    if outcome == "UNKNOWN":
        conn.execute(
            "UPDATE reservation SET state='uncertain' WHERE reservation_id=?",
            (reservation_id,),
        )
        return "uncertain"
    raise AsgError("E101", f"未知结算结果: {outcome}")


class Ledger:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def ensure_budget(self, resource_key: str, limit_amount: float) -> None:
        with write_txn(self.conn) as conn:
            conn.execute(
                "INSERT INTO budget_ledger (resource_key, limit_amount) VALUES (?,?)"
                " ON CONFLICT(resource_key) DO NOTHING",
                (resource_key, limit_amount),
            )

    def reserve(self, workspace_id: str, intent_id: str, asks: list[dict]) -> list[str]:
        with write_txn(self.conn) as conn:
            return reserve_in_txn(conn, workspace_id, intent_id, asks)

    def settle(self, reservation_id: str, outcome: str) -> str:
        with write_txn(self.conn) as conn:
            return settle_in_txn(conn, reservation_id, outcome)

    def balance(self, resource_key: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM budget_ledger WHERE resource_key=?", (resource_key,)
        ).fetchone()
        if row is None:
            raise AsgError("E501", f"预算账本不存在: {resource_key}")
        return {
            "resource_key": resource_key,
            "limit": row["limit_amount"],
            "reserved": row["reserved_amount"],
            "committed": row["committed_amount"],
            "remaining": row["limit_amount"] - row["reserved_amount"] - row["committed_amount"],
        }

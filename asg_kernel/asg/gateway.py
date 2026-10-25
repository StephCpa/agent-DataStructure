"""执行网关（M2）—— 《详细设计说明书》5.5 / 5.6 / 7.6 / 7.7 节与 6.1 节状态机。

关键约束：
- 准备意图、预算预留、授权占用、outbox 写入在同一数据库事务内完成（5.5 节）；
- 网关先记录 attempt、再外呼；外部调用一律在事务外（7.6 节）；
- 租约到期只说明本地持有者失联，不证明远端未执行：重领取先对账，不盲目重发；
- 已确认成功不被迟到回执覆盖；回执冲突保留并对账（E605）；
- 无幂等契约的自动重发默认暂停（E604）；键过期不得声称重试安全（E603）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .authorize import compute_operation_digest
from .invalidation import read_gate
from .errors import AsgError
from .ids import uuid7
from .ledger import reserve_in_txn, settle_in_txn
from .storage import write_txn

TERMINAL_STATES = {"CONFIRMED_SUCCEEDED", "CONFIRMED_NOT_APPLIED", "CANCELLED_BEFORE_DISPATCH"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class ConnectorContract:
    """连接器能力契约（5.6 节 / 6.2 节）：未声明的能力不得被运行时假设。"""
    mode: str = "NONE"                       # KEYED / QUERY_ONLY / NONE
    supports_job_query: bool = False
    final_query_guarantee: bool = False      # 查询覆盖范围、可见性与最终性


class Connector:
    """连接器接口的最小实现基类；测试与真实适配器继承并覆写。"""

    def __init__(self, contract: ConnectorContract):
        self.contract = contract

    def call(self, operation: str, arguments: dict, idempotency_key: str | None) -> dict | None:
        raise NotImplementedError

    def query_by_key(self, key: str) -> dict | None:
        return None

    def query_job(self, job_id: str) -> dict | None:
        return None


def _head(conn, workspace_id: str) -> int:
    row = conn.execute(
        "SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    return row["head_sequence"] if row else 0


def _epoch(conn, workspace_id: str) -> int:
    row = conn.execute(
        "SELECT validation_epoch FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    return row["validation_epoch"] if row else 1


def _load_intent(conn, workspace_id: str, intent_id: str) -> tuple[dict, str]:
    """读取意图最新版本的载荷与 record_id（记录不可变，最新即唯一）。"""
    row = conn.execute(
        "SELECT record_id, payload FROM record WHERE workspace_id=? AND object_id=?"
        " AND kind='ACTION_INTENT' ORDER BY sequence DESC, rowid DESC LIMIT 1",
        (workspace_id, intent_id),
    ).fetchone()
    if row is None:
        raise AsgError("E502", f"意图不存在: {intent_id}")
    return json.loads(row["payload"]), row["record_id"]


class Gateway:
    def __init__(self, conn: sqlite3.Connection, write_service, authorizer,
                 principal: dict | None = None):
        self.conn = conn
        self.svc = write_service
        self.auth = authorizer
        self.principal = principal or {"kind": "service", "id": "effect-gateway"}

    # ── 5.5 prepare_effect：意图准备（单事务） ──────────
    def prepare_effect(self, workspace_id: str, intent_payload: dict, expected_head: int,
                       request_id: str, grant_id: str, asks: list[dict],
                       principal: dict | None = None) -> dict:
        intent_id = intent_payload["intent_id"]
        grant_row = None
        with write_txn(self.conn) as conn:
            self.auth.check_grant(conn, grant_id, intent_payload, "prepare")
            self._check_preconditions(conn, workspace_id, intent_payload)
            self.auth.consume_use(conn, grant_id, intent_id)
            reservation_ids = reserve_in_txn(conn, workspace_id, intent_id, asks)

            grant_row = conn.execute(
                "SELECT grant_record_id FROM grant_record WHERE grant_id=?", (grant_id,)
            ).fetchone()
            refs = [{"role": "authorized_by", "target_record_id": grant_row["grant_record_id"]}]
            refs += [{"role": "uses_version", "target_record_id": p}
                     for p in intent_payload.get("precondition_refs", [])]
            record = {
                "workspace_id": workspace_id,
                "object_id": intent_id,
                "kind": "ACTION_INTENT",
                "schema_version": 1,
                "authenticated_writer": principal or self.principal,
                "policy_domain": "default",
                "payload": intent_payload,
                "typed_refs": refs,
                "prior_revision_ids": [],
            }
            result = self.svc.commit_in_txn(conn, workspace_id, "main", [record],
                                            expected_head, request_id)
            conn.execute(
                "INSERT INTO effect_state (workspace_id, intent_id, state, updated_epoch)"
                " VALUES (?,?, 'PREPARED', ?)"
                " ON CONFLICT(workspace_id, intent_id) DO UPDATE SET"
                " state='PREPARED', updated_epoch=excluded.updated_epoch, unresolved_reason=NULL",
                (workspace_id, intent_id, _epoch(conn, workspace_id)),
            )
            conn.execute(
                "INSERT INTO effect_outbox (outbox_id, intent_id, state, next_retry_at, asks_json)"
                " VALUES (?,?, 'PENDING', ?, ?)",
                ("obx-" + uuid7(), intent_id, _now(), json.dumps(asks)),
            )
        return {"intent_id": intent_id, "commit_id": result["commit_id"],
                "sequence": result["sequence"], "outbox_state": "PENDING",
                "effect_state": "PREPARED", "reservations": reservation_ids}

    def _check_preconditions(self, conn, workspace_id: str, intent_payload: dict) -> None:
        """E401 闸门（7.3 节 read_gate：critical 目的，投影落后或前提命中即拒绝）。"""
        preconds = intent_payload.get("precondition_refs", [])
        if not preconds:
            return
        read_gate(conn, workspace_id, preconds, purpose="critical")

    # ── 7.6 outbox：领取 / 尝试 / 回执 / 聚合 ───────────
    def claim(self, intent_id: str | None = None, lease_seconds: float = 30.0) -> dict | None:
        with write_txn(self.conn) as conn:
            now = _now()
            if intent_id is not None:
                row = conn.execute(
                    "SELECT o.intent_id, e.workspace_id FROM effect_outbox o"
                    " JOIN effect_state e ON e.intent_id = o.intent_id"
                    " WHERE o.intent_id=? AND ((o.state='PENDING' AND o.next_retry_at<=?)"
                    " OR (o.state='CLAIMED' AND o.claim_expires_at<=?))",
                    (intent_id, now, now),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT o.intent_id, e.workspace_id FROM effect_outbox o"
                    " JOIN effect_state e ON e.intent_id = o.intent_id"
                    " WHERE (o.state='PENDING' AND o.next_retry_at<=?)"
                    " OR (o.state='CLAIMED' AND o.claim_expires_at<=?)"
                    " ORDER BY o.next_retry_at LIMIT 1",
                    (now, now),
                ).fetchone()
            if row is None:
                return None
            token = uuid7()
            conn.execute(
                "UPDATE effect_outbox SET state='CLAIMED', claim_token=?, claim_expires_at=?"
                " WHERE intent_id=?",
                (token, _now_after(lease_seconds), row["intent_id"]),
            )
            return {"intent_id": row["intent_id"], "workspace_id": row["workspace_id"],
                    "token": token}

    def begin_attempt(self, workspace_id: str, intent_id: str, token: str) -> dict:
        """重验证 + 先记录 ATTEMPT 再外呼（6.2 节：崩溃窗口可识别为 UNKNOWN）。

        契约违反（E603/E604）时先在事务内落 ABANDONED 与 UNKNOWN 状态并提交，
        事务外再抛错——若在事务内抛出，回滚会撤销放弃标记。
        """
        abort_error: AsgError | None = None
        result = None
        attempt_id = None
        mode = None
        contract = None
        with write_txn(self.conn) as conn:
            ob = conn.execute(
                "SELECT * FROM effect_outbox WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if (ob is None or ob["claim_token"] != token or ob["state"] != "CLAIMED"
                    or ob["claim_expires_at"] <= _now()):
                raise AsgError("E201", "租约失效或令牌不匹配，重新领取后再试", retryable=True)

            payload, intent_record_id = _load_intent(conn, workspace_id, intent_id)
            grant_id = self._grant_of(conn, intent_record_id)

            # dispatch 时重验证（与 prepare 不同的检查点：此刻的最新性，6.4 节）
            self.auth.check_grant(conn, grant_id, payload, "dispatch")
            self._check_preconditions(conn, workspace_id, payload)

            # 幂等契约闸门（6.2 节）
            prior = conn.execute(
                "SELECT * FROM attempt WHERE intent_id=?", (intent_id,)
            ).fetchall()
            contract = payload.get("idempotency_contract", {})
            mode = contract.get("mode", "NONE")
            if prior and mode == "NONE":
                self._abandon_in_txn(conn, workspace_id, intent_id,
                                     "E604 无幂等契约，暂停自动重发")
                abort_error = AsgError("E604", "存在先前尝试且连接器无幂等契约，默认暂停自动重发")
            elif prior and mode == "KEYED" and contract.get("key_expiry") and \
                    contract["key_expiry"] <= _now():
                self._abandon_in_txn(conn, workspace_id, intent_id,
                                     "E603 幂等键已过期，不能声称重试安全")
                abort_error = AsgError("E603", "幂等键已过期或参数改变，不能继续声称重试安全")

            if abort_error is None:
                # 预留重验证；重试场景下若已释放则按快照重预留（释放守卫已由对账保证）
                active = conn.execute(
                    "SELECT COUNT(*) c FROM reservation WHERE intent_id=?"
                    " AND state IN ('reserved','uncertain')", (intent_id,),
                ).fetchone()["c"]
                if active == 0:
                    asks = json.loads(ob["asks_json"]) if ob["asks_json"] else []
                    if not asks:
                        raise AsgError("E502", "意图无有效预留且无预留快照")
                    reserve_in_txn(conn, workspace_id, intent_id, asks)

                attempt_id = "att-" + uuid7()
                started_at = _now()
                digest = compute_operation_digest(payload)
                contract_state = {k: contract.get(k)
                                  for k in ("mode", "key", "key_scope", "key_expiry")}
                att_payload = {
                    "attempt_id": attempt_id, "intent_id": intent_id,
                    "gateway_principal": self.principal, "started_at": started_at,
                    "dispatch_digest": digest, "idempotency_key_state": contract_state,
                    "external_job_id": None,
                }
                att_record = {
                    "workspace_id": workspace_id, "object_id": attempt_id, "kind": "ATTEMPT",
                    "schema_version": 1, "authenticated_writer": self.principal,
                    "policy_domain": "default", "payload": att_payload,
                    "typed_refs": [{"role": "attempt_of", "target_record_id": intent_record_id}],
                    "prior_revision_ids": [],
                }
                head = _head(conn, workspace_id)
                result = self.svc.commit_in_txn(conn, workspace_id, "main", [att_record],
                                                head, "req-" + uuid7())
                conn.execute(
                    "INSERT INTO attempt (attempt_id, intent_id, gateway_principal, started_at,"
                    " dispatch_digest, idempotency_key_state, external_job_id, attempt_status,"
                    " attempt_record_id) VALUES (?,?,?,?,?,?,NULL,'STARTED',?)",
                    (attempt_id, intent_id, json.dumps(self.principal), started_at, digest,
                     json.dumps(contract_state), result["record_ids"][0]),
                )
                conn.execute(
                    "UPDATE effect_state SET state='IN_FLIGHT', updated_epoch=?"
                    " WHERE workspace_id=? AND intent_id=?",
                    (_epoch(conn, workspace_id), workspace_id, intent_id),
                )
                conn.execute(
                    "UPDATE effect_outbox SET attempts_count = attempts_count + 1"
                    " WHERE intent_id=?", (intent_id,),
                )
        if abort_error is not None:
            raise abort_error
        return {"attempt_id": attempt_id, "attempt_record_id": result["record_ids"][0],
                "operation": payload["operation"],
                "arguments": (payload.get("arguments_ref") or {}).get("arguments"),
                "idempotency_key": contract.get("key") if mode == "KEYED" else None}

    def record_response(self, workspace_id: str, attempt_id: str, response: dict) -> str:
        """外呼返回后：同一事务内落 Observation + Receipt 并聚合结算（7.6 节步骤 4-5）。"""
        with write_txn(self.conn) as conn:
            att = conn.execute("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
            if att is None:
                raise AsgError("E502", f"尝试不存在: {attempt_id}")
            payload, _ = _load_intent(conn, workspace_id, att["intent_id"])
            now = _now()
            obs_rid = uuid7()
            obs_payload = {
                "source": {"connector_identity":
                           f"{payload['connector_identity']}@{payload['connector_version']}",
                           "fetched_via": "gateway",
                           "url_or_handle": payload["operation"],
                           "content_version": attempt_id},
                "modality": "api_response",
                "span": {"offset": 0, "length": len(json.dumps(response, default=str))},
                "acquired_at": now,
                "acquisition_method": {"tool": payload["operation"],
                                       "tool_version": payload["connector_version"],
                                       "parameters_digest": att["dispatch_digest"]},
            }
            obs_record = {
                "workspace_id": workspace_id, "record_id": obs_rid,
                "object_id": "obs-" + uuid7(), "kind": "OBSERVATION",
                "schema_version": 1, "authenticated_writer": self.principal,
                "policy_domain": "default", "payload": obs_payload,
                "typed_refs": [], "prior_revision_ids": [],
            }
            receipt_payload = {
                "attempt_id": attempt_id,
                "outcome": response.get("outcome", "OUTCOME_UNKNOWN"),
                "classification_basis": {
                    "contract_mode": (payload.get("idempotency_contract") or {}).get("mode"),
                    "clause": response.get("clause", "连接器契约条款"),
                },
                "evidence_observation_id": obs_rid,
                "recorded_by": self.principal,
                "external_job_id": response.get("external_job_id"),
            }
            receipt_record = {
                "workspace_id": workspace_id, "object_id": "rcp-" + uuid7(),
                "kind": "RECEIPT", "schema_version": 1,
                "authenticated_writer": self.principal, "policy_domain": "default",
                "payload": receipt_payload,
                "typed_refs": [{"role": "receipt_of", "target_record_id": att["attempt_record_id"]}],
                "prior_revision_ids": [],
            }
            head = _head(conn, workspace_id)
            self.svc.commit_in_txn(conn, workspace_id, "main", [obs_record, receipt_record],
                                   head, "req-" + uuid7())
            conn.execute(
                "UPDATE attempt SET attempt_status='RESPONSE_PERSISTED', external_job_id=?"
                " WHERE attempt_id=?",
                (response.get("external_job_id"), attempt_id),
            )
            state = self._aggregate_in_txn(conn, workspace_id, att["intent_id"])
        return state

    def mark_no_response(self, workspace_id: str, attempt_id: str) -> str:
        """超时无响应：尝试置 NO_RESPONSE，意图聚合为 UNKNOWN（保留占用）。"""
        with write_txn(self.conn) as conn:
            conn.execute(
                "UPDATE attempt SET attempt_status='NO_RESPONSE' WHERE attempt_id=?",
                (attempt_id,),
            )
            att = conn.execute("SELECT intent_id FROM attempt WHERE attempt_id=?",
                               (attempt_id,)).fetchone()
            state = self._aggregate_in_txn(conn, workspace_id, att["intent_id"])
        return state

    # ── 6.1 聚合：按意图聚合回执并结算预留 ──────────────
    def _aggregate_in_txn(self, conn, workspace_id: str, intent_id: str) -> str:
        attempts = conn.execute(
            "SELECT * FROM attempt WHERE intent_id=? ORDER BY started_at, rowid",
            (intent_id,),
        ).fetchall()
        cur = conn.execute(
            "SELECT state FROM effect_state WHERE workspace_id=? AND intent_id=?",
            (workspace_id, intent_id),
        ).fetchone()
        current = cur["state"] if cur else "PREPARED"

        receipts_by_outcome: dict[str, list[dict]] = {"CONFIRMED_SUCCEEDED": [],
                                                      "CONFIRMED_NOT_APPLIED": [],
                                                      "OUTCOME_UNKNOWN": [], "PARTIAL": []}
        attempts_with_receipt: set[str] = set()
        for att in attempts:
            rows = conn.execute(
                "SELECT payload FROM record WHERE kind='RECEIPT' AND"
                " json_extract(payload, '$.attempt_id')=?", (att["attempt_id"],),
            ).fetchall()
            for r in rows:
                p = json.loads(r["payload"])
                if p["outcome"] in receipts_by_outcome:
                    receipts_by_outcome[p["outcome"]].append(p)
                attempts_with_receipt.add(att["attempt_id"])

        unresolved = None
        if receipts_by_outcome["CONFIRMED_SUCCEEDED"]:
            # 已确认成功不被迟到回执覆盖；冲突保留并对账（E605 语义）
            if current == "CONFIRMED_SUCCEEDED" and len(
                    {p["attempt_id"] for p in receipts_by_outcome["CONFIRMED_SUCCEEDED"]}) > 1:
                unresolved = "receipt_conflict: 多个尝试声称成功，保留冲突并对账"
            new_state = "CONFIRMED_SUCCEEDED"
        elif receipts_by_outcome["PARTIAL"]:
            new_state = "PARTIAL"
        elif attempts and all(a["attempt_id"] in attempts_with_receipt for a in attempts) \
                and receipts_by_outcome["CONFIRMED_NOT_APPLIED"] \
                and not receipts_by_outcome["OUTCOME_UNKNOWN"]:
            # 释放守卫：全部可能送达的尝试均确认未生效且无在途/未知尝试
            new_state = "CONFIRMED_NOT_APPLIED"
        else:
            new_state = "OUTCOME_UNKNOWN"

        conn.execute(
            "UPDATE effect_state SET state=?, unresolved_reason=?, updated_epoch=?"
            " WHERE workspace_id=? AND intent_id=?",
            (new_state, unresolved, _epoch(conn, workspace_id), workspace_id, intent_id),
        )

        reservations = conn.execute(
            "SELECT reservation_id FROM reservation WHERE intent_id=?"
            " AND state IN ('reserved','uncertain')", (intent_id,),
        ).fetchall()
        if new_state == "CONFIRMED_SUCCEEDED":
            for r in reservations:
                settle_in_txn(conn, r["reservation_id"], "SUCCEEDED")
        elif new_state == "CONFIRMED_NOT_APPLIED":
            for r in reservations:
                settle_in_txn(conn, r["reservation_id"], "NOT_APPLIED")
        elif new_state == "OUTCOME_UNKNOWN":
            for r in reservations:
                settle_in_txn(conn, r["reservation_id"], "UNKNOWN")
        # PARTIAL：逐项对账（阶段 B 简化为保留 uncertain）

        if new_state in ("CONFIRMED_SUCCEEDED", "CANCELLED_BEFORE_DISPATCH"):
            conn.execute("UPDATE effect_outbox SET state='DONE', claim_token=NULL"
                         " WHERE intent_id=?", (intent_id,))
        elif new_state == "CONFIRMED_NOT_APPLIED":
            # 释放守卫成立；是否重试由重试预算决定（同一逻辑意图、同一幂等键）
            payload, _ = _load_intent(conn, workspace_id, intent_id)
            max_r = (payload.get("idempotency_contract", {})
                     .get("retry_policy", {}).get("max", 1))
            if len(attempts) >= max_r:
                conn.execute("UPDATE effect_outbox SET state='DONE', claim_token=NULL"
                             " WHERE intent_id=?", (intent_id,))
            else:
                conn.execute(
                    "UPDATE effect_outbox SET state='PENDING', next_retry_at=?,"
                    " claim_token=NULL WHERE intent_id=?", (_now(), intent_id),
                )
        elif new_state in ("OUTCOME_UNKNOWN", "PARTIAL"):
            conn.execute(
                "UPDATE effect_outbox SET state='PENDING', next_retry_at=?, claim_token=NULL"
                " WHERE intent_id=?", (_now_after(1.0), intent_id),
            )
        return new_state

    # ── 7.7 reconcile：对账（信任边界：覆盖/可见性/最终性） ──
    def reconcile(self, workspace_id: str, intent_id: str, connector: Connector) -> str:
        payload, _ = _load_intent(self.conn, workspace_id, intent_id)
        contract = payload.get("idempotency_contract", {})
        mode = contract.get("mode", "NONE")
        key = contract.get("key")
        key_expired = bool(contract.get("key_expiry") and contract["key_expiry"] <= _now())

        basis = None
        response = None
        if mode == "KEYED" and key and not key_expired:
            response = connector.query_by_key(key)
            basis = "query_by_key"
        else:
            job = conn_job(self.conn, intent_id)
            if job and connector.contract.supports_job_query:
                response = connector.query_job(job)
                basis = "query_job"
            elif key_expired:
                raise AsgError("E603", "幂等键已过期，不能声称重试安全")
            else:
                raise AsgError("E606", "无可信对账通道（无幂等键且查询不受支持）")

        if response is None:
            if connector.contract.final_query_guarantee:
                response = {"outcome": "CONFIRMED_NOT_APPLIED", "external_job_id": None,
                            "clause": f"{basis}: 契约保证查询覆盖范围、可见性与最终性，查无即未生效"}
            else:
                raise AsgError("E606", "查询不满足覆盖/可见性/最终性，查无不能作为未生效依据，维持 UNKNOWN")

        state_row = self.conn.execute(
            "SELECT state FROM effect_state WHERE workspace_id=? AND intent_id=?",
            (workspace_id, intent_id),
        ).fetchone()
        if state_row and state_row["state"] == "CONFIRMED_SUCCEEDED" \
                and response["outcome"] != "CONFIRMED_SUCCEEDED":
            with write_txn(self.conn) as conn:
                conn.execute(
                    "UPDATE effect_state SET unresolved_reason='receipt_conflict: 迟到回执与已确认成功冲突'"
                    " WHERE workspace_id=? AND intent_id=?", (workspace_id, intent_id),
                )
            raise AsgError("E605", "迟到回执与已确认成功冲突，保留冲突并对账")

        response = dict(response)
        response.setdefault("clause", f"{basis}: 连接器契约查询")
        att = self.conn.execute(
            "SELECT attempt_id FROM attempt WHERE intent_id=?"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1", (intent_id,),
        ).fetchone()
        return self.record_response(workspace_id, att["attempt_id"], response)

    # ── 7.6 执行循环 ────────────────────────────────────
    def run_once(self, intent_id: str | None, connector: Connector,
                 lease_seconds: float = 30.0) -> dict | None:
        """领取一条并处理：有未决尝试先对账，不盲目重发；否则新尝试外呼。"""
        claim = self.claim(intent_id, lease_seconds)
        if claim is None:
            return None
        ws, iid, token = claim["workspace_id"], claim["intent_id"], claim["token"]

        pending = self.conn.execute(
            "SELECT attempt_id FROM attempt WHERE intent_id=? AND attempt_status IN"
            " ('STARTED','NO_RESPONSE')", (iid,),
        ).fetchall()
        if pending:
            try:
                state = self.reconcile(ws, iid, connector)
            except AsgError as e:
                if e.code in ("E603", "E604", "E606"):
                    self._mark_unknown_keep(ws, iid, f"{e.code} {e.message}")
                    return {"intent_id": iid, "state": "OUTCOME_UNKNOWN", "reason": e.code}
                raise
            if state == "CONFIRMED_NOT_APPLIED":
                # 对账确认未生效：同一逻辑意图、同一幂等键安全重试（重新领取令牌）
                claim2 = self.claim(iid, lease_seconds)
                if claim2 is None:
                    return {"intent_id": iid, "state": state, "terminal": True}
                try:
                    return self._dispatch(ws, iid, claim2["token"], connector)
                except AsgError as e:
                    if e.code in ("E603", "E604"):
                        # begin_attempt 已在事务内落 ABANDONED 与 UNKNOWN，这里只上报
                        return {"intent_id": iid,
                                "state": self.effect_status(ws, iid)["state"],
                                "reason": e.code}
                    raise
            return {"intent_id": iid, "state": state}
        try:
            return self._dispatch(ws, iid, token, connector)
        except AsgError as e:
            if e.code in ("E603", "E604"):
                # begin_attempt 已在事务内落 ABANDONED 与 UNKNOWN，这里只上报
                return {"intent_id": iid,
                        "state": self.effect_status(ws, iid)["state"],
                        "reason": e.code}
            raise

    def _dispatch(self, workspace_id: str, intent_id: str, token: str,
                  connector: Connector) -> dict:
        ctx = self.begin_attempt(workspace_id, intent_id, token)   # 事务内：先记录
        response = connector.call(ctx["operation"], ctx["arguments"], ctx["idempotency_key"])
        if response is None:                                        # 事务外：再外呼
            state = self.mark_no_response(workspace_id, ctx["attempt_id"])
        else:
            state = self.record_response(workspace_id, ctx["attempt_id"], response)
        return {"intent_id": intent_id, "attempt_id": ctx["attempt_id"], "state": state}

    def _mark_unknown_keep(self, workspace_id: str, intent_id: str, reason: str) -> None:
        with write_txn(self.conn) as conn:
            for att in conn.execute(
                "SELECT attempt_id FROM attempt WHERE intent_id=? AND attempt_status='STARTED'",
                (intent_id,),
            ).fetchall():
                conn.execute(
                    "UPDATE attempt SET attempt_status='NO_RESPONSE' WHERE attempt_id=?",
                    (att["attempt_id"],),
                )
            conn.execute(
                "UPDATE effect_state SET state='OUTCOME_UNKNOWN', unresolved_reason=?,"
                " updated_epoch=? WHERE workspace_id=? AND intent_id=?",
                (reason, _epoch(conn, workspace_id), workspace_id, intent_id),
            )
            for r in conn.execute(
                "SELECT reservation_id FROM reservation WHERE intent_id=?"
                " AND state='reserved'", (intent_id,),
            ).fetchall():
                settle_in_txn(conn, r["reservation_id"], "UNKNOWN")
            conn.execute(
                "UPDATE effect_outbox SET state='PENDING', claim_token=NULL, last_error=?,"
                " next_retry_at=? WHERE intent_id=?",
                (reason, _now_after(3600.0), intent_id),
            )

    def _abandon_in_txn(self, conn, workspace_id: str, intent_id: str, reason: str) -> None:
        """E603/E604：放弃自动重发，保留 UNKNOWN 边界（阶段 B：转人工或策略通道）。"""
        conn.execute(
            "UPDATE effect_state SET state='OUTCOME_UNKNOWN', unresolved_reason=?,"
            " updated_epoch=? WHERE workspace_id=? AND intent_id=?",
            (reason, _epoch(conn, workspace_id), workspace_id, intent_id),
        )
        for r in conn.execute(
            "SELECT reservation_id FROM reservation WHERE intent_id=? AND state='reserved'",
            (intent_id,),
        ).fetchall():
            settle_in_txn(conn, r["reservation_id"], "UNKNOWN")
        conn.execute(
            "UPDATE effect_outbox SET state='ABANDONED', last_error=?, claim_token=NULL"
            " WHERE intent_id=?", (reason, intent_id),
        )

    def _grant_of(self, conn, intent_record_id: str) -> str:
        ref = conn.execute(
            "SELECT dst_record_id FROM record_ref WHERE src_record_id=? AND ref_role='authorized_by'",
            (intent_record_id,),
        ).fetchone()
        if ref is None:
            raise AsgError("E403", "意图未绑定授权（authorized_by 缺失）")
        g = conn.execute(
            "SELECT grant_id FROM grant_record WHERE grant_record_id=?", (ref["dst_record_id"],),
        ).fetchone()
        if g is None:
            raise AsgError("E403", "授权记录缺失")
        return g["grant_id"]

    def effect_status(self, workspace_id: str, intent_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM effect_state WHERE workspace_id=? AND intent_id=?",
            (workspace_id, intent_id),
        ).fetchone()
        if row is None:
            raise AsgError("E502", f"意图无效果状态: {intent_id}")
        return {"intent_id": intent_id, "state": row["state"],
                "unresolved_reason": row["unresolved_reason"]}


def conn_job(conn, intent_id: str) -> str | None:
    row = conn.execute(
        "SELECT external_job_id FROM attempt WHERE intent_id=? AND external_job_id IS NOT NULL"
        " ORDER BY started_at DESC LIMIT 1", (intent_id,),
    ).fetchone()
    return row["external_job_id"] if row else None

"""策略服务（M4）—— operation_digest 规范化（9.1 节）与授权生命周期（6.4 节）。

授权绑定具体操作内容而非操作名称：摘要覆盖 operation、target_ids、executor_principal、
connector_identity 与 connector_version、arguments、artifact_revision_ids 的确切内容。
规范化基于 JCS 思路（键排序、紧凑分隔）并固定 canonicalization_version；
时间一律 RFC 3339 UTC 字符串、金额与计量为字符串加显式单位（由载荷结构保证）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .errors import AsgError
from .ids import canonical_json, uuid7
from .storage import write_txn

CANONICALIZATION_VERSION = 1
DIGEST_PREFIX = f"opv{CANONICALIZATION_VERSION}:"

# 允许的签发者：人工审批者（经认证通道）与策略服务（第 2.3 节信任边界）
ALLOWED_ISSUER_KINDS = {"human", "policy-service"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def compute_operation_digest(intent_payload: dict) -> str:
    """按 9.1 节覆盖字段清单计算规范化摘要。参数可为内联 arguments 或其摘要引用。"""
    arguments_ref = intent_payload.get("arguments_ref") or {}
    arguments_repr = (
        arguments_ref.get("arguments")
        if arguments_ref.get("arguments") is not None
        else {"arguments_digest": arguments_ref.get("digest")}
    )
    covered = {
        "operation": intent_payload["operation"],
        "target_ids": sorted(intent_payload.get("target_ids", [])),
        "executor_principal": intent_payload["executor_principal"],
        "connector_identity": intent_payload["connector_identity"],
        "connector_version": intent_payload["connector_version"],
        "arguments": arguments_repr,
        "artifact_revision_ids": sorted(intent_payload.get("artifact_revision_ids", [])),
    }
    hexdigest = hashlib.sha256(canonical_json(covered).encode("utf-8")).hexdigest()
    return DIGEST_PREFIX + hexdigest


class Authorizer:
    def __init__(self, conn: sqlite3.Connection, write_service):
        self.conn = conn
        self.svc = write_service

    # ── 签发（6.4 节：仅被允许的签发者） ────────────────
    def issue_grant(self, workspace_id: str, issuer: dict, grantee: dict,
                    operation_digest: str, purpose: str, expiry: str,
                    allowed_destinations: list[str], use_limit: int = 1,
                    scope: dict | None = None, expected_head: int = 0,
                    request_id: str | None = None, policy_version: int = 1,
                    parent_grant_id: str | None = None) -> dict:
        if issuer.get("kind") not in ALLOWED_ISSUER_KINDS:
            raise AsgError("E103", f"主体 {issuer} 不被允许签发授权")
        grant_id = "grant-" + uuid7()
        payload = {
            "grant_id": grant_id,
            "issuer": issuer,
            "grantee": grantee,
            "operation_digest": operation_digest,
            "scope": scope or {},
            "purpose": purpose,
            "expiry": expiry,
            "revocation_handle": "rvk-" + uuid7(),
            "use_limit": use_limit,
            "allowed_destinations": allowed_destinations,
            "policy_version": policy_version,
        }
        record = {
            "workspace_id": workspace_id,
            "object_id": grant_id,
            "kind": "GRANT",
            "schema_version": 1,
            "authenticated_writer": issuer,
            "policy_domain": "default",
            "payload": payload,
            "typed_refs": [],
            "prior_revision_ids": [],
        }
        with write_txn(self.conn) as conn:
            result = self.svc.commit_in_txn(
                conn, workspace_id, "main", [record], expected_head,
                request_id or "req-" + uuid7())
            conn.execute(
                "INSERT INTO grant_record (grant_id, issuer, grantee, parent_grant_id,"
                " operation_digest, scope, purpose, expiry, revocation_handle, use_limit,"
                " uses_consumed, allowed_destinations, policy_version, status, grant_record_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?, 'active', ?)",
                (grant_id, json.dumps(issuer), json.dumps(grantee), parent_grant_id,
                 operation_digest, json.dumps(payload["scope"]), purpose, expiry,
                 payload["revocation_handle"], use_limit, json.dumps(allowed_destinations),
                 policy_version, result["record_ids"][0]),
            )
        return {"grant_id": grant_id, "record_id": result["record_ids"][0],
                "revocation_handle": payload["revocation_handle"],
                "sequence": result["sequence"]}

    def revoke(self, grant_id: str) -> None:
        """撤销立即影响后续 dispatch；已发出的外部请求无法收回（6.4 节）。"""
        with write_txn(self.conn) as conn:
            cur = conn.execute(
                "UPDATE grant_record SET status='revoked' WHERE grant_id=? AND status='active'",
                (grant_id,),
            )
            if cur.rowcount == 0:
                raise AsgError("E403", f"授权不存在或已不在活跃状态: {grant_id}")

    # ── 校验（CP-2：prepare 与 dispatch 各查一次） ──────
    def check_grant(self, conn: sqlite3.Connection, grant_id: str, intent_payload: dict,
                    purpose_of_check: str) -> None:
        g = conn.execute("SELECT * FROM grant_record WHERE grant_id=?", (grant_id,)).fetchone()
        if g is None:
            raise AsgError("E403", f"授权不存在: {grant_id}")
        now = _now()
        if g["status"] == "revoked":
            raise AsgError("E403", f"授权已撤销: {grant_id}")
        if g["expiry"] <= now:
            if g["status"] == "active":
                conn.execute("UPDATE grant_record SET status='expired' WHERE grant_id=?", (grant_id,))
            raise AsgError("E403", f"授权已过期: {grant_id}", details={"expiry": g["expiry"], "now": now})
        # 内容绑定（I6）：摘要覆盖操作、目标、主体、连接器、参数与产物
        digest = compute_operation_digest(intent_payload)
        if g["operation_digest"] != digest:
            raise AsgError("E402", "operation_digest 不匹配：新动作不能借用旧授权",
                           details={"expected": g["operation_digest"], "actual": digest,
                                    "check": purpose_of_check})
        # 主体匹配：被授权者与执行主体一致
        grantee = json.loads(g["grantee"])
        if grantee != intent_payload["executor_principal"]:
            raise AsgError("E402", "执行主体与被授权者不一致",
                           details={"grantee": grantee,
                                    "executor": intent_payload["executor_principal"]})
        # 目标范围：读取权限与向目标披露的权限分开判定（9.2 节 CP-6/CP-7）
        allowed = set(json.loads(g["allowed_destinations"]))
        targets = set(intent_payload.get("target_ids", []))
        if not targets <= allowed:
            raise AsgError("E402", "目标不在授权范围 allowed_destinations 内",
                           details={"targets": sorted(targets),
                                    "allowed": sorted(allowed)})

    def consume_use(self, conn: sqlite3.Connection, grant_id: str, intent_id: str) -> None:
        """占用授权次数：按逻辑意图计量，重试不重复计数（7.5 节）。"""
        g = conn.execute("SELECT * FROM grant_record WHERE grant_id=?", (grant_id,)).fetchone()
        if g["uses_consumed"] >= g["use_limit"]:
            conn.execute("UPDATE grant_record SET status='exhausted' WHERE grant_id=?", (grant_id,))
            raise AsgError("E403", "授权次数已用尽",
                           details={"use_limit": g["use_limit"]})
        conn.execute(
            "INSERT INTO grant_usage (grant_id, intent_id, taken_at) VALUES (?,?,?)"
            " ON CONFLICT(grant_id, intent_id) DO NOTHING",
            (grant_id, intent_id, _now()),
        )
        distinct = conn.execute(
            "SELECT COUNT(*) c FROM grant_usage WHERE grant_id=?", (grant_id,)
        ).fetchone()["c"]
        conn.execute("UPDATE grant_record SET uses_consumed=?, status=? WHERE grant_id=?",
                     (distinct, "exhausted" if distinct >= g["use_limit"] else g["status"], grant_id))

    def grant_status(self, grant_id: str) -> dict:
        g = self.conn.execute("SELECT * FROM grant_record WHERE grant_id=?", (grant_id,)).fetchone()
        if g is None:
            raise AsgError("E403", f"授权不存在: {grant_id}")
        return {"grant_id": g["grant_id"], "status": g["status"],
                "uses_consumed": g["uses_consumed"], "use_limit": g["use_limit"]}

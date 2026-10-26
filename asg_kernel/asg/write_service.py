"""写入服务（M1）—— 串行提交协议，《详细设计说明书》7.1 节与 5.1–5.4 节。

职责：请求去重（I3）、schema 校验、类型与引用检查（I2）、提交序号与 recorded_at 分配、
expected_head 冲突检查（I4）、失效入队。模型与外部调用不得进入本模块事务。

record_id 说明：默认由服务分配（3.2 节）；接受调用方预分配的合法 UUID 以支持
同批前序引用（批内拓扑仍是 I2 的检查对象），服务负责唯一性与格式校验。
recorded_at 与 sequence 永远由服务分配，不采信调用方填写（第 4 章）。
"""
from __future__ import annotations

import json
import sqlite3
import uuid as uuidlib
from datetime import datetime, timezone

from . import registry
from .errors import AsgError
from .ids import digest_obj, uuid7
from .storage import write_txn

REDUCER_VERSION = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _EarlyExit(Exception):
    """事务内只读提前返回：经 write_txn 回滚后携带结果返回。"""

    def __init__(self, result: dict):
        self.result = result


def _valid_uuid(value) -> bool:
    try:
        uuidlib.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


class WriteService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── 7.1 串行提交协议 ────────────────────────────────
    def submit_proposal(self, workspace_id: str, records: list[dict], expected_head: int,
                        request_id: str, request_digest: str | None = None,
                        principal: dict | None = None, branch_id: str = "main") -> dict:
        request_digest = request_digest or digest_obj(records)
        principal = principal or {"kind": "service", "id": "write-service"}

        if branch_id != "main":
            raise AsgError("E104", f"分支 {branch_id!r} 尚未支持，当前仅接受 main")
        if request_digest != digest_obj(records):
            raise AsgError("E202", "请求摘要与记录内容不一致")

        # 快速路径：事务外先查幂等（摘要由服务计算，调用方传入值仅供比对）
        row = self.conn.execute(
            'SELECT * FROM "commit" WHERE workspace_id=? AND request_id=?',
            (workspace_id, request_id),
        ).fetchone()
        if row is not None:
            if row["request_digest"] != digest_obj(records):
                raise AsgError("E202", "同一 request_id 携带不同请求内容，拒绝一键两用",
                               details={"request_id": request_id})
            return self._commit_result(row, replayed=True)

        try:
            with write_txn(self.conn) as conn:
                return self.commit_in_txn(conn, workspace_id, branch_id, records,
                                           expected_head, request_id, request_digest,
                                           principal=principal)
        except _EarlyExit as e:
            return e.result
        except sqlite3.IntegrityError as e:
            raise AsgError("E101", f"约束冲突: {e}") from e

    def commit_in_txn(self, conn, workspace_id, branch_id, records, expected_head,
                       request_id, request_digest: str | None = None,
                       principal: dict | None = None) -> dict:
        """事务内提交：供网关/策略服务在同一事务内组合（5.5 节）。

        写入边界契约（评审收敛第一轮）：
        - 分支：当前仅支持 main，其他 branch_id 显式拒绝（E104）；
        - 摘要：request_digest 由服务按记录计算，调用方传入值只允许一致比对；
        - 身份：authenticated_writer 由 principal 回填，调用方自报值仅存 asserted_source；
        - 签发：GRANT/ATTEMPT/RECEIPT 仅限可信签发者（CP-1，E103）；
        - 修订基线：supersedes 必须指向该对象当前头（E203）；
        - 引用：目标须存在且同工作空间（E303 跨域拒绝），prior_revision_ids 同样校验。
        """
        if branch_id != "main":
            raise AsgError("E104", f"分支 {branch_id!r} 尚未支持，当前仅接受 main")
        computed = digest_obj(records)
        if request_digest is not None and request_digest != computed:
            raise AsgError("E202", "请求摘要与记录内容不一致（摘要由服务计算）",
                           details={"request_id": request_id})
        request_digest = computed
        principal = principal or {"kind": "service", "id": "write-service"}
        # 幂等兜底（并发同键写入）
        dup = conn.execute(
            'SELECT * FROM "commit" WHERE workspace_id=? AND request_id=?',
            (workspace_id, request_id),
        ).fetchone()
        if dup is not None:
            if dup["request_digest"] != request_digest:
                raise AsgError("E202", "同一 request_id 携带不同请求内容，拒绝一键两用",
                               details={"request_id": request_id})
            raise _EarlyExit(self._commit_result(dup, replayed=True))

        # schema 与类型校验 + 写入边界契约
        records = [registry.normalize_record(rec) for rec in records]
        for rec in records:
            registry.validate_record(rec)
            if rec["workspace_id"] != workspace_id:
                raise AsgError("E303", "记录信封与提交工作空间不一致")
            registry.validate_kind_phase(rec["kind"])
            self._enforce_issuer(rec, principal)
            self._enforce_refs(conn, workspace_id, rec, {r.get("record_id") for r in records})

        ws = conn.execute(
            "SELECT * FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        if ws is None:
            conn.execute("INSERT INTO workspace_epoch (workspace_id) VALUES (?)", (workspace_id,))
            ws = conn.execute(
                "SELECT * FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
        head = ws["head_sequence"]

        if expected_head != head:
            raise AsgError("E201", "expected_head 与当前分支头不一致，请在最新快照上重检查后重提",
                           retryable=True,
                           details={"expected_head": expected_head, "current_head": head})

        prior_head_row = conn.execute(
            'SELECT commit_id FROM "commit" WHERE workspace_id=? AND branch_id=? AND sequence=?',
            (workspace_id, branch_id, head),
        ).fetchone()
        previous_commit_id = prior_head_row["commit_id"] if prior_head_row else ""

        seq = head + 1
        commit_id = uuid7()
        recorded_at = _now()

        # record_id 分配/校验 + 批内去重
        batch_ids: list[str] = []
        assigned = []
        for rec in records:
            rid = rec.get("record_id")
            if rid is not None:
                if not _valid_uuid(rid):
                    raise AsgError("E101", f"调用方预分配的 record_id 不是合法 UUID: {rid}")
                if rid in batch_ids:
                    raise AsgError("E101", f"批内 record_id 重复: {rid}")
                hit = conn.execute("SELECT 1 FROM record WHERE record_id=?", (rid,)).fetchone()
                if hit is not None:
                    raise AsgError("E101", f"record_id 已存在: {rid}")
            else:
                rid = uuid7()
            batch_ids.append(rid)
            assigned.append({**rec, "record_id": rid})

        # 引用校验（I2：不得引用未来记录；同批前序合法）
        for i, rec in enumerate(assigned):
            for ref in rec.get("typed_refs", []):
                target = ref["target_record_id"]
                if target in batch_ids[:i]:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM record WHERE record_id=?", (target,)
                ).fetchone()
                if exists is None:
                    if target in batch_ids:
                        raise AsgError("E302", f"引用了同批靠后的未来记录: {target}")
                    raise AsgError("E301", f"引用的记录不存在: {target}")

        # 写入提交与记录
        conn.execute(
            'INSERT INTO "commit" (commit_id, workspace_id, branch_id, sequence, previous_commit_id,'
            " reducer_version, record_ids, request_id, request_digest, committed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (commit_id, workspace_id, branch_id, seq, previous_commit_id,
             REDUCER_VERSION, json.dumps(batch_ids), request_id, request_digest, recorded_at),
        )
        retire_roots = []
        for rec in assigned:
            # Validate against heads advanced by earlier records in this transaction.
            self._enforce_revision_baseline(conn, workspace_id, rec)
            for parent_id in rec.get("prior_revision_ids", []):
                parent = conn.execute("SELECT object_id,kind FROM record WHERE record_id=?",
                                      (parent_id,)).fetchone()
                if parent["object_id"] != rec["object_id"] or parent["kind"] != rec["kind"]:
                    raise AsgError("E203", "父版本必须属于同一对象和记录类型")
            if rec["kind"] == "JUSTIFICATION":
                target = conn.execute("SELECT kind FROM record WHERE record_id=?",
                                      (rec["payload"]["target_claim_id"],)).fetchone()
                if target is None or target["kind"] != "CLAIM":
                    raise AsgError("E101", "论证目标必须是 CLAIM")
            if rec["kind"] in {"TASK_REVISION", "CONTEXT_MANIFEST"}:
                from .dependencies import current_stamp
                from .evidence import eligible_new_task
                is_task = rec["kind"] == "TASK_REVISION"
                roots = (sorted({r["target_record_id"] for r in rec.get("typed_refs", [])
                                 if r["role"] in {"derived_from", "extracted_from", "transcribed_from",
                                                  "uses_version", "depends_on", "selected_into"}}) if is_task else
                         [item["record_id"] for item in rec["payload"]["selected_items"]])
                rec["payload"]["_validation"] = {
                    "stamp": current_stamp(conn, workspace_id, roots),
                    "roots": roots,
                    "status": ("ACTIVE" if eligible_new_task(conn, workspace_id, roots, seq)
                               else "PENDING_REEVALUATION")
                              if is_task else "SNAPSHOT",
                }
            payload_json = json.dumps(rec["payload"], ensure_ascii=False, sort_keys=True)
            asserted_source = rec.get("asserted_source")
            if asserted_source is None and rec.get("authenticated_writer") != principal:
                asserted_source = rec.get("authenticated_writer")
            conn.execute(
                "INSERT INTO record (record_id, object_id, workspace_id, branch_id, kind,"
                " schema_version, authenticated_writer, asserted_source, recorded_at,"
                " payload_digest, payload, policy_domain, commit_id, sequence)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["record_id"], rec["object_id"], workspace_id, branch_id, rec["kind"],
                 rec["schema_version"], json.dumps(principal),
                 json.dumps(asserted_source) if asserted_source is not None else None,
                 recorded_at, digest_obj(rec["payload"]), payload_json,
                 rec["policy_domain"], commit_id, seq),
            )
            if rec["kind"] == "OBSERVATION":
                # 删除域复活阻止（I10 / 7.8 节）：按稳定内容身份比对（来源 + 片段），
                # 采集时间等会自然变化的元数据不参与——否则改 acquired_at 即可绕过
                content_id = digest_obj({
                    "source": rec["payload"].get("source"),
                    "span": rec["payload"].get("span"),
                })
                hit = conn.execute(
                    "SELECT 1 FROM tombstone WHERE payload_digest=? AND mode='purge'",
                    (content_id,),
                ).fetchone()
                if hit is not None:
                    raise AsgError("E802", "观察内容命中删除域墓碑，阻止复活",
                                   details={"content_identity": content_id})
                # 对象登记：正文纳入删除域管理（9.4 节删除域清单）
                payload_json_c = json.dumps(rec["payload"], ensure_ascii=False)
                conn.execute(
                    "INSERT OR IGNORE INTO blob_object (blob_id, digest, media_type, size_bytes,"
                    " storage_uri, deletion_state, data_labels) VALUES (?,?,?,?,?,'live','[]')",
                    ("blob-" + uuid7(), digest_obj(rec["payload"]),
                     "application/x-asg-observation+" + str(rec["payload"].get("modality", "text")),
                     len(payload_json_c), "inline:" + rec["record_id"]),
                )
            for ref in rec.get("typed_refs", []):
                if ref["role"] in {"derived_from", "extracted_from", "transcribed_from"}:
                    if conn.execute(
                        "SELECT 1 FROM tombstone WHERE workspace_id=? AND record_id=? AND mode='purge'",
                        (workspace_id, ref["target_record_id"]),
                    ).fetchone():
                        raise AsgError("E802", "禁止从已删除的记录创建新的派生副本")
                dst = conn.execute(
                    "SELECT object_id FROM record WHERE record_id=?", (ref["target_record_id"],)
                ).fetchone()
                conn.execute(
                    "INSERT INTO record_ref (src_record_id, ref_role, ref_layer, dst_record_id,"
                    " dst_object_id, note) VALUES (?,?,?,?,?,?)",
                    (rec["record_id"], ref["role"], registry.ROLE_LAYER[ref["role"]],
                     ref["target_record_id"], dst["object_id"], ref.get("note")),
                )
            # 投影：object_heads 条件更新（I4；批内同对象按序后写胜，旧序号不得覆盖新头）
            self._upsert_head(conn, workspace_id, rec, seq, recorded_at)
            if rec["kind"] == "TASK_REVISION":
                conn.execute(
                    "INSERT INTO task_state (workspace_id, task_object_id, status, current_revision)"
                    " VALUES (?,?, ?, ?)"
                    " ON CONFLICT(workspace_id, task_object_id) DO UPDATE SET"
                    " current_revision=excluded.current_revision,"
                    " status=excluded.status, reevaluation_reason=NULL",
                    (workspace_id, rec["payload"]["task_object_id"],
                     rec["payload"]["_validation"]["status"], rec["record_id"]),
                )
            if rec["kind"] == "RETIRE_PURGE_MARK":
                # 失效根是被撤回/更正的目标记录，不是标记本身（4.2 节 invalidation_queue）
                retire_roots.append(rec["payload"]["target"]["record_id"])

        # 失效事务部分（5.4 节）：推进 validation_epoch 并入队；异步传播随阶段 C
        new_epoch = ws["validation_epoch"]
        if retire_roots:
            new_epoch = ws["validation_epoch"] + 1
            for root in retire_roots:
                conn.execute(
                    "INSERT INTO invalidation_queue (workspace_id, epoch, root_record_id, created_at)"
                    " VALUES (?,?,?,?)",
                    (workspace_id, new_epoch, root, recorded_at),
                )
        conn.execute(
            "UPDATE workspace_epoch SET head_sequence=?, validation_epoch=? WHERE workspace_id=?",
            (seq, new_epoch, workspace_id),
        )

        row = conn.execute('SELECT * FROM "commit" WHERE commit_id=?', (commit_id,)).fetchone()
        return self._commit_result(row, replayed=False)

    def current_head(self, workspace_id: str, branch_id: str = "main") -> int:
        row = self.conn.execute(
            "SELECT head_sequence FROM workspace_epoch WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        return row["head_sequence"] if row else 0

    # ── 写入边界契约（评审收敛第一轮） ─────────────────
    # 可信签发者：仅这些主体可签发 Grant/Attempt/Receipt（CP-1，第 2.3 节信任边界）
    TRUSTED_ISSUER_KINDS = {"service", "policy-service", "human"}
    TRUSTED_KINDS = {"GRANT", "ATTEMPT", "RECEIPT"}

    def _enforce_issuer(self, rec: dict, principal: dict) -> None:
        if rec["kind"] in self.TRUSTED_KINDS and principal.get("kind") not in self.TRUSTED_ISSUER_KINDS:
            raise AsgError("E103", f"主体 {principal.get('kind')} 不被允许签发 {rec['kind']}")

    def _enforce_revision_baseline(self, conn, workspace_id: str, rec: dict) -> None:
        """supersedes 必须指向该对象当前头；全局 expected_head 不能替代对象级基线（I4）。"""
        supersedes = [r["target_record_id"] for r in rec.get("typed_refs", [])
                      if r.get("role") == "supersedes"]
        head = conn.execute(
            "SELECT head_record_id FROM object_heads WHERE workspace_id=? AND object_id=?",
            (workspace_id, rec["object_id"]),
        ).fetchone()
        if head is None and not supersedes:
            return
        if head is None or supersedes != [head["head_record_id"]]:
            raise AsgError("E203", "修订基线与当前头不一致（supersedes 须指向对象当前版本）",
                           details={"expected": supersedes,
                                    "current": head["head_record_id"] if head else None})

    def _enforce_refs(self, conn, workspace_id: str, rec: dict, batch_ids=()) -> None:
        """引用与父版本校验：父版本（prior_revision_ids）必须已存在（E301）；
        一切引用不得跨工作空间（E303）。typed_refs 的存在性与批内拓扑由后续步骤校验。"""
        for tid in rec.get("prior_revision_ids", []):
            if tid in batch_ids:
                continue  # Full ordered existence check follows ID assignment.
            row = conn.execute(
                "SELECT workspace_id FROM record WHERE record_id=?", (tid,)
            ).fetchone()
            if row is None:
                raise AsgError("E301", f"父版本记录不存在: {tid}")
            if row["workspace_id"] != workspace_id:
                raise AsgError("E303", f"父版本跨工作空间: {tid}")
        for ref in rec.get("typed_refs", []):
            row = conn.execute(
                "SELECT workspace_id FROM record WHERE record_id=?", (ref["target_record_id"],)
            ).fetchone()
            if row is not None and row["workspace_id"] != workspace_id:
                raise AsgError("E303", f"引用跨工作空间：{ref['target_record_id']} 不属于 {workspace_id}",
                               details={"target": ref["target_record_id"],
                                        "target_workspace": row["workspace_id"]})

    def _upsert_head(self, conn, workspace_id: str, rec: dict, seq: int, at: str) -> None:
        cur = conn.execute(
            "UPDATE object_heads SET head_record_id=?, head_sequence=?, updated_at=?"
            " WHERE workspace_id=? AND object_id=? AND head_sequence <= ?",
            (rec["record_id"], seq, at, workspace_id, rec["object_id"], seq),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO object_heads (workspace_id, object_id, head_record_id,"
                " head_sequence, updated_at) VALUES (?,?,?,?,?)",
                (workspace_id, rec["object_id"], rec["record_id"], seq, at),
            )

    def _commit_result(self, row, replayed: bool) -> dict:
        ws = self.conn.execute(
            "SELECT * FROM workspace_epoch WHERE workspace_id=?", (row["workspace_id"],)
        ).fetchone()
        return {
            "commit_id": row["commit_id"],
            "sequence": row["sequence"],
            "record_ids": json.loads(row["record_ids"]),
            "replayed": replayed,
            "snapshot": {
                "workspace_id": row["workspace_id"],
                "branch_id": row["branch_id"],
                "head_sequence": ws["head_sequence"],
                "validation_epoch": ws["validation_epoch"],
                "policy_epoch": ws["policy_epoch"],
                "reducer_version": REDUCER_VERSION,
            },
        }

    # ── 便捷入口（5.1–5.4） ─────────────────────────────
    def observe(self, workspace_id: str, payload: dict, expected_head: int,
                request_id: str, principal: dict, object_id: str | None = None,
                policy_domain: str = "default", typed_refs=None, asserted_source=None,
                record_id: str | None = None) -> dict:
        rec = {
            "workspace_id": workspace_id,
            "object_id": object_id or uuid7(),
            "kind": "OBSERVATION",
            "schema_version": 1,
            "authenticated_writer": principal,
            "asserted_source": asserted_source,
            "policy_domain": policy_domain,
            "payload": payload,
            "typed_refs": typed_refs or [],
            "prior_revision_ids": [],
        }
        if record_id:
            rec["record_id"] = record_id
        return self.submit_proposal(workspace_id, [rec], expected_head, request_id,
                                    principal=principal)

    def claim(self, workspace_id: str, payload: dict, expected_head: int,
              request_id: str, principal: dict, object_id: str,
              supersedes: str | None = None, typed_refs=None,
              policy_domain: str = "default") -> dict:
        refs = list(typed_refs or [])
        if supersedes:
            refs.append({"role": "supersedes", "target_record_id": supersedes})
        rec = {
            "workspace_id": workspace_id,
            "object_id": object_id,
            "kind": "CLAIM",
            "schema_version": 1,
            "authenticated_writer": principal,
            "policy_domain": policy_domain,
            "payload": payload,
            "typed_refs": refs,
            "prior_revision_ids": [supersedes] if supersedes else [],
        }
        return self.submit_proposal(workspace_id, [rec], expected_head, request_id,
                                    principal=principal)

    def revise_task(self, workspace_id: str, payload: dict, expected_head: int,
                    request_id: str, principal: dict, supersedes: str,
                    typed_refs=None, policy_domain: str = "default") -> dict:
        """revise（5.3 节）：基线版本匹配——supersedes 必须是该任务对象的当前头。"""
        head = self.conn.execute(
            "SELECT head_record_id FROM object_heads WHERE workspace_id=? AND object_id=?",
            (workspace_id, payload["task_object_id"]),
        ).fetchone()
        if head is not None and head["head_record_id"] != supersedes:
            raise AsgError("E203", "修订基线与当前头不一致",
                           details={"expected": supersedes, "current": head["head_record_id"]})
        refs = [{"role": "supersedes", "target_record_id": supersedes}] + list(typed_refs or [])
        rec = {
            "workspace_id": workspace_id,
            "object_id": payload["task_object_id"],
            "kind": "TASK_REVISION",
            "schema_version": 1,
            "authenticated_writer": principal,
            "policy_domain": policy_domain,
            "payload": payload,
            "typed_refs": refs,
            "prior_revision_ids": [supersedes],
        }
        return self.submit_proposal(workspace_id, [rec], expected_head, request_id,
                                    principal=principal)

    def invalidate(self, workspace_id: str, target_record_id: str, reason: str,
                   expected_head: int, request_id: str, principal: dict) -> dict:
        payload = {"target": {"record_id": target_record_id}, "reason": reason}
        rec = {
            "workspace_id": workspace_id,
            "object_id": uuid7(),
            "kind": "RETIRE_PURGE_MARK",
            "schema_version": 1,
            "authenticated_writer": principal,
            "policy_domain": "default",
            "payload": payload,
            "typed_refs": [{"role": "uses_version", "target_record_id": target_record_id}],
            "prior_revision_ids": [],
        }
        return self.submit_proposal(workspace_id, [rec], expected_head, request_id,
                                    principal=principal)

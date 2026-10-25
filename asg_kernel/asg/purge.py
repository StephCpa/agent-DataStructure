"""删除域与墓碑门控（M4）—— 《详细设计说明书》7.8 节与 9.4 节。

时序：解析范围 -> 同一事务内（失效标记记录 + policy_epoch 推进 + 墓碑持久化）
-> 在线副本清理 -> 派生内容再生（阶段 C 记录计划，正文再生随阶段 D）
-> 回放与恢复路径的读取门控。

能力边界（9.4 节，须向产品与用户明示）：仅删除主副本或软删除不能证明备份、
第三方副本及已输出信息全部消失；记录 ID、关系边、时间与哈希也可能泄露信息；
差分隐私不在本结构承诺内。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .errors import AsgError
from .ids import digest_obj, uuid7
from .storage import write_txn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Purger:
    def __init__(self, conn, write_service):
        self.conn = conn
        self.svc = write_service

    def retire_or_purge(self, workspace_id: str, record_ids: list[str],
                        expected_head: int, request_id: str, principal: dict,
                        mode: str = "purge") -> dict:
        """retire 走失效入队（5.4）；purge 在同事务内先落墓碑与策略纪元（7.8 节）。"""
        if mode not in ("retire", "purge"):
            raise AsgError("E101", f"未知模式: {mode}")
        if not record_ids:
            raise AsgError("E801", "删除范围为空，须显式解析范围")
        targets = []
        with write_txn(self.conn) as conn:
            for rid in record_ids:
                row = conn.execute("SELECT * FROM record WHERE record_id=?", (rid,)).fetchone()
                if row is None:
                    raise AsgError("E801", f"范围解析失败，记录不存在: {rid}")
                targets.append(row)
            marks = [{
                "workspace_id": workspace_id,
                "object_id": "mark-" + uuid7(),
                "kind": "RETIRE_PURGE_MARK",
                "schema_version": 1,
                "authenticated_writer": principal,
                "policy_domain": "default",
                "payload": {"target": {"record_id": t["record_id"]},
                            "reason": "purged" if mode == "purge" else "withdrawn"},
                "typed_refs": [{"role": "uses_version", "target_record_id": t["record_id"]}],
                "prior_revision_ids": [],
            } for t in targets]
            # commit_in_txn 内部落失效根入队与 epoch 推进（阶段 A/B 已有路径）
            result = self.svc.commit_in_txn(conn, workspace_id, "main", marks,
                                            expected_head, request_id)
            if mode == "purge":
                conn.execute(
                    "UPDATE workspace_epoch SET policy_epoch = policy_epoch + 1"
                    " WHERE workspace_id=?", (workspace_id,))
                pe = conn.execute(
                    "SELECT policy_epoch FROM workspace_epoch WHERE workspace_id=?",
                    (workspace_id,)).fetchone()["policy_epoch"]
                for t in targets:
                    # 墓碑按稳定内容身份登记（来源+片段），与复活检查同一口径
                    p = json.loads(t["payload"]) if t["kind"] == "OBSERVATION" else None
                    content_id = (digest_obj({"source": p.get("source"), "span": p.get("span")})
                                  if p else t["payload_digest"])
                    conn.execute(
                        "INSERT OR IGNORE INTO tombstone (workspace_id, object_id, record_id,"
                        " payload_digest, policy_domain, mode, purged_at, policy_epoch)"
                        " VALUES (?,?,?,?,?, 'purge', ?, ?)",
                        (workspace_id, t["object_id"], t["record_id"], content_id,
                         t["policy_domain"], _now(), pe),
                    )
                    # 派生副本隔离：显式声明派生自被删记录的在线副本同步墓碑
                    # （正文再生完成前先隔离，评审反例：派生副本仍可读）
                    for d in conn.execute(
                        "SELECT r.record_id, r.object_id, r.payload_digest, r.policy_domain"
                        " FROM record_ref rr JOIN record r ON r.record_id = rr.src_record_id"
                        " WHERE rr.dst_record_id=? AND rr.ref_role IN"
                        " ('derived_from','extracted_from','transcribed_from')",
                        (t["record_id"],),
                    ).fetchall():
                        conn.execute(
                            "INSERT OR IGNORE INTO tombstone (workspace_id, object_id, record_id,"
                            " payload_digest, policy_domain, mode, purged_at, policy_epoch)"
                            " VALUES (?,?,?,?,?, 'purge', ?, ?)",
                            (workspace_id, d["object_id"], d["record_id"], d["payload_digest"],
                             d["policy_domain"], _now(), pe),
                        )
        cleanup = self._cleanup_online(targets) if mode == "purge" else {"blobs": 0}
        return {"commit_id": result["commit_id"], "sequence": result["sequence"],
                "mode": mode, "targets": [t["record_id"] for t in targets],
                "derived_regeneration_plan": self._regeneration_plan(workspace_id, targets)
                if mode == "purge" else []}

    def _cleanup_online(self, targets) -> dict:
        """在线副本清理：墓碑已先行，此处清理目标与其派生副本的 blob 登记态。"""
        n = 0
        for t in targets:
            cur = self.conn.execute(
                "UPDATE blob_object SET deletion_state='purged' WHERE storage_uri=?",
                ("inline:" + t["record_id"],),
            )
            n += cur.rowcount
            for d in self.conn.execute(
                "SELECT src_record_id FROM record_ref WHERE dst_record_id=? AND ref_role IN"
                " ('derived_from','extracted_from','transcribed_from')", (t["record_id"],),
            ).fetchall():
                cur = self.conn.execute(
                    "UPDATE blob_object SET deletion_state='purged' WHERE storage_uri=?",
                    ("inline:" + d["src_record_id"],),
                )
                n += cur.rowcount
        return {"blobs": n}

    def _regeneration_plan(self, workspace_id: str, targets) -> list[dict]:
        """派生再生计划：仍获准使用的证据重新生成干净派生内容（9.4 节）。"""
        plan = []
        for t in targets:
            dependents = self.conn.execute(
                "SELECT DISTINCT src_record_id FROM record_ref WHERE dst_record_id=?",
                (t["record_id"],),
            ).fetchall()
            plan.append({"purged": t["record_id"],
                         "dependents_to_regenerate": [d["src_record_id"] for d in dependents]})
        return plan

    def read_record(self, workspace_id: str, record_id: str) -> dict:
        """读取门控：墓碑命中即 E802，旧快照恢复不复活（I10）。"""
        row = self.conn.execute("SELECT * FROM record WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise AsgError("E801", f"记录不存在: {record_id}")
        hit = self.conn.execute(
            "SELECT 1 FROM tombstone WHERE workspace_id=? AND record_id=?",
            (workspace_id, record_id),
        ).fetchone()
        if hit is not None:
            raise AsgError("E802", "记录正文已进入删除域，禁止读取（复活阻止）",
                           details={"record_id": record_id})
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        return out

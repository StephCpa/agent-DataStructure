"""存储层 —— 《详细设计说明书》第 4 章物理模式的 SQLite 开发适配层。

生产目标为 PostgreSQL（第 4.2 节 DDL 为权威定义）。开发期以 SQLite 落地同一 ADT：
- 单写者串行化：BEGIN IMMEDIATE（PostgreSQL 下为可串行化事务，接口不变）；
- 不可变性：BEFORE UPDATE/DELETE 触发器拒绝（对应 PostgreSQL reject_mutation()）；
- 约束即不变量第一道防线（8.5 节）：request_id 唯一（I3）、账本 CHECK（I5）、
  条件更新 object_heads（I4）。
分支说明：阶段 A 仅 main 分支，head_sequence 记于 workspace_epoch；fork 支持随阶段 C 增加 branch_head 表。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_epoch (
  workspace_id     TEXT PRIMARY KEY,
  validation_epoch INTEGER NOT NULL DEFAULT 1,
  policy_epoch     INTEGER NOT NULL DEFAULT 1,
  head_sequence    INTEGER NOT NULL DEFAULT 0
);

-- 提交（历史日志 L）；I3：UNIQUE(workspace, request_id)
CREATE TABLE IF NOT EXISTS "commit" (
  commit_id          TEXT PRIMARY KEY,
  workspace_id       TEXT NOT NULL,
  branch_id          TEXT NOT NULL DEFAULT 'main',
  sequence           INTEGER NOT NULL,
  previous_commit_id TEXT NOT NULL,
  reducer_version    INTEGER NOT NULL,
  record_ids         TEXT NOT NULL,            -- JSON 数组
  request_id         TEXT NOT NULL,
  request_digest     TEXT NOT NULL,
  committed_at       TEXT NOT NULL,
  UNIQUE (workspace_id, branch_id, sequence),
  UNIQUE (workspace_id, request_id)
);

-- 记录（记录集合 R）；I1：插入后不可变（触发器）
CREATE TABLE IF NOT EXISTS record (
  record_id            TEXT PRIMARY KEY,
  object_id            TEXT NOT NULL,
  workspace_id         TEXT NOT NULL,
  branch_id            TEXT NOT NULL DEFAULT 'main',
  kind                 TEXT NOT NULL,
  schema_version       INTEGER NOT NULL CHECK (schema_version >= 1),
  authenticated_writer TEXT NOT NULL,          -- JSON
  asserted_source      TEXT,                   -- JSON，可空
  recorded_at          TEXT NOT NULL,
  payload_digest       TEXT NOT NULL,
  payload              TEXT NOT NULL,          -- 规范化 JSON 副本
  policy_domain        TEXT NOT NULL,
  commit_id            TEXT NOT NULL REFERENCES "commit"(commit_id),
  sequence             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_record_ws_seq ON record (workspace_id, branch_id, sequence);
CREATE INDEX IF NOT EXISTS idx_record_object ON record (object_id);

CREATE TRIGGER IF NOT EXISTS record_immutable_upd BEFORE UPDATE ON record
BEGIN SELECT RAISE(ABORT, 'I1 record is immutable'); END;
CREATE TRIGGER IF NOT EXISTS record_immutable_del BEFORE DELETE ON record
BEGIN SELECT RAISE(ABORT, 'I1 record is immutable'); END;

-- 类型化引用边（正反向共用；反向索引为失效传播入口）
CREATE TABLE IF NOT EXISTS record_ref (
  src_record_id TEXT NOT NULL REFERENCES record(record_id),
  ref_role      TEXT NOT NULL,
  ref_layer     TEXT NOT NULL CHECK (ref_layer IN ('DERIVATION','SEMANTIC','CONTROL')),
  dst_record_id TEXT NOT NULL REFERENCES record(record_id),
  dst_object_id TEXT NOT NULL,
  note          TEXT,
  PRIMARY KEY (src_record_id, ref_role, dst_record_id)
);
CREATE INDEX IF NOT EXISTS idx_ref_dst ON record_ref (dst_record_id);
CREATE INDEX IF NOT EXISTS idx_ref_dst_layer ON record_ref (dst_record_id, ref_layer);

-- 当前版本（投影 V 的强一致部分）；I4：条件更新
CREATE TABLE IF NOT EXISTS object_heads (
  workspace_id   TEXT NOT NULL,
  object_id      TEXT NOT NULL,
  head_record_id TEXT NOT NULL REFERENCES record(record_id),
  head_sequence  INTEGER NOT NULL,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (workspace_id, object_id)
);

CREATE TABLE IF NOT EXISTS task_state (
  workspace_id     TEXT NOT NULL,
  task_object_id   TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN
    ('DRAFT','ACTIVE','PENDING_REEVALUATION','COMPLETED','CANCELLED')),
  current_revision TEXT NOT NULL,
  reevaluation_reason TEXT,
  PRIMARY KEY (workspace_id, task_object_id)
);

-- 预算账本（I5）与预留
CREATE TABLE IF NOT EXISTS budget_ledger (
  resource_key     TEXT PRIMARY KEY,
  limit_amount     REAL NOT NULL CHECK (limit_amount >= 0),
  reserved_amount  REAL NOT NULL DEFAULT 0,
  committed_amount REAL NOT NULL DEFAULT 0,
  CHECK (reserved_amount + committed_amount <= limit_amount)
);
CREATE TABLE IF NOT EXISTS reservation (
  reservation_id TEXT PRIMARY KEY,
  workspace_id   TEXT NOT NULL,
  intent_id      TEXT NOT NULL,
  resource_key   TEXT NOT NULL REFERENCES budget_ledger(resource_key),
  amount         REAL NOT NULL CHECK (amount > 0),
  state          TEXT NOT NULL CHECK (state IN ('reserved','uncertain','committed','released')),
  created_at     TEXT NOT NULL,
  settled_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_reservation_intent ON reservation (intent_id);

-- 失效队列（传播工作者随阶段 C 落地，入队属阶段 A 的 invalidate 事务部分）
CREATE TABLE IF NOT EXISTS invalidation_queue (
  item_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id   TEXT NOT NULL,
  epoch          INTEGER NOT NULL,
  root_record_id TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','PROCESSING','DONE')),
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_watermark (
  index_name       TEXT PRIMARY KEY,
  applied_sequence INTEGER NOT NULL,
  validation_epoch INTEGER NOT NULL,
  updated_at       TEXT NOT NULL
);

-- ── 阶段 B：授权、效果状态与执行簿记（对应第 4.2 节 DDL） ──
CREATE TABLE IF NOT EXISTS grant_record (
  grant_id            TEXT PRIMARY KEY,
  issuer              TEXT NOT NULL,             -- JSON
  grantee             TEXT NOT NULL,             -- JSON
  parent_grant_id     TEXT REFERENCES grant_record(grant_id),
  operation_digest    TEXT NOT NULL,
  scope               TEXT NOT NULL,             -- JSON
  purpose             TEXT NOT NULL,
  expiry              TEXT NOT NULL,
  revocation_handle   TEXT NOT NULL UNIQUE,
  use_limit           INTEGER NOT NULL CHECK (use_limit >= 1),
  uses_consumed       INTEGER NOT NULL DEFAULT 0,
  allowed_destinations TEXT NOT NULL,            -- JSON 数组
  policy_version      INTEGER NOT NULL,
  status              TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','expired','revoked','exhausted')),
  grant_record_id     TEXT                       -- 对应 GRANT 记录的 record_id
);
CREATE INDEX IF NOT EXISTS idx_grant_record_id ON grant_record (grant_record_id);

CREATE TABLE IF NOT EXISTS grant_usage (
  grant_id  TEXT NOT NULL REFERENCES grant_record(grant_id),
  intent_id TEXT NOT NULL,
  taken_at  TEXT NOT NULL,
  PRIMARY KEY (grant_id, intent_id)              -- 同一意图重试不重复计数
);

CREATE TABLE IF NOT EXISTS effect_state (
  workspace_id      TEXT NOT NULL,
  intent_id         TEXT NOT NULL,
  state             TEXT NOT NULL CHECK (state IN ('PROPOSED','PREPARED','IN_FLIGHT',
    'OUTCOME_UNKNOWN','CONFIRMED_SUCCEEDED','CONFIRMED_NOT_APPLIED',
    'CANCELLED_BEFORE_DISPATCH','PARTIAL')),
  unresolved_reason TEXT,
  updated_epoch     INTEGER NOT NULL,
  PRIMARY KEY (workspace_id, intent_id)
);

CREATE TABLE IF NOT EXISTS effect_outbox (
  outbox_id         TEXT PRIMARY KEY,
  intent_id         TEXT NOT NULL UNIQUE,
  state             TEXT NOT NULL DEFAULT 'PENDING' CHECK (state IN
    ('PENDING','CLAIMED','DONE','ABANDONED')),
  claim_token       TEXT,
  claim_expires_at  TEXT,
  attempts_count    INTEGER NOT NULL DEFAULT 0,
  next_retry_at     TEXT NOT NULL,
  asks_json         TEXT,                         -- 预留请求快照，重试前重预留用
  last_error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_scan ON effect_outbox (state, next_retry_at);

CREATE TABLE IF NOT EXISTS attempt (
  attempt_id            TEXT PRIMARY KEY,
  intent_id             TEXT NOT NULL,
  gateway_principal     TEXT NOT NULL,           -- JSON
  started_at            TEXT NOT NULL,
  dispatch_digest       TEXT NOT NULL,
  idempotency_key_state TEXT,                    -- JSON
  external_job_id       TEXT,
  attempt_status        TEXT NOT NULL CHECK (attempt_status IN
    ('STARTED','RESPONSE_PERSISTED','NO_RESPONSE')),
  attempt_record_id     TEXT                     -- 对应 ATTEMPT 记录的 record_id
);
CREATE INDEX IF NOT EXISTS idx_attempt_intent ON attempt (intent_id);

-- ── 阶段 C：失效标记、删除域与对象登记（对应第 4.2 节 DDL） ──
CREATE TABLE IF NOT EXISTS invalidation_mark (
  workspace_id  TEXT NOT NULL,
  record_id     TEXT NOT NULL,               -- 受影响的下游记录
  via_record_id TEXT NOT NULL,               -- 传播路径上的直接依赖者
  path_role     TEXT NOT NULL,               -- 命中的依赖路径类型
  epoch         INTEGER NOT NULL,
  PRIMARY KEY (workspace_id, record_id, via_record_id)
);
CREATE INDEX IF NOT EXISTS idx_mark_record ON invalidation_mark (workspace_id, record_id);

CREATE TABLE IF NOT EXISTS tombstone (
  workspace_id  TEXT NOT NULL,
  object_id     TEXT NOT NULL DEFAULT '',
  record_id     TEXT NOT NULL DEFAULT '',
  payload_digest TEXT NOT NULL DEFAULT '',
  policy_domain TEXT NOT NULL,
  mode          TEXT NOT NULL DEFAULT 'purge' CHECK (mode IN ('purge','retire')),
  purged_at     TEXT NOT NULL,
  policy_epoch  INTEGER NOT NULL,            -- 墓碑先于清理持久化（7.8 节）
  PRIMARY KEY (workspace_id, object_id, record_id, payload_digest)
);
CREATE INDEX IF NOT EXISTS idx_tombstone_digest ON tombstone (payload_digest);

CREATE TABLE IF NOT EXISTS blob_object (
  blob_id        TEXT PRIMARY KEY,
  digest         TEXT NOT NULL,
  media_type     TEXT NOT NULL,
  size_bytes     INTEGER NOT NULL,
  storage_uri    TEXT NOT NULL,
  deletion_state TEXT NOT NULL DEFAULT 'live' CHECK (deletion_state IN
    ('live','pending_purge','purged')),
  data_labels    TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_blob_digest ON blob_object (digest);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)  # 自动事务由调用方管理
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """写事务：BEGIN IMMEDIATE 取得写锁（SQLite 单写者；PostgreSQL 下为可串行化事务）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

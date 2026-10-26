"""Persistent test remote: external truth is independent of worker memory.

SQLite is only a simulator transport. Truth, visibility and responses are
separate; metrics count requests, queries and effects independently.
"""
import json
import sqlite3

from asg.gateway import Connector, ConnectorContract
from asg.ids import digest_obj, uuid7


class DurableRemote(Connector):
    def __init__(self, path):
        super().__init__(ConnectorContract(mode="KEYED", supports_job_query=True,
                                           final_query_guarantee=False, supports_attempt_closure=True))
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS approved (key TEXT PRIMARY KEY, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS managed_key (key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS closed_attempt (
                key TEXT NOT NULL, attempt_id TEXT NOT NULL, PRIMARY KEY(key,attempt_id));
            CREATE TABLE IF NOT EXISTS effect (
                key TEXT PRIMARY KEY, logical_id TEXT NOT NULL, response TEXT NOT NULL,
                unauthorized INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS metric (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO metric VALUES ('calls',0),('queries',0),('key_reuses',0);
            CREATE TABLE IF NOT EXISTS setting (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO setting VALUES ('visible',1),('lose_response',0);
        """)

    def approve(self, key, operation, arguments, managed=False):
        self.conn.execute("INSERT INTO approved VALUES (?,?)", (key, digest_obj([operation, arguments])))
        if managed:
            self.conn.execute("INSERT INTO managed_key VALUES (?)", (key,))

    def set_visibility(self, visible):
        self.conn.execute("UPDATE setting SET value=? WHERE name='visible'", (int(visible),))

    def set_response_loss(self, lost):
        self.conn.execute("UPDATE setting SET value=? WHERE name='lose_response'", (int(lost),))

    def call(self, operation, arguments, idempotency_key):
        # Unmanaged calls exist only for the simulator's metric controls.
        return self._call(operation, arguments, idempotency_key, None)

    def call_attempt(self, context):
        return self._call(context["operation"], context["arguments"], context["idempotency_key"],
                          context["attempt_id"])

    def _call(self, operation, arguments, idempotency_key, attempt_id):
        key = idempotency_key or "unkeyed-" + uuid7()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("UPDATE metric SET value=value+1 WHERE name='calls'")
            existing = self.conn.execute("SELECT response FROM effect WHERE key=?", (key,)).fetchone()
            closed = self.conn.execute("SELECT 1 FROM closed_attempt WHERE key=? AND attempt_id=?",
                                        (key, attempt_id)).fetchone()
            missing_id = attempt_id is None and self.conn.execute(
                "SELECT 1 FROM managed_key WHERE key=?", (key,)).fetchone()
            if missing_id:
                response = {"outcome": "CONFIRMED_NOT_APPLIED",
                            "clause": "managed key requires immutable attempt identity"}
            elif closed:
                response = {"outcome": "CONFIRMED_NOT_APPLIED", "closed_attempt_id": attempt_id,
                            "clause": "remote permanently closed this attempt before dispatch"}
            elif existing:
                self.conn.execute("UPDATE metric SET value=value+1 WHERE name='key_reuses'")
                response = json.loads(existing[0])
            else:
                approved = self.conn.execute("SELECT digest FROM approved WHERE key=?", (key,)).fetchone()
                unauthorized = not approved or approved[0] != digest_obj([operation, arguments])
                response = {"outcome": "CONFIRMED_SUCCEEDED", "external_job_id": key,
                            "clause": "durable external effect"}
                logical_id = (arguments or {}).get("logical_id", digest_obj([operation, arguments]))
                self.conn.execute("INSERT INTO effect VALUES (?,?,?,?)",
                                  (key, logical_id, json.dumps(response), int(unauthorized)))
            lost = self.conn.execute("SELECT value FROM setting WHERE name='lose_response'").fetchone()[0]
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return None if lost else response

    def close_attempt(self, key, attempt_id):
        # Same remote transaction lock as _call: either the effect already
        # exists or this attempt becomes permanently unable to create one.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            visible = self.conn.execute("SELECT value FROM setting WHERE name='visible'").fetchone()[0]
            closed = self.conn.execute("SELECT 1 FROM closed_attempt WHERE key=? AND attempt_id=?",
                                        (key, attempt_id)).fetchone()
            effect = self.conn.execute("SELECT response FROM effect WHERE key=?", (key,)).fetchone()
            if not visible:
                response = None
            elif closed:
                response = {"outcome": "CONFIRMED_NOT_APPLIED", "closed_attempt_id": attempt_id,
                            "clause": "durable remote closure"}
            elif effect:
                response = json.loads(effect[0])
            else:
                self.conn.execute("INSERT INTO closed_attempt VALUES (?,?)", (key, attempt_id))
                response = {"outcome": "CONFIRMED_NOT_APPLIED", "closed_attempt_id": attempt_id,
                            "clause": "durable remote closure"}
            self.conn.execute("COMMIT")
            return response
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def query_by_key(self, key):
        self.conn.execute("UPDATE metric SET value=value+1 WHERE name='queries'")
        visible = self.conn.execute("SELECT value FROM setting WHERE name='visible'").fetchone()[0]
        if not visible:
            return None
        row = self.conn.execute("SELECT response FROM effect WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def query_job(self, job):
        return self.query_by_key(job)

    def metrics(self):
        result = dict(self.conn.execute("SELECT name,value FROM metric"))
        result["effects"] = self.conn.execute("SELECT COUNT(*) FROM effect").fetchone()[0]
        result["duplicate_effects"] = self.conn.execute(
            "SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM effect GROUP BY logical_id)"
        ).fetchone()[0]
        result["unauthorized_effects"] = self.conn.execute(
            "SELECT COALESCE(SUM(unauthorized),0) FROM effect").fetchone()[0]
        return result

    def close(self):
        self.conn.close()

"""Historical checkpoint views use a separate, current authority database.

This is not permission to replace the authority DB with a stale backup. All
execution and mutable business tables remain in the current authority DB.
"""
import json

from .errors import AsgError
from .purge import Purger
from .projection import Projection


class HistoricalView:
    def __init__(self, checkpoint, authority):
        if checkpoint is authority:
            raise ValueError("Checkpoint and authority connections must be distinct")
        self.checkpoint = checkpoint
        self.authority = authority

    def read_record(self, workspace_id, record_id):
        old = self.checkpoint.execute("SELECT * FROM record WHERE record_id=? AND workspace_id=?",
                                      (record_id, workspace_id)).fetchone()
        if old is None:
            raise AsgError("E801", "Record is absent from the historical checkpoint")
        # Missing current policy/record is fail-closed; immutable record identity
        # never gives permission to read a previously purged body.
        Purger(self.authority, None).read_record(workspace_id, record_id)
        result = dict(old)
        result["payload"] = json.loads(result["payload"])
        return result

    def projection(self, workspace_id):
        return Projection(self.checkpoint).replay(workspace_id)

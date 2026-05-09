import sqlite3
import os
import threading
import config
from models.schema import SchemaMixin, _migrate_db_to_subdir
from models.mixins import (
    EvaluationMixin,
    TestingMixin,
    ToolsMixin,
    AgentMixin,
    ChannelMixin,
    ChatDelegationMixin,
    SettingsMixin,
    ScheduleMixin,
    DashboardMixin,
    ModelsMixin,
    WorkplaceMixin,
    SafetyRuleMixin,
)


class Database(
    SchemaMixin,
    EvaluationMixin,
    TestingMixin,
    ToolsMixin,
    AgentMixin,
    ChannelMixin,
    ChatDelegationMixin,
    SettingsMixin,
    ScheduleMixin,
    DashboardMixin,
    ModelsMixin,
    WorkplaceMixin,
    SafetyRuleMixin,
):
    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        self._tlocal = threading.local()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        _migrate_db_to_subdir(db_path)
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        """Return a thread-local cached connection with WAL mode and busy timeout.

        The connection is created once per thread and reused across all
        ``with self._connect() as conn:`` calls.  sqlite3.Connection.__exit__
        handles transactions but does not close, so the connection stays
        alive for the lifetime of the thread.
        """
        existing = getattr(self._tlocal, "conn", None)
        if existing is not None:
            existing.row_factory = None
            return existing
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode=WAL")
        self._tlocal.conn = conn
        return conn


# Re-export chat classes for backward compatibility
from models.chat import AgentChatDB, AgentChatManager, agent_chat_manager  # noqa: F401

# Global singleton
db = Database()

"""Schema 2 for local work data; releases do not migrate live schema-1 data.

Migration accepts an already isolated schema-1 DB only after explicit constructor
consent. The transaction adds tables and changes user_version, never old rows.
"""
from __future__ import annotations
import contextlib
import hashlib
import sqlite3
import uuid
from pathlib import Path

SCHEMA_VERSION = 2
COMMERCE_SQL = """
CREATE TABLE commerce_offerings(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,title TEXT NOT NULL,offering_kind TEXT NOT NULL,program_id TEXT,details_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE commerce_slots(id TEXT PRIMARY KEY,offering_id TEXT NOT NULL,revision INTEGER NOT NULL,schedule_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(offering_id) REFERENCES commerce_offerings(id));
CREATE TABLE commerce_requests(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,source TEXT NOT NULL,server_request_id TEXT UNIQUE,server_revision INTEGER,offering_id TEXT NOT NULL,contact_json TEXT NOT NULL,participants INTEGER,review_status TEXT NOT NULL,booking_status TEXT NOT NULL,payment_status TEXT NOT NULL,details_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(offering_id) REFERENCES commerce_offerings(id));
CREATE TABLE commerce_request_history(request_id TEXT NOT NULL,revision INTEGER NOT NULL,snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(request_id,revision),FOREIGN KEY(request_id) REFERENCES commerce_requests(id));
CREATE TABLE commerce_orders(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,revision INTEGER NOT NULL,details_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(request_id) REFERENCES commerce_requests(id));
CREATE TABLE commerce_inbox(server_epoch TEXT NOT NULL,event_id TEXT NOT NULL,entity_id TEXT NOT NULL,server_revision INTEGER NOT NULL,payload_hash TEXT NOT NULL,snapshot_json TEXT NOT NULL,applied_at TEXT NOT NULL,PRIMARY KEY(server_epoch,event_id));
CREATE TABLE commerce_outbox(operation_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,base_server_revision INTEGER,payload_hash TEXT NOT NULL,payload_json TEXT NOT NULL,state TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(request_id) REFERENCES commerce_requests(id));
CREATE TABLE commerce_sync_state(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
"""
COMMERCE_TABLES = frozenset({"commerce_offerings", "commerce_slots", "commerce_requests",
    "commerce_request_history", "commerce_orders", "commerce_inbox", "commerce_outbox", "commerce_sync_state"})

class MigrationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

def add_tables(conn):
    # executescript implicitly commits: execute each fixed statement instead.
    for statement in COMMERCE_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)

def initialize_or_migrate(store, base_sql, *, confirmed=False, fault_hook=None):
    """Fresh DB→2; schema1→2 only with explicit consent + preserved local DB.

    Validates old normative schema and integrity before touching it. The backup
    contains the existing PIN and stays in local backups; it is never portable.
    fault_hook('after_tables') is for synthetic rollback tests only.
    """
    with store.lock, store.connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version == 2:
            from whole_backup import WholeBackup
            WholeBackup(store, schema_version=2)._check_db(conn)
            return {"migrated": False, "schema_version": 2}
        if version not in (0, 1):
            raise MigrationError("schema_unsupported", "이 개발 앱에서 지원하지 않는 DB 버전입니다.")
        if version == 0 and tables:
            raise MigrationError("unversioned_database", "버전이 없는 기존 DB는 자동 변경하지 않습니다.")
        backup = None
        if version == 1:
            if confirmed is not True:
                raise MigrationError("migration_confirmation_required", "schema1 사본의 명시적 이관 승인이 필요합니다.")
            from whole_backup import WholeBackup
            WholeBackup(store, schema_version=1)._check_db(conn)
            if "ai_jobs" in tables and conn.execute("SELECT 1 FROM ai_jobs WHERE state IN ('queued','running','cancelling')").fetchone():
                raise MigrationError("migration_busy", "진행 상태인 AI 작업이 있어 이관을 중단했습니다.")
            backup = store.backup_dir / ("before_commerce_migration_" + uuid.uuid4().hex + ".sqlite3")
            # Exclusive destination reservation; only this new file is owned.
            with backup.open("xb"):
                pass
            with contextlib.closing(sqlite3.connect(backup)) as target:
                conn.backup(target)
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if version == 0:
                for statement in base_sql.split(";"):
                    if statement.strip(): conn.execute(statement)
            add_tables(conn)
            if fault_hook: fault_hook("after_tables")
            conn.execute("PRAGMA user_version=2")
            from whole_backup import WholeBackup
            WholeBackup(store, schema_version=2)._check_db(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        conn.execute("PRAGMA journal_mode=WAL")
        return {"migrated": version == 1, "schema_version": 2,
            "safety_database": str(backup) if backup else None,
            "safety_sha256": digest if backup else None,
            "original_rows_changed": False, "original_files_changed": False,
            "safety_pin_included_local_only": bool(backup)}

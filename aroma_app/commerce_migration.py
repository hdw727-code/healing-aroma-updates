"""Explicit offline conversion of a schema-1 portable backup copy.

Never restores a live database. Original archive bytes and PIN stay untouched.
Both backup archive formats 1 (content absent) and 2 (current content) retain
their content semantics; the local DB is migrated to schema 2 in a new archive.
"""
from __future__ import annotations
import contextlib
import hashlib
import os
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from commerce_schema import add_tables
from whole_backup import WholeBackup, canonical, digest_file, BACKUP_NAME

def convert_schema1_backup(store, source_path, *, confirmed=False, fault_hook=None):
    if confirmed is not True:
        raise ValueError("Explicit schema-1 backup conversion consent is required")
    # Accept only a previously imported copy in this app's backup directory.
    # Original user-selected archive paths are never opened by conversion.
    source=Path(source_path)
    backups=Path(store.backup_dir).resolve()
    if (not BACKUP_NAME.fullmatch(source.name) or source.resolve().parent != backups
            or source.is_symlink() or source.is_junction() or not source.is_file()
            or source.stat().st_nlink != 1):
        raise ValueError("Only imported regular backup copies are accepted")
    source_hash=digest_file(source)
    with store.lock:
        store.require_runtime_idle()
        if store.recovery_pending():raise ValueError("Pending restore must be resolved first")
        with tempfile.TemporaryDirectory(prefix="conversion_",dir=store.backup_dir) as temporary:
            stage=Path(temporary)/"validated"
            manifest=WholeBackup(store,schema_version=1).validate(source,stage)
            database=stage/"office.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    add_tables(conn)
                    if fault_hook:fault_hook("after_tables")
                    conn.execute("PRAGMA user_version=2")
                    WholeBackup(store,schema_version=2)._check_db(conn,require_no_pin=True)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            for row in manifest["members"]:
                if row["path"]=="office.sqlite3":
                    row["size"]=database.stat().st_size;row["sha256"]=digest_file(database)
            manifest["schema_version"]=2
            manifest["scope"]={**manifest.get("scope",{}),"commerce_database":True,"sync_credentials":False,"admin_pin":False}
            manifest["conversion"]={"source_schema_version":1,"source_archive_sha256":source_hash,"explicit_offline_copy":True}
            stamp=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            name="aroma_full_"+stamp+"_"+uuid.uuid4().hex[:12]+".zip"
            assert BACKUP_NAME.fullmatch(name)
            target=store.backup_dir/name;owned=False
            try:
                with target.open("xb") as output:
                    owned=True
                    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
                        for row in manifest["members"]:
                            archive.write(stage.joinpath(*row["path"].split("/")),row["path"])
                        archive.writestr("manifest.json",canonical(manifest))
                    output.flush();os.fsync(output.fileno())
                WholeBackup(store,schema_version=2).validate(target,Path(temporary)/"revalidated")
                if digest_file(source)!=source_hash:raise ValueError("Source archive changed during conversion")
                return {"converted":True,"name":name,"path":str(target),"schema_version":2,
                    "original_archive_unchanged":True,"admin_pin_included":False,"live_database_changed":False,
                    "content_preserved_on_restore":manifest["format_version"]==1,
                    "format":"aroma-full-work-backup", "format_version":manifest["format_version"],
                    "local_program_content_included":manifest["format_version"]==2,
                    "local_file_metadata_included":manifest["format_version"]==2}
            except BaseException:
                if owned:target.unlink(missing_ok=True)
                raise

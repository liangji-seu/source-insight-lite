from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .models import Symbol

# Kept as explicit schema SQL so the database can be inspected independently by the extension.
SCHEMA = """
PRAGMA journal_mode = DELETE;
CREATE TABLE symbols (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
 qualified_name TEXT NOT NULL, file TEXT NOT NULL, start_line INTEGER NOT NULL,
 end_line INTEGER NOT NULL, parent_id TEXT, module_id TEXT, details_json TEXT NOT NULL
);
CREATE INDEX symbols_kind_idx ON symbols(kind);
CREATE INDEX symbols_parent_idx ON symbols(parent_id);
CREATE INDEX symbols_name_idx ON symbols(name);
CREATE INDEX symbols_qualified_idx ON symbols(qualified_name);
CREATE INDEX symbols_module_idx ON symbols(module_id);
CREATE TABLE relationships (
 source_id TEXT NOT NULL, target_id TEXT, type TEXT NOT NULL, status TEXT NOT NULL,
 confidence TEXT NOT NULL, label TEXT NOT NULL, file TEXT NOT NULL, line INTEGER NOT NULL
);
CREATE INDEX relationships_source_idx ON relationships(source_id);
CREATE INDEX relationships_target_idx ON relationships(target_id);
CREATE INDEX relationships_type_idx ON relationships(type);
CREATE TABLE errors (file TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
"""


class _Connection(sqlite3.Connection):
    """Connection context manager that also closes the handle on exit."""

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Store:
    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).expanduser().resolve())

    def replace(self, symbols: list[Symbol], relationships: list[Any], errors: list[dict[str, str]],
               summary: dict[str, Any]) -> None:
        destination = Path(self.db_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent))
        os.close(fd)
        try:
            db = sqlite3.connect(temp_name)
            try:
                db.executescript(SCHEMA)
                db.executemany(
                    "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(s.id, s.kind, s.name, s.qualified_name, s.file, s.start_line,
                      s.end_line, s.parent_id, s.module_id, json.dumps(s.details, ensure_ascii=False))
                     for s in symbols],
                )
                db.executemany(
                    "INSERT INTO relationships VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(r.source_id, r.target_id, r.type, r.status, r.confidence, r.label, r.file, r.line)
                     for r in relationships],
                )
                db.executemany("INSERT INTO errors VALUES (?, ?)",
                               [(e["file"], e["message"]) for e in errors])
                db.execute("INSERT INTO meta VALUES (?, ?)", ("summary", json.dumps(summary, ensure_ascii=False)))
                db.commit()
            finally:
                db.close()
            os.replace(temp_name, destination)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, factory=_Connection)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def symbol(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"], "kind": row["kind"], "name": row["name"],
            "qualifiedName": row["qualified_name"], "file": row["file"],
            "startLine": row["start_line"], "endLine": row["end_line"],
            "parentId": row["parent_id"], "moduleId": row["module_id"],
            "details": json.loads(row["details_json"]),
        }

    @staticmethod
    def relationship(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sourceId": row["source_id"], "targetId": row["target_id"], "type": row["type"],
            "status": row["status"], "confidence": row["confidence"], "label": row["label"],
            "file": row["file"], "line": row["line"],
        }


# SPDX-License-Identifier: GPL-3.0-only
"""SQLite repository: single-file authoritative store (MVP stand-in for PostgreSQL).

Swap-in path: same method signatures against Postgres later; JSON columns map to JSONB.
State updates and Outbox inserts happen in ONE transaction (doc section 05).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from ..schemas.core import (
    Asset, AssetBinding, Character, DecisionAdvice, DirectorReview, Event,
    Project, RepairPlan, Run, Scene, ScoreReport, Shot, StepRun, UploadSession,
    now_ts,
)

TABLES = {
    "projects": ("project_id", Project),
    "scenes": ("scene_id", Scene),
    "shots": ("shot_id", Shot),
    "characters": ("character_id", Character),
    "assets": ("asset_id", Asset),
    "bindings": ("binding_id", AssetBinding),
    "uploads": ("upload_id", UploadSession),
    "runs": ("run_id", Run),
    "step_runs": ("step_run_id", StepRun),
    "events": ("event_id", Event),
    "decisions": ("decision_id", DecisionAdvice),
    "score_reports": ("run_id", ScoreReport),
    "repair_plans": ("run_id", RepairPlan),
    "director_reviews": ("report_id", DirectorReview),
}


class Store:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        for table, (pk, _model) in TABLES.items():
            self._db.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ({pk} TEXT PRIMARY KEY, doc TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS outbox ("
                         "outbox_id TEXT PRIMARY KEY, published INTEGER DEFAULT 0, doc TEXT NOT NULL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_events_seq ON events(json_extract(doc,'$.seq'))")
        self._db.commit()

    def _check_open(self) -> None:
        """关闭后的访问直接报错:进程退出阶段后台线程仍可能碰库,
        访问已关闭的 sqlite 连接会直接段错误(而不是抛异常)。"""
        if self._closed:
            raise RuntimeError("store is closed")

    # ------------------------------------------------------------ generics --
    def _put(self, table: str, obj) -> None:
        pk, _ = TABLES[table]
        doc = obj.model_dump_json()
        with self._lock:
            self._check_open()
            self._db.execute(f"INSERT OR REPLACE INTO {table} ({pk}, doc) VALUES (?, ?)",
                             (getattr(obj, pk), doc))
            self._db.commit()

    def _get(self, table: str, key: str):
        pk, model = TABLES[table]
        with self._lock:
            self._check_open()
            row = self._db.execute(f"SELECT doc FROM {table} WHERE {pk}=?", (key,)).fetchone()
        return model.model_validate_json(row["doc"]) if row else None

    def _all(self, table: str, **where) -> list:
        pk, model = TABLES[table]
        with self._lock:
            self._check_open()
            rows = self._db.execute(f"SELECT doc FROM {table}").fetchall()
        out = [model.model_validate_json(r["doc"]) for r in rows]
        for k, v in where.items():
            out = [o for o in out if getattr(o, k, None) == v]
        return out

    # ------------------------------------------------- state + outbox (tx) --
    def transition_run(self, run_id: str, to_state: str, event: Event,
                       patch: Optional[dict[str, Any]] = None) -> Run:
        """Run state change + event + outbox in a single transaction."""
        from ..state_machine.machine import transition
        with self._lock:
            self._check_open()
            run = self._get("runs", run_id)
            if run is None:
                raise KeyError(run_id)
            run.state = transition(run.state, to_state)  # raises on illegal
            run.state_version += 1
            run.updated_at = now_ts()
            for k, v in (patch or {}).items():
                setattr(run, k, v)
            event.state_version = run.state_version
            event.seq = self._next_seq_locked()
            self._db.execute("INSERT OR REPLACE INTO runs (run_id, doc) VALUES (?, ?)",
                             (run.run_id, run.model_dump_json()))
            self._db.execute("INSERT OR REPLACE INTO events (event_id, doc) VALUES (?, ?)",
                             (event.event_id, event.model_dump_json()))
            self._db.execute("INSERT INTO outbox (outbox_id, published, doc) VALUES (?, 0, ?)",
                             (f"out_{event.event_id}", event.model_dump_json()))
            self._db.commit()
            return run

    def _next_seq_locked(self) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(json_extract(doc,'$.seq')),0) AS m FROM events").fetchone()
        return int(row["m"]) + 1

    def append_event(self, event: Event) -> Event:
        with self._lock:
            self._check_open()
            event.seq = self._next_seq_locked()
            self._db.execute("INSERT OR REPLACE INTO events (event_id, doc) VALUES (?, ?)",
                             (event.event_id, event.model_dump_json()))
            self._db.execute("INSERT INTO outbox (outbox_id, published, doc) VALUES (?, 0, ?)",
                             (f"out_{event.event_id}", event.model_dump_json()))
            self._db.commit()
        return event

    def events_since(self, project_id: str, seq: int = 0, limit: int = 500) -> list[Event]:
        with self._lock:
            self._check_open()
            rows = self._db.execute("SELECT doc FROM events").fetchall()
        out = [Event.model_validate_json(r["doc"]) for r in rows]
        return sorted([e for e in out if e.project_id == project_id and e.seq > seq],
                      key=lambda e: e.seq)[:limit]

    def unpublish_pending(self) -> list[Event]:
        with self._lock:
            self._check_open()
            rows = self._db.execute("SELECT doc FROM outbox WHERE published=0").fetchall()
        return [Event.model_validate_json(r["doc"]) for r in rows]

    def mark_published(self, event_id: str) -> None:
        with self._lock:
            self._check_open()
            self._db.execute("UPDATE outbox SET published=1 WHERE outbox_id=?",
                             (f"out_{event_id}",))
            self._db.commit()

    # ------------------------------------------------------- typed helpers --
    def put(self, table: str, obj) -> None:
        self._put(table, obj)

    def get(self, table: str, key: str):
        return self._get(table, key)

    def all(self, table: str, **where) -> list:
        return self._all(table, **where)

    def delete(self, table: str, key: str) -> bool:
        pk, _ = TABLES[table]
        with self._lock:
            self._check_open()
            cur = self._db.execute(f"DELETE FROM {table} WHERE {pk}=?", (key,))
            self._db.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()

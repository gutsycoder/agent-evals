"""SQLite execution ledger. Stdlib sqlite3 only, no ORM.

This is a ledger rather than a passive log: the solver's captured diff is a first-class
column written *at the moment it exists* (before grading), which is what makes
`evals resume` / `evals replay` possible without re-paying for LLM inference.

Two columns that are deliberately separate:
  status  - lifecycle: RUNNING | PATCH_CAPTURED | SUCCESS | ERROR
  verdict - outcome:   SOLVED | PARTIAL | UNSOLVED | REGRESSION  (run/resume/replay/grade)
Conflating them makes "did this crash?" unanswerable for a run that produced a verdict,
and leaves `init`/`validate` (which have a lifecycle but no verdict) with nowhere to sit.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path("runs.db")

# Lifecycle values for `status`.
STATUS_RUNNING = "RUNNING"
STATUS_PATCH_CAPTURED = "PATCH_CAPTURED"  # the resume checkpoint
STATUS_SUCCESS = "SUCCESS"
STATUS_ERROR = "ERROR"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    command       TEXT NOT NULL,
    task_id       TEXT,
    status        TEXT NOT NULL,
    verdict       TEXT,
    args_json     TEXT NOT NULL,
    solver_name   TEXT,
    provider      TEXT,
    model_id      TEXT,
    captured_diff TEXT,
    patch_applied INTEGER,
    patch_error   TEXT,
    results_json  TEXT,
    artifacts_dir TEXT,
    source_run_id INTEGER,
    log           TEXT
);
"""

# Columns `update_run` is allowed to set. An explicit allowlist rather than interpolating
# caller-supplied keys into SQL - callers are internal today, but this is the pattern that
# becomes an injection bug the first time a key comes from anywhere else.
_UPDATABLE_COLUMNS = frozenset({
    "status", "verdict", "solver_name", "provider", "model_id", "captured_diff",
    "patch_applied", "patch_error", "results_json", "artifacts_dir", "source_run_id", "log",
})

# Columns added after v1. Existing databases are migrated in place: CREATE TABLE IF NOT
# EXISTS is a no-op on an existing table, so without this an older runs.db would fail with
# "no such column" on the first ledger write.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
    ("verdict", "TEXT"),
    ("solver_name", "TEXT"),
    ("provider", "TEXT"),
    ("model_id", "TEXT"),
    ("captured_diff", "TEXT"),
    ("patch_applied", "INTEGER"),
    ("patch_error", "TEXT"),
    ("artifacts_dir", "TEXT"),
    ("source_run_id", "INTEGER"),
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunDB:
    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        # WAL lets a reader (e.g. `evals history` in another shell) proceed while a run
        # writes, and reduces "database is locked" under concurrent runs.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass  # e.g. a filesystem that doesn't support WAL; the default journal is fine
        self._conn.execute(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add any columns missing from an older runs.db. Idempotent, no data loss."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(runs)")}
        for column, decl in _MIGRATIONS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {decl}")
        # v1 stored a single `ts NOT NULL`. SQLite can't drop a column in older versions,
        # so the legacy column stays and inserts must keep satisfying its NOT NULL
        # constraint - hence `_has_legacy_ts`, used by create_run.
        self._has_legacy_ts = "ts" in existing
        if self._has_legacy_ts:
            self._conn.execute(
                "UPDATE runs SET created_at = ts WHERE created_at IS NULL AND ts IS NOT NULL"
            )
            self._conn.execute(
                "UPDATE runs SET updated_at = ts WHERE updated_at IS NULL AND ts IS NOT NULL"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "RunDB":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def create_run(self, command: str, task_id: Optional[str], args: dict[str, Any]) -> int:
        now = _utcnow()
        columns = ["created_at", "updated_at", "command", "task_id", "args_json", "status"]
        values: list[Any] = [now, now, command, task_id, json.dumps(args), STATUS_RUNNING]
        if getattr(self, "_has_legacy_ts", False):
            # A v1 database still carries `ts NOT NULL`; keep satisfying it so existing
            # databases keep working instead of forcing users to delete runs.db.
            columns.append("ts")
            values.append(now)
        placeholders = ", ".join("?" for _ in columns)
        cur = self._conn.execute(
            f"INSERT INTO runs ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        self._conn.commit()
        return cur.lastrowid

    def update_run(self, run_id: int, **fields: Any) -> None:
        """Set specific ledger columns mid-run (e.g. the captured diff at the checkpoint).

        `results` is accepted as a convenience alias and JSON-encoded into results_json.
        """
        if "results" in fields:
            results = fields.pop("results")
            fields["results_json"] = json.dumps(results) if results is not None else None

        unknown = set(fields) - _UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(
                f"update_run got non-updatable column(s): {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(_UPDATABLE_COLUMNS))}"
            )
        if not fields:
            return

        fields["updated_at"] = _utcnow()
        assignments = ", ".join(f"{col} = ?" for col in fields)
        self._conn.execute(
            f"UPDATE runs SET {assignments} WHERE run_id = ?",
            (*fields.values(), run_id),
        )
        self._conn.commit()

    def finish_run(
        self,
        run_id: int,
        status: str,
        results: Optional[dict[str, Any]] = None,
        log: Optional[str] = None,
        verdict: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            """UPDATE runs SET status = ?, verdict = ?, results_json = ?, log = ?, updated_at = ?
               WHERE run_id = ?""",
            (
                status,
                verdict,
                json.dumps(results) if results is not None else None,
                log,
                _utcnow(),
                run_id,
            ),
        )
        self._conn.commit()

    def get_run(self, run_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def latest_run_id(
        self, *, with_diff: bool = False, with_log: bool = False, task_like: Optional[str] = None
    ) -> Optional[int]:
        """Most recent run matching the given conditions, or None.

        Exists so `resume`, `replay` and `logs` can default to "the obvious one" instead of
        making the user hand-write a SQL query to discover an id. `with_diff` is what
        re-grading needs (a run that got far enough to capture a patch); `with_log` is what
        `logs` needs, since a row that errored early has nothing to show.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if with_diff:
            clauses.append("captured_diff IS NOT NULL AND captured_diff != ''")
        if with_log:
            clauses.append("log IS NOT NULL AND log != ''")
        if task_like:
            clauses.append("task_id LIKE ?")
            params.append(f"%{task_like}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._conn.execute(
            f"SELECT run_id FROM runs {where} ORDER BY run_id DESC LIMIT 1", params
        ).fetchone()
        return row["run_id"] if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        args_json = d.pop("args_json", None)
        d["args"] = json.loads(args_json) if args_json else {}
        results_json = d.pop("results_json", None)
        d["results"] = json.loads(results_json) if results_json else None
        # v1 rows only have `ts`; expose a single timestamp either way.
        d.setdefault("created_at", d.get("ts"))
        return d

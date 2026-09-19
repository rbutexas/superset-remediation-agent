"""SQLite persistence.

Three tables, and the third is the interesting one.

`findings` and `sessions` hold current state — what we know right now. `events`
is an append-only log of every transition observed. Current state alone cannot
answer "how long did this take?" or "how many sessions stalled before finishing?",
and those are the questions an engineering leader actually asks. State tells you
where things are; the log tells you how they got there.

SQLite because the whole point is that this survives a restart without operating
a database. Devin has no outbound webhook, so a collector that loses its memory
loses history permanently — there is nothing to replay from.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sqlite3
import threading
import time
from typing import Any, Iterator

from .models import SessionRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    key             TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    detector        TEXT NOT NULL,
    severity        TEXT NOT NULL,
    scanner_hint    TEXT,
    route           TEXT,
    route_reasons   TEXT,
    issue_number    INTEGER,
    issue_url       TEXT,
    first_seen      INTEGER NOT NULL,
    last_seen       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    stage               TEXT,
    finding_key         TEXT,
    issue_number        INTEGER,
    status              TEXT NOT NULL,
    status_detail       TEXT,
    acus                REAL NOT NULL DEFAULT 0,
    url                 TEXT,
    title               TEXT,
    triage_decision     TEXT,
    remediation_outcome TEXT,
    structured_output   TEXT,
    pull_requests       TEXT,
    created_at          INTEGER,
    updated_at          INTEGER,
    first_seen          INTEGER NOT NULL,
    last_seen           INTEGER NOT NULL,
    completed_at        INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    subject  TEXT NOT NULL,
    detail   TEXT
);

CREATE INDEX IF NOT EXISTS ix_sessions_finding ON sessions(finding_key);
CREATE INDEX IF NOT EXISTS ix_sessions_stage   ON sessions(stage);
CREATE INDEX IF NOT EXISTS ix_events_subject   ON events(subject);
CREATE INDEX IF NOT EXISTS ix_events_kind      ON events(kind);
"""


def now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: pathlib.Path, *, read_only: bool | None = None) -> None:
        """Open the store, read-only when the file cannot be written.

        `read_only` is detected rather than declared, because the case that
        needs it is one nobody remembers to declare: the committed evidence
        snapshot is mounted read-only into the replay container, and SQLite
        cannot open it at all if the connection tries to set a pragma or run
        the schema script. That failed as `unable to open database file`, which
        names neither the mount nor the write.

        Pass it explicitly to assert the intent; leave it None to work it out.
        """
        self.path = path
        self._lock = threading.Lock()

        if read_only is None:
            read_only = path.exists() and not os.access(path.parent, os.W_OK)
        self.read_only = read_only

        if read_only:
            # immutable=1 also tells SQLite there is no -wal to look for, which
            # there is not: the snapshot is checkpointed before it is copied.
            uri = f"file:{path}?mode=ro&immutable=1"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        # `serve` runs the collector on a background thread while the HTTP
        # server answers on others. A connection pinned to its creating thread
        # raises ProgrammingError on every tick — silently, in a thread nobody
        # is reading — so the pipeline just quietly stops advancing.
        # check_same_thread=False lifts the pin; the lock restores the safety it
        # was providing. Serialising writes is fine at a handful of rows per
        # poll, and WAL keeps reads concurrent with them.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise RuntimeError(f"{self.path} is open read-only")
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ---------------------------------------------------------------- events

    def log(self, kind: str, subject: str, detail: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events (ts, kind, subject, detail) VALUES (?,?,?,?)",
                (now(), kind, subject, detail),
            )

    def events(self, subject: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM events"
        args: tuple = ()
        if subject:
            sql += " WHERE subject = ?"
            args = (subject,)
        return list(self.conn.execute(sql + " ORDER BY ts, id", args))

    # -------------------------------------------------------------- findings

    def upsert_finding(self, key: str, title: str, detector: str, severity: str,
                       scanner_hint: str | None = None, route: str | None = None,
                       route_reasons: list[str] | None = None) -> bool:
        """Insert or refresh a finding. Returns True if it is new.

        `first_seen` is preserved across runs — it is how the report answers
        "how long did this sit before anyone acted on it?"
        """
        ts = now()
        reasons = json.dumps(route_reasons or [])
        with self.tx() as c:
            row = c.execute("SELECT key FROM findings WHERE key = ?", (key,)).fetchone()
            if row:
                c.execute(
                    "UPDATE findings SET title=?, detector=?, severity=?, "
                    "scanner_hint=?, route=?, route_reasons=?, last_seen=? WHERE key=?",
                    (title, detector, severity, scanner_hint, route, reasons, ts, key),
                )
                return False
            c.execute(
                "INSERT INTO findings (key, title, detector, severity, scanner_hint, "
                "route, route_reasons, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, title, detector, severity, scanner_hint, route, reasons, ts, ts),
            )
        self.log("finding.detected", key, title)
        return True

    def attach_issue(self, key: str, number: int, url: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE findings SET issue_number=?, issue_url=? WHERE key=?",
                      (number, url, key))
        self.log("issue.filed", key, f"#{number} {url}")

    def findings(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM findings ORDER BY first_seen"))

    def finding(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM findings WHERE key = ?", (key,)).fetchone()

    # -------------------------------------------------------------- sessions

    def record_session(self, rec: SessionRecord) -> list[str]:
        """Upsert a session, returning the transitions observed.

        Only genuine changes are logged. The collector polls on a timer, so most
        observations are identical to the last one and writing those would drown
        the event log in noise.
        """
        ts = now()
        row = rec.to_row()
        transitions: list[str] = []

        with self.tx() as c:
            prior = c.execute(
                "SELECT status, status_detail, acus, triage_decision, "
                "remediation_outcome, completed_at FROM sessions WHERE session_id=?",
                (rec.session_id,),
            ).fetchone()

            completed_at = prior["completed_at"] if prior else None
            if rec.is_terminal and completed_at is None:
                completed_at = ts

            if prior is None:
                transitions.append(f"discovered ({rec.status})")
            else:
                if (prior["status"], prior["status_detail"]) != (rec.status, rec.status_detail):
                    transitions.append(
                        f"{prior['status']}/{prior['status_detail']} -> "
                        f"{rec.status}/{rec.status_detail}")
                if rec.triage_decision and not prior["triage_decision"]:
                    transitions.append(f"triage decided: {rec.triage_decision.value}")
                if rec.remediation_outcome and not prior["remediation_outcome"]:
                    transitions.append(f"remediation outcome: {rec.remediation_outcome.value}")
                if rec.acus > (prior["acus"] or 0):
                    transitions.append(f"acus {prior['acus']:.2f} -> {rec.acus:.2f}")

            c.execute(
                """INSERT INTO sessions (
                       session_id, stage, finding_key, issue_number, status,
                       status_detail, acus, url, title, triage_decision,
                       remediation_outcome, structured_output, pull_requests,
                       created_at, updated_at, first_seen, last_seen, completed_at)
                   VALUES (:session_id,:stage,:finding_key,:issue_number,:status,
                           :status_detail,:acus,:url,:title,:triage_decision,
                           :remediation_outcome,:structured_output,:pull_requests,
                           :created_at,:updated_at,:ts,:ts,:completed_at)
                   ON CONFLICT(session_id) DO UPDATE SET
                       stage=excluded.stage,
                       finding_key=COALESCE(excluded.finding_key, sessions.finding_key),
                       issue_number=COALESCE(excluded.issue_number, sessions.issue_number),
                       status=excluded.status,
                       status_detail=excluded.status_detail,
                       acus=MAX(excluded.acus, sessions.acus),
                       triage_decision=COALESCE(excluded.triage_decision,
                                                sessions.triage_decision),
                       remediation_outcome=COALESCE(excluded.remediation_outcome,
                                                    sessions.remediation_outcome),
                       structured_output=COALESCE(excluded.structured_output,
                                                  sessions.structured_output),
                       pull_requests=excluded.pull_requests,
                       updated_at=excluded.updated_at,
                       last_seen=excluded.last_seen,
                       completed_at=excluded.completed_at""",
                {**row, "ts": ts, "completed_at": completed_at},
            )

        for transition in transitions:
            self.log("session.transition", rec.session_id, transition)
        return transitions

    def sessions(self, stage: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sessions"
        args: tuple = ()
        if stage:
            sql += " WHERE stage = ?"
            args = (stage,)
        return list(self.conn.execute(sql + " ORDER BY first_seen", args))

    def sessions_for(self, finding_key: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM sessions WHERE finding_key = ? ORDER BY first_seen",
            (finding_key,)))

    def latest_session(self, finding_key: str, stage: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE finding_key=? AND stage=? "
            "ORDER BY first_seen DESC LIMIT 1", (finding_key, stage)).fetchone()

    # ------------------------------------------------------------- reporting

    def counts(self) -> dict[str, Any]:
        def scalar(sql: str, *args: Any) -> Any:
            return self.conn.execute(sql, args).fetchone()[0]

        return {
            "findings": scalar("SELECT COUNT(*) FROM findings"),
            "issues_filed": scalar(
                "SELECT COUNT(*) FROM findings WHERE issue_number IS NOT NULL"),
            "sessions": scalar("SELECT COUNT(*) FROM sessions"),
            "sessions_complete": scalar(
                "SELECT COUNT(*) FROM sessions WHERE completed_at IS NOT NULL"),
            "total_acus": scalar("SELECT COALESCE(SUM(acus), 0) FROM sessions"),
        }

    @staticmethod
    def output(row: sqlite3.Row) -> dict[str, Any]:
        raw = row["structured_output"]
        return json.loads(raw) if raw else {}

    @staticmethod
    def prs(row: sqlite3.Row) -> list[str]:
        raw = row["pull_requests"]
        return json.loads(raw) if raw else []

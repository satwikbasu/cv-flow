"""The tracking store (SQLite) and the form-fields loader.

Every status change is routed through :mod:`cvflow.statemachine`, so an illegal move can never be
persisted: a discovered job can only become tailored, skipped, or expired. The form-fields loader
keeps the never-guess rule — an empty value is a known-but-unfilled field the bot must ask about,
never invent.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cvflow.analysis import JDAnalysis
from cvflow.statemachine import IllegalTransition, Status, transition

__all__ = [
    "Application",
    "ApplicationStore",
    "DuplicateJob",
    "UnknownJob",
    "FormFields",
    "MissingField",
]


class DuplicateJob(Exception):
    """Raised when adding a job_id that already exists."""


class UnknownJob(Exception):
    """Raised when operating on a job_id that is not in the store."""


class MissingField(Exception):
    """Raised when a form field is unknown or known-but-empty (never guess)."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Application:
    job_id: str
    company: str
    role: str
    jd_url: str
    status: Status
    discovered_at: str
    date_posted: str = ""
    date_posted_parsed: str | None = None
    tailored_at: str | None = None
    tailored_pdf_path: str | None = None
    benchmark: int | None = None
    fit_score: int | None = None
    fit_reason: str | None = None
    concerns: str | None = None          # JSON-encoded list[str]
    cohort: str | None = None            # "M" | "N"
    ctc_lpa: float | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    job_id             TEXT PRIMARY KEY,
    company            TEXT NOT NULL,
    role               TEXT NOT NULL,
    jd_url             TEXT NOT NULL,
    status             TEXT NOT NULL,
    discovered_at      TEXT NOT NULL,
    date_posted        TEXT NOT NULL DEFAULT '',
    date_posted_parsed TEXT,
    tailored_at        TEXT,
    tailored_pdf_path  TEXT,
    benchmark          INTEGER,
    fit_score          INTEGER,
    fit_reason         TEXT,
    concerns           TEXT,
    cohort             TEXT,
    ctc_lpa            REAL
);
CREATE TABLE IF NOT EXISTS jd_analyses (
    job_id    TEXT PRIMARY KEY REFERENCES applications(job_id),
    analysis  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_descriptions (
    job_id      TEXT PRIMARY KEY REFERENCES applications(job_id),
    description TEXT NOT NULL,
    scraped_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_slots (
    slot         INTEGER PRIMARY KEY,
    job_id       TEXT NOT NULL,
    presented_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS last_digest (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    digest       TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_cruxes (
    job_id        TEXT PRIMARY KEY,
    crux_json     TEXT NOT NULL,
    distilled_at  TEXT NOT NULL,
    crux_version  TEXT
);
"""

# Columns added to ``applications`` after the first release; added idempotently on open so a DB
# from an older version keeps working after a plain upgrade. All nullable or defaulted.
_ADDED_COLUMNS = {
    "date_posted": "TEXT NOT NULL DEFAULT ''",
    "date_posted_parsed": "TEXT",
    "tailored_at": "TEXT",
    "tailored_pdf_path": "TEXT",
    "benchmark": "INTEGER",
    "fit_score": "INTEGER",
    "fit_reason": "TEXT",
    "concerns": "TEXT",
    "cohort": "TEXT",
    "ctc_lpa": "REAL",
}


class ApplicationStore:
    """SQLite-backed store for tracked jobs, keyed by stable ``job_id``."""

    def __init__(self, db_path: str | Path) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(applications)")}
        for col, col_type in _ADDED_COLUMNS.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {col_type}")

    def _row_to_app(self, row: sqlite3.Row) -> Application:
        return Application(
            job_id=row["job_id"],
            company=row["company"],
            role=row["role"],
            jd_url=row["jd_url"],
            status=Status(row["status"]),
            discovered_at=row["discovered_at"],
            date_posted=row["date_posted"],
            date_posted_parsed=row["date_posted_parsed"],
            tailored_at=row["tailored_at"],
            tailored_pdf_path=row["tailored_pdf_path"],
            benchmark=row["benchmark"],
            fit_score=row["fit_score"],
            fit_reason=row["fit_reason"],
            concerns=row["concerns"],
            cohort=row["cohort"],
            ctc_lpa=row["ctc_lpa"],
        )

    def add(
        self,
        job_id: str,
        company: str,
        role: str,
        jd_url: str,
        *,
        date_posted: str = "",
        date_posted_parsed: str | None = None,
    ) -> Application:
        if self.exists(job_id):
            raise DuplicateJob(f"job_id already tracked: {job_id}")
        self._conn.execute(
            "INSERT INTO applications "
            "(job_id, company, role, jd_url, status, discovered_at, date_posted, "
            "date_posted_parsed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, company, role, jd_url, Status.DISCOVERED.value, _now(),
             date_posted, date_posted_parsed),
        )
        self._conn.commit()
        app = self.get(job_id)
        assert app is not None  # noqa: S101 — just inserted, always present (also narrows for mypy)
        return app

    def get(self, job_id: str) -> Application | None:
        row = self._conn.execute(
            "SELECT * FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._row_to_app(row) if row is not None else None

    def exists(self, job_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM applications WHERE job_id = ?", (job_id,)
            ).fetchone()
            is not None
        )

    def list_by_status(self, status: Status) -> list[Application]:
        rows = self._conn.execute(
            "SELECT * FROM applications WHERE status = ? ORDER BY discovered_at", (status.value,)
        ).fetchall()
        return [self._row_to_app(r) for r in rows]

    def _require(self, job_id: str) -> Application:
        app = self.get(job_id)
        if app is None:
            raise UnknownJob(f"no such job_id: {job_id}")
        return app

    def set_status(self, job_id: str, target: Status) -> None:
        """Apply a legal status change (validated by the state machine)."""
        app = self._require(job_id)
        new_status = transition(app.status, target)
        self._conn.execute(
            "UPDATE applications SET status = ? WHERE job_id = ?", (new_status.value, job_id)
        )
        self._conn.commit()

    def set_tailored(self, job_id: str, pdf_path: str) -> None:
        """Record a tailored resume: flip ``discovered`` to ``tailored`` and store the PDF path.

        Re-tailoring an already-tailored job updates the PDF without error; a skipped or expired
        job cannot be tailored.
        """
        app = self._require(job_id)
        if app.status is Status.DISCOVERED:
            transition(app.status, Status.TAILORED)
        elif app.status is not Status.TAILORED:
            raise IllegalTransition(f"cannot tailor a {app.status.value} job")
        self._conn.execute(
            "UPDATE applications SET status = ?, tailored_at = ?, tailored_pdf_path = ? "
            "WHERE job_id = ?",
            (Status.TAILORED.value, _now(), pdf_path, job_id),
        )
        self._conn.commit()

    def expire_stale(self, now: datetime, retention_days: int) -> int:
        """Mark untailored ``discovered`` jobs older than ``retention_days`` as ``expired``.

        Keyed off ``discovered_at`` (reliable), not the fuzzy posting date. Returns how many
        jobs were expired. Tailored/skipped jobs are untouched.
        """
        cutoff = (now - timedelta(days=retention_days)).isoformat()
        cur = self._conn.execute(
            "UPDATE applications SET status = ? WHERE status = ? AND discovered_at < ?",
            (Status.EXPIRED.value, Status.DISCOVERED.value, cutoff),
        )
        self._conn.commit()
        return cur.rowcount

    def set_discovery_meta(
        self, job_id: str, *, benchmark: int | None, fit_score: int | None,
        fit_reason: str | None, concerns: list[str] | None, cohort: str | None,
        ctc_lpa: float | None,
    ) -> None:
        """Persist discovery ranking signals (never touches status)."""
        self._require(job_id)
        self._conn.execute(
            "UPDATE applications SET benchmark = ?, fit_score = ?, fit_reason = ?, "
            "concerns = ?, cohort = ?, ctc_lpa = ? WHERE job_id = ?",
            (benchmark, fit_score, fit_reason, json.dumps(concerns or []),
             cohort, ctc_lpa, job_id),
        )
        self._conn.commit()

    def set_jd_text(self, job_id: str, description: str) -> None:
        """Store the raw scraped JD text for later tailoring (upsert)."""
        self._require(job_id)
        self._conn.execute(
            "INSERT INTO job_descriptions (job_id, description, scraped_at) VALUES (?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET description = excluded.description, "
            "scraped_at = excluded.scraped_at",
            (job_id, description, _now()),
        )
        self._conn.commit()

    def get_jd_text(self, job_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT description FROM job_descriptions WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row["description"] if row is not None else None

    def set_digest_slots(self, job_ids: list[str]) -> None:
        """Replace the ordinal -> job_id map (slot 1..N) for the most recently shown list."""
        with self._conn:
            self._conn.execute("DELETE FROM digest_slots")
            self._conn.executemany(
                "INSERT INTO digest_slots (slot, job_id, presented_at) VALUES (?, ?, ?)",
                [(i, jid, _now()) for i, jid in enumerate(job_ids, start=1)],
            )

    def get_digest_slot(self, slot: int) -> str | None:
        row = self._conn.execute(
            "SELECT job_id FROM digest_slots WHERE slot = ?", (slot,)
        ).fetchone()
        return row["job_id"] if row is not None else None

    def digest_slots(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT job_id FROM digest_slots ORDER BY slot"
        ).fetchall()
        return [r["job_id"] for r in rows]

    def set_last_digest(self, text: str) -> None:
        """Persist the most recent rendered digest verbatim (single row) for re-display."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO last_digest (id, digest, generated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "digest = excluded.digest, generated_at = excluded.generated_at",
                (text, _now()),
            )

    def get_last_digest(self) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT digest, generated_at FROM last_digest WHERE id = 1"
        ).fetchone()
        return (row["digest"], row["generated_at"]) if row is not None else None

    def save_analysis(self, job_id: str, analysis: JDAnalysis) -> None:
        self._require(job_id)
        self._conn.execute(
            "INSERT INTO jd_analyses (job_id, analysis) VALUES (?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET analysis = excluded.analysis",
            (job_id, analysis.to_json()),
        )
        self._conn.commit()

    def get_analysis(self, job_id: str) -> JDAnalysis | None:
        row = self._conn.execute(
            "SELECT analysis FROM jd_analyses WHERE job_id = ?", (job_id,)
        ).fetchone()
        return JDAnalysis.from_json(row["analysis"]) if row is not None else None

    def save_crux(self, job_id: str, crux_json: str, *, version: str = "") -> None:
        self._conn.execute(
            "INSERT INTO job_cruxes (job_id, crux_json, distilled_at, crux_version) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET crux_json = excluded.crux_json, "
            "distilled_at = excluded.distilled_at, crux_version = excluded.crux_version",
            (job_id, crux_json, _now(), version),
        )
        self._conn.commit()

    def get_crux(self, job_id: str, *, version: str | None = None) -> str | None:
        """Return the cached crux JSON; with ``version`` set, only if it matches (else None so
        the caller re-distills)."""
        row = self._conn.execute(
            "SELECT crux_json, crux_version FROM job_cruxes WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        if version is not None and (row["crux_version"] or "") != version:
            return None
        return str(row["crux_json"])


@dataclass(frozen=True)
class FormFields:
    """Recurring form values. An empty string is a known-but-unfilled field to ask about."""

    values: dict[str, str]

    @classmethod
    def load(cls, path: str | Path) -> FormFields:
        raw = json.loads(Path(path).read_text())
        values = {k: v for k, v in raw.items() if k != "_comment"}
        return cls(values=values)

    @property
    def populated(self) -> dict[str, str]:
        return {k: v for k, v in self.values.items() if v != ""}

    def missing(self) -> list[str]:
        return [k for k, v in self.values.items() if v == ""]

    def is_filled(self, key: str) -> bool:
        return self.values.get(key, "") != ""

    def require(self, key: str) -> str:
        if not self.is_filled(key):
            raise MissingField(
                f"form field {key!r} is unknown or empty; must be asked, never guessed"
            )
        return self.values[key]

"""SQLite-backed crawl history and send queue."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    """Remove query/fragment so tracking parameters do not defeat deduplication."""
    url, _ = urldefrag((url or "").strip())
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


class JobHistory:
    """Keep only the latest three successful crawl windows and a send queue."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS crawl_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS seen_jobs (
                    url TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    company TEXT,
                    title TEXT,
                    city TEXT,
                    job_json TEXT NOT NULL,
                    greeting TEXT NOT NULL DEFAULT '',
                    score INTEGER,
                    queue_status TEXT NOT NULL DEFAULT 'screened_out',
                    first_seen_run_id INTEGER NOT NULL,
                    last_seen_run_id INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_seen_jobs_last_run
                    ON seen_jobs(last_seen_run_id);
                CREATE INDEX IF NOT EXISTS idx_seen_jobs_queue
                    ON seen_jobs(queue_status);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def start_run(self) -> int:
        now = _now()
        with self._connect() as conn:
            result = conn.execute(
                "INSERT INTO crawl_runs (started_at, status) VALUES (?, 'running')", (now,)
            )
            return int(result.lastrowid)

    def recent_urls(self) -> set[str]:
        """URLs seen in the last three completed crawls, before the current run."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT url FROM seen_jobs
                WHERE last_seen_run_id IN (
                    SELECT id FROM crawl_runs
                    WHERE status = 'completed'
                    ORDER BY id DESC LIMIT 3
                )
                """
            ).fetchall()
        return {row["url"] for row in rows}

    def complete_run(self, run_id: int, new_jobs: list[dict], repeated_jobs: list[dict],
                     greetings: dict[str, str], statuses: dict[str, str] | None = None) -> None:
        """Persist this successful crawl, then retain only the newest three runs."""
        now = _now()
        statuses = statuses or {}
        with self._connect() as conn:
            for job in repeated_jobs:
                url = normalize_url(job.get("url", ""))
                if url:
                    conn.execute(
                        "UPDATE seen_jobs SET last_seen_run_id=?, last_seen_at=? WHERE url=?",
                        (run_id, now, url),
                    )

            for job in new_jobs:
                url = normalize_url(job.get("url", ""))
                if not url:
                    continue
                greeting = greetings.get(url, "")
                status = statuses.get(url, "ready_to_send" if greeting else "crawled")
                job_copy = dict(job)
                job_copy["url"] = url
                job_copy.pop("_greeting", None)
                import json
                conn.execute(
                    """
                    INSERT INTO seen_jobs (
                        url, platform, company, title, city, job_json, greeting, score, queue_status,
                        first_seen_run_id, last_seen_run_id, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        platform=excluded.platform, company=excluded.company, title=excluded.title,
                        city=excluded.city, job_json=excluded.job_json,
                        last_seen_run_id=excluded.last_seen_run_id, last_seen_at=excluded.last_seen_at
                    """,
                    (
                        url, job.get("platform", "boss"), job.get("company", ""), job.get("title", ""),
                        job.get("city", ""), json.dumps(job_copy, ensure_ascii=False), greeting,
                        job.get("score"), status, run_id, run_id, now, now,
                    ),
                )

            conn.execute(
                "UPDATE crawl_runs SET status='completed', finished_at=? WHERE id=?", (now, run_id)
            )
            keep_rows = conn.execute(
                "SELECT id FROM crawl_runs WHERE status='completed' ORDER BY id DESC LIMIT 3"
            ).fetchall()
            keep_ids = [row["id"] for row in keep_rows]
            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                conn.execute(f"DELETE FROM seen_jobs WHERE last_seen_run_id NOT IN ({placeholders})", keep_ids)
                conn.execute(
                    f"DELETE FROM crawl_runs WHERE status='completed' AND id NOT IN ({placeholders})", keep_ids
                )
            conn.execute("DELETE FROM crawl_runs WHERE status != 'completed'")

    def fail_run(self, run_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE crawl_runs SET status='failed', finished_at=? WHERE id=?", (_now(), run_id))

    def ready_jobs(self) -> list[dict]:
        return self._jobs_by_status("ready_to_send")

    def crawled_jobs(self) -> list[dict]:
        """Jobs crawled by an earlier run but not yet scored/processed."""
        return self._jobs_by_status("crawled")

    def _jobs_by_status(self, status: str) -> list[dict]:
        import json
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT url, job_json, greeting FROM seen_jobs WHERE queue_status=? ORDER BY last_seen_run_id, rowid",
                (status,),
            ).fetchall()
        jobs = []
        for row in rows:
            job = json.loads(row["job_json"])
            job["url"] = row["url"]
            job["_greeting"] = row["greeting"]
            jobs.append(job)
        return jobs

    def update_processing(self, jobs: list[dict], statuses: dict[str, str],
                          greetings: dict[str, str] | None = None) -> None:
        """Persist scoring/greeting state for already-crawled jobs."""
        import json
        greetings = greetings or {}
        now = _now()
        with self._connect() as conn:
            for job in jobs:
                url = normalize_url(job.get("url", ""))
                status = statuses.get(url)
                if not url or not status:
                    continue
                job_copy = dict(job)
                job_copy["url"] = url
                job_copy.pop("_greeting", None)
                greeting = greetings.get(url)
                if greeting is None:
                    conn.execute(
                        "UPDATE seen_jobs SET job_json=?, score=?, queue_status=?, last_seen_at=? WHERE url=?",
                        (json.dumps(job_copy, ensure_ascii=False), job.get("score"), status, now, url),
                    )
                else:
                    conn.execute(
                        "UPDATE seen_jobs SET job_json=?, score=?, greeting=?, queue_status=?, last_seen_at=? WHERE url=?",
                        (json.dumps(job_copy, ensure_ascii=False), job.get("score"), greeting, status, now, url),
                    )

    def mark_sent(self, urls: list[str]) -> None:
        if not urls:
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE seen_jobs SET queue_status='sent', sent_at=? WHERE url=?",
                [(_now(), normalize_url(url)) for url in urls],
            )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

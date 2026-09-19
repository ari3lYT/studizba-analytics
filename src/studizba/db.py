from __future__ import annotations

import gzip
import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .config import Config


_SENSITIVE_HTML_PATTERNS = (
    (re.compile(r"(dle_login_hash\s*=\s*')[^']*'"), r"\1REDACTED'"),
    (re.compile(r'(id="si_user_id"\s+value=")[^"]*'), r"\1REDACTED"),
    (re.compile(r'(id="si_user_hash"\s+value=")[^"]*'), r"\1REDACTED"),
    (re.compile(r'(id="si_p_user_key"\s+value=")[^"]*'), r"\1REDACTED"),
)


def sanitize_source_html(content: str) -> str:
    """Remove account/session fields while preserving parser-relevant source HTML."""
    for pattern, replacement in _SENSITIVE_HTML_PATTERNS:
        content = pattern.sub(replacement, content)
    return content


class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.cfg.database_url, row_factory=dict_row) as conn:
            yield conn

    def init(self) -> None:
        sql = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.execute(sql)

    def save_raw_page(
        self, conn: psycopg.Connection, url: str, kind: str, status: int, content: str, metadata: dict[str, Any] | None = None
    ) -> tuple[int, str]:
        raw = sanitize_source_html(content).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        row = conn.execute(
            """INSERT INTO raw_pages(source_url,kind,status_code,content_sha256,content,metadata)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT(source_url,content_sha256) DO UPDATE SET fetched_at=now()
               RETURNING id""",
            (url, kind, status, digest, gzip.compress(raw, 6), json.dumps(metadata or {})),
        ).fetchone()
        return int(row["id"]), digest

    @staticmethod
    def event(conn: psycopg.Connection, run_id: int | None, entity_type: str, entity_id: int | None, event_type: str, **details: Any) -> None:
        conn.execute(
            "INSERT INTO crawl_events(crawl_run_id,entity_type,entity_id,event_type,details) VALUES(%s,%s,%s,%s,%s)",
            (run_id, entity_type, entity_id, event_type, json.dumps(details)),
        )

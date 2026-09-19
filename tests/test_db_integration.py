import os

import pytest
from psycopg import Rollback

from studizba.collector import Collector
from studizba.config import load_config
from studizba.db import Database


pytestmark = pytest.mark.live


def test_review_unique_constraint_and_adaptive_reschedule():
    cfg = load_config()
    db = Database(cfg)
    with db.connect() as conn:
        with conn.transaction():
            uni = conn.execute("INSERT INTO universities(source_key,name,source_url) VALUES('test-uni','T','https://example.test') ON CONFLICT(source_key) DO UPDATE SET name='T' RETURNING id").fetchone()["id"]
            teacher = conn.execute("INSERT INTO teachers(university_id,source_id,name,normalized_name,source_url) VALUES(%s,999999999,'Test','test','https://example.test/t') ON CONFLICT(university_id,source_id) DO UPDATE SET name='Test' RETURNING id", (uni,)).fetchone()["id"]
            conn.execute("INSERT INTO crawl_targets(entity_type,entity_id,source_url) VALUES('teacher',%s,'https://example.test/t') ON CONFLICT(entity_type,entity_id) DO NOTHING", (teacher,))
            Collector._reschedule(conn, "teacher", teacher, False)
            row = conn.execute("SELECT consecutive_unchanged,next_due_at>now() future FROM crawl_targets WHERE entity_type='teacher' AND entity_id=%s", (teacher,)).fetchone()
            assert row["consecutive_unchanged"] >= 1 and row["future"]
            raise Rollback

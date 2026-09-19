from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .client import StudizbaClient, StudizbaUnavailable
from .config import Config
from .db import Database
from .models import DepartmentCard, TeacherCard, TeacherPage
from .parsers import content_hash, norm, normalized_name, parse_department, parse_departments, parse_reviews_fragment, parse_teacher


class Collector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg)

    def _start_run(self, mode: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute("INSERT INTO crawl_runs(mode) VALUES(%s) RETURNING id", (mode,)).fetchone()
            return int(row["id"])

    def _finish_run(self, run_id: int, status: str, stats: dict[str, Any], error: str | None = None) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE crawl_runs SET finished_at=now(),status=%s,stats=%s,error=%s WHERE id=%s",
                (status, json.dumps(stats), error, run_id),
            )

    def _ensure_university(self, conn) -> int:
        row = conn.execute(
            """INSERT INTO universities(source_key,name,source_url) VALUES(%s,%s,%s)
               ON CONFLICT(source_key) DO UPDATE SET last_seen_at=now(),active=true
               RETURNING id""",
            (self.cfg.university_slug, "МГТУ им. Н. Э. Баумана", f"{self.cfg.base_url}/hs/{self.cfg.university_slug}/"),
        ).fetchone()
        return int(row["id"])

    async def auth_check(self) -> dict[str, Any]:
        async with StudizbaClient(self.cfg) as client:
            result = await client.login(force=True)
            response = await client.get(f"/hs/{self.cfg.university_slug}/teachers/")
            marker = f'/user/{self.cfg.studizba_username}/'
            authenticated = marker in response.text or 'id="si_user_id"' in response.text
            return {"ok": authenticated, "login_response_keys": sorted(result), "page_status": response.status_code, "authenticated_ui": authenticated}

    async def discover(self, client: StudizbaClient, run_id: int | None = None) -> list[DepartmentCard]:
        url = f"{self.cfg.base_url}/hs/{self.cfg.university_slug}/teachers/"
        response = await client.get(url)
        departments = parse_departments(response.text, url)
        with self.db.connect() as conn:
            university_id = self._ensure_university(conn)
            raw_id, digest = self.db.save_raw_page(conn, url, "university_teachers", response.status_code, response.text)
            seen: list[str] = []
            for dep in departments:
                old = conn.execute(
                    "SELECT id,name,active FROM departments WHERE university_id=%s AND source_key=%s",
                    (university_id, dep.source_key),
                ).fetchone()
                row = conn.execute(
                    """INSERT INTO departments(university_id,source_key,slug,name,source_url,raw_hash)
                       VALUES(%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(university_id,source_key) DO UPDATE SET
                         name=excluded.name,slug=excluded.slug,source_url=excluded.source_url,last_seen_at=now(),missing_since=NULL,active=true
                       RETURNING id""",
                    (university_id, dep.source_key, dep.slug, dep.name, dep.url, digest),
                ).fetchone()
                dep_id = int(row["id"])
                seen.append(dep.source_key)
                if old is None:
                    self.db.event(conn, run_id, "department", dep_id, "discovered", name=dep.name, url=dep.url)
                elif old["name"] != dep.name:
                    self.db.event(conn, run_id, "department", dep_id, "changed", field="name", old=old["name"], new=dep.name)
                conn.execute(
                    """INSERT INTO crawl_targets(entity_type,entity_id,source_url,priority)
                       VALUES('department',%s,%s,2) ON CONFLICT(entity_type,entity_id) DO UPDATE SET source_url=excluded.source_url""",
                    (dep_id, dep.url),
                )
            if seen:
                missing = conn.execute(
                    """UPDATE departments SET active=false,missing_since=coalesce(missing_since,now())
                       WHERE university_id=%s AND active AND NOT(source_key=ANY(%s)) RETURNING id,name""",
                    (university_id, seen),
                ).fetchall()
                for row in missing:
                    self.db.event(conn, run_id, "department", int(row["id"]), "missing", name=row["name"])
        return departments

    async def refresh_department(self, client: StudizbaClient, dep: DepartmentCard | dict[str, Any], run_id: int | None = None) -> dict[str, int]:
        dep_url = dep.url if isinstance(dep, DepartmentCard) else dep["source_url"]
        response = await client.get(dep_url)
        parsed_name, cards = parse_department(response.text, dep_url)
        with self.db.connect() as conn:
            university_id = self._ensure_university(conn)
            if isinstance(dep, DepartmentCard):
                dep_key = dep.source_key
            else:
                dep_key = dep["source_key"]
            dep_row = conn.execute(
                "SELECT id,raw_hash FROM departments WHERE university_id=%s AND source_key=%s", (university_id, dep_key)
            ).fetchone()
            if not dep_row:
                raise RuntimeError(f"department not discovered: {dep_key}")
            dep_id = int(dep_row["id"])
            raw_id, digest = self.db.save_raw_page(conn, dep_url, "department", response.status_code, response.text)
            changed = dep_row["raw_hash"] != digest
            conn.execute(
                "UPDATE departments SET name=%s,raw_hash=%s,last_seen_at=now(),active=true,missing_since=NULL WHERE id=%s",
                (parsed_name or (dep.name if isinstance(dep, DepartmentCard) else dep["name"]), digest, dep_id),
            )
            seen_ids: list[int] = []
            new_count = 0
            for card in cards:
                old = conn.execute(
                    "SELECT id,name,source_url,active FROM teachers WHERE university_id=%s AND source_id=%s", (university_id, card.source_id)
                ).fetchone()
                row = conn.execute(
                    """INSERT INTO teachers(university_id,source_id,name,normalized_name,source_url,description,overall_rating,
                         explanation_rating,attitude_rating,grades_rating,declared_reviews_count)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(university_id,source_id) DO UPDATE SET
                         name=excluded.name,normalized_name=excluded.normalized_name,source_url=excluded.source_url,
                         overall_rating=coalesce(excluded.overall_rating,teachers.overall_rating),
                         explanation_rating=coalesce(excluded.explanation_rating,teachers.explanation_rating),
                         attitude_rating=coalesce(excluded.attitude_rating,teachers.attitude_rating),
                         grades_rating=coalesce(excluded.grades_rating,teachers.grades_rating),
                         declared_reviews_count=coalesce(excluded.declared_reviews_count,teachers.declared_reviews_count),
                         last_seen_at=now(),active=true,missing_since=NULL RETURNING id""",
                    (university_id, card.source_id, card.name, normalized_name(card.name), card.url, card.description,
                     card.overall_rating, card.explanation_rating, card.attitude_rating, card.grades_rating, card.reviews_count),
                ).fetchone()
                teacher_id = int(row["id"])
                seen_ids.append(teacher_id)
                membership = conn.execute(
                    "SELECT active FROM teacher_departments WHERE teacher_id=%s AND department_id=%s",
                    (teacher_id, dep_id),
                ).fetchone()
                conn.execute(
                    """INSERT INTO teacher_departments(teacher_id,department_id) VALUES(%s,%s)
                       ON CONFLICT(teacher_id,department_id) DO UPDATE SET last_seen_at=now(),active=true""",
                    (teacher_id, dep_id),
                )
                conn.execute(
                    """INSERT INTO crawl_targets(entity_type,entity_id,source_url,priority)
                       VALUES('teacher',%s,%s,%s) ON CONFLICT(entity_type,entity_id) DO UPDATE SET source_url=excluded.source_url""",
                    (teacher_id, card.url, 1 + (card.reviews_count or 0) / 50),
                )
                if old is None:
                    new_count += 1
                    self.db.event(conn, run_id, "teacher", teacher_id, "discovered", source_id=card.source_id, name=card.name, department_id=dep_id)
                elif not old["active"]:
                    self.db.event(conn, run_id, "teacher", teacher_id, "reappeared", department_id=dep_id)
                if old is not None and (membership is None or not membership["active"]):
                    self.db.event(conn, run_id, "membership", teacher_id, "department_joined", department_id=dep_id)
            if seen_ids:
                disappeared = conn.execute(
                    """UPDATE teacher_departments SET active=false WHERE department_id=%s AND active
                       AND NOT(teacher_id=ANY(%s)) RETURNING teacher_id""", (dep_id, seen_ids)
                ).fetchall()
                for row in disappeared:
                    self.db.event(conn, run_id, "membership", int(row["teacher_id"]), "department_left", department_id=dep_id)
            if changed:
                self.db.event(conn, run_id, "department", dep_id, "page_changed", raw_page_id=raw_id, teachers=len(cards))
            self._reschedule(conn, "department", dep_id, changed)
        return {"teachers": len(cards), "new_teachers": new_count, "changed": int(changed)}

    def _reconcile_teacher_activity(self, run_id: int | None) -> int:
        """A teacher is globally missing only after all department rosters were refreshed."""
        with self.db.connect() as conn:
            missing = conn.execute(
                """UPDATE teachers t SET active=false,missing_since=coalesce(missing_since,now())
                   WHERE t.active AND NOT EXISTS (
                     SELECT 1 FROM teacher_departments td WHERE td.teacher_id=t.id AND td.active
                   ) RETURNING id,name"""
            ).fetchall()
            for row in missing:
                self.db.event(conn, run_id, "teacher", int(row["id"]), "missing", name=row["name"])
        return len(missing)

    @staticmethod
    def _reschedule(conn, entity_type: str, entity_id: int, changed: bool, error: str | None = None) -> None:
        if error:
            conn.execute(
                """UPDATE crawl_targets SET last_checked_at=now(),error_count=error_count+1,last_error=%s,
                   next_due_at=now() + make_interval(secs => least(86400,300*power(2,least(error_count,8))))
                   WHERE entity_type=%s AND entity_id=%s""", (error[:1000], entity_type, entity_id)
            )
        elif changed:
            interval = "6 hours" if entity_type == "department" else "12 hours"
            conn.execute(
                """UPDATE crawl_targets SET last_checked_at=now(),consecutive_unchanged=0,error_count=0,last_error=NULL,
                   next_due_at=now() + %s::interval WHERE entity_type=%s AND entity_id=%s""", (interval, entity_type, entity_id)
            )
        else:
            conn.execute(
                """UPDATE crawl_targets SET last_checked_at=now(),consecutive_unchanged=consecutive_unchanged+1,
                   error_count=0,last_error=NULL,next_due_at=now() + make_interval(secs => least(%s, %s*power(1.7,least(consecutive_unchanged,12))))
                   WHERE entity_type=%s AND entity_id=%s""",
                (7 * 86400 if entity_type == "teacher" else 3 * 86400, 12 * 3600 if entity_type == "teacher" else 6 * 3600, entity_type, entity_id),
            )

    async def refresh_teacher(self, client: StudizbaClient, teacher: dict[str, Any], run_id: int | None = None, *, all_reviews: bool = True) -> dict[str, int]:
        response = await client.get(teacher["source_url"])
        page = parse_teacher(response.text, teacher["source_url"])
        ajax_raw_pages: list[dict[str, Any]] = []
        if all_reviews and page.declared_reviews_count and len(page.reviews) < page.declared_reviews_count:
            page.reviews = await client.fetch_all_review_pages(
                page.source_id, page.declared_reviews_count, page.reviews, parse_reviews_fragment, ajax_raw_pages
            )
        snapshot_hash = content_hash(
            page.overall_rating, page.explanation_rating, page.attitude_rating, page.grades_rating,
            page.declared_reviews_count, page.description,
        )
        with self.db.connect() as conn:
            trow = conn.execute("SELECT * FROM teachers WHERE id=%s", (teacher["id"],)).fetchone()
            raw_id, raw_hash = self.db.save_raw_page(conn, page.url, "teacher", response.status_code, response.text, {"all_reviews_fetched": all_reviews})
            for raw_page in ajax_raw_pages:
                self.db.save_raw_page(conn, raw_page["url"], "reviews_ajax", raw_page["status"],
                                      raw_page["content"], raw_page["metadata"])
            changed_fields = {}
            mapping = {
                "name": page.name, "source_url": page.url, "title": page.title,
                "overall_rating": page.overall_rating, "explanation_rating": page.explanation_rating,
                "attitude_rating": page.attitude_rating, "grades_rating": page.grades_rating,
                "declared_reviews_count": page.declared_reviews_count, "description": page.description,
            }
            for key, new_value in mapping.items():
                if trow and trow[key] != new_value and new_value is not None:
                    changed_fields[key] = {"old": trow[key], "new": new_value}
            conn.execute(
                """UPDATE teachers SET name=%s,normalized_name=%s,source_url=%s,description=%s,title=%s,
                   overall_rating=%s,explanation_rating=%s,attitude_rating=%s,grades_rating=%s,
                   declared_reviews_count=%s,raw_hash=%s,last_seen_at=now(),active=true,missing_since=NULL WHERE id=%s""",
                (page.name, normalized_name(page.name), page.url, page.description, page.title,
                 page.overall_rating, page.explanation_rating, page.attitude_rating, page.grades_rating,
                 page.declared_reviews_count, raw_hash, teacher["id"]),
            )
            inserted_snapshot = conn.execute(
                """INSERT INTO teacher_snapshots(teacher_id,overall_rating,explanation_rating,attitude_rating,grades_rating,
                   declared_reviews_count,description,raw_page_id,snapshot_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(teacher_id,snapshot_hash) DO NOTHING RETURNING id""",
                (teacher["id"], page.overall_rating, page.explanation_rating, page.attitude_rating, page.grades_rating,
                 page.declared_reviews_count, page.description, raw_id, snapshot_hash),
            ).fetchone()
            if changed_fields:
                self.db.event(conn, run_id, "teacher", int(teacher["id"]), "metrics_changed", fields=changed_fields)
            new_reviews = changed_reviews = 0
            seen_review_ids: list[int] = []
            for review in page.reviews:
                body_hash = hashlib.sha256(review.body.encode()).hexdigest()
                old = conn.execute(
                    "SELECT id,body_hash FROM reviews WHERE teacher_id=%s AND source_id=%s", (teacher["id"], review.source_id)
                ).fetchone()
                row = conn.execute(
                    """INSERT INTO reviews(teacher_id,source_id,parent_source_id,kind,author_name,author_url,body,body_hash,
                       vote_rating,published_at,date_raw,source_url) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(teacher_id,source_id) DO UPDATE SET parent_source_id=excluded.parent_source_id,kind=excluded.kind,
                       author_name=excluded.author_name,author_url=excluded.author_url,body=excluded.body,body_hash=excluded.body_hash,
                       vote_rating=excluded.vote_rating,published_at=coalesce(excluded.published_at,reviews.published_at),date_raw=excluded.date_raw,
                       last_seen_at=now(),changed_at=CASE WHEN reviews.body_hash<>excluded.body_hash THEN now() ELSE reviews.changed_at END,active=true
                       RETURNING id""",
                    (teacher["id"], review.source_id, review.parent_source_id, review.kind, review.author_name, review.author_url,
                     review.body, body_hash, review.vote_rating, review.published_at, review.date_raw, page.url),
                ).fetchone()
                review_id = int(row["id"])
                seen_review_ids.append(review_id)
                conn.execute(
                    """INSERT INTO review_versions(review_id,body,body_hash) VALUES(%s,%s,%s)
                       ON CONFLICT(review_id,body_hash) DO NOTHING""", (review_id, review.body, body_hash)
                )
                if old is None:
                    new_reviews += 1
                    event_type = "new_review"
                elif old["body_hash"] != body_hash:
                    changed_reviews += 1
                    event_type = "review_changed"
                    conn.execute("UPDATE review_versions SET valid_to=now() WHERE review_id=%s AND body_hash<>%s AND valid_to IS NULL", (review_id, body_hash))
                else:
                    event_type = None
                if event_type:
                    self.db.event(conn, run_id, "review", review_id, event_type, teacher_id=teacher["id"], source_id=review.source_id, source_url=page.url)
                    signal_words = ("экзам", "лаб", "спис", "автомат", "зач", "домаш", "контрол", "код", "оцен")
                    analysis_priority = 2.0 + min(2.0, len(review.body) / 500) + sum(0.7 for word in signal_words if word in review.body.lower())
                    conn.execute(
                        """INSERT INTO analysis_queue(review_id,priority) VALUES(%s,%s)
                           ON CONFLICT(review_id) DO UPDATE SET status='pending',priority=greatest(analysis_queue.priority,excluded.priority),updated_at=now()""",
                        (review_id, analysis_priority),
                    )
            missing_reviews = []
            complete_review_set = all_reviews and (
                not page.declared_reviews_count or len(page.reviews) >= page.declared_reviews_count
            )
            if complete_review_set:
                if seen_review_ids:
                    missing_reviews = conn.execute(
                        """UPDATE reviews SET active=false,last_seen_at=now()
                           WHERE teacher_id=%s AND active AND NOT(id=ANY(%s)) RETURNING id,source_id""",
                        (teacher["id"], seen_review_ids),
                    ).fetchall()
                else:
                    missing_reviews = conn.execute(
                        "UPDATE reviews SET active=false,last_seen_at=now() WHERE teacher_id=%s AND active RETURNING id,source_id",
                        (teacher["id"],),
                    ).fetchall()
                for missing in missing_reviews:
                    self.db.event(conn, run_id, "review", int(missing["id"]), "review_missing",
                                  teacher_id=teacher["id"], source_id=missing["source_id"])
            changed = bool(inserted_snapshot or new_reviews or changed_reviews or changed_fields or missing_reviews)
            self._reschedule(conn, "teacher", int(teacher["id"]), changed)
        return {"reviews": len(page.reviews), "new_reviews": new_reviews, "changed_reviews": changed_reviews, "changed": int(changed)}

    async def bootstrap(
        self, *, details_limit: int | None = None, all_reviews: bool = True, resume: bool = True,
        refresh_catalog: bool = True,
    ) -> dict[str, Any]:
        run_id = self._start_run("bootstrap")
        stats: dict[str, Any] = {"departments": 0, "teachers_discovered": 0, "teachers_refreshed": 0, "reviews": 0, "errors": 0}
        try:
            async with StudizbaClient(self.cfg) as client:
                await client.login()
                if refresh_catalog:
                    departments = await self.discover(client, run_id)
                    stats["departments"] = len(departments)
                    for dep in departments:
                        try:
                            result = await self.refresh_department(client, dep, run_id)
                            stats["teachers_discovered"] += result["new_teachers"]
                        except Exception as exc:
                            if isinstance(exc, StudizbaUnavailable):
                                raise
                            stats["errors"] += 1
                            with self.db.connect() as conn:
                                self.db.event(conn, run_id, "department", None, "error", url=dep.url, error=str(exc)[:1000])
                    stats["teachers_missing"] = self._reconcile_teacher_activity(run_id)
                else:
                    with self.db.connect() as conn:
                        stats["departments"] = int(conn.execute(
                            "SELECT count(*) n FROM departments WHERE active"
                        ).fetchone()["n"])
                with self.db.connect() as conn:
                    query = "SELECT id,source_url FROM teachers WHERE active"
                    if resume:
                        query += " AND NOT EXISTS (SELECT 1 FROM teacher_snapshots s WHERE s.teacher_id=teachers.id)"
                    query += " ORDER BY declared_reviews_count DESC NULLS LAST,id"
                    if details_limit is not None:
                        query += " LIMIT %s"
                        teachers = conn.execute(query, (details_limit,)).fetchall()
                    else:
                        teachers = conn.execute(query).fetchall()
                semaphore = asyncio.Semaphore(max(1, self.cfg.concurrency))
                async def refresh_one(teacher):
                    async with semaphore:
                        try:
                            result = await self.refresh_teacher(client, teacher, run_id, all_reviews=all_reviews)
                            stats["teachers_refreshed"] += 1
                            stats["reviews"] += result["reviews"]
                        except Exception as exc:
                            if isinstance(exc, StudizbaUnavailable):
                                raise
                            stats["errors"] += 1
                            with self.db.connect() as conn:
                                self.db.event(conn, run_id, "teacher", int(teacher["id"]), "error", error=str(exc)[:1000])
                                self._reschedule(conn, "teacher", int(teacher["id"]), False, str(exc))
                for start in range(0, len(teachers), 200):
                    await asyncio.gather(*(refresh_one(t) for t in teachers[start:start + 200]))
            self._finish_run(run_id, "completed" if not stats["errors"] else "completed_with_errors", stats)
            return stats
        except Exception as exc:
            self._finish_run(run_id, "failed", stats, str(exc)[:2000])
            raise

    async def incremental(self, *, limit: int = 50) -> dict[str, int]:
        run_id = self._start_run("incremental")
        stats = {"checked": 0, "changed": 0, "errors": 0}
        try:
            async with StudizbaClient(self.cfg) as client:
                await client.login()
                with self.db.connect() as conn:
                    targets = conn.execute(
                        "SELECT * FROM crawl_targets WHERE next_due_at<=now() ORDER BY priority DESC,next_due_at LIMIT %s", (limit,)
                    ).fetchall()
                for target in targets:
                    try:
                        with self.db.connect() as conn:
                            entity = conn.execute(
                                f"SELECT * FROM {'departments' if target['entity_type']=='department' else 'teachers'} WHERE id=%s",
                                (target["entity_id"],),
                            ).fetchone()
                        result = await (self.refresh_department(client, entity, run_id) if target["entity_type"] == "department" else self.refresh_teacher(client, entity, run_id))
                        stats["checked"] += 1
                        stats["changed"] += result["changed"]
                    except Exception as exc:
                        if isinstance(exc, StudizbaUnavailable):
                            raise
                        stats["errors"] += 1
                        with self.db.connect() as conn:
                            self._reschedule(conn, target["entity_type"], int(target["entity_id"]), False, str(exc))
                            self.db.event(conn, run_id, target["entity_type"], int(target["entity_id"]), "error", error=str(exc)[:1000])
            self._finish_run(run_id, "completed" if not stats["errors"] else "completed_with_errors", stats)
            return stats
        except Exception as exc:
            self._finish_run(run_id, "failed", stats, str(exc)[:2000])
            raise

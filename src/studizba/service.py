from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .analytics import DERIVED, METRIC_VERSION
from .config import Config
from .db import Database
from .glm import ANALYZER_VERSION


SYNONYMS = {
    "списать": ["списывание", "шпаргалка", "телефон", "подсмотреть", "контроль"],
    "автомат": ["автоматом", "баллы за семестр", "без экзамена", "автозачёт"],
    "лабы": ["лабораторные", "защита", "код", "чужие работы"],
    "экзамен": ["экзамене", "билеты", "принимает", "валит"],
    "конспект": ["записи", "материалы", "open book"],
    "домашка": ["домашнее задание", "дз", "нагрузка"],
}


def expand_search_query(query: str) -> str:
    """Build PostgreSQL websearch syntax with optional domain synonym branches."""
    lower = query.lower()
    terms = [query]
    for key, synonyms in SYNONYMS.items():
        if key in lower:
            terms.extend(synonyms)
    unique = list(dict.fromkeys(x.strip() for x in terms if x.strip()))
    return " OR ".join(f'"{term}"' if " " in term else term for term in unique)


def normalize_department_query(query: str) -> str:
    """Make short department codes tolerant of separators (``ИУ7`` = ``ИУ-7``)."""
    return re.sub(r"[^0-9a-zа-яё]+", "", query.casefold())


def serializable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: serializable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [serializable(v) for v in value]
    return value


def _format_duration(seconds: float | None) -> str | None:
    """Human-readable duration for the public progress API (never a promise)."""
    if seconds is None or seconds < 0:
        return None
    seconds = int(round(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _estimate(remaining: int, events: list[datetime], now: datetime, window_minutes: int) -> dict[str, Any]:
    """Estimate completion from a fixed recent window, returning null on no signal."""
    cutoff = now - timedelta(minutes=window_minutes)
    completed = sum(1 for timestamp in events if timestamp >= cutoff)
    rate_per_hour = completed * 60 / window_minutes
    eta_seconds = remaining / rate_per_hour * 3600 if rate_per_hour > 0 else None
    return {
        "window_minutes": window_minutes,
        "completed_in_window": completed,
        "rate_per_hour": round(rate_per_hour, 2),
        "eta_seconds": round(eta_seconds) if eta_seconds is not None else None,
        "eta_human": _format_duration(eta_seconds),
        "estimated_finish_at": (now + timedelta(seconds=eta_seconds)).isoformat() if eta_seconds is not None else None,
    }


class QueryService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg)

    @staticmethod
    def _limit(value: int, maximum: int = 100) -> int:
        return max(1, min(maximum, int(value)))

    def _resolve_teacher_id(self, value: int) -> int | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM teachers WHERE id=%s OR source_id=%s ORDER BY (id=%s) DESC LIMIT 1", (value, value, value)
            ).fetchone()
        return int(row["id"]) if row else None

    def search_teachers(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT t.id,t.source_id,t.name,t.source_url,t.overall_rating,t.declared_reviews_count,
                   string_agg(DISTINCT d.name,'; ') departments,
                   greatest(similarity(t.name,%s),similarity(t.normalized_name,%s)) relevance
                   FROM teachers t LEFT JOIN teacher_departments td ON td.teacher_id=t.id AND td.active
                   LEFT JOIN departments d ON d.id=td.department_id
                   WHERE t.active AND (t.name %% %s OR t.normalized_name LIKE '%%'||%s||'%%')
                   GROUP BY t.id ORDER BY relevance DESC,t.declared_reviews_count DESC NULLS LAST LIMIT %s""",
                (query, query.lower(), query, query.lower(), self._limit(limit)),
            ).fetchall()
        return serializable(rows)

    def teacher_profile(self, teacher_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            teacher = conn.execute("SELECT * FROM teachers WHERE id=%s OR source_id=%s ORDER BY (id=%s) DESC LIMIT 1", (teacher_id, teacher_id, teacher_id)).fetchone()
            if not teacher:
                return None
            departments = conn.execute(
                """SELECT d.id,d.name,d.source_url,td.first_seen_at,td.last_seen_at,td.active
                   FROM teacher_departments td JOIN departments d ON d.id=td.department_id WHERE td.teacher_id=%s""", (teacher["id"],)
            ).fetchall()
            metrics = conn.execute(
                "SELECT metric,score,recent_score,all_time_score,sample_size,evidence_count,confidence,calculated_at,metric_version FROM teacher_metrics WHERE teacher_id=%s AND metric_version=%s",
                (teacher["id"], METRIC_VERSION),
            ).fetchall()
            result = dict(teacher)
            result["departments"] = departments
            result["analytics"] = metrics
            return serializable(result)

    def teacher_reviews(self, teacher_id: int, limit: int = 50, offset: int = 0, kind: str | None = None) -> list[dict[str, Any]]:
        resolved = self._resolve_teacher_id(teacher_id)
        if resolved is None:
            return []
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT r.id,r.source_id,r.kind,r.author_name,r.body,r.vote_rating,r.published_at,r.date_raw,r.source_url,
                   EXISTS(SELECT 1 FROM review_analyses a WHERE a.review_id=r.id AND a.analyzer_version=%s) analyzed
                   FROM reviews r JOIN teachers t ON t.id=r.teacher_id
                   WHERE t.id=%s AND r.active AND (%s::text IS NULL OR r.kind=%s)
                   ORDER BY r.published_at DESC NULLS LAST,r.source_id DESC LIMIT %s OFFSET %s""",
                (ANALYZER_VERSION, resolved, kind, kind, self._limit(limit, 200), max(0, offset)),
            ).fetchall()
        return serializable(rows)

    def search_reviews(self, query: str, limit: int = 30, department_id: int | None = None, teacher_id: int | None = None) -> list[dict[str, Any]]:
        expanded = expand_search_query(query)
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT r.id,r.source_id,r.body,r.published_at,r.source_url,t.id teacher_id,t.name teacher_name,
                   string_agg(DISTINCT d.name,'; ') departments,
                   ts_rank_cd(r.search_vector,websearch_to_tsquery('russian',%s))*2 + similarity(r.body,%s) relevance
                   FROM reviews r JOIN teachers t ON t.id=r.teacher_id
                   LEFT JOIN teacher_departments td ON td.teacher_id=t.id AND td.active
                   LEFT JOIN departments d ON d.id=td.department_id
                   WHERE r.active AND (%s::bigint IS NULL OR t.id=%s OR t.source_id=%s) AND (%s::bigint IS NULL OR d.id=%s)
                     AND (r.search_vector @@ websearch_to_tsquery('russian',%s) OR r.body %% %s)
                   GROUP BY r.id,t.id ORDER BY relevance DESC,r.published_at DESC NULLS LAST LIMIT %s""",
                (expanded, query, teacher_id, teacher_id, teacher_id, department_id, department_id, expanded, query, self._limit(limit)),
            ).fetchall()
        return serializable(rows)

    def teacher_analytics(self, teacher_id: int) -> list[dict[str, Any]]:
        resolved = self._resolve_teacher_id(teacher_id)
        if resolved is None:
            return []
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT tm.*,t.name FROM teacher_metrics tm JOIN teachers t ON t.id=tm.teacher_id
                   WHERE t.id=%s AND tm.metric_version=%s ORDER BY tm.metric""",
                (resolved, METRIC_VERSION),
            ).fetchall()
        return serializable(rows)

    def explain_score(self, teacher_id: int, metric: str, limit: int = 30) -> dict[str, Any]:
        components = DERIVED.get(metric, [(metric, 1.0)])
        resolved = self._resolve_teacher_id(teacher_id)
        if resolved is None:
            return {"error": "teacher_not_found"}
        with self.db.connect() as conn:
            teacher = conn.execute("SELECT id,name,source_url FROM teachers WHERE id=%s", (resolved,)).fetchone()
            summary = conn.execute(
                "SELECT * FROM teacher_metrics WHERE teacher_id=%s AND metric=%s AND metric_version=%s",
                (teacher["id"], metric, METRIC_VERSION),
            ).fetchone()
            rows = conn.execute(
                """SELECT r.id,r.body,r.published_at,r.source_url,a.result,a.model,a.analyzed_at
                   FROM review_analyses a JOIN reviews r ON r.id=a.review_id
                   WHERE r.teacher_id=%s AND a.analyzer_version=%s ORDER BY r.published_at DESC NULLS LAST""",
                (teacher["id"], ANALYZER_VERSION),
            ).fetchall()
        evidence = []
        for row in rows:
            result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
            for component, direction in components:
                item = result.get("metrics", {}).get(component)
                if not item or not item.get("evidence"):
                    continue
                evidence.append({
                    "review_id": row["id"], "component": component, "direction": "raises" if direction > 0 else "lowers",
                    "raw_value": item["value"], "confidence": item["confidence"], "evidence": item["evidence"],
                    "context": item.get("context", []), "published_at": row["published_at"], "source_url": row["source_url"],
                })
        evidence.sort(key=lambda x: float(x["confidence"]), reverse=True)
        return serializable({
            "teacher": dict(teacher), "metric": metric, "result": dict(summary) if summary else None,
            "method": {"version": METRIC_VERSION, "analyzer_version": ANALYZER_VERSION, "components": components,
                       "prior": {"mean": .5, "strength": 4}, "time_decay_half_life_days": self.cfg.analytics_half_life_days},
            "evidence": evidence[:self._limit(limit, 100)],
        })

    def rank_teachers(self, metric: str, limit: int = 20, department_id: int | None = None, min_confidence: float = .15, descending: bool = True) -> list[dict[str, Any]]:
        order = "DESC" if descending else "ASC"
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT t.id,t.source_id,t.name,t.source_url,tm.score,tm.confidence,tm.sample_size,tm.evidence_count,
                    string_agg(DISTINCT d.name,'; ') departments
                    FROM teacher_metrics tm JOIN teachers t ON t.id=tm.teacher_id
                    LEFT JOIN teacher_departments td ON td.teacher_id=t.id AND td.active
                    LEFT JOIN departments d ON d.id=td.department_id
                    WHERE tm.metric=%s AND tm.metric_version=%s AND tm.confidence>=%s AND (%s::bigint IS NULL OR d.id=%s)
                    GROUP BY t.id,tm.score,tm.confidence,tm.sample_size,tm.evidence_count
                    ORDER BY tm.score {order},tm.confidence DESC LIMIT %s""",
                (metric, METRIC_VERSION, min_confidence, department_id, department_id, self._limit(limit)),
            ).fetchall()
        return serializable(rows)

    def compare_teachers(self, teacher_ids: list[int], metrics: list[str] | None = None) -> list[dict[str, Any]]:
        wanted = metrics or list(DERIVED) + ["polarization"]
        return [{"teacher": self.teacher_profile(tid), "metrics": [m for m in self.teacher_analytics(tid) if m["metric"] in wanted]} for tid in teacher_ids[:20]]

    def search_departments(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        normalized = normalize_department_query(query)
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT d.*,similarity(d.name,%s) relevance,count(td.teacher_id) FILTER(WHERE td.active) teachers
                   FROM departments d LEFT JOIN teacher_departments td ON td.department_id=d.id
                   WHERE d.active AND (
                       d.name %% %s
                       OR d.name ILIKE '%%'||%s||'%%'
                       OR regexp_replace(lower(d.name), '[^0-9a-zа-яё]+', '', 'g') LIKE '%%'||%s||'%%'
                   ) GROUP BY d.id ORDER BY relevance DESC LIMIT %s""",
                (query, query, query, normalized, self._limit(limit)),
            ).fetchall()
        return serializable(rows)

    def department_analytics(self, department_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            dep = conn.execute("SELECT * FROM departments WHERE id=%s", (department_id,)).fetchone()
            if not dep:
                return None
            metrics = conn.execute("SELECT * FROM department_metrics WHERE department_id=%s AND metric_version=%s ORDER BY metric", (department_id, METRIC_VERSION)).fetchall()
            return serializable({"department": dict(dep), "metrics": metrics})

    def rank_departments(self, metric: str, limit: int = 20, min_confidence: float = .1, descending: bool = True) -> list[dict[str, Any]]:
        order = "DESC" if descending else "ASC"
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT d.id,d.name,d.source_url,dm.score,dm.sample_size,dm.confidence,dm.details
                    FROM department_metrics dm JOIN departments d ON d.id=dm.department_id
                    WHERE dm.metric=%s AND dm.metric_version=%s AND dm.confidence>=%s AND d.active
                    ORDER BY dm.score {order},dm.confidence DESC LIMIT %s""",
                (metric, METRIC_VERSION, min_confidence, self._limit(limit)),
            ).fetchall()
        return serializable(rows)

    def compare_departments(self, department_ids: list[int], metrics: list[str] | None = None) -> list[dict[str, Any]]:
        wanted = set(metrics or [])
        results = []
        for dep_id in department_ids[:20]:
            item = self.department_analytics(dep_id)
            if item and wanted:
                item["metrics"] = [m for m in item["metrics"] if m["metric"] in wanted]
            if item:
                results.append(item)
        return results

    def rating_history(self, teacher_id: int, days: int = 365) -> list[dict[str, Any]]:
        resolved = self._resolve_teacher_id(teacher_id)
        if resolved is None:
            return []
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT s.observed_at,s.overall_rating,s.explanation_rating,s.attitude_rating,s.grades_rating,s.declared_reviews_count
                   FROM teacher_snapshots s JOIN teachers t ON t.id=s.teacher_id
                   WHERE t.id=%s AND s.observed_at>=now()-(%s||' days')::interval ORDER BY s.observed_at""",
                (resolved, max(1, days)),
            ).fetchall()
        return serializable(rows)

    def recent_changes(self, days: int = 7, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM crawl_events WHERE occurred_at>=now()-(%s||' days')::interval ORDER BY occurred_at DESC LIMIT %s",
                (max(1, days), self._limit(limit, 500)),
            ).fetchall()
        return serializable(rows)

    def new_reviews(self, days: int = 7, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT r.id,r.kind,r.body,r.published_at,r.first_seen_at,r.source_url,t.id teacher_id,t.name teacher_name
                   FROM reviews r JOIN teachers t ON t.id=r.teacher_id WHERE r.first_seen_at>=now()-(%s||' days')::interval
                   ORDER BY r.first_seen_at DESC LIMIT %s""", (max(1, days), self._limit(limit, 500))
            ).fetchall()
        return serializable(rows)

    def monitor_status(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            counts = conn.execute(
                """SELECT (SELECT count(*) FROM departments WHERE active) departments,
                   (SELECT count(*) FROM teachers WHERE active) teachers,(SELECT count(*) FROM reviews WHERE active) reviews,
                   (SELECT count(DISTINCT r.id) FROM reviews r JOIN review_analyses a ON a.review_id=r.id
                     WHERE r.active AND a.analyzer_version=%s AND a.source_body_hash=r.body_hash) analyzed_reviews,
                   (SELECT count(DISTINCT teacher_id) FROM teacher_snapshots) detailed_teachers,
                   (SELECT count(*) FROM teachers t WHERE t.active AND NOT EXISTS
                     (SELECT 1 FROM teacher_snapshots s WHERE s.teacher_id=t.id)) teacher_details_remaining,
                   (SELECT count(*) FROM analysis_queue WHERE status='pending') glm_pending,
                   (SELECT count(*) FROM analysis_queue WHERE status='running') glm_running,
                   (SELECT count(*) FROM analysis_queue WHERE status='failed') glm_failed,
                   (SELECT count(*) FROM raw_pages) raw_pages,
                   (SELECT count(*) FROM crawl_targets WHERE next_due_at<=now()) crawl_due""",
                (ANALYZER_VERSION,),
            ).fetchone()
            run = conn.execute("SELECT * FROM crawl_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            errors = conn.execute("SELECT * FROM crawl_events WHERE event_type='error' ORDER BY occurred_at DESC LIMIT 10").fetchall()
        return serializable({"counts": dict(counts), "last_run": dict(run) if run else None, "recent_errors": errors})

    def progress_status(self, hours: int = 24, bucket_minutes: int = 15) -> dict[str, Any]:
        """Operational progress, throughput and a deliberately conservative ETA.

        Completion is defined against the live database, so the denominator can grow
        when a crawl discovers new reviews.  This makes the result useful for an
        operator without pretending that a site-side rate limit is predictable.
        """
        hours = max(1, min(168, int(hours)))
        bucket_minutes = max(5, min(60, int(bucket_minutes)))
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        with self.db.connect() as conn:
            counts = conn.execute(
                """SELECT
                   (SELECT count(*) FROM teachers WHERE active) teachers,
                   (SELECT count(DISTINCT teacher_id) FROM teacher_snapshots) detailed_teachers,
                   (SELECT count(*) FROM teachers t WHERE t.active AND NOT EXISTS
                    (SELECT 1 FROM teacher_snapshots s WHERE s.teacher_id=t.id)) teacher_details_remaining,
                   (SELECT count(*) FROM reviews WHERE active) reviews,
                   (SELECT count(DISTINCT r.id) FROM reviews r JOIN review_analyses a ON a.review_id=r.id
                    WHERE r.active AND a.analyzer_version=%s AND a.source_body_hash=r.body_hash) analyzed_reviews,
                   (SELECT count(*) FROM analysis_queue WHERE status='pending') glm_pending,
                   (SELECT count(*) FROM analysis_queue WHERE status='running') glm_running,
                   (SELECT count(*) FROM analysis_queue WHERE status='failed') glm_failed,
                   (SELECT count(*) FROM crawl_targets WHERE next_due_at<=now()) crawl_due,
                   (SELECT count(*) FROM crawl_targets WHERE error_count>0) crawl_targets_with_errors""",
                (ANALYZER_VERSION,),
            ).fetchone()
            # The first snapshot is the moment a teacher detail was completed. It
            # avoids counting routine monitoring refreshes as bootstrap progress.
            teacher_events = conn.execute(
                """SELECT min(observed_at) observed_at FROM teacher_snapshots
                   GROUP BY teacher_id HAVING min(observed_at)>=%s""", (cutoff,)
            ).fetchall()
            analysis_events = conn.execute(
                """SELECT min(a.analyzed_at) analyzed_at FROM review_analyses a JOIN reviews r ON r.id=a.review_id
                   WHERE r.active AND a.analyzer_version=%s AND a.source_body_hash=r.body_hash
                   GROUP BY r.id HAVING min(a.analyzed_at)>=%s""", (ANALYZER_VERSION, cutoff)
            ).fetchall()
            review_events = conn.execute(
                "SELECT first_seen_at FROM reviews WHERE active AND first_seen_at>=%s", (cutoff,)
            ).fetchall()
            error_events = conn.execute(
                "SELECT occurred_at FROM crawl_events WHERE event_type='error' AND occurred_at>=%s", (cutoff,)
            ).fetchall()
            runs = conn.execute(
                """SELECT id,mode,started_at,finished_at,status,stats,error FROM crawl_runs
                   ORDER BY started_at DESC LIMIT 20"""
            ).fetchall()
            queue = conn.execute(
                """SELECT status,count(*) count,min(next_attempt_at) next_attempt_at,max(attempts) max_attempts
                   FROM analysis_queue GROUP BY status ORDER BY status"""
            ).fetchall()
            target_health = conn.execute(
                """SELECT entity_type,count(*) count,
                   count(*) FILTER (WHERE next_due_at<=now()) due,
                   count(*) FILTER (WHERE error_count>0) with_errors,
                   max(error_count) max_error_count,min(next_due_at) next_due_at
                   FROM crawl_targets GROUP BY entity_type ORDER BY entity_type"""
            ).fetchall()
            account_pool = conn.execute(
                """SELECT label,enabled,healthy,model,proxy_label,active_requests,processed_count,failed_count,http_429_count,
                   network_error_count,round((latency_total_ms/nullif(latency_samples,0))::numeric,1) average_latency_ms,
                   round((processed_count*3600/greatest(extract(epoch FROM now()-started_at),1))::numeric,2) reviews_per_hour,
                   cooldown_until,last_success_at,last_error,last_error_at,updated_at
                   FROM glm_account_stats ORDER BY label"""
            ).fetchall()

        teacher_times = [row["observed_at"] for row in teacher_events]
        analysis_times = [row["analyzed_at"] for row in analysis_events]
        review_times = [row["first_seen_at"] for row in review_events]
        error_times = [row["occurred_at"] for row in error_events]
        details_remaining = int(counts["teacher_details_remaining"])
        analysis_remaining = max(0, int(counts["reviews"]) - int(counts["analyzed_reviews"]))

        # Prefer a one-hour signal; during a pause use six hours to retain the last
        # observed rate, otherwise state that an ETA cannot currently be estimated.
        teacher_short = _estimate(details_remaining, teacher_times, now, 60)
        teacher_long = _estimate(details_remaining, teacher_times, now, min(hours * 60, 360))
        analysis_short = _estimate(analysis_remaining, analysis_times, now, 60)
        analysis_long = _estimate(analysis_remaining, analysis_times, now, min(hours * 60, 360))
        teacher_eta = teacher_short if teacher_short["rate_per_hour"] else teacher_long
        analysis_eta = analysis_short if analysis_short["rate_per_hour"] else analysis_long

        buckets: dict[datetime, dict[str, Any]] = {}
        def add(events: list[datetime], key: str) -> None:
            for timestamp in events:
                stamp = timestamp.astimezone(timezone.utc)
                epoch = int(stamp.timestamp()) // (bucket_minutes * 60) * (bucket_minutes * 60)
                bucket = datetime.fromtimestamp(epoch, timezone.utc)
                buckets.setdefault(bucket, {"at": bucket, "teacher_details": 0, "analyses": 0, "new_reviews": 0, "errors": 0})[key] += 1
        add(teacher_times, "teacher_details")
        add(analysis_times, "analyses")
        add(review_times, "new_reviews")
        add(error_times, "errors")
        timeline = [buckets[key] for key in sorted(buckets)]
        coverage = {
            "teacher_details_percent": round(100 * int(counts["detailed_teachers"]) / max(1, int(counts["teachers"])), 2),
            "review_analysis_percent": round(100 * int(counts["analyzed_reviews"]) / max(1, int(counts["reviews"])), 2),
        }
        return serializable({
            "generated_at": now,
            "window": {"hours": hours, "bucket_minutes": bucket_minutes},
            "coverage": coverage,
            "bootstrap": {"completed": int(counts["detailed_teachers"]), "total": int(counts["teachers"]),
                          "remaining": details_remaining, "eta": teacher_eta,
                          "rate_windows": {"last_hour": teacher_short, "last_6_hours": teacher_long}},
            "glm_analysis": {"completed": int(counts["analyzed_reviews"]), "total": int(counts["reviews"]),
                             "remaining": analysis_remaining, "queue": {"pending": int(counts["glm_pending"]), "running": int(counts["glm_running"]), "failed": int(counts["glm_failed"])},
                             "eta": analysis_eta, "rate_windows": {"last_hour": analysis_short, "last_6_hours": analysis_long}},
            "crawl": {"due": int(counts["crawl_due"]), "targets_with_errors": int(counts["crawl_targets_with_errors"]),
                      "target_health": target_health},
            "queue_details": queue,
            "glm_account_pool": account_pool,
            "runs": runs,
            "timeline": timeline,
            "notes": ["ETA is calculated from completed work in a recent fixed window and is an estimate, not a deadline.",
                      "Review-analysis total is live: new reviews discovered by the crawler increase the total."]
        })

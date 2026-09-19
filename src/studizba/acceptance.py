from __future__ import annotations

import asyncio
import json
from typing import Any

from .analytics import METRIC_VERSION
from .config import Config
from .db import Database
from .glm import ANALYZER_VERSION
from .mcp_server import mcp
from .service import QueryService


def run_acceptance(cfg: Config) -> dict[str, Any]:
    db = Database(cfg)
    with db.connect() as conn:
        counts = dict(conn.execute(
            """SELECT
              (SELECT count(*) FROM departments WHERE active) departments,
              (SELECT count(*) FROM teachers WHERE active) teachers,
              (SELECT count(*) FROM teachers t WHERE t.active AND NOT EXISTS
                (SELECT 1 FROM teacher_snapshots s WHERE s.teacher_id=t.id)) teachers_without_snapshot,
              (SELECT count(*) FROM reviews WHERE active) reviews,
              (SELECT count(*) FROM raw_pages WHERE kind='reviews_ajax') ajax_raw_pages,
              (SELECT count(*) FROM reviews r WHERE r.active AND NOT EXISTS
                (SELECT 1 FROM review_analyses a WHERE a.review_id=r.id AND
                 a.analyzer_version=%s AND a.source_body_hash=r.body_hash)) reviews_without_current_analysis,
              (SELECT count(DISTINCT teacher_id) FROM teacher_metrics WHERE metric_version=%s) teachers_with_metrics,
              (SELECT count(DISTINCT department_id) FROM department_metrics WHERE metric_version=%s) departments_with_metrics,
              (SELECT count(*) FROM analysis_queue WHERE status='failed') glm_failed,
              (SELECT count(*) FROM crawl_events WHERE event_type='new_review') new_review_events,
              (SELECT count(*) FROM crawl_events WHERE event_type='metrics_changed') metric_change_events""",
            (ANALYZER_VERSION, METRIC_VERSION, METRIC_VERSION),
        ).fetchone())
        analyses = conn.execute(
            """SELECT r.body,a.result FROM review_analyses a JOIN reviews r ON r.id=a.review_id
               WHERE r.active AND a.analyzer_version=%s AND a.source_body_hash=r.body_hash""",
            (ANALYZER_VERSION,),
        ).fetchall()

    invalid_evidence = 0
    for row in analyses:
        payload = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
        source = " ".join(row["body"].lower().split())
        for metric in payload.get("metrics", {}).values():
            invalid_evidence += sum(" ".join(span.lower().split()) not in source for span in metric.get("evidence", []))

    service = QueryService(cfg)
    search_examples = {
        query: len(service.search_reviews(query, limit=3))
        for query in ("можно получить автомат", "много домашки", "валит на экзамене", "строгий, но справедливый")
    }
    tools = sorted(tool.name for tool in asyncio.run(mcp.list_tools()))
    checks = {
        "department_discovery": counts["departments"] >= 91,
        "teacher_discovery": counts["teachers"] >= 4500,
        "full_teacher_bootstrap": counts["teachers_without_snapshot"] == 0,
        "reviews_collected": counts["reviews"] > 0,
        "all_reviews_analyzed": counts["reviews_without_current_analysis"] == 0 and counts["glm_failed"] == 0,
        "evidence_traceable": invalid_evidence == 0,
        "teacher_analytics": counts["teachers_with_metrics"] > 0,
        "department_analytics": counts["departments_with_metrics"] == counts["departments"],
        "change_detection": counts["new_review_events"] > 0 and counts["metric_change_events"] > 0,
        "mcp_surface": len(tools) >= 17 and "explain_score" in tools and "refresh_entity" in tools,
        "review_search": any(search_examples.values()),
    }
    return {
        "ok": all(checks.values()), "checks": checks, "counts": counts,
        "invalid_evidence": invalid_evidence, "search_examples": search_examples,
        "mcp_tools": tools, "analyzer_version": ANALYZER_VERSION, "metric_version": METRIC_VERSION,
    }

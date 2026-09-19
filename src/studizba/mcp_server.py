from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .analytics import AnalyticsEngine
from .client import StudizbaClient
from .collector import Collector
from .config import load_config
from .service import QueryService


cfg = load_config()
service = QueryService(cfg)
mcp = FastMCP(
    "Studizba BMSTU Analytics",
    host=os.getenv("STUDIZBA_MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("STUDIZBA_MCP_PORT", "8897")),
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
def search_teachers(query: str, limit: int = 20):
    """Find BMSTU teachers by fuzzy full-name search."""
    return service.search_teachers(query, limit)


@mcp.tool()
def get_teacher_profile(teacher_id: int):
    """Get source profile, departments, ratings and analytical metrics."""
    return service.teacher_profile(teacher_id)


@mcp.tool()
def get_teacher_reviews(teacher_id: int, limit: int = 50, offset: int = 0, kind: str | None = None):
    """Get source reviews for a teacher with dates and Studizba links."""
    return service.teacher_reviews(teacher_id, limit, offset, kind)


@mcp.tool()
def search_reviews(query: str, limit: int = 30, department_id: int | None = None, teacher_id: int | None = None):
    """Search review text with Russian full-text matching and typo tolerance, preserving source links."""
    return service.search_reviews(query, limit, department_id, teacher_id)


@mcp.tool()
def get_teacher_analytics(teacher_id: int):
    """Get shrinkage-adjusted, time-decayed teacher analytics."""
    return service.teacher_analytics(teacher_id)


@mcp.tool()
def explain_score(teacher_id: int, metric: str, limit: int = 30):
    """Explain any teacher metric with confidence, formula and exact evidence spans."""
    return service.explain_score(teacher_id, metric, limit)


@mcp.tool()
def compare_teachers(teacher_ids: list[int], metrics: list[str] | None = None):
    """Compare several teachers on independent metrics."""
    return service.compare_teachers(teacher_ids, metrics)


@mcp.tool()
def rank_teachers(metric: str, limit: int = 20, department_id: int | None = None, min_confidence: float = .15, descending: bool = True):
    """Rank teachers by an analytical metric and confidence threshold."""
    return service.rank_teachers(metric, limit, department_id, min_confidence, descending)


@mcp.tool()
def search_departments(query: str, limit: int = 20):
    """Find BMSTU departments and subdivisions."""
    return service.search_departments(query, limit)


@mcp.tool()
def get_department_analytics(department_id: int):
    """Get department metrics including draw risk, variance and coverage."""
    return service.department_analytics(department_id)


@mcp.tool()
def compare_departments(department_ids: list[int], metrics: list[str] | None = None):
    """Compare several departments."""
    return service.compare_departments(department_ids, metrics)


@mcp.tool()
def rank_departments(metric: str, limit: int = 20, min_confidence: float = .1, descending: bool = True):
    """Rank departments by quality, ease, cheating environment, variance or draw risk."""
    return service.rank_departments(metric, limit, min_confidence, descending)


@mcp.tool()
def get_rating_history(teacher_id: int, days: int = 365):
    """Get historical source ratings and review counts."""
    return service.rating_history(teacher_id, days)


@mcp.tool()
def get_recent_changes(days: int = 7, limit: int = 100):
    """Get new/missing/changed entities and rating changes."""
    return service.recent_changes(days, limit)


@mcp.tool()
def get_new_reviews(days: int = 7, limit: int = 100):
    """Get reviews first observed during a period."""
    return service.new_reviews(days, limit)


@mcp.tool()
def get_monitoring_status():
    """Get crawl, GLM queue, database counts and recent errors."""
    return service.monitor_status()


@mcp.tool()
def get_progress(hours: int = 24, bucket_minutes: int = 15):
    """Get bootstrap/GLM completion, ETA estimates, throughput, queue health and crawl-run history."""
    return service.progress_status(hours, bucket_minutes)


@mcp.tool()
async def refresh_entity(entity_type: str, entity_id: int):
    """Request and immediately perform a live refresh for a department or teacher."""
    collector = Collector(cfg)
    table = "departments" if entity_type == "department" else "teachers" if entity_type == "teacher" else None
    if not table:
        return {"error": "entity_type must be department or teacher"}
    with collector.db.connect() as conn:
        entity = conn.execute(f"SELECT * FROM {table} WHERE id=%s", (entity_id,)).fetchone()
    if not entity:
        return {"error": "entity_not_found"}
    async with StudizbaClient(cfg) as client:
        await client.login()
        return await (collector.refresh_department(client, entity) if entity_type == "department" else collector.refresh_teacher(client, entity))


def main() -> None:
    mcp.run(transport="stdio")


def http_main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

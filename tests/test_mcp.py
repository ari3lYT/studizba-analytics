import asyncio

from studizba.mcp_server import mcp


def test_required_mcp_capabilities_are_registered():
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {"search_teachers", "search_reviews", "explain_score", "rank_departments", "get_recent_changes", "refresh_entity"} <= names
    assert "semantic_review_search" not in names

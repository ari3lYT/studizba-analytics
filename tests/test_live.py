import pytest

from studizba.client import StudizbaClient
from studizba.config import load_config
from studizba.parsers import parse_teacher


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_auth_and_authorized_reviews():
    cfg = load_config(require_credentials=True)
    url = f"/hs/{cfg.university_slug}/teachers/fof-1-fizicheskoe-vospitanie/33127-umarov-murad-muhamedovich.html"
    async with StudizbaClient(cfg) as client:
        await client.login(force=True)
        response = await client.get(url)
    teacher = parse_teacher(response.text, str(response.url))
    assert teacher.declared_reviews_count and teacher.declared_reviews_count > 30
    assert len(teacher.reviews) == 30

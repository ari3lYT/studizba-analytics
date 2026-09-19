import json
from datetime import datetime, timezone
from pathlib import Path

from studizba.parsers import content_hash, parse_department, parse_departments, parse_reviews_fragment, parse_russian_date, parse_teacher
from studizba.service import expand_search_query, normalize_department_query


FIXTURES = Path(__file__).parent / "fixtures"
DEPARTMENT_URL = "https://studizba.com/hs/mgtu-im-baumana/teachers/iu-7-programmnoe-obespechenie-evm-i/"
TEACHER_URL = DEPARTMENT_URL + "1001-aleksandra-primerova.html"


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_discovery_synthetic_fixture():
    departments = parse_departments(text("discovery.html"))
    assert len(departments) == 2
    assert any(x.slug == "iu-7-programmnoe-obespechenie-evm-i" for x in departments)
    assert len({x.source_key for x in departments}) == len(departments)


def test_department_synthetic_fixture():
    name, teachers = parse_department(text("department.html"), DEPARTMENT_URL)
    assert name == "Кафедра ИУ7"
    assert len(teachers) == 2
    teacher = next(x for x in teachers if x.source_id == 1001)
    assert teacher.name == "Александра Примерова"
    assert round(teacher.overall_rating, 2) == 4.88
    # A department card can report 0 while the teacher page has reviews;
    # the collector intentionally treats the detail page as authoritative.
    assert teacher.reviews_count == 0


def test_teacher_and_first_review_page_synthetic_fixture():
    teacher = parse_teacher(text("teacher.html"), TEACHER_URL)
    assert teacher.source_id == 1001
    assert teacher.name == "Александра Примерова"
    assert teacher.declared_reviews_count == 2
    assert len(teacher.reviews) == 2
    assert len({x.source_id for x in teacher.reviews}) == 2


def test_ajax_review_pagination_synthetic_fixture():
    payload = json.loads(text("reviews_page_2.json"))
    reviews = parse_reviews_fragment(payload["list"])
    assert payload["counts"]["1"] == 3
    assert len(reviews) == 1
    assert all(x.body for x in reviews)


def test_hash_supports_dedup_and_change_detection():
    stable = content_hash(4.8, 4.9, "same")
    assert stable == content_hash(4.8, 4.9, "same")
    assert stable != content_hash(4.8, 4.7, "same")


def test_domain_search_synonyms_are_or_branches():
    expanded = expand_search_query("можно списать на экзамене")
    assert " OR " in expanded
    assert "шпаргалка" in expanded
    assert "билеты" in expanded


def test_normalize_department_query_accepts_code_separators():
    assert normalize_department_query("ИУ-7") == "иу7"
    assert normalize_department_query("  ИУ  7 ") == "иу7"


def test_relative_yesterday_date_handles_month_boundary():
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    parsed = parse_russian_date("вчера в 23:15", now)
    assert parsed == datetime(2026, 2, 28, 23, 15, tzinfo=timezone.utc)

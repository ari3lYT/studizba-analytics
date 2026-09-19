from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import DepartmentCard, ReviewRecord, TeacherCard, TeacherPage


DEPARTMENT_RE = re.compile(r"^/hs/mgtu-im-baumana/teachers/([^/]+)/$")
TEACHER_RE = re.compile(r"/(\d+)-[^/]+\.html$")
SPACE_RE = re.compile(r"\s+")
RUS_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def norm(value: str | None) -> str:
    return SPACE_RE.sub(" ", value or "").strip()


def normalized_name(value: str) -> str:
    return re.sub(r"[^а-яёa-z0-9]+", " ", value.lower()).strip()


def number(value: str | None) -> float | None:
    if not value:
        return None
    m = re.search(r"-?\d+(?:[,.]\d+)?", value.replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def content_hash(*values: object) -> str:
    return hashlib.sha256("\x1f".join("" if v is None else str(v) for v in values).encode()).hexdigest()


def parse_russian_date(raw: str | None, now: datetime | None = None) -> datetime | None:
    text = norm(raw).lower()
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    today = now
    yesterday = now - timedelta(days=1)
    text = text.replace("сегодня", f"{today.day} {list(RUS_MONTHS)[today.month - 1]} {today.year}")
    text = text.replace("вчера", f"{yesterday.day} {list(RUS_MONTHS)[yesterday.month - 1]} {yesterday.year}")
    pat = r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?\s+в\s+(\d{1,2}):(\d{2})"
    m = re.search(pat, text)
    if not m or m.group(2) not in RUS_MONTHS:
        return None
    year = int(m.group(3) or now.year)
    result = datetime(year, RUS_MONTHS[m.group(2)], int(m.group(1)), int(m.group(4)), int(m.group(5)), tzinfo=timezone.utc)
    if not m.group(3) and result > now.replace(hour=23, minute=59) and (result - now).days > 7:
        result = result.replace(year=year - 1)
    return result


def parse_departments(html: str, page_url: str = "https://studizba.com/hs/mgtu-im-baumana/teachers/") -> list[DepartmentCard]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, DepartmentCard] = {}
    for a in soup.select(".link-subj a[href], a.link-subj-a[href]"):
        href = urljoin(page_url, a.get("href", ""))
        path = urlparse(href).path
        m = DEPARTMENT_RE.match(path)
        name_el = a.select_one(".searchable")
        name = norm(name_el.get_text(" ") if name_el else a.get_text(" "))
        if not m or not name or name.startswith("Преподаватели МГТУ"):
            continue
        slug = m.group(1)
        found[slug] = DepartmentCard(source_key=slug, slug=slug, name=name, url=href)
    return list(found.values())


def _card_container(a: Tag) -> Tag:
    node: Tag = a
    for _ in range(4):
        parent = node.parent
        if not isinstance(parent, Tag):
            break
        node = parent
        text = norm(node.get_text(" "))
        if "средний рейтинг" in text and "качество объяснений" in text:
            return node
    return a


def parse_department(html: str, url: str) -> tuple[str, list[TeacherCard]]:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    name = norm(heading.get_text(" ") if heading else "").removesuffix(" - преподаватели")
    path = urlparse(url).path.rstrip("/") + "/"
    cards: dict[int, TeacherCard] = {}
    for container in soup.select(".cat-list .one-t"):
        a = container.select_one("a.link-teacher-a[href]")
        if not a:
            continue
        absolute = urljoin(url, a.get("href", ""))
        parsed = urlparse(absolute)
        match = TEACHER_RE.search(parsed.path)
        if not match or not parsed.path.startswith(path):
            continue
        teacher_id = int(match.group(1))
        name_el = a.select_one(".searchable")
        desc_el = a.select_one(".one-teacher-descr")
        text = norm(name_el.get_text(" ") if name_el else a.get_text(" "))
        if not text:
            continue
        container_text = norm(container.get_text(" "))
        def extract(label: str) -> float | None:
            m = re.search(rf"(-|\d+[,.]\d+)\s*-\s*{re.escape(label)}", container_text, re.I)
            return number(m.group(1)) if m else None
        desc = norm(desc_el.get_text(" ") if desc_el else "") or None
        def sort_rating(attr: str) -> float | None:
            raw = container.get(attr)
            return int(raw) / 10000 if raw and raw.isdigit() and int(raw) > 0 else None
        cards[teacher_id] = TeacherCard(
            source_id=teacher_id,
            name=text.split("  ")[0] if "  " in text else text,
            url=absolute,
            description=desc,
            overall_rating=sort_rating("data-sort-sr") or extract("средний рейтинг"),
            explanation_rating=sort_rating("data-sort-qe") or extract("качество объяснений"),
            grades_rating=sort_rating("data-sort-r") or extract("оценки студентов"),
            attitude_rating=sort_rating("data-sort-v") or extract("отношение к студентам"),
            reviews_count=int(container.get("data-sort-reviews") or 0),
        )
    return name, list(cards.values())


def _rating_by_id(soup: BeautifulSoup, element_id: str) -> float | None:
    el = soup.find(id=element_id)
    return number(el.get_text(" ") if el else None)


def parse_reviews_fragment(html: str, *, now: datetime | None = None) -> list[ReviewRecord]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[ReviewRecord] = []
    for c in soup.select(".comment[data-id]"):
        source_id = int(c.get("data-id"))
        parent = int(c.get("data-parent-id") or 0) or None
        kind_el = c.select_one(".comment-left .ac")
        author_el = c.select_one(".comment-meta a[href]")
        body_el = c.select_one(".comm_content_text")
        date_el = c.select_one(".comment-date")
        vote_el = c.select_one(".comment-rating-num")
        body = norm(body_el.get_text(" ") if body_el else "")
        if not body:
            continue
        date_raw = norm(date_el.get_text(" ") if date_el else "")
        result.append(ReviewRecord(
            source_id=source_id,
            parent_source_id=parent,
            kind=norm(kind_el.get_text(" ") if kind_el else "") or None,
            author_name=norm(author_el.get_text(" ") if author_el else "") or None,
            author_url=urljoin("https://studizba.com", author_el.get("href", "")) if author_el else None,
            body=body,
            vote_rating=int(number(vote_el.get_text(" ") if vote_el else None)) if number(vote_el.get_text(" ") if vote_el else None) is not None else None,
            published_at=parse_russian_date(date_raw, now),
            date_raw=date_raw or None,
        ))
    return result


def parse_teacher(html: str, url: str, *, now: datetime | None = None) -> TeacherPage:
    soup = BeautifulSoup(html, "html.parser")
    match = TEACHER_RE.search(urlparse(url).path)
    if not match:
        raise ValueError(f"teacher id absent in URL: {url}")
    h1 = soup.find("h1")
    name = norm(h1.get_text(" ") if h1 else "")
    info = soup.find(id="teacher_info_inside")
    description = norm(info.get_text(" ") if info else "") or None
    count = None
    if description:
        m = re.search(r"У преподавателя\s+(\d+)\s+шт\.\s+отзывов", description)
        count = int(m.group(1)) if m else None
    crumbs = soup.select(".speedbar a[href]")
    dept = next((a for a in reversed(crumbs) if DEPARTMENT_RE.match(urlparse(urljoin(url, a.get("href", ""))).path)), None)
    canonical = soup.find("link", rel="canonical")
    canonical_url = canonical.get("href") if canonical and canonical.get("href") else url
    return TeacherPage(
        source_id=int(match.group(1)),
        name=name,
        url=urljoin(url, canonical_url),
        department_name=norm(dept.get_text(" ")) if dept else None,
        department_url=urljoin(url, dept.get("href", "")) if dept else None,
        description=description,
        title=norm(soup.title.get_text(" ") if soup.title else "") or None,
        overall_rating=_rating_by_id(soup, "all_sr"),
        explanation_rating=_rating_by_id(soup, "rqsr"),
        attitude_rating=_rating_by_id(soup, "rrsr"),
        grades_rating=_rating_by_id(soup, "rvsr"),
        declared_reviews_count=count,
        reviews=parse_reviews_fragment(str(soup.select_one("#comments_tree") or ""), now=now),
    )

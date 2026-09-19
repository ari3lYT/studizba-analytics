from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from .config import Config


class StudizbaAuthError(RuntimeError):
    pass


class StudizbaUnavailable(RuntimeError):
    """The source is unavailable after the shared backoff budget is exhausted."""


class StudizbaClient:
    LOGIN_PATH = "/engine/modules/filearray/ajax/user_ajax.php"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cookies_path = cfg.state_dir / "cookies.json"
        self._last_request = 0.0
        self._throttle = asyncio.Lock()
        cookies: dict[str, str] = {}
        persisted: list[dict[str, str]] = []
        if self.cookies_path.exists():
            try:
                loaded = json.loads(self.cookies_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cookies = loaded
                elif isinstance(loaded, list):
                    persisted = loaded
            except (ValueError, OSError):
                pass
        self.http = httpx.AsyncClient(
            base_url=cfg.base_url,
            cookies=cookies,
            follow_redirects=True,
            timeout=httpx.Timeout(40),
            headers={"User-Agent": "studizba-analytics/0.1 (+https://github.com/ari3lYT/studizba-analytics)", "Accept-Language": "ru,en;q=0.7"},
        )
        for cookie in persisted:
            self.http.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""), path=cookie.get("path", "/"))

    async def __aenter__(self) -> "StudizbaClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        self._save_cookies()
        await self.http.aclose()

    def _save_cookies(self) -> None:
        values = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in self.http.cookies.jar]
        self.cookies_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        self.cookies_path.chmod(0o600)

    async def _wait(self) -> None:
        async with self._throttle:
            delay = self.cfg.request_delay - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        backoffs = (5.0, 15.0, 45.0, 120.0, 300.0)
        for attempt in range(len(backoffs) + 1):
            try:
                # _wait serializes request *starts*, not response latency. This
                # preserves the global rate while allowing a few in-flight pages.
                await self._wait()
                response = await self.http.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                retryable = isinstance(exc, httpx.TransportError)
                if isinstance(exc, httpx.HTTPStatusError):
                    retryable = exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
                if not retryable:
                    raise
                if attempt >= len(backoffs):
                    raise StudizbaUnavailable(f"Studizba unavailable after retries: {exc}") from exc
                # Jitter prevents independently started workers from
                # reconnecting to the site at exactly the same time.
                await asyncio.sleep(backoffs[attempt] + random.uniform(0, 2.0))
        raise RuntimeError("unreachable")

    async def get(self, url: str) -> httpx.Response:
        return await self._request("GET", url)

    async def post(self, url: str, data: dict[str, Any]) -> httpx.Response:
        response = await self._request(
            "POST",
            url,
            data=data,
            headers={"X-Requested-With": "XMLHttpRequest", "Origin": self.cfg.base_url, "Referer": self.cfg.base_url + "/"},
        )
        return response

    async def login(self, *, force: bool = False) -> dict[str, Any]:
        if not self.cfg.studizba_username or not self.cfg.studizba_password:
            raise StudizbaAuthError("Studizba credentials are not configured")
        response = await self.post(self.LOGIN_PATH, {
            "action": "fast_registration", "step": 2,
            "email": self.cfg.studizba_username, "pass": self.cfg.studizba_password,
        })
        try:
            data = response.json()
        except ValueError as exc:
            raise StudizbaAuthError("Studizba login returned non-JSON") from exc
        if isinstance(data, dict) and data.get("error"):
            raise StudizbaAuthError(str(data["error"]))
        if not isinstance(data, (dict, list)):
            raise StudizbaAuthError("Studizba login returned unexpected JSON")
        self._save_cookies()
        return data if isinstance(data, dict) else {"response": data}

    async def fetch_all_review_pages(
        self, teacher_id: int, declared: int | None, first_page: list, parser,
        raw_pages: list[dict[str, Any]] | None = None,
    ) -> list:
        reviews = {r.source_id: r for r in first_page}
        if not declared or len(reviews) >= declared:
            return list(reviews.values())
        page = 2
        while page <= 200 and len(reviews) < declared:
            response = await self.post(self.LOGIN_PATH, {
                "action": "show_more_comments", "id": teacher_id, "p": page, "t": "dossier_comm",
                "z": 0, "s": 30, "q": 0, "oa": 0, "author_reviews_page": 0, "author_id": 0,
            })
            if raw_pages is not None:
                raw_pages.append({
                    "url": urljoin(self.cfg.base_url, self.LOGIN_PATH) + f"#show_more_comments:{teacher_id}:page:{page}",
                    "content": response.text,
                    "status": response.status_code,
                    "metadata": {"action": "show_more_comments", "teacher_source_id": teacher_id, "page": page},
                })
            payload = response.json()
            batch = parser(payload.get("list", ""))
            if not batch:
                break
            before = len(reviews)
            reviews.update({r.source_id: r for r in batch})
            if len(reviews) == before:
                break
            page += 1
        return list(reviews.values())

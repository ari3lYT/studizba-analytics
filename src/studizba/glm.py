from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config, GLMAccount, GLMProxy
from .db import Database
from .models import ANALYSIS_METRICS, ReviewAnalysis


ANALYZER_VERSION = "studizba-review-rubric-v2"
# httpx otherwise logs request URLs at INFO, which would expose a Gemini API key
# passed in its documented query parameter to systemd journals.
logging.getLogger("httpx").setLevel(logging.WARNING)


def analysis_schema() -> dict[str, Any]:
    metric = {
        "type": "object",
        "required": ["value", "confidence", "evidence", "context"],
        "properties": {
            "value": {"type": "number", "minimum": 0, "maximum": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "context": {"type": "array", "items": {"type": "string"}},
        },
    }
    return {
        "type": "object",
        "required": ["language", "metrics", "overall_confidence"],
        "properties": {
            "language": {"type": "string"}, "subject": {"type": ["string", "null"]},
            "semester": {"type": ["string", "null"]},
            "activity_types": {"type": "array", "items": {"type": "string"}},
            "metrics": {"type": "object", "additionalProperties": False, "properties": {name: metric for name in ANALYSIS_METRICS}},
            "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
    }


SYSTEM_PROMPT = f"""Ты извлекаешь проверяемые признаки из русского отзыва о преподавателе.
Верни ТОЛЬКО JSON. Шкала всех value: 0 = признак явно отсутствует/противоположен, 0.5 = нейтрально или неоднозначно, 1 = признак явно выражен.
Не создавай метрику без прямого текстового основания. Для каждой созданной метрики evidence должен содержать точные непрерывные цитаты из исходного отзыва.
Разделяй контексты: lecture, seminar, lab, coursework, exam, credit, rk, thesis, general.
Разрешённые материалы (allowed_notes/internet/materials/open_book_exam) не являются списыванием. cheating_opportunity ставь только при свидетельстве о возможности запрещённой помощи или слабого контроля.
Не смешивай лёгкие лабораторные с тяжёлым экзаменом. Поля метрик: value, confidence, evidence, context.
Допустимые метрики: {', '.join(ANALYSIS_METRICS)}.
Для одного отзыва строго используй такую верхнеуровневую структуру и не оборачивай её в answer/result/properties:
{{"language":"ru","subject":null,"semester":null,"activity_types":["exam"],"metrics":{{"exam_difficulty":{{"value":0.8,"confidence":0.9,"evidence":["точная цитата"],"context":["exam"]}}}},"overall_confidence":0.9,"notes":[]}}
Для пакетного входа верни {{"items":[{{"id":"переданный id","analysis":<та же структура анализа>}}]}}; один элемент для каждого входного id.
Если прямых сигналов нет, metrics должен быть пустым объектом, а overall_confidence равен 0.
Версия инструкции: {ANALYZER_VERSION}."""


class GLMError(RuntimeError):
    pass


class GLMProvider:
    def __init__(self, cfg: Config, account: GLMAccount | None = None, model: str | None = None, retries: int = 4,
                 proxy: GLMProxy | None = None):
        self.cfg = cfg
        self.account = account or GLMAccount("default", cfg.glm_api_key)
        self.model = model or cfg.glm_model
        self.retries = retries
        self.proxy = proxy
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    async def _wait_for_slot(self) -> None:
        async with self._request_lock:
            delay = self.cfg.glm_request_delay - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()

    @property
    def models(self) -> list[str]:
        if self.account.provider == "zai" and self.model == "glm-4.7-flash":
            # Prefer the newer model, but keep the queue moving when that model
            # is overloaded or request-filtered. _complete records whichever
            # model actually produced the successful response.
            return ["glm-4.7-flash", "glm-4.5-flash"]
        return [self.model]

    async def _complete(self, user_content: str, max_tokens: int) -> tuple[dict[str, Any], dict[str, Any], str]:
        if not self.account.api_key:
            raise GLMError("GLM API key is not configured")
        # Pools may mix OpenAI-compatible providers.  An account-level endpoint
        # must win over the legacy global Z.AI endpoint.
        url = (self.account.base_url or self.cfg.glm_base_url).rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        # ``thinking`` is a Z.AI extension; other OpenAI-compatible gateways
        # either reject it or silently change their request semantics.
        if self.account.provider == "zai":
            payload["thinking"] = {"type": "disabled"}
        response = None
        used_model = self.model
        last_transport_error: Exception | None = None
        async with httpx.AsyncClient(timeout=60, proxy=self.proxy.url if self.proxy else None) as client:
            for model in self.models:
                payload["model"] = model
                used_model = model
                for attempt in range(self.retries):
                    try:
                        await self._wait_for_slot()
                        response = await client.post(url, headers={"Authorization": f"Bearer {self.account.api_key}"}, json=payload)
                    except httpx.HTTPError as exc:
                        last_transport_error = exc
                        if attempt + 1 < self.retries:
                            await asyncio.sleep((10, 30, 60, 120)[min(attempt, 3)])
                        continue
                    if response.status_code not in (429, 500, 502, 503, 504):
                        break
                    retry_after = response.headers.get("Retry-After")
                    if attempt + 1 < self.retries:
                        await asyncio.sleep(float(retry_after) if retry_after and retry_after.isdigit() else (10, 30, 60, 120)[min(attempt, 3)])
                if response is not None and response.status_code < 400:
                    break
        if response is None:
            raise GLMError(f"GLM transport failed: {type(last_transport_error).__name__}: {last_transport_error}")
        if response.status_code >= 400:
            raise GLMError(f"GLM HTTP {response.status_code}: {response.text[:300]}")
        data = response.json()
        content = data["choices"][0]["message"].get("content") or ""
        return self._extract_json(content), data.get("usage", {}), used_model

    @staticmethod
    def _validate_evidence(text: str, parsed: dict[str, Any]) -> ReviewAnalysis:
        metrics = parsed.get("metrics")
        if isinstance(metrics, dict):
            parsed = dict(parsed)
            parsed["metrics"] = {name: value for name, value in metrics.items() if name in ANALYSIS_METRICS}
        result = ReviewAnalysis.model_validate(parsed)
        # Evidence must be traceable to the source; hallucinated spans are removed.
        folded = " ".join(text.lower().split())
        for name in list(result.metrics):
            metric = result.metrics[name]
            metric.evidence = [e for e in metric.evidence if " ".join(e.lower().split()) in folded]
            if not metric.evidence:
                del result.metrics[name]
        if result.metrics:
            result.overall_confidence = sum(m.confidence for m in result.metrics.values()) / len(result.metrics)
        else:
            result.overall_confidence = 0
        return result

    async def analyze(self, text: str) -> tuple[ReviewAnalysis, dict[str, Any], str]:
        parsed, usage, used_model = await self._complete("Исходный отзыв:\n" + text, 3000)
        return self._validate_evidence(text, parsed), usage, used_model

    async def analyze_many(self, items: list[tuple[int, str]]) -> tuple[dict[int, ReviewAnalysis], dict[str, Any], str]:
        if len(items) == 1:
            result, usage, model = await self.analyze(items[0][1])
            return {items[0][0]: result}, usage, model
        request_items = [{"id": str(review_id), "text": text} for review_id, text in items]
        parsed, usage, used_model = await self._complete(
            "Проанализируй пакет отзывов. Верни один item для каждого id:\n"
            + json.dumps(request_items, ensure_ascii=False),
            min(14000, 2200 * len(items)),
        )
        raw_items = parsed.get("items")
        if not isinstance(raw_items, list):
            raise GLMError("batch response has no items array")
        source = {review_id: text for review_id, text in items}
        results: dict[int, ReviewAnalysis] = {}
        for item in raw_items:
            try:
                review_id = int(item["id"])
                if review_id not in source or review_id in results:
                    continue
                results[review_id] = self._validate_evidence(source[review_id], item["analysis"])
            except (KeyError, TypeError, ValueError):
                continue
        missing = set(source) - set(results)
        if missing:
            raise GLMError(f"batch response missing valid review ids: {sorted(missing)}")
        return results, usage, used_model

    @staticmethod
    def _extract_json(value: str) -> dict[str, Any]:
        cleaned = re.sub(r"<think>.*?</think>", "", value, flags=re.S).strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S)
        try:
            return json.loads(cleaned)
        except ValueError:
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start >= 0 and end > start:
                return json.loads(cleaned[start:end + 1])
            raise GLMError("GLM response does not contain valid JSON")


class GeminiProvider(GLMProvider):
    """Google Gemini adapter retaining the same validated structured result."""
    async def _complete(self, user_content: str, max_tokens: int) -> tuple[dict[str, Any], dict[str, Any], str]:
        model = self.account.model or "gemini-3.6-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.account.api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": user_content}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": min(max_tokens, 4096),
                                 "responseMimeType": "application/json"},
        }
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise GLMError(f"Gemini transport failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise GLMError(f"Gemini HTTP {response.status_code}: {response.text[:300]}")
        data = response.json()
        try:
            content = "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            raise GLMError("Gemini response has no candidate content") from exc
        return self._extract_json(content), data.get("usageMetadata", {}), model


class AnalysisWorker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg)
        self.provider = GLMProvider(cfg)

    def register_version(self) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO analyzer_versions(version,prompt,json_schema) VALUES(%s,%s,%s)
                   ON CONFLICT(version) DO UPDATE SET prompt=excluded.prompt,json_schema=excluded.json_schema""",
                (ANALYZER_VERSION, SYSTEM_PROMPT, json.dumps(analysis_schema())),
            )
            conn.execute(
                """INSERT INTO analysis_queue(review_id,priority,status,next_attempt_at,updated_at)
                   SELECT r.id,1,'pending',now(),now() FROM reviews r
                   WHERE r.active AND NOT EXISTS (
                     SELECT 1 FROM review_analyses a WHERE a.review_id=r.id
                       AND a.analyzer_version=%s AND a.source_body_hash=r.body_hash
                   ) ON CONFLICT(review_id) DO UPDATE SET
                     status='pending',next_attempt_at=now(),updated_at=now()""",
                (ANALYZER_VERSION,),
            )
            conn.execute(
                """UPDATE analysis_queue q SET status='done',updated_at=now(),last_error=NULL
                   FROM reviews r WHERE q.review_id=r.id AND EXISTS (
                     SELECT 1 FROM review_analyses a WHERE a.review_id=r.id
                       AND a.analyzer_version=%s AND a.source_body_hash=r.body_hash
                   )""",
                (ANALYZER_VERSION,),
            )

    async def run(
        self, *, limit: int = 100, concurrency: int = 3, retry: bool = False, batch_size: int = 5
    ) -> dict[str, int]:
        self.register_version()
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE analysis_queue SET status='pending',updated_at=now(),
                   last_error=coalesce(last_error,'recovered stale running job')
                   WHERE status='running' AND updated_at < now()-interval '15 minutes'"""
            )
            if retry:
                conn.execute("UPDATE analysis_queue SET status='pending',attempts=0,next_attempt_at=now() WHERE status IN ('failed','pending')")
            rows = conn.execute(
                """SELECT q.review_id,r.body,r.body_hash FROM analysis_queue q JOIN reviews r ON r.id=q.review_id
                   WHERE q.status='pending' AND q.next_attempt_at<=now()
                   ORDER BY q.priority DESC,char_length(r.body) DESC,q.updated_at LIMIT %s""", (limit,)
            ).fetchall()
        semaphore = asyncio.Semaphore(concurrency)
        stats = {"selected": len(rows), "analyzed": 0, "cached": 0, "failed": 0}

        uncached = rows

        async def one_batch(batch: list[dict[str, Any]]) -> None:
            ids = [int(row["review_id"]) for row in batch]
            try:
                async with semaphore:
                    with self.db.connect() as conn:
                        conn.execute("UPDATE analysis_queue SET status='running',updated_at=now() WHERE review_id=ANY(%s)", (ids,))
                    results, usage, used_model = await self.provider.analyze_many(
                        [(int(row["review_id"]), row["body"]) for row in batch]
                    )
                with self.db.connect() as conn:
                    allocated_usage = {"batch_size": len(batch), "allocated": {
                        key: round(value / len(batch), 2) if isinstance(value, (int, float)) else value
                        for key, value in usage.items()
                    }}
                    for row in batch:
                        result = results[int(row["review_id"])]
                        input_hash = hashlib.sha256((row["body_hash"] + ANALYZER_VERSION + used_model).encode()).hexdigest()
                        conn.execute(
                            """INSERT INTO review_analyses(review_id,analyzer_version,model,input_hash,source_body_hash,result,overall_confidence,usage)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (row["review_id"], ANALYZER_VERSION, used_model, input_hash, row["body_hash"],
                             json.dumps(result.model_dump(mode="json"), ensure_ascii=False), result.overall_confidence,
                             json.dumps(allocated_usage)),
                        )
                    conn.execute("UPDATE analysis_queue SET status='done',updated_at=now(),last_error=NULL WHERE review_id=ANY(%s)", (ids,))
                stats["analyzed"] += len(batch)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                if len(batch) > 1 and "transport" not in message.lower() and "http" not in message.lower():
                    midpoint = len(batch) // 2
                    await one_batch(batch[:midpoint])
                    await one_batch(batch[midpoint:])
                    return
                with self.db.connect() as conn:
                    conn.execute(
                        """UPDATE analysis_queue SET status=CASE WHEN attempts>=4 THEN 'failed' ELSE 'pending' END,
                           attempts=attempts+1,next_attempt_at=now()+make_interval(secs=>least(86400,60*power(2,attempts))),
                           last_error=%s,updated_at=now() WHERE review_id=ANY(%s)""",
                        (message[:1000], ids)
                    )
                stats["failed"] += len(batch)

        size = max(1, min(10, batch_size))
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = 0
        for row in uncached:
            chars = len(row["body"])
            if current and (len(current) >= size or current_chars + chars > 9000):
                batches.append(current)
                current, current_chars = [], 0
            current.append(row)
            current_chars += chars
        if current:
            batches.append(current)
        await asyncio.gather(*(one_batch(batch) for batch in batches))
        return stats


class MultiAccountAnalysisWorker(AnalysisWorker):
    """Durable, independently throttled GLM account pool.

    Every batch is claimed inside a PostgreSQL transaction before any HTTP call.
    Consequently two service instances (including a restart overlap) cannot send
    the same review to Z.AI at the same time.
    """
    def __init__(self, cfg: Config, *, model: str | None = None, batch_size: int = 10, concurrency_per_account: int = 2):
        super().__init__(cfg)
        self.model = model or cfg.glm_model
        self.batch_size = max(1, min(15, batch_size))
        self.concurrency_per_account = max(1, min(4, concurrency_per_account))
        self._cooldowns: dict[str, float] = {}
        self._cooldown_lock = asyncio.Lock()
        self._proxy_index: dict[str, int] = {}
        self._failure_streaks: dict[str, int] = {}

    async def _wait_account_ready(self, label: str) -> None:
        async with self._cooldown_lock:
            until = self._cooldowns.get(label, 0.0)
        if until > time.monotonic():
            await asyncio.sleep(until - time.monotonic())

    async def _cool_account(self, label: str, seconds: int) -> None:
        async with self._cooldown_lock:
            self._cooldowns[label] = max(self._cooldowns.get(label, 0.0), time.monotonic() + seconds)

    def _register_accounts(self) -> None:
        self.db.init()
        with self.db.connect() as conn:
            # Keep historical counters, but remove providers disabled in the
            # credentials file from the live worker pool and dashboard state.
            conn.execute("UPDATE glm_account_stats SET enabled=false,active_requests=0,updated_at=now()")
            for account in self.cfg.glm_accounts:
                conn.execute(
                    """INSERT INTO glm_account_stats(label,enabled,healthy,model,updated_at)
                       VALUES(%s,true,true,%s,now())
                       ON CONFLICT(label) DO UPDATE SET enabled=true,model=excluded.model,updated_at=now()""",
                    (account.label, account.model or self.model),
                )

    def _proxy_for(self, label: str) -> GLMProxy | None:
        if not self.cfg.glm_proxies:
            return None
        return self.cfg.glm_proxies[self._proxy_index.get(label, 0) % len(self.cfg.glm_proxies)]

    def _rotate_proxy(self, label: str) -> None:
        if self.cfg.glm_proxies:
            self._proxy_index[label] = self._proxy_index.get(label, 0) + 1

    def _set_proxy(self, label: str, proxy: GLMProxy | None) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE glm_account_stats SET proxy_label=%s,updated_at=now() WHERE label=%s", (proxy.label if proxy else "direct", label))

    def _claim(self, amount: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT q.review_id,r.body,r.body_hash FROM analysis_queue q JOIN reviews r ON r.id=q.review_id
                   WHERE q.status='pending' AND q.next_attempt_at<=now() AND r.active
                     AND NOT EXISTS (SELECT 1 FROM review_analyses a WHERE a.review_id=r.id
                       AND a.analyzer_version=%s AND a.source_body_hash=r.body_hash)
                   ORDER BY q.priority DESC,char_length(r.body) DESC,q.updated_at
                   FOR UPDATE OF q SKIP LOCKED LIMIT %s""", (ANALYZER_VERSION, amount)
            ).fetchall()
            if rows:
                conn.execute("UPDATE analysis_queue SET status='running',updated_at=now() WHERE review_id=ANY(%s)",
                             ([int(row["review_id"]) for row in rows],))
            return rows

    def _account_update(self, label: str, *, success: int = 0, failed: int = 0, http429: int = 0,
                        network: int = 0, latency_ms: float | None = None, cooldown_seconds: int | None = None,
                        error: str | None = None, disable: bool = False) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE glm_account_stats SET processed_count=processed_count+%s,failed_count=failed_count+%s,
                   http_429_count=http_429_count+%s,network_error_count=network_error_count+%s,
                   latency_total_ms=latency_total_ms+%s,latency_samples=latency_samples+%s,
                   healthy=%s,enabled=CASE WHEN %s THEN false ELSE enabled END,
                   cooldown_until=CASE WHEN %s>0 THEN NULL WHEN %s::int IS NULL THEN cooldown_until
                     ELSE now()+make_interval(secs=>%s::int) END,
                   last_success_at=CASE WHEN %s>0 THEN now() ELSE last_success_at END,
                   last_error=%s,last_error_at=CASE WHEN %s::text IS NULL THEN last_error_at ELSE now() END,updated_at=now()
                   WHERE label=%s""",
                (success, failed, http429, network, latency_ms or 0, 1 if latency_ms is not None else 0,
                 error is None, disable, success, cooldown_seconds, cooldown_seconds or 0,
                 success, error, error, label),
            )

    def _set_active(self, label: str, delta: int) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE glm_account_stats SET active_requests=greatest(0,active_requests+%s),updated_at=now() WHERE label=%s", (delta, label))

    def _release(self, rows: list[dict[str, Any]], error: str, *, transient: bool = False) -> None:
        ids = [int(row["review_id"]) for row in rows]
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE analysis_queue SET status=CASE WHEN %s THEN 'pending' WHEN attempts>=4 THEN 'failed' ELSE 'pending' END,
                   attempts=attempts+1,next_attempt_at=now()+make_interval(secs=>least(21600,60*power(2,attempts))),
                   last_error=%s,updated_at=now() WHERE review_id=ANY(%s)""", (transient, error[:1000], ids))

    def _store(self, rows: list[dict[str, Any]], results: dict[int, ReviewAnalysis], usage: dict[str, Any], model: str) -> None:
        ids = [int(row["review_id"]) for row in rows]
        with self.db.connect() as conn:
            for row in rows:
                result = results[int(row["review_id"])]
                input_hash = hashlib.sha256((row["body_hash"] + ANALYZER_VERSION + model).encode()).hexdigest()
                conn.execute(
                    """INSERT INTO review_analyses(review_id,analyzer_version,model,input_hash,source_body_hash,result,overall_confidence,usage)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (row["review_id"], ANALYZER_VERSION, model, input_hash, row["body_hash"],
                     json.dumps(result.model_dump(mode="json"), ensure_ascii=False), result.overall_confidence,
                     json.dumps({"batch_size": len(rows), "provider_pool": True, "usage": usage})),
                )
            conn.execute("UPDATE analysis_queue SET status='done',updated_at=now(),last_error=NULL WHERE review_id=ANY(%s)", (ids,))

    async def _account_loop(self, account: GLMAccount, stats: dict[str, int]) -> None:
        while True:
            await self._wait_account_ready(account.label)
            rows = self._claim(1 if account.provider == "gemini" else self.batch_size)
            if not rows:
                return
            began = time.monotonic()
            # Gemini uses a distinct native API; all OpenAI-compatible providers
            # benefit from the independently health-checked egress pool.
            proxy = self._proxy_for(account.label) if account.provider != "gemini" else None
            self._set_proxy(account.label, proxy)
            provider = (GeminiProvider(self.cfg, account, account.model, retries=1)
                        if account.provider == "gemini"
                        else GLMProvider(self.cfg, account, account.model or self.model, retries=1, proxy=proxy))
            self._set_active(account.label, 1)
            try:
                results, usage, model = await provider.analyze_many([(int(row["review_id"]), row["body"]) for row in rows])
                self._store(rows, results, usage, model)
                elapsed = (time.monotonic() - began) * 1000
                self._failure_streaks.pop(account.label, None)
                self._account_update(account.label, success=len(rows), latency_ms=elapsed)
                stats["analyzed"] += len(rows)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                lower = message.lower()
                is_429 = "http 429" in lower
                # Z.AI can put application code 1305 in a HTTP 429 response.
                # It is normally documented as a temporary rate/traffic limit,
                # but it has also been observed for deterministic request-body
                # filtering.  Therefore a 1305 response must not be used as
                # evidence that a particular egress proxy is bad.
                is_ambiguous_1305 = is_429 and "1305" in lower
                is_concurrency_1302 = is_429 and "1302" in lower
                invalid = any(x in lower for x in ("http 401", "http 403", "invalid api", "invalid key"))
                transient = is_429 or "http 5" in lower or "transport" in lower or "timeout" in lower
                streak = self._failure_streaks.get(account.label, 0) + 1
                self._failure_streaks[account.label] = streak
                # Recover quickly from isolated throttles while still backing
                # off exponentially when one account keeps failing.  State is
                # independent per account, so healthy keys continue at speed.
                if is_concurrency_1302:
                    base, cap = 20, 180
                elif is_ambiguous_1305:
                    base, cap = 45, 600
                elif is_429:
                    base, cap = 60, 900
                elif transient:
                    base, cap = 30, 300
                else:
                    base = cap = 0
                cooldown = min(cap, base * (2 ** min(streak - 1, 4))) + (abs(hash(account.label)) % 11) if base else None
                self._release(rows, message, transient=transient)
                self._account_update(account.label, failed=len(rows), http429=int(is_429),
                                     network=int(transient and not is_429), cooldown_seconds=cooldown,
                                     error=message, disable=invalid)
                stats["failed"] += len(rows)
                if invalid:
                    return
                # Switching IPs on an ambiguous provider/body rejection only
                # creates noisy retries and makes diagnosis less reproducible.
                # Rotate solely for genuine transport/server failures.
                if transient and not is_ambiguous_1305:
                    self._rotate_proxy(account.label)
                if cooldown:
                    await self._cool_account(account.label, cooldown)
            finally:
                self._set_active(account.label, -1)

    async def run_pool(self, *, max_batches: int | None = None) -> dict[str, Any]:
        if not self.cfg.glm_accounts:
            return {"skipped": "no GLM accounts configured"}
        self.register_version()
        self._register_accounts()
        # A process crash cannot leave an in-flight HTTP request alive; clear the
        # dashboard counter before this durable worker starts claiming batches.
        with self.db.connect() as conn:
            conn.execute("UPDATE glm_account_stats SET active_requests=0,updated_at=now()")
        if self.cfg.glm_proxies:
            self._proxy_index = {account.label: index % len(self.cfg.glm_proxies)
                                 for index, account in enumerate(self.cfg.glm_accounts)}
        with self.db.connect() as conn:
            conn.execute("UPDATE analysis_queue SET status='pending',updated_at=now() WHERE status='running' AND updated_at < now()-interval '20 minutes'")
        stats: dict[str, int] = {"analyzed": 0, "failed": 0}
        tasks = [self._account_loop(account, stats) for account in self.cfg.glm_accounts for _ in range(self.concurrency_per_account)]
        await asyncio.gather(*tasks)
        return {**stats, "accounts": len(self.cfg.glm_accounts), "model": self.model,
                "batch_size": self.batch_size, "concurrency_per_account": self.concurrency_per_account}

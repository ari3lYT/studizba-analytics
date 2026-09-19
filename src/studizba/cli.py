from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

import typer

from .analytics import AnalyticsEngine
from .acceptance import run_acceptance
from .client import StudizbaClient
from .collector import Collector
from .config import GLMAccount, load_config
from .db import Database, sanitize_source_html
from .glm import ANALYZER_VERSION, AnalysisWorker, GLMProvider, MultiAccountAnalysisWorker
from .service import QueryService


app = typer.Typer(no_args_is_help=True, help="Studizba BMSTU collector, analytics and MCP platform")


def output(value) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.command("init-db")
def init_db() -> None:
    cfg = load_config()
    Database(cfg).init()
    output({"ok": True})


@app.command("auth-check")
def auth_check() -> None:
    output(asyncio.run(Collector(load_config(require_credentials=True)).auth_check()))


@app.command()
def discover() -> None:
    async def run():
        cfg = load_config(require_credentials=True)
        collector = Collector(cfg)
        async with StudizbaClient(cfg) as client:
            await client.login()
            return [x.model_dump() for x in await collector.discover(client)]
    output(asyncio.run(run()))


@app.command()
def bootstrap(
    details_limit: Optional[int] = typer.Option(None, help="Limit detail pages for a staged bootstrap; omit for full"),
    all_reviews: bool = typer.Option(True, help="Load all review pages, not only the first 30"),
    resume: bool = typer.Option(True, help="Skip teachers that already have a stored detail snapshot"),
    catalog: bool = typer.Option(True, help="Refresh university and department rosters before teacher details"),
) -> None:
    output(asyncio.run(Collector(load_config(require_credentials=True)).bootstrap(
        details_limit=details_limit, all_reviews=all_reviews, resume=resume, refresh_catalog=catalog
    )))


@app.command("refresh-department")
def refresh_department(department_id: int) -> None:
    async def run():
        cfg = load_config(require_credentials=True); collector = Collector(cfg)
        with collector.db.connect() as conn:
            row = conn.execute("SELECT * FROM departments WHERE id=%s", (department_id,)).fetchone()
        if not row: raise typer.BadParameter("department not found")
        async with StudizbaClient(cfg) as client:
            await client.login(); return await collector.refresh_department(client, row)
    output(asyncio.run(run()))


@app.command("refresh-teacher")
def refresh_teacher(teacher_id: int, all_reviews: bool = True) -> None:
    async def run():
        cfg = load_config(require_credentials=True); collector = Collector(cfg)
        with collector.db.connect() as conn:
            row = conn.execute("SELECT * FROM teachers WHERE id=%s OR source_id=%s LIMIT 1", (teacher_id, teacher_id)).fetchone()
        if not row: raise typer.BadParameter("teacher not found")
        async with StudizbaClient(cfg) as client:
            await client.login(); return await collector.refresh_teacher(client, row, all_reviews=all_reviews)
    output(asyncio.run(run()))


@app.command()
def sync(limit: int = 100) -> None:
    output(asyncio.run(Collector(load_config(require_credentials=True)).incremental(limit=limit)))


@app.command()
def analyze(limit: int = 100, concurrency: int = 3, retry: bool = False, batch_size: int = 5) -> None:
    cfg = load_config()
    if cfg.glm_accounts:
        output(asyncio.run(MultiAccountAnalysisWorker(cfg, batch_size=batch_size,
            concurrency_per_account=max(1, min(2, concurrency))).run_pool()))
    else:
        output(asyncio.run(AnalysisWorker(cfg).run(limit=limit, concurrency=concurrency, retry=retry, batch_size=batch_size)))


@app.command("analyze-chatmock-budget")
def analyze_chatmock_budget(
    max_percentage_points: float = typer.Option(5.0, min=0.1, max=100.0),
    max_used_percent: Optional[float] = typer.Option(None, min=0.1, max=100.0,
                                                      help="Absolute ceiling for the selected allowance."),
    allowance: str = typer.Option("primary", help="primary Work/Codex pool or spark preview pool"),
    primary_max_used_percent: Optional[float] = typer.Option(None, min=0.1, max=100.0,
                                                              help="Independent ceiling for the main weekly Work/Codex pool."),
    max_batches: int = typer.Option(10, min=1, max=1000),
    batch_size: int = typer.Option(5, min=1, max=10),
    model: str = typer.Option("gpt-5.3-codex-spark"),
) -> None:
    """Drain a small, measured queue slice through the authenticated ChatMock.

    The upstream rate-limit response headers are persisted by ChatMock.  They are
    checked before every batch, so this command cannot intentionally consume more
    than the requested number of weekly percentage points.  ``max_batches`` is a
    second independent, conservative safety cap.
    """
    chatmock_base = os.getenv("CHATMOCK_BASE_URL", "http://127.0.0.1:6666/v1").rstrip("/")

    def allowance_values() -> dict[str, float | None]:
        # The ChatMock header cache has both five-hour and weekly windows.  Ask
        # the same authenticated Codex analytics endpoint displayed in the UI,
        # so the budget guard tracks the actual weekly allowance.
        import urllib.request
        with urllib.request.urlopen(chatmock_base + "/models", timeout=30):
            pass
        code = '''import json, requests
auth=json.load(open("/data/auth.json"))["tokens"]
data=requests.get("https://chatgpt.com/backend-api/wham/usage",headers={"Authorization":"Bearer "+auth["access_token"],"chatgpt-account-id":auth["account_id"],"User-Agent":"Mozilla/5.0"},timeout=30).json()
spark=next((item for item in data.get("additional_rate_limits",[]) if item.get("limit_name")=="GPT-5.3-Codex-Spark"), None)
print(json.dumps({"primary":data["rate_limit"]["primary_window"]["used_percent"], "spark":spark["rate_limit"]["primary_window"]["used_percent"] if spark else None}))'''
        result = subprocess.run(["/usr/bin/docker", "exec", "chatmock", "python", "-c", code],
                                check=True, text=True, capture_output=True, timeout=45)
        values = json.loads(result.stdout)
        if allowance not in values or values[allowance] is None:
            raise RuntimeError(f"ChatMock allowance is unavailable: {allowance}")
        return {key: (float(value) if value is not None else None) for key, value in values.items()}

    def used_percent() -> float:
        return float(allowance_values()[allowance])

    async def run() -> dict:
        cfg = load_config()
        worker = MultiAccountAnalysisWorker(cfg, batch_size=batch_size, concurrency_per_account=1)
        worker.db.init()
        worker.register_version()
        initial = used_percent()
        ceiling = (max_used_percent if max_used_percent is not None
                   else min(100.0, initial + max_percentage_points))
        if ceiling < initial:
            return {"skipped": "allowance already above ceiling", "allowance": allowance,
                    "baseline_percent": initial, "ceiling_percent": ceiling}
        account = GLMAccount(
            "chatmock-budget", "local-session", "chatmock", model,
            os.getenv("CHATMOCK_BASE_URL", "http://127.0.0.1:6666/v1"),
        )
        provider = GLMProvider(cfg, account, account.model, retries=1)
        result = {"baseline_percent": initial, "ceiling_percent": ceiling,
                  "analyzed": 0, "batches": 0, "errors": 0, "model": model,
                  "allowance": allowance}
        for _ in range(max_batches):
            values = allowance_values()
            if float(values[allowance]) >= ceiling:
                result["stop_reason"] = "weekly_budget_reached"
                break
            if primary_max_used_percent is not None and float(values["primary"]) >= primary_max_used_percent:
                result["stop_reason"] = "primary_weekly_budget_reached"
                break
            rows = worker._claim(batch_size)
            if not rows:
                result["stop_reason"] = "queue_empty"
                break
            try:
                analyses, usage, used_model = await provider.analyze_many(
                    [(int(row["review_id"]), row["body"]) for row in rows]
                )
                worker._store(rows, analyses, usage, used_model)
                result["analyzed"] += len(analyses)
                result["batches"] += 1
            except Exception as exc:
                worker._release(rows, f"{type(exc).__name__}: {exc}", transient=True)
                result["errors"] += len(rows)
                result["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
                result["stop_reason"] = "provider_error"
                break
        else:
            result["stop_reason"] = "batch_safety_cap"
        final_values = allowance_values()
        result["final_percent"] = final_values[allowance]
        result["final_primary_percent"] = final_values["primary"]
        return result
    output(asyncio.run(run()))


@app.command()
def aggregate() -> None:
    engine = AnalyticsEngine(load_config())
    output({**engine.calculate_all_teachers(), **engine.calculate_departments()})


@app.command()
def status() -> None:
    output(QueryService(load_config()).monitor_status())


@app.command()
def acceptance() -> None:
    result = run_acceptance(load_config())
    output(result)
    if not result["ok"]:
        raise typer.Exit(1)


@app.command()
def finalize(max_rounds: int = 100, retry_delay: int = 60) -> None:
    """Drain GLM work, calculate aggregates and require acceptance success."""
    cfg = load_config()
    db = Database(cfg)
    for round_number in range(1, max_rounds + 1):
        glm_result = asyncio.run(MultiAccountAnalysisWorker(cfg, batch_size=5,
            concurrency_per_account=2).run_pool()) if cfg.glm_accounts else asyncio.run(AnalysisWorker(cfg).run(
                limit=100000, concurrency=2, retry=True, batch_size=5))
        with db.connect() as conn:
            remaining = int(conn.execute(
                """SELECT count(*) n FROM reviews r WHERE r.active AND NOT EXISTS (
                     SELECT 1 FROM review_analyses a WHERE a.review_id=r.id
                       AND a.analyzer_version=%s AND a.source_body_hash=r.body_hash)""",
                (ANALYZER_VERSION,),
            ).fetchone()["n"])
        output({"round": round_number, "glm": glm_result, "remaining": remaining})
        if remaining == 0:
            break
        time.sleep(max(10, retry_delay))
    else:
        raise RuntimeError("GLM queue did not converge")
    engine = AnalyticsEngine(cfg)
    output({"analytics": {**engine.calculate_all_teachers(), **engine.calculate_departments()}})
    result = run_acceptance(cfg)
    output(result)
    if not result["ok"]:
        raise RuntimeError("acceptance checks failed")


@app.command()
def monitor(interval: int = 300, batch: int = 100, loop: bool = True, analyze_batch: int = 50) -> None:
    cfg = load_config(require_credentials=True)
    async def iteration():
        crawl = await Collector(cfg).incremental(limit=batch)
        # The production configuration uses a multi-account pool; a legacy
        # single ``glm_api_key`` must not be required for incremental analysis.
        glm = (await MultiAccountAnalysisWorker(cfg, batch_size=min(5, analyze_batch),
                concurrency_per_account=1).run_pool()
               if cfg.glm_accounts else {"skipped": "no enabled accounts"})
        engine = AnalyticsEngine(cfg); teachers = engine.calculate_all_teachers(); departments = engine.calculate_departments()
        return {"crawl": crawl, "glm": glm, "analytics": {**teachers, **departments}}
    while True:
        output(asyncio.run(iteration()))
        if not loop: break
        time.sleep(max(30, interval))


@app.command("serve-dashboard")
def serve_dashboard(host: str = "0.0.0.0", port: int = 8898) -> None:
    import uvicorn
    uvicorn.run("studizba.dashboard:app", host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")


@app.command("capture-fixtures")
def capture_fixtures(output_dir: Path = Path("tests/fixtures")) -> None:
    async def run():
        cfg = load_config(require_credentials=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        async with StudizbaClient(cfg) as client:
            await client.login()
            urls = {
                "discovery.html": f"/hs/{cfg.university_slug}/teachers/",
                "department.html": f"/hs/{cfg.university_slug}/teachers/fof-1-fizicheskoe-vospitanie/",
                "teacher.html": f"/hs/{cfg.university_slug}/teachers/fof-1-fizicheskoe-vospitanie/33127-umarov-murad-muhamedovich.html",
            }
            for filename, url in urls.items():
                response = await client.get(url)
                (output_dir / filename).write_text(sanitize_source_html(response.text), encoding="utf-8")
            response = await client.post(client.LOGIN_PATH, {"action":"show_more_comments","id":33127,"p":2,"t":"dossier_comm","z":0,"s":30,"q":0,"oa":0,"author_reviews_page":0,"author_id":0})
            (output_dir / "reviews_page_2.json").write_text(response.text, encoding="utf-8")
        return {"saved": sorted(p.name for p in output_dir.iterdir())}
    output(asyncio.run(run()))


if __name__ == "__main__":
    app()

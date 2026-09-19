from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .config import load_config
from .service import QueryService


app = FastAPI(
    title="Studizba BMSTU Analytics API",
    description="Public read API over the local Studizba BMSTU database. MCP is available at /studizba/mcp.",
    version="0.1.0",
    root_path="/studizba",
)
service = QueryService(load_config())
_DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")


@app.get("/api/health", tags=["monitoring"])
def health():
    return {"ok": True}


@app.get("/api/status")
def status():
    return service.monitor_status()


@app.get("/api/progress", tags=["monitoring"])
def progress(hours: int = 24, bucket_minutes: int = 15):
    """Bootstrap/GLM completion, rolling throughput, queue health and crawl-run history."""
    return service.progress_status(hours, bucket_minutes)


@app.get("/api/changes")
def changes(days: int = 7, limit: int = 100):
    return service.recent_changes(days, limit)


@app.get("/api/reviews/new")
def reviews(days: int = 7, limit: int = 100):
    return service.new_reviews(days, limit)


@app.get("/api/reviews/search", tags=["reviews"])
def search_reviews(query: str, limit: int = 30, department_id: int | None = None, teacher_id: int | None = None):
    return service.search_reviews(query, limit, department_id, teacher_id)


@app.get("/api/teachers/search", tags=["teachers"])
def search_teachers(query: str, limit: int = 20):
    return service.search_teachers(query, limit)


@app.get("/api/teachers/rank", tags=["teachers"])
def rank_teachers(metric: str, limit: int = 20, department_id: int | None = None, min_confidence: float = .15, descending: bool = True):
    return service.rank_teachers(metric, limit, department_id, min_confidence, descending)


@app.get("/api/teachers/{teacher_id}", tags=["teachers"])
def teacher_profile(teacher_id: int):
    result = service.teacher_profile(teacher_id)
    if result is None:
        raise HTTPException(404, "teacher not found")
    return result


@app.get("/api/teachers/{teacher_id}/reviews", tags=["teachers"])
def teacher_reviews(teacher_id: int, limit: int = 50, offset: int = 0, kind: str | None = None):
    return service.teacher_reviews(teacher_id, limit, offset, kind)


@app.get("/api/teachers/{teacher_id}/analytics", tags=["teachers"])
def teacher_analytics(teacher_id: int):
    return service.teacher_analytics(teacher_id)


@app.get("/api/teachers/{teacher_id}/explain/{metric}", tags=["teachers"])
def explain_teacher_score(teacher_id: int, metric: str, limit: int = 30):
    return service.explain_score(teacher_id, metric, limit)


@app.get("/api/teachers/{teacher_id}/history", tags=["teachers"])
def teacher_history(teacher_id: int, days: int = 365):
    return service.rating_history(teacher_id, days)


@app.get("/api/departments/search", tags=["departments"])
def search_departments(query: str, limit: int = 20):
    return service.search_departments(query, limit)


@app.get("/api/departments/rank", tags=["departments"])
def rank_departments(metric: str, limit: int = 20, min_confidence: float = .1, descending: bool = True):
    return service.rank_departments(metric, limit, min_confidence, descending)


@app.get("/api/departments/{department_id}", tags=["departments"])
def department_analytics(department_id: int):
    result = service.department_analytics(department_id)
    if result is None:
        raise HTTPException(404, "department not found")
    return result


@app.get("/", response_class=HTMLResponse)
def modern_home():
    return HTMLResponse(_DASHBOARD_HTML.read_text(encoding="utf-8"))


@app.get("/_legacy", response_class=HTMLResponse)
def home():
    return """<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Studizba Monitor</title>
<style>
:root{color-scheme:dark;color:#e8edf6;background:#090e18;font:15px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}*{box-sizing:border-box}body{max-width:1200px;margin:0 auto;padding:24px 18px 28px;min-height:100vh;background:radial-gradient(900px 500px at 8% -10%,#193665 0%,transparent 62%),radial-gradient(700px 430px at 100% 10%,#18264c 0%,transparent 58%),#090e18}.top{display:flex;justify-content:space-between;gap:20px;align-items:center;padding:20px 22px;border:1px solid #26354d;border-radius:18px;background:linear-gradient(135deg,#111a2aee,#0f1725ee);box-shadow:0 18px 45px #0004}h1{font-size:26px;letter-spacing:-.6px;margin:0;color:#f5f8ff}h1:before{content:"";display:inline-block;width:10px;height:10px;border-radius:50%;background:#35d07f;box-shadow:0 0 0 5px #35d07f22;margin:0 10px 1px 1px}.top p{margin:7px 0 0}h2{margin:4px 0 14px;font-size:15px;text-transform:uppercase;letter-spacing:.09em;color:#aebbd0}a{color:#7eb6ff;text-decoration:none}a:hover{text-decoration:underline}.tabs{display:flex;gap:7px;margin:15px 0}.tabs button{background:#121d2d;border-color:#2c405c}.tabs button.active{background:linear-gradient(135deg,#2978d7,#455de2);border-color:#5e9df5}.panel{display:none;min-height:420px;max-height:calc(100vh - 205px);overflow:auto;padding:4px 4px 20px}.panel.active{display:block}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:14px}.card{position:relative;overflow:hidden;background:linear-gradient(145deg,#121c2c,#0e1624);border:1px solid #27364e;border-radius:15px;padding:17px;box-shadow:0 12px 25px #0002;transition:transform .16s,border-color .16s}.card:hover{transform:translateY(-2px);border-color:#456388}.title{font-weight:650;color:#c7d4e8;letter-spacing:.01em}.number{font-size:31px;font-weight:730;letter-spacing:-1px;color:#f3f7ff;margin:8px 0}.muted{color:#8393aa;font-size:12px}.bar{height:8px;background:#202d41;border-radius:9px;overflow:hidden;margin:12px 0}.bar>i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#4f9dff,#65d9ff);box-shadow:0 0 14px #3398ff88}.warning{border-color:#7b5c27;background:linear-gradient(145deg,#241d10,#16130e)}.bad{border-color:#743843;background:linear-gradient(145deg,#291419,#171014)}table{border-collapse:separate;border-spacing:0;width:100%;overflow:hidden;border:1px solid #27364e;border-radius:14px;background:#101927;box-shadow:0 12px 25px #0002}th,td{padding:11px 12px;text-align:left;border-bottom:1px solid #202d40;font-size:13px}th{background:#182438;color:#9eafc7;font-size:11px;text-transform:uppercase;letter-spacing:.06em}tr:last-child td{border-bottom:0}tbody tr:hover{background:#162239}.chart{display:flex;height:140px;align-items:flex-end;gap:4px;padding:14px;border:1px solid #27364e;border-radius:14px;background:linear-gradient(180deg,#101a29,#0d1521);box-shadow:inset 0 -1px #ffffff08}.tick{flex:1;min-width:4px;background:linear-gradient(#5fc6ff,#397ff5);border-radius:4px 4px 1px 1px;position:relative;opacity:.9;transition:opacity .15s,transform .15s}.tick:hover{opacity:1;transform:scaleX(1.7)}.tick:hover:after{content:attr(title);position:absolute;bottom:calc(100% + 8px);left:0;background:#07101d;color:#e9f2ff;border:1px solid #3a5070;border-radius:7px;padding:7px 9px;white-space:pre;z-index:2;font-size:11px;box-shadow:0 10px 22px #0008}button,select{color:#e8effb;padding:9px 12px;border:1px solid #38516f;border-radius:9px;background:#172338;font:inherit;outline:none}button{cursor:pointer;background:linear-gradient(135deg,#2978d7,#455de2);border-color:#5087e7;font-weight:650}button:hover{filter:brightness(1.12)}select:focus,button:focus{box-shadow:0 0 0 3px #478efa55}pre{white-space:pre-wrap;overflow:auto;color:#b9cae2;background:#0b121d;border:1px solid #27364e;padding:14px;border-radius:12px;font-size:12px}.right{display:flex;gap:8px;align-items:center}details{border:1px solid #27364e;border-radius:12px;background:#101927;padding:12px 14px}summary{cursor:pointer;color:#aebbd0}@media(max-width:600px){body{padding:18px 12px}.top{align-items:flex-start;flex-direction:column;padding:17px}h1{font-size:22px}.right{width:100%}.right select,.right button{flex:1}.tabs{overflow:auto;padding-bottom:3px}.tabs button{white-space:nowrap}.panel{max-height:none;min-height:0}th:nth-child(2),td:nth-child(2){display:none}table{display:block;overflow-x:auto}}
</style>
<div class=top><div><h1>Studizba · МГТУ analytics monitor</h1><p class=muted>Operational dashboard · <a href="docs">REST / OpenAPI</a> · MCP: <code>./mcp</code></p></div><div class=right><select id=hours><option value=6>6 часов</option><option value=24 selected>24 часа</option><option value=72>3 дня</option><option value=168>7 дней</option></select><button onclick="load()">Обновить</button></div></div>
<p id=updated class=muted>Загрузка…</p><nav class=tabs><button class=active data-tab=overview>Обзор</button><button data-tab=accounts>GLM keys</button><button data-tab=throughput>Скорость</button><button data-tab=operations>Операции</button><button data-tab=diagnostics>Диагностика</button></nav>
<main><section class="panel active" id=overview><h2>Общий прогресс</h2><div id=progress class=grid></div></section>
<section class=panel id=accounts><h2>GLM account pool</h2><div id=accounts class=grid></div></section>
<section class=panel id=throughput><h2>Скорость и динамика</h2><div id=chart class=chart title="Наведите на столбец для деталей"></div><p class=muted>Столбец = завершённые карточки преподавателей + анализы отзывов за интервал. Красный — ошибка crawler.</p></section>
<section class=panel id=operations><h2>Очередь и здоровье crawler</h2><div id=health class=grid></div><h2>Последние crawl runs</h2><table><thead><tr><th>ID</th><th>Режим</th><th>Статус</th><th>Старт</th><th>Завершение / ошибка</th></tr></thead><tbody id=runs></tbody></table></section>
<section class=panel id=diagnostics><h2>Диагностика</h2><details open><summary>Полный JSON progress API</summary><pre id=json></pre></details></section></main>
<script>
const esc=s=>String(s??'—').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const pct=(n,d)=>d?Math.round(n/d*10000)/100:0;
const eta=x=>x?.eta_human?`ETA ≈ ${x.eta_human}`:'ETA появится после измеримой скорости';
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')});
function card(title,done,total,remaining,info,klass=''){const p=pct(done,total);return `<section class="card ${klass}"><div class=title>${esc(title)}</div><div class=number>${esc(done)} <span class=muted>/ ${esc(total)}</span></div><div class=bar><i style="width:${Math.min(100,p)}%"></i></div><div>${p}% · осталось <b>${esc(remaining)}</b></div><div class=muted>${esc(info)}</div></section>`}
function render(x){
 updated.textContent=`Обновлено: ${new Date(x.generated_at).toLocaleString('ru-RU')} · окно скорости: ${x.window.hours} ч · автообновление 30 с`;
 const b=x.bootstrap,g=x.glm_analysis;
 progress.innerHTML=card('Bootstrap: карточки преподавателей',b.completed,b.total,b.remaining,`${eta(b.eta)} · ${b.eta.rate_per_hour}/ч за ${b.eta.window_minutes} мин`)+card('GLM: анализ отзывов',g.completed,g.total,g.remaining,`${eta(g.eta)} · ${g.eta.rate_per_hour}/ч за ${g.eta.window_minutes} мин`)+`<section class="card ${g.queue.failed?'warning':''}"><div class=title>GLM queue</div><div class=number>${g.queue.pending}</div><div>pending · ${g.queue.running} running · ${g.queue.failed} failed</div><div class=muted>Полный анализ: ${x.coverage.review_analysis_percent}%</div></section>`;
 accounts.innerHTML=(x.glm_account_pool||[]).map(a=>`<section class="card ${(!a.enabled||!a.healthy)?'warning':''}"><div class=title>${esc(a.label)} <span class=muted>· ${a.enabled&&a.healthy?'healthy':'cooldown / unavailable'}</span></div><div class=number>${a.processed_count}</div><div>обработано · ${a.active_requests} active · ${a.reviews_per_hour}/ч</div><div class=muted>${esc(a.model)} · route: ${esc(a.proxy_label??'direct')} · avg ${a.average_latency_ms??'—'} ms</div><div class=muted>429: ${a.http_429_count} · network: ${a.network_error_count}</div><div class=muted>${a.cooldown_until?'cooldown до '+new Date(a.cooldown_until).toLocaleTimeString('ru-RU'):a.last_error?esc(a.last_error):'последний успех: '+(a.last_success_at?new Date(a.last_success_at).toLocaleTimeString('ru-RU'):'—')}</div></section>`).join('')||'<section class="card"><div class=muted>Account pool ещё инициализируется.</div></section>';
 const max=Math.max(1,...x.timeline.map(z=>z.teacher_details+z.analyses));
 chart.innerHTML=x.timeline.length?x.timeline.map(z=>{let h=Math.max(3,(z.teacher_details+z.analyses)/max*100);let t=`${new Date(z.at).toLocaleString('ru-RU')}\\nКарточки: ${z.teacher_details}\\nGLM: ${z.analyses}\\nНовые отзывы: ${z.new_reviews}\\nОшибки: ${z.errors}`;return `<i class=tick style="height:${h}%;${z.errors?'background:#d84a4a':''}" title="${esc(t)}"></i>`}).join(''):'<span class=muted>За выбранное окно событий ещё нет.</span>';
 health.innerHTML=`<section class=card><div class=title>Crawl targets</div><div class=number>${x.crawl.due}</div><div>страниц пора проверить · ${x.crawl.targets_with_errors} с ошибками</div></section>`+x.crawl.target_health.map(t=>`<section class="card ${t.with_errors?'warning':''}"><div class=title>${esc(t.entity_type)}</div><div>${t.count} targets · due ${t.due}</div><div class=muted>ошибок: ${t.with_errors}; max attempts: ${t.max_error_count}</div></section>`).join('');
 runs.innerHTML=x.runs.map(r=>`<tr><td>${r.id}</td><td>${esc(r.mode)}</td><td>${esc(r.status)}</td><td>${new Date(r.started_at).toLocaleString('ru-RU')}</td><td>${r.finished_at?new Date(r.finished_at).toLocaleString('ru-RU'):esc(r.error||'выполняется')}</td></tr>`).join('');
 json.textContent=JSON.stringify(x,null,2);
 }
async function load(){try{const h=hours.value;const r=await fetch(`api/progress?hours=${h}&bucket_minutes=15`);if(!r.ok)throw Error(await r.text());render(await r.json())}catch(e){updated.textContent='Ошибка получения progress: '+e}}
load();setInterval(load,30000);
</script></html>"""

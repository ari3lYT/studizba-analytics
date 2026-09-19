# Studizba Analytics

A self-hosted historical collector, analytics engine, dashboard, REST API, and MCP server for public Studizba teacher reviews at BMSTU.

The project preserves source snapshots, tracks revisions instead of overwriting them, calculates confidence-aware teacher and department metrics, and exposes every score together with its evidence and formula metadata.

## What it includes

- adaptive asynchronous collection of departments, teacher cards, and paginated reviews;
- gzip source snapshots identified by SHA-256;
- PostgreSQL history for teacher snapshots, review versions, crawl events, and schedules;
- Russian full-text and trigram search with domain synonym expansion;
- optional OpenAI-compatible structured review analysis;
- evidence validation that rejects model quotes absent from the original review;
- confidence weighting, time decay, Bayesian shrinkage, polarization, and department coverage metrics;
- FastAPI dashboard and OpenAPI surface;
- stdio and Streamable HTTP MCP servers;
- explicit refresh operations and observable background progress.

## Architecture

```text
Studizba HTML/AJAX
        │
        ▼
async collector ── gzip snapshots ── change journal / adaptive schedule
        │
        ▼
PostgreSQL + pg_trgm + Russian FTS
        │
        ├── optional LLM adapter → validated structured evidence
        ├── statistical aggregation → teacher/department metrics
        ├── FastAPI dashboard and REST API
        └── MCP server
```

## Quick start

Requirements: Python 3.10+, PostgreSQL 16+, and Docker Compose if you want the bundled database service.

```bash
git clone https://github.com/ari3lYT/studizba-analytics.git
cd studizba-analytics
cp credentials.example.json credentials.local.json
chmod 600 credentials.local.json
docker compose up -d postgres
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/studizba init-db
```

Credentials can also be supplied through `STUDIZBA_USERNAME`, `STUDIZBA_PASSWORD`, `GLM_API_KEY`, `GLM_MODEL`, `GLM_BASE_URL`, and `DATABASE_URL`.

Start with a small validation run:

```bash
.venv/bin/studizba bootstrap --details-limit 10
.venv/bin/studizba status
```

Then run a resumable full collection and aggregation:

```bash
.venv/bin/studizba bootstrap
.venv/bin/studizba analyze --limit 200 --concurrency 3
.venv/bin/studizba aggregate
```

The collector backs off after failures and gradually reduces the refresh frequency of unchanged pages. Raw snapshots allow parser and analytics changes to be replayed without requesting the source again.

## Services

```bash
.venv/bin/studizba serve-dashboard --host 127.0.0.1 --port 8898
STUDIZBA_ROOT="$PWD" .venv/bin/studizba-mcp
STUDIZBA_ROOT="$PWD" .venv/bin/studizba-mcp-http
```

Example systemd and nginx files live in `deploy/`. Review paths, users, ports, and access controls before installing them.

## MCP capabilities

The MCP surface includes teacher and department search, review search, score explanations, rankings, comparisons, history, recent changes, monitoring status, and explicit refresh operations.

Scores are not returned as unexplained numbers: `explain_score` includes the formula version, sample size, coverage, confidence, dates, source URLs, and validated evidence spans.

## Tests

```bash
.venv/bin/pytest -q -m 'not live'
```

Committed fixtures are small synthetic pages. The repository intentionally contains no copied production reviews, session cookies, account credentials, database dumps, or crawl state. Live tests are isolated behind the `live` marker.

## Responsible use

- Respect the source site's terms, robots policy, and operational capacity.
- Keep concurrency low and preserve adaptive delays.
- Treat reviews as subjective reports, not facts about a person.
- Do not expose credentials, raw session state, or private data through a public deployment.
- Use authentication and rate limits before enabling refresh or analysis endpoints for untrusted users.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

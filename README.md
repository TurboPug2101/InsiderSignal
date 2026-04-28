# Smart Money Tracker

Tracks Indian market signals (insider trades, SAST disclosures, bulk/block deals, FII/DII flows, MF holdings) and surfaces them through a dashboard with cluster scoring and fundamentals.

## Architecture

Three pieces sharing a Turso (libSQL) database:

- **Scraper CLI** (`run.py`) — runs locally, fetches from NSE.
- **Dashboard** (`api.py`) — FastAPI app serving the UI + read APIs.
- **Worker** (`worker/main.py`) — FastAPI app that recomputes streaks, clusters, fundamentals.

See `microservice.md` and `technical_architecture.md` for details.

## Setup

```bash
pip install -r requirements.txt
```

Set environment variables (in a `.env` or your shell):

```
TURSO_DATABASE_URL=...
TURSO_AUTH_TOKEN=...
WORKER_URL=https://smart-money-worker.onrender.com   # for dashboard
WORKER_SECRET=...                                    # shared between dashboard + worker
SENTRY_DSN=...                                       # optional
```

## Running

**Scrape data (local — NSE blocks cloud IPs):**
```bash
python run.py full                  # scrape everything + recompute
python run.py all                   # scrapers only
python run.py insider sast          # specific scrapers
python run.py full --fresh          # truncate tables first
```

**Dashboard:**
```bash
python api.py        # http://localhost:8000
```

**Worker:**
```bash
pip install -r worker/requirements.txt
uvicorn worker.main:app --port 8001
```

Trigger a recompute via the dashboard's "Run Analysis" button, or directly:
```bash
curl -X POST http://localhost:8001/recompute -H "X-Worker-Secret: $WORKER_SECRET"
```

## Tests

```bash
pytest
```

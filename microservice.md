# Microservice Split — Smart Money Tracker

## What Changed and Why

Before this, the entire application was one single Python process doing everything — serving the dashboard, answering API requests, running scrapers, and computing signals. This is called a monolith. It works fine when you're building something, but as the project grows it becomes harder to manage: one crash takes down everything, one slow operation blocks everything else, and you can't update one part without redeploying the whole thing.

The goal of this change was to split that one process into two independent services, each with a clear single responsibility.

---

## Before vs After

**Before — one service doing everything:**

```
Render (single deployment)
└── api.py
    ├── Serves the dashboard UI
    ├── Answers all /api/* requests
    ├── Runs scrapers (NSE data fetching)
    ├── Computes clusters, streaks, fundamentals
    └── Streams progress updates to the browser
```

**After — two services with clear responsibilities:**

```
Your Laptop
└── run.py  →  Fetches data from NSE, writes to Turso

Render — Service 1: Dashboard
└── api.py  →  Serves UI + reads Turso. Nothing else.

Render — Service 2: Worker
└── worker/main.py  →  Computes clusters, streaks, fundamentals. Nothing else.

Turso (shared database)
└── Both services read/write here independently
```

The scraper stays on your laptop because NSE blocks requests from non-Indian cloud servers. The dashboard just reads data. The worker does the heavy computation.

---

## New Files

### `worker/main.py` — The Worker Service
This is a small FastAPI application with three routes:

- **POST /recompute** — runs streak detection, cluster scoring, and fundamentals enrichment in sequence. This is the only thing the worker does.
- **GET /health** — returns "ok" immediately. Used by Render to check if the process is alive.
- **GET /ready** — checks if the database connection is working before accepting requests. If the DB is unreachable, this returns an error so Render knows not to send traffic here yet.

### `worker/tasks.py` — The Computation Logic
All the cluster, streak, and fundamentals code that used to live scattered across `api.py` now lives here. Each step is a separate function that runs its job and returns how many rows it wrote and how long it took.

### `worker/logging_config.py` — Structured Logging
Previously, log lines looked like plain text: `"Computed 149 clusters"`. Now every log line is a JSON object that includes the service name, timestamp, log level, and any relevant details like row counts and timing. This makes it much easier to search and filter logs in production tools.

Example of what a log line looks like now:
```json
{"event": "clusters_done", "count": 149, "elapsed_ms": 4200, "service": "worker", "level": "info", "timestamp": "2026-04-23T04:00:00Z"}
```

### `worker/Dockerfile` and `Dockerfile`
Each service now has a Dockerfile. This packages the service into a container — a self-contained unit that runs identically on any machine, whether your laptop or a cloud server. No more "works on my machine" problems.

### `worker/requirements.txt`
The worker only installs the Python packages it actually needs — not the full list. This keeps the container smaller and the build faster.

---

## Changed Files

### `api.py` — Stripped Down
The dashboard service no longer runs any computation. The "Run Analysis" endpoint used to directly execute scrapers and stream progress updates. Now it simply forwards the request to the worker and returns whatever the worker responds with. About 100 lines of scraper orchestration code was removed.

### `render.yaml` — Two Services
Previously defined one Render service. Now defines two:
- `smart-money-tracker` — the dashboard, same as before
- `smart-money-worker` — the new worker service

Render will deploy both from the same GitHub repository but as completely independent services with their own URLs, restarts, and scaling.

### `dashboard/index.html` — Run Analysis Button
The progress modal used to receive a live stream of updates as each scraper ran. Since scrapers no longer run in the cloud (they run locally), the Run Analysis button now triggers only the computation steps. The modal waits for the worker to finish and then shows results for the three steps it ran: streaks, clusters, fundamentals.

### `run.py` — Full Pipeline Command
Added `python run.py full` which runs everything in sequence: all scrapers, then streaks, then clusters, then fundamentals. Also added `python run.py full --fresh` which clears all tables first before running everything, useful when you want a clean slate.

---

## Security — Service-to-Service Authentication

The worker's `/recompute` endpoint should not be callable by anyone on the internet. A shared secret is used: the dashboard sends a header `X-Worker-Secret` with every request to the worker, and the worker rejects anything that doesn't match. Both services have the same secret set as an environment variable.

---

## Error Tracking — Sentry

Sentry is wired into the worker. When an unhandled exception happens in production, Sentry captures it and can send an alert. This replaces the current situation where you'd only find out something broke by checking logs manually. It's opt-in — only activates if you set the `SENTRY_DSN` environment variable.

---

## Environment Variables Added

| Variable | Where it's used | What it is |
|----------|----------------|------------|
| `WORKER_URL` | Dashboard | URL of the worker service on Render |
| `WORKER_SECRET` | Both | Shared secret for service-to-service auth |
| `SENTRY_DSN` | Worker | Paste from sentry.io to enable error tracking |

---

## To Deploy the Worker on Render

1. Push the code (already done)
2. In the Render dashboard, the `smart-money-worker` service should appear automatically from `render.yaml`
3. Set `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, and `WORKER_SECRET` on the worker service
4. Copy the worker's Render URL and set it as `WORKER_URL` on the dashboard service
5. Set the same `WORKER_SECRET` on the dashboard service

After that, clicking "Run Analysis" in the dashboard will trigger the worker on Render, and `python run.py all` on your laptop will continue to fetch fresh data from NSE.

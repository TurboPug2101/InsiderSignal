"""
Worker Service — Smart Money Tracker
Handles computation only: streaks, clusters, fundamentals.
Never scrapes NSE. Never serves the dashboard.
"""

import os
import sys
import time

# Add project root to path so shared modules (db, config, scrapers) are importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

from worker.logging_config import configure_logging
configure_logging(service_name="worker")

import structlog
log = structlog.get_logger()

# --- Sentry (error tracking) ---
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.2)
    log.info("sentry_enabled")

# --- Service-to-service auth ---
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")


def verify_secret(x_worker_secret: str = Header(default="")):
    """Reject requests that don't carry the shared secret header."""
    if WORKER_SECRET and x_worker_secret != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="Invalid worker secret")


# --- App lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    from db import init_db
    init_db()
    log.info("worker_ready")
    yield
    log.info("worker_shutdown")


app = FastAPI(title="Smart Money Worker", version="1.0.0", lifespan=lifespan)


# ---------- Health / Readiness ----------

@app.get("/health")
def health():
    """Render and load balancers call this to check if the process is alive."""
    return {"status": "ok", "service": "worker"}


@app.get("/ready")
def ready():
    """
    Deeper check — verifies the DB is reachable before accepting work.
    Render uses this to decide whether to send traffic to this instance.
    """
    try:
        from db import query
        query("SELECT 1")
        return {"status": "ready", "db": "ok"}
    except Exception as e:
        log.error("readiness_check_failed", error=str(e))
        return JSONResponse(status_code=503, content={"status": "not_ready", "db": str(e)})


# ---------- Recompute ----------

@app.post("/recompute", dependencies=[Depends(verify_secret)])
def recompute():
    """
    Run the full computation pipeline: streaks → clusters → fundamentals.
    Protected by X-Worker-Secret header.
    Called by the Render cron job and optionally by the dashboard's Run Analysis button.
    """
    log.info("recompute_triggered")
    from worker.tasks import run_recompute
    result = run_recompute()
    status_code = 200 if result["status"] == "done" else 207
    return JSONResponse(status_code=status_code, content=result)


# ---------- Entry point ----------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("worker.main:app", host="0.0.0.0", port=port, reload=False)

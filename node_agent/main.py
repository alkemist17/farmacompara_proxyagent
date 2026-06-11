"""
FarmaCompara Node Agent

Runs on each proxy node. Responsibilities:
  1. Expose /health endpoint → manager pulls health reports (active check)
  2. Expose /metrics endpoint → manager or Prometheus can scrape system state
  3. Push metrics to manager every PUSH_INTERVAL seconds (heartbeat)
  4. Execute proxy jobs (Phase 5)

Environment variables required:
  NODE_AGENT_API_KEY  — shared secret, must match manager's NODE_AGENT_API_KEY
  NODE_ID             — UUID assigned by manager at registration
  MANAGER_URL         — e.g. http://proxy-manager:8000
  NODE_JWT            — JWT returned by manager at registration (for /metrics push)
  PUSH_INTERVAL       — seconds between metric pushes (default: 15)
"""
import asyncio
import os
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request, Security, Query
from fastapi.security import APIKeyHeader

from node_agent.checks import run_all_checks, system_metrics
from node_agent.executor import execute_and_report
from node_agent.models import NodeHealthReport, SystemMetrics, JobExecuteRequest
from node_agent.security import verify_hmac_signature, seen_request_ids

logger = structlog.get_logger()

_NODE_AGENT_API_KEY = os.getenv("NODE_AGENT_API_KEY", "")
_NODE_ID            = os.getenv("NODE_ID", "unknown")
_MANAGER_URL        = os.getenv("MANAGER_URL", "http://proxy-manager:8000")
_NODE_JWT           = os.getenv("NODE_JWT", "")
_PUSH_INTERVAL      = int(os.getenv("PUSH_INTERVAL", "15"))

_api_key_header = APIKeyHeader(name="X-Agent-Key", auto_error=False)


def _require_key(key: str = Security(_api_key_header)) -> None:
    if not _NODE_AGENT_API_KEY or key != _NODE_AGENT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid agent key")


# ── Metric push background task ───────────────────────────────────────────────

async def _push_metrics_loop() -> None:
    """Push system metrics to manager on a fixed interval (heartbeat)."""
    await asyncio.sleep(5)  # short initial delay for manager to be ready
    while True:
        try:
            metrics = await system_metrics()
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{_MANAGER_URL}/nodes/{_NODE_ID}/metrics",
                    json=metrics.model_dump(),
                    params={"token": _NODE_JWT},
                )
            logger.debug("metrics_pushed", node_id=_NODE_ID)
        except Exception as e:
            logger.warning("metrics_push_failed", error=str(e))
        await asyncio.sleep(_PUSH_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _NODE_ID != "unknown" and _NODE_JWT:
        asyncio.create_task(_push_metrics_loop())
        logger.info("agent_started", node_id=_NODE_ID, manager=_MANAGER_URL)
    else:
        logger.warning("agent_started_without_registration", msg="Set NODE_ID and NODE_JWT")
    yield


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="FarmaCompara Node Agent",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/ping")
async def ping(_: None = Security(_require_key)) -> dict:
    return {"alive": True, "node_id": _NODE_ID}


@app.get("/health", response_model=NodeHealthReport)
async def health(
    probe: list[str] = Query(default=[], description="Domains to probe (bare, no https://)"),
    _:     None      = Security(_require_key),
) -> NodeHealthReport:
    """
    Run full health check suite from this node's network.
    The manager calls this to assess if the node can reach target domains.
    """
    return await run_all_checks(node_id=_NODE_ID, probe_domains=probe)


@app.get("/metrics", response_model=SystemMetrics)
async def metrics(_: None = Security(_require_key)) -> SystemMetrics:
    """System metrics snapshot (CPU, RAM, connections, latency P95)."""
    return await system_metrics()


@app.post("/execute", status_code=202)
async def execute(
    request: Request,
    req:     JobExecuteRequest,
    _:       None = Security(_require_key),
) -> dict:
    """
    Receive a job from the dispatcher, verify its HMAC signature, deduplicate
    via X-Request-Id, then execute asynchronously. Returns 202 immediately.
    """
    signature  = request.headers.get("X-Signature", "")
    timestamp  = request.headers.get("X-Timestamp", "")
    request_id = request.headers.get("X-Request-Id", req.job_id)

    body_bytes = await request.body()
    url        = str(request.url)

    if not verify_hmac_signature("POST", url, body_bytes, signature, timestamp):
        raise HTTPException(status_code=401, detail="Invalid or expired request signature")

    if seen_request_ids.is_seen(request_id):
        return {"job_id": req.job_id, "accepted": False, "reason": "duplicate"}
    seen_request_ids.mark(request_id)

    asyncio.create_task(execute_and_report(_NODE_ID, _NODE_JWT, req))
    return {"job_id": req.job_id, "accepted": True}

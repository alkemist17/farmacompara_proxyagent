"""
Job executor — runs the actual HTTP request from the node's network
and reports the result back to the manager.

Uses curl_cffi with TLS browser impersonation (same as health checks)
to evade Cloudflare and basic bot detection.

L1 cache integration:
  - GET requests are checked against the in-memory L1 cache first.
  - Successful (200) GET responses are stored in L1 after execution.
"""
import asyncio
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import structlog

from node_agent.cache import l1
from node_agent.detection import detect, to_detail_string
from node_agent.models import JobExecuteRequest

logger    = structlog.get_logger()
_executor = ThreadPoolExecutor(max_workers=16)

_IMPERSONATE     = ["firefox133", "firefox135", "firefox144", "safari184"]
_REQUEST_TIMEOUT = 30  # seconds per proxied request
_MAX_BODY_SIZE   = int(os.getenv("NODE_MAX_BODY_SIZE", "512000"))

_NO_CACHE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _sync_execute(req: JobExecuteRequest) -> dict:
    """
    Execute the request using curl_cffi in a thread (it's synchronous).
    Returns a result dict compatible with JobResultSubmit.
    """
    from curl_cffi.requests import Session

    profile = random.choice(_IMPERSONATE)
    start   = time.monotonic()
    try:
        with Session() as sess:
            resp = getattr(sess, req.method.lower())(
                req.target_url,
                headers=req.headers or {},
                params=req.params or {},
                json=req.body if req.method.upper() in ("POST", "PUT", "PATCH") else None,
                impersonate=profile,
                timeout=_REQUEST_TIMEOUT,
                allow_redirects=True,
                verify=False,
            )
        latency_ms = (time.monotonic() - start) * 1000
        body       = resp.text[:_MAX_BODY_SIZE]

        d = detect(resp.status_code, body, dict(resp.headers))

        json_body = None
        try:
            json_body = resp.json()
        except Exception:
            pass

        return {
            "status_code":         resp.status_code,
            "body":                body,
            "response_headers":    dict(resp.headers),
            "json_body":           json_body,
            "latency_ms":          round(latency_ms, 2),
            "captcha_detected":    d.captcha,
            "cloudflare_detected": d.cloudflare_challenge,
            "blocked_detected":    d.geo_blocked or resp.status_code == 403,
            "error":               None,
        }
    except Exception as e:
        latency_ms = (time.monotonic() - start) * 1000
        return {
            "status_code":         0,
            "body":                "",
            "response_headers":    {},
            "json_body":           None,
            "latency_ms":          round(latency_ms, 2),
            "captcha_detected":    False,
            "cloudflare_detected": False,
            "blocked_detected":    False,
            "error":               str(e),
        }


async def execute_and_report(node_id: str, node_jwt: str, req: JobExecuteRequest) -> None:
    """
    Execute the job in the thread pool and POST the result back to the manager.
    Checks L1 cache first for GET requests; writes to L1 on GET 200.
    Errors during callback are logged but not re-raised.
    """
    # ── L1 cache check ────────────────────────────────────────────────────────
    is_get     = req.method.upper() == "GET"
    cached_l1  = l1.get(req.target_url, req.params) if is_get else None
    if cached_l1:
        result    = cached_l1
        from_l1   = True
        logger.debug("cache_hit_l1", job_id=req.job_id, url=req.target_url)
    else:
        from_l1   = False
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(_executor, _sync_execute, req)

        # Write to L1 on successful GET
        if is_get and result["status_code"] == 200 and not result["error"]:
            l1.set(req.target_url, req.params, result)

    callback_url = f"{req.manager_url}/jobs/{req.job_id}/result"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                callback_url,
                json=result,
                params={"token": node_jwt},
            )
            resp.raise_for_status()
        logger.info(
            "job_result_reported",
            job_id=req.job_id,
            status_code=result["status_code"],
            error=result["error"],
            from_l1=from_l1,
        )
    except Exception as e:
        logger.error(
            "job_result_callback_failed",
            job_id=req.job_id,
            callback_url=callback_url,
            error=str(e),
        )

"""
Health check implementations — all run from the node's network perspective.

- check_internet : HTTP GET to Cloudflare's 1.1.1.1 (plain httpx, no browser sim needed)
- check_dns      : DNS resolution via asyncio (detects DNS filtering)
- check_target   : HTTP GET with curl_cffi TLS impersonation (detects CF/CAPTCHA/geo-block)
- system_metrics : CPU, RAM, active connections via psutil
"""
import asyncio
import os
import random
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import httpx
import psutil
from curl_cffi.requests import Session

from node_agent.detection import detect, to_detail_string
from node_agent.models import CheckResult, NodeHealthReport, SystemMetrics

_IMPERSONATE = ["firefox133", "firefox135", "firefox144", "safari184"]
_executor    = ThreadPoolExecutor(max_workers=8)

# Probe result cache: url → (monotonic_timestamp, CheckResult)
# Avoids hitting pharmacy sites on every /health call — TTL is configurable.
_probe_cache: dict[str, tuple[float, CheckResult]] = {}
_PROBE_CACHE_TTL = int(os.getenv("PROBE_CACHE_TTL_SECONDS", "300"))

# Sliding window of recent latencies (for P95 calculation)
_recent_latencies: list[float] = []
_MAX_LATENCY_SAMPLES = 100


def _record_latency(ms: float) -> None:
    _recent_latencies.append(ms)
    if len(_recent_latencies) > _MAX_LATENCY_SAMPLES:
        _recent_latencies.pop(0)


def _latency_p95() -> Optional[float]:
    if len(_recent_latencies) < 5:
        return None
    sorted_lat = sorted(_recent_latencies)
    idx = int(len(sorted_lat) * 0.95)
    return sorted_lat[min(idx, len(sorted_lat) - 1)]


# ── Individual checks ─────────────────────────────────────────────────────────

async def check_internet(timeout: float = 5.0) -> CheckResult:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get("https://1.1.1.1", follow_redirects=True)
        latency = (time.monotonic() - start) * 1000
        passed = resp.status_code < 500
        return CheckResult(name="internet", passed=passed, latency_ms=round(latency, 2))
    except Exception as e:
        return CheckResult(name="internet", passed=False, detail=str(e))


async def check_dns(hostname: str = "google.com", timeout: float = 3.0) -> CheckResult:
    start = time.monotonic()
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.getaddrinfo(hostname, None, family=socket.AF_INET),
            timeout=timeout,
        )
        latency = (time.monotonic() - start) * 1000
        return CheckResult(name="dns", passed=True, latency_ms=round(latency, 2))
    except asyncio.TimeoutError:
        return CheckResult(name="dns", passed=False, detail="dns_timeout")
    except Exception as e:
        return CheckResult(name="dns", passed=False, detail=str(e))


def _sync_check_target(url: str, timeout: int = 15) -> dict:
    """Synchronous curl_cffi request — runs in thread pool."""
    profile = random.choice(_IMPERSONATE)
    start   = time.monotonic()
    try:
        with Session() as sess:
            resp = sess.get(
                url,
                impersonate=profile,
                timeout=timeout,
                allow_redirects=True,
                verify=False,
            )
        latency = (time.monotonic() - start) * 1000
        body    = resp.text[:8000]  # first 8KB is enough for detection
        return {
            "ok":          True,
            "status_code": resp.status_code,
            "body":        body,
            "headers":     dict(resp.headers),
            "latency_ms":  round(latency, 2),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "latency_ms": (time.monotonic() - start) * 1000}


async def check_target(url: str, timeout: int = 15) -> CheckResult:
    """Check target URL reachability using curl_cffi TLS impersonation.

    Results are cached for PROBE_CACHE_TTL_SECONDS to avoid hammering pharmacy
    sites on every /health call from the manager.
    """
    now    = time.monotonic()
    cached = _probe_cache.get(url)
    if cached and (now - cached[0]) < _PROBE_CACHE_TTL:
        return cached[1]

    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(_executor, _sync_check_target, url, timeout)

    if not result["ok"]:
        check = CheckResult(name=f"target:{url}", passed=False, detail=result.get("error"))
        _probe_cache[url] = (time.monotonic(), check)
        return check

    d          = detect(result["status_code"], result["body"], result["headers"])
    latency_ms = result["latency_ms"]
    _record_latency(latency_ms)

    # Success = 2xx/3xx and no CAPTCHA/CF challenge/geo-block
    blocked = d.captcha or d.cloudflare_challenge or d.geo_blocked
    passed  = result["status_code"] < 400 and not blocked

    detail = to_detail_string(d) or (
        f"http_{result['status_code']}" if result["status_code"] >= 400 else None
    )

    check = CheckResult(
        name=f"target:{url}",
        passed=passed,
        latency_ms=round(latency_ms, 2),
        detail=detail,
    )
    _probe_cache[url] = (time.monotonic(), check)
    return check


async def system_metrics() -> SystemMetrics:
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent
    net = psutil.net_connections(kind="tcp")
    active = len([c for c in net if c.status == "ESTABLISHED"])
    return SystemMetrics(
        cpu_usage=round(cpu, 1),
        ram_usage=round(ram, 1),
        active_requests=active,
        latency_p95_ms=_latency_p95(),
    )


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def run_all_checks(node_id: str, probe_domains: list[str]) -> NodeHealthReport:
    """
    Run all health checks concurrently and return a NodeHealthReport.
    probe_domains: list of bare domains (e.g. "farmatodo.com.co")
    """
    probe_urls = [f"https://{d}" for d in probe_domains]

    # Run connectivity checks concurrently
    internet_task = asyncio.create_task(check_internet())
    dns_task      = asyncio.create_task(check_dns())
    target_tasks  = [asyncio.create_task(check_target(url)) for url in probe_urls]

    internet = await internet_task
    dns      = await dns_task
    targets  = await asyncio.gather(*target_tasks, return_exceptions=True)

    checks: list[CheckResult] = [internet, dns]
    for t in targets:
        if isinstance(t, Exception):
            checks.append(CheckResult(name="target", passed=False, detail=str(t)))
        else:
            checks.append(t)

    # Overall passes if internet + DNS work AND at least one target is reachable
    critical_ok = internet.passed and dns.passed
    targets_ok  = any(c.passed for c in checks if c.name.startswith("target:"))
    overall     = critical_ok and (not probe_domains or targets_ok)

    return NodeHealthReport(
        node_id=node_id,
        timestamp=datetime.utcnow(),
        checks=checks,
        overall=overall,
    )

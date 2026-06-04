"""
Minimal local models for the node agent.
Must stay structurally compatible with app/models/health.py in the manager.
"""
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


class CheckResult(BaseModel):
    name:       str
    passed:     bool
    latency_ms: Optional[float] = None
    detail:     Optional[str]   = None


class NodeHealthReport(BaseModel):
    node_id:   str
    timestamp: datetime
    checks:    list[CheckResult]
    overall:   bool


class SystemMetrics(BaseModel):
    cpu_usage:       float
    ram_usage:       float
    active_requests: int
    latency_p95_ms:  Optional[float] = None


class JobExecuteRequest(BaseModel):
    """Payload sent by the dispatcher to POST /execute on the node agent."""
    job_id:      str
    target_url:  str
    method:      str = Field(default="GET")
    headers:     dict[str, str]       = Field(default_factory=dict)
    body:        Optional[Any]        = None
    params:      dict[str, str]       = Field(default_factory=dict)
    manager_url: str                  = "http://proxy-manager:8000"

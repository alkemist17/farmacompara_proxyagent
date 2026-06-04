"""
Node-agent security helpers.

- verify_hmac_signature  : validate X-Signature / X-Timestamp from the manager
- SeenRequestIds         : bounded in-memory idempotency store
  Rejects X-Request-Id values already seen within the HMAC timestamp window.
  Since the manager signs requests with a ±30s window, keeping IDs for 90s
  is more than enough to block replays.
"""
import hashlib
import hmac
import os
import time
from collections import OrderedDict


_HMAC_SECRET   = os.getenv("HMAC_SECRET", "hmac-secret-change-me-min-32-chars")
_TS_TOLERANCE  = int(os.getenv("HMAC_TIMESTAMP_TOLERANCE_SECONDS", "30"))
_ID_TTL        = _TS_TOLERANCE * 3      # keep IDs for 3× the tolerance window
_MAX_IDS       = 10_000                 # prevent unbounded growth


# ── Signature verification ────────────────────────────────────────────────────

def verify_hmac_signature(
    method:    str,
    url:       str,
    body_bytes: bytes,
    signature: str,
    timestamp: str,
) -> bool:
    """
    Return True if the HMAC-SHA256 signature is valid and not too old/future.
    Uses constant-time comparison to prevent timing attacks.
    """
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False

    if abs(time.time() - ts) > _TS_TOLERANCE:
        return False

    body_hash = hashlib.sha256(body_bytes).hexdigest()
    message   = f"{method.upper()}{url}{timestamp}{body_hash}"
    expected  = hmac.new(
        _HMAC_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Request idempotency ───────────────────────────────────────────────────────

class _SeenRequestIds:
    """
    Thread-safe (GIL) bounded LRU store of seen request IDs with TTL eviction.
    Entries older than _ID_TTL seconds are pruned on each insertion.
    """

    def __init__(self, ttl: int = _ID_TTL, max_size: int = _MAX_IDS) -> None:
        self._store: OrderedDict[str, float] = OrderedDict()
        self._ttl    = ttl
        self._max    = max_size

    def is_seen(self, request_id: str) -> bool:
        """Return True if this ID was already accepted and is still within TTL."""
        self._evict()
        return request_id in self._store

    def mark(self, request_id: str) -> None:
        """Record an accepted request ID."""
        self._evict()
        if len(self._store) >= self._max:
            self._store.popitem(last=False)  # evict oldest
        self._store[request_id] = time.monotonic()

    def _evict(self) -> None:
        now   = time.monotonic()
        stale = [k for k, ts in self._store.items() if now - ts > self._ttl]
        for k in stale:
            del self._store[k]


seen_request_ids = _SeenRequestIds()

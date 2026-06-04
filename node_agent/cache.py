"""
L1 in-memory cache — per node-agent process.

Absorbs identical burst requests on the same node before they hit the network.
TTL is intentionally short (60s default) — just enough to protect against
duplicated dispatcher assignments during spikes.

Not shared across nodes — that's L2's job (Redis DB3 on the manager).
"""
import hashlib
import time
from collections import OrderedDict
from typing import Any, Optional
from urllib.parse import parse_qsl, urlparse

_DEFAULT_TTL = 60       # seconds
_MAX_ENTRIES = 5_000    # prevent unbounded growth per node


def _cache_key(url: str, params: dict[str, str]) -> str:
    parsed     = urlparse(url)
    all_params = {**dict(parse_qsl(parsed.query)), **params}
    sorted_qs  = "&".join(f"{k}={v}" for k, v in sorted(all_params.items()))
    base       = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}"
    full       = f"{base}?{sorted_qs}" if sorted_qs else base
    return hashlib.sha256(full.encode()).hexdigest()


class L1Cache:
    """
    Thread-safe (GIL) LRU cache with per-entry TTL.
    Each entry is (value, expiry_monotonic).
    """

    def __init__(self, ttl: int = _DEFAULT_TTL, max_size: int = _MAX_ENTRIES) -> None:
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._ttl     = ttl
        self._max     = max_size

    def get(self, url: str, params: dict[str, str]) -> Optional[dict]:
        key  = _cache_key(url, params)
        item = self._store.get(key)
        if item is None:
            return None
        value, expiry = item
        if time.monotonic() > expiry:
            del self._store[key]
            return None
        # Move to end (LRU)
        self._store.move_to_end(key)
        return value

    def set(self, url: str, params: dict[str, str], value: dict) -> None:
        key    = _cache_key(url, params)
        expiry = time.monotonic() + self._ttl
        if len(self._store) >= self._max:
            self._store.popitem(last=False)  # evict oldest
        self._store[key] = (value, expiry)

    def invalidate(self, url: str, params: dict[str, str]) -> bool:
        key = _cache_key(url, params)
        return self._store.pop(key, None) is not None

    def clear(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


# Module-level singleton used by the executor
l1 = L1Cache()

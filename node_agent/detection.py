"""
Detection patterns for HTTP responses analyzed by the node agent.
Runs on the node, not the manager — checks are from the node's network perspective.
"""
from dataclasses import dataclass

_CF_HTML = [
    "cf-browser-verification",
    "just a moment",
    "checking your browser before accessing",
    "_cf_chl_",
    "cf-turnstile",
    "__cf_bm",
    "cloudflare ray id",
    "enable javascript and cookies to continue",
]

_CAPTCHA = [
    "g-recaptcha",
    "recaptcha/api.js",
    "h-captcha",
    "hcaptcha.com/1/api.js",
    "cf-turnstile",
    "data-sitekey",
    "verifying you are human",
    "verify you are human",
    "i'm not a robot",
    "im not a robot",
    "bot verification",
]

_GEO = [
    "not available in your region",
    "not available in your country",
    "not available where you live",
    "geo-blocked",
    "geo blocked",
    "content not available",
    "acceso denegado",
    "acceso no disponible",
    "no disponible en tu país",
]


@dataclass(frozen=True)
class Detection:
    cloudflare_present:   bool
    cloudflare_challenge: bool
    captcha:              bool
    geo_blocked:          bool
    empty_page:           bool


def detect(status_code: int, body: str, headers: dict[str, str]) -> Detection:
    lh = {k.lower(): v.lower() for k, v in headers.items()}
    lb = body.lower()

    cf_present   = "cf-ray" in lh or lh.get("server", "").startswith("cloudflare")
    cf_challenge = cf_present and _is_challenge(status_code, lh, lb)
    captcha      = any(p in lb for p in _CAPTCHA)
    geo          = status_code == 451 or any(p in lb for p in _GEO)
    empty        = status_code == 200 and len(body.strip()) < 200

    return Detection(
        cloudflare_present=cf_present,
        cloudflare_challenge=cf_challenge,
        captcha=captcha,
        geo_blocked=geo,
        empty_page=empty,
    )


def _is_challenge(status_code: int, lh: dict, lb: str) -> bool:
    if lh.get("cf-mitigated") == "challenge":
        return True
    if status_code in (403, 503):
        return any(p in lb for p in _CF_HTML)
    if status_code == 200:
        return any(p in lb for p in _CF_HTML)
    return False


def to_detail_string(d: Detection) -> str | None:
    """Human-readable summary for CheckResult.detail field."""
    parts = []
    if d.captcha:
        parts.append("captcha")
    if d.cloudflare_challenge:
        parts.append("cloudflare_challenge")
    if d.geo_blocked:
        parts.append("geo_block")
    if d.empty_page:
        parts.append("empty_page")
    return ",".join(parts) if parts else None

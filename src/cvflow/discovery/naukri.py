"""Naukri job search via its internal jobapi.

JobSpy's Naukri scraper 406s because Naukri's search API requires ``nkparam`` — a one-time
anti-bot token its frontend mints by RSA-encrypting a fixed plaintext with a public key embedded
in Naukri's own JS. We reproduce that in pure Python (no browser, no proxy, no paid service).

Technique credit: github.com/Traverser25/NopeRi (the public key is Naukri's, shipped in their
frontend bundle). We reimplement the minimal generator; no code is copied.

``search_naukri`` returns rows in the same dict shape ``normalize_rows`` expects, so it slots into
the discovery pipeline alongside JobSpy results. When the datacenter IP is blocked it raises
:class:`NaukriUnreachable` rather than returning silently empty, so the caller can flag it.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("cvflow.discovery.naukri")

SEARCH_URL = "https://www.naukri.com/jobapi/v3/search"
_SEED_URL = "https://www.naukri.com/devops-jobs"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Naukri's frontend RSA public key (embedded in their JS bundle, not a secret).
_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBALrlQ+djR0RjJwBF1xuisHmdFv334MIm
K6LgzJhmLhN7B5yuEyaKoasgXQk3+OQglsOaBxEJ0j5PcTL3nbOvt80CAwEAAQ==
-----END PUBLIC KEY-----"""

GetFn = Callable[..., "tuple[int, dict[str, Any]]"]


class NaukriUnreachable(Exception):
    """Raised when Naukri's search API cannot be reached (e.g. the datacenter IP is blocked),
    as distinct from reaching it and getting no matching jobs."""


def make_nkparam(page_type: str = "srp") -> str:
    """Mint a fresh single-use ``nkparam`` token (RSA-encrypt ``v0|<ms>|121_<type>``)."""
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA

    cipher = PKCS1_v1_5.new(RSA.import_key(_PUBLIC_KEY))
    plaintext = f"v0|{int(time.time() * 1000)}|121_{page_type}"
    return base64.b64encode(cipher.encrypt(plaintext.encode())).decode()


_SAL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)")


def parse_salary(label: str | None) -> tuple[float | None, float | None, str | None]:
    """Parse a Naukri salary label like '3-6 Lacs PA' -> (300000, 600000, 'INR').

    Returns (None, None, None) for 'Not disclosed' or anything unparseable.
    """
    if not label:
        return None, None, None
    low = label.lower()
    if "disclos" in low:
        return None, None, None
    m = _SAL_RE.search(label)
    if not m:
        return None, None, None
    lo, hi = float(m.group(1)), float(m.group(2))
    mult = 100_000.0 if ("lac" in low or "lakh" in low) else (1.0 if "," in label else 100_000.0)
    return lo * mult, hi * mult, "INR"


def _seo_key(keyword: str, location: str, page: int) -> str:
    kw = keyword.strip().lower().replace(".", "-dot-").replace(" ", "-").replace("+", "-")
    kw = kw.strip("-")
    if location.strip():
        return f"{kw}-jobs-in-{location.strip().lower().replace(' ', '-')}-{page}"
    return f"{kw}-jobs-{page}"


def _row_from_job(raw: dict[str, Any]) -> dict[str, Any]:
    """Map one Naukri API job into the normalize_rows row shape."""
    ph = {p.get("type"): p.get("label") for p in raw.get("placeholders", []) if isinstance(p, dict)}
    min_a, max_a, cur = parse_salary(ph.get("salary"))
    skills = raw.get("tagsAndSkills") or ""
    desc = (raw.get("jobDescription") or "").strip()
    if skills:
        desc = f"{desc}\nKey skills: {skills}"
    jd_url = raw.get("jdURL") or ""
    if jd_url.startswith("/"):
        jd_url = "https://www.naukri.com" + jd_url
    return {
        "id": str(raw.get("jobId") or ""),
        "site": "naukri",
        "title": raw.get("title") or "",
        "company": raw.get("companyName") or "",
        "location": ph.get("location") or "",
        "description": desc,
        "job_url": jd_url,
        "date_posted": raw.get("footerPlaceholderLabel") or "",
        "min_amount": min_a,
        "max_amount": max_a,
        "currency": cur,
        "experience_range": ph.get("experience") or "",
        "job_type": None,
    }


def _requests_get(
    url: str, *, params: dict[str, Any], headers: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    sess = _session()
    r = sess.get(url, params=params, headers=headers, timeout=25)
    ctype = r.headers.get("content-type", "")
    body = r.json() if ctype.startswith("application/json") else {}
    return r.status_code, body


_SESSION: Any = None


def _session() -> Any:
    """Lazily build a cookie-seeded requests session (Naukri sets cookies on the first GET)."""
    global _SESSION
    if _SESSION is None:
        import requests

        _SESSION = requests.Session()
        _SESSION.headers.update({"user-agent": _UA, "accept-language": "en-US,en;q=0.9"})
        try:
            _SESSION.get(_SEED_URL, timeout=20)
        except Exception as exc:  # noqa: BLE001 — cookies are best-effort
            logger.warning("naukri cookie seed failed: %s", exc)
    return _SESSION


def search_naukri(
    *,
    search_term: str,
    location: str,
    results_wanted: int,
    hours_old: int,
    get_fn: GetFn = _requests_get,
    nkparam_fn: Callable[[], str] = make_nkparam,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """Search Naukri's jobapi and return rows (normalize_rows shape).

    Paginates to ``results_wanted`` with a fresh nkparam per request. Returns normally (possibly
    empty) when the API was reached; raises :class:`NaukriUnreachable` when the very first page is
    blocked. A block on a later page, after some rows were already collected, just ends pagination.
    """
    rows: list[dict[str, Any]] = []
    job_age_days = max(1, hours_old // 24)
    for page in range(1, max_pages + 1):
        params = {
            "noOfResults": 20, "urlType": "search_by_keyword", "searchType": "adv",
            "keyword": search_term, "k": search_term, "pageNo": page,
            "jobAge": job_age_days, "seoKey": _seo_key(search_term, location, page),
            "src": "jobsearchDesk", "latLong": "",
        }
        if location.strip():
            params["location"] = location
        try:
            page_jobs = _fetch_page(params, get_fn, nkparam_fn)
        except NaukriUnreachable:
            if rows:
                break  # reached the API earlier; treat a later-page block as end-of-pages
            raise  # first page blocked — we never reached Naukri at all
        if not page_jobs:
            break
        rows.extend(_row_from_job(j) for j in page_jobs)
        if len(rows) >= results_wanted:
            break
    logger.info("naukri: %d rows for term=%r location=%r", len(rows), search_term, location)
    return rows[:results_wanted]


def _fetch_page(
    params: dict[str, Any], get_fn: GetFn, nkparam_fn: Callable[[], str]
) -> list[dict[str, Any]]:
    for attempt in (1, 2):  # a 403 means the one-time token was rejected; retry once fresh
        headers = {
            "authority": "www.naukri.com", "accept": "application/json",
            "accept-language": "en-US,en;q=0.9", "appid": "109", "systemid": "Naukri",
            "gid": "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE", "nkparam": nkparam_fn(),
            "user-agent": _UA,
        }
        status, body = get_fn(SEARCH_URL, params=params, headers=headers)
        if status == 200:
            jobs = body.get("jobDetails") or body.get("jobs") or []
            return list(jobs)
        if status == 403 and attempt == 1:
            logger.info("naukri 403 (stale nkparam); retrying with a fresh token")
            continue
        if status in (400, 404):
            # No more pages for this query (common on later pages / sparse locations).
            logger.debug("naukri end-of-pages HTTP %d (page %s)", status, params.get("pageNo"))
            return []
        # A persistent 403, or a 429/5xx: the API is blocking or failing us, not just out of pages.
        logger.warning("naukri search HTTP %d (page %s)", status, params.get("pageNo"))
        raise NaukriUnreachable(f"naukri search returned HTTP {status}")
    raise NaukriUnreachable("naukri search exhausted retries")

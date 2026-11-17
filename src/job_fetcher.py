"""Job discovery from authorized, public sources (APIs, public ATS boards, RSS, custom JSON APIs).

No scraping of sites that forbid it, no CAPTCHA/login bypass. LinkedIn / Naukri / Indeed have no open public
job-search API: connect them only through an API/feed you are authorized to use via `custom_json` or `rss`.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .utils import alnum, html_to_text

log = logging.getLogger(__name__)

USER_AGENT = "ai-job-alert-system/1.0 (personal job discovery; +https://github.com)"
HTTP_TIMEOUT = 20


@dataclass
class Job:
    source: str
    job_id: str
    title: str
    company: str
    location: str
    url: str
    description: str
    experience: str = "Not specified"
    posted_at: Optional[str] = None  # ISO-8601 UTC

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- helpers
_session: Optional[requests.Session] = None


def _http() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET",), respect_retry_after_header=True)
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.headers["User-Agent"] = USER_AGENT
        _session = s
    return _session


def http_get(url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None) -> requests.Response:
    resp = _http().get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp


def parse_date(value: Any) -> Optional[str]:
    """Normalize epoch (s/ms), struct_time, RFC-2822 or ISO strings to ISO-8601 UTC; None if unknown."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        elif hasattr(value, "tm_year"):
            dt = datetime(*value[:6], tzinfo=timezone.utc)
        else:
            s = str(value).strip()
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError):
        return None


_EXP_RE = re.compile(r"(\d{1,2})\s*(\+|plus)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*(\+)?)?\s*(?:years?|yrs?)\b", re.I)


def extract_experience(text: str) -> str:
    """Best-effort 'N-M years' extraction, preferring mentions next to the word 'experience'."""
    fallback = None
    for m in _EXP_RE.finditer(text or ""):
        label = (f"{m.group(1)}-{m.group(3)} years" if m.group(3)
                 else f"{m.group(1)}+ years" if m.group(2) else f"{m.group(1)} years")
        window = text[max(0, m.start() - 60): m.end() + 60].lower()
        if "experience" in window or "exp" in window:
            return label
        fallback = fallback or label
    return fallback or "Not specified"


def make_job(*, source: str, job_id: Any, title: Any, company: Any, location: Any, url: Any,
             description_html: Any, posted: Any) -> Optional[Job]:
    title, url = str(title or "").strip(), str(url or "").strip()
    if not title or not url.startswith(("http://", "https://")):
        return None
    desc = html_to_text(description_html)
    return Job(
        source=source,
        job_id=str(job_id or "").strip(),
        title=title,
        company=str(company or "Unknown company").strip(),
        location=str(location or "Not specified").strip() or "Not specified",
        url=url,
        description=desc,
        experience=extract_experience(desc),
        posted_at=parse_date(posted),
    )


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


# --------------------------------------------------------------------------- sources
def fetch_remotive(cfg: dict, queries: List[str]) -> List[Job]:
    # One request per run (Remotive asks API users to poll sparingly); filtering happens locally.
    data = http_get("https://remotive.com/api/remote-jobs").json()
    return [j for j in (make_job(source="remotive", job_id=r.get("id"), title=r.get("title"),
                                 company=r.get("company_name"),
                                 location=r.get("candidate_required_location") or "Remote",
                                 url=r.get("url"), description_html=r.get("description"),
                                 posted=r.get("publication_date")) for r in data.get("jobs", [])) if j]


def fetch_arbeitnow(cfg: dict, queries: List[str]) -> List[Job]:
    data = http_get("https://www.arbeitnow.com/api/job-board-api").json()
    out = []
    for r in data.get("data", []):
        loc = r.get("location") or ""
        if r.get("remote"):
            loc = f"{loc} (Remote)".strip()
        job = make_job(source="arbeitnow", job_id=r.get("slug"), title=r.get("title"),
                       company=r.get("company_name"), location=loc, url=r.get("url"),
                       description_html=r.get("description"), posted=r.get("created_at"))
        if job:
            out.append(job)
    return out


def fetch_adzuna(cfg: dict, queries: List[str]) -> List[Job]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        raise RuntimeError("Adzuna enabled but ADZUNA_APP_ID / ADZUNA_APP_KEY are not set")
    country = cfg.get("country", "in")
    out: List[Job] = []
    for q in queries[:3]:
        params = {"app_id": app_id, "app_key": app_key, "what": q,
                  "results_per_page": cfg.get("results_per_page", 30), "sort_by": "date",
                  "content-type": "application/json"}
        if cfg.get("where"):
            params["where"] = cfg["where"]
        data = http_get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params=params).json()
        for r in data.get("results", []):
            job = make_job(source="adzuna", job_id=r.get("id"), title=r.get("title"),
                           company=(r.get("company") or {}).get("display_name"),
                           location=(r.get("location") or {}).get("display_name"),
                           url=r.get("redirect_url"), description_html=r.get("description"),
                           posted=r.get("created"))
            if job:
                out.append(job)
    return out


def fetch_greenhouse(cfg: dict, queries: List[str]) -> List[Job]:
    out, ok, last_err = [], 0, None
    for board in cfg.get("boards", []):
        try:
            data = http_get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                            params={"content": "true"}).json()
            ok += 1
        except requests.RequestException as exc:
            log.warning("Greenhouse board failed", extra={"ctx": {"board": board, "error": str(exc)}})
            last_err = exc
            continue
        for r in data.get("jobs", []):
            job = make_job(source=f"greenhouse:{board}", job_id=r.get("id"), title=r.get("title"),
                           company=board, location=(r.get("location") or {}).get("name"),
                           url=r.get("absolute_url"), description_html=r.get("content"),
                           posted=r.get("updated_at"))
            if job:
                out.append(job)
    if cfg.get("boards") and ok == 0:
        raise RuntimeError(f"All Greenhouse boards failed: {last_err}")
    return out


def fetch_lever(cfg: dict, queries: List[str]) -> List[Job]:
    out, ok, last_err = [], 0, None
    for company in cfg.get("companies", []):
        try:
            data = http_get(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"}).json()
            ok += 1
        except requests.RequestException as exc:
            log.warning("Lever company failed", extra={"ctx": {"company": company, "error": str(exc)}})
            last_err = exc
            continue
        for r in data:
            job = make_job(source=f"lever:{company}", job_id=r.get("id"), title=r.get("text"),
                           company=company, location=(r.get("categories") or {}).get("location"),
                           url=r.get("hostedUrl"),
                           description_html=r.get("descriptionPlain") or r.get("description"),
                           posted=r.get("createdAt"))
            if job:
                out.append(job)
    if cfg.get("companies") and ok == 0:
        raise RuntimeError(f"All Lever companies failed: {last_err}")
    return out


def fetch_rss(cfg: dict, queries: List[str]) -> List[Job]:
    import feedparser

    out, ok, last_err = [], 0, None
    for feed in cfg.get("feeds", []):
        try:
            resp = http_get(feed["url"])  # fetched with requests so timeout/retry apply
            parsed = feedparser.parse(resp.content)
            ok += 1
        except requests.RequestException as exc:
            log.warning("RSS feed failed", extra={"ctx": {"feed": feed.get("name"), "error": str(exc)}})
            last_err = exc
            continue
        for e in parsed.entries:
            job = make_job(source=f"rss:{feed.get('name', 'feed')}", job_id=e.get("id") or e.get("guid"),
                           title=e.get("title"), company=e.get("author") or feed.get("name"),
                           location=e.get("location") or feed.get("location"), url=e.get("link"),
                           description_html=e.get("summary") or e.get("description"),
                           posted=e.get("published_parsed") or e.get("updated_parsed"))
            if job:
                out.append(job)
    if cfg.get("feeds") and ok == 0:
        raise RuntimeError(f"All RSS feeds failed: {last_err}")
    return out


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: str) -> str:
    def sub(m: "re.Match") -> str:
        v = os.environ.get(m.group(1))
        if not v:
            raise RuntimeError(f"Environment variable {m.group(1)} is not set (add it to the workflow env)")
        return v
    return _ENV_REF.sub(sub, value)


def fetch_custom_json(cfg: dict, queries: List[str]) -> List[Job]:
    """Generic adapter for any JSON job API you are authorized to call (partner APIs, ATS exports...)."""
    out: List[Job] = []
    for ep in cfg.get("endpoints", []):
        if ep.get("enabled", True) is False:
            continue
        headers = {k: _expand_env(v) for k, v in ep.get("headers", {}).items()}
        fields = ep["fields"]
        for q in (queries[:3] if "{query}" in str(ep.get("params", {})) else [""]):
            params = {k: str(v).replace("{query}", q) for k, v in ep.get("params", {}).items()}
            data = http_get(ep["url"], params=params, headers=headers).json()
            items = _dig(data, ep["items_path"]) if ep.get("items_path") else data
            for r in items or []:
                job = make_job(source=f"custom:{ep.get('name', 'api')}", job_id=_dig(r, fields.get("id", "id")),
                               title=_dig(r, fields["title"]), company=_dig(r, fields.get("company", "company")),
                               location=_dig(r, fields.get("location", "location")), url=_dig(r, fields["url"]),
                               description_html=_dig(r, fields.get("description", "description")),
                               posted=_dig(r, fields.get("posted_at", "posted_at")))
                if job:
                    out.append(job)
    return out


SOURCES: Dict[str, Callable[[dict, List[str]], List[Job]]] = {
    "remotive": fetch_remotive,
    "arbeitnow": fetch_arbeitnow,
    "adzuna": fetch_adzuna,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "rss": fetch_rss,
    "custom_json": fetch_custom_json,
}


# --------------------------------------------------------------------------- orchestration
def build_queries(profile: dict, max_queries: int) -> List[str]:
    seen, queries = set(), []
    for q in (profile.get("preferred_roles") or []) + (profile.get("search_keywords") or []):
        if q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)
    return queries[:max_queries]


def fetch_all(config: dict, profile: dict) -> Tuple[List[Job], int, int]:
    """Returns (jobs, sources_failed, sources_attempted). One failing source never aborts the run."""
    queries = build_queries(profile, config["search"]["max_queries"])
    jobs: List[Job] = []
    attempted = failed = 0
    for name, scfg in config["sources"].items():
        if not scfg.get("enabled") or name not in SOURCES:
            continue
        attempted += 1
        try:
            found = SOURCES[name](scfg, queries)
            log.info("Fetched source", extra={"ctx": {"source": name, "jobs": len(found)}})
            jobs.extend(found)
        except Exception as exc:  # noqa: BLE001 - isolate each source
            failed += 1
            log.error("Source failed", extra={"ctx": {"source": name, "error": str(exc)}})
    return jobs, failed, attempted


# --------------------------------------------------------------------------- filtering
def is_valid(job: Job, min_desc_chars: int) -> bool:
    return bool(job.title and job.url and len(job.description) >= min_desc_chars)


def is_recent(job: Job, max_age_days: int, now: Optional[datetime] = None) -> bool:
    if not job.posted_at:
        return True  # unknown date: let the duplicate filter handle repeats
    now = now or datetime.now(timezone.utc)
    return datetime.fromisoformat(job.posted_at) >= now - timedelta(days=max_age_days)


def _term_regex(term: str) -> "re.Pattern":
    return re.compile(r"(?<![a-z0-9+#.])" + re.escape(term.lower()) + r"(?![a-z0-9+#])")


def prefilter(jobs: List[Job], profile: dict, min_score: int, limit: int) -> List[Job]:
    """Cheap keyword relevance gate so we only pay for AI scoring on plausible jobs."""
    terms = {t.lower() for t in (profile.get("technical_skills") or []) + (profile.get("preferred_roles") or [])
             + (profile.get("search_keywords") or []) if len(t.strip()) >= 2}
    patterns = [_term_regex(t) for t in terms]
    scored = []
    for job in jobs:
        title, body = job.title.lower(), job.description.lower()
        score = sum(3 for p in patterns if p.search(title)) + sum(1 for p in patterns if p.search(body))
        if score >= min_score:
            scored.append((score, job))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [j for _, j in scored[:limit]]

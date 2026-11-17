"""Duplicate detection via normalized URLs, source job IDs and a title/company/location fingerprint."""
from __future__ import annotations

import re
from typing import Iterable, List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .database import JobHistory
from .job_fetcher import Job

TRACKING_PARAMS = {"gclid", "fbclid", "ref", "refid", "source", "src", "trk", "trackingid", "mc_cid", "mc_eid",
                   "lipi", "from", "campaign", "cmp"}
# Query params that actually identify the posting on some sites.
ID_PARAMS = {"id", "jobid", "job_id", "gh_jid", "jk", "vjk", "jid", "requisitionid", "reqid", "currentjobid"}


def normalize_url(url: str) -> str:
    """Lowercase host, drop fragment/tracking params/trailing slash, sort remaining params."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    query.sort()
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def job_keys(job: Job) -> List[str]:
    """First key is the primary key. A job matches history if ANY key matches."""
    keys = [f"url:{normalize_url(job.url)}"]
    if job.job_id:
        keys.append(f"id:{_slug(job.source.split(':')[0])}:{job.job_id}")
    keys.append(f"fp:{_slug(job.title)}|{_slug(job.company)}|{_slug(job.location)}")
    return keys


def filter_new(jobs: Iterable[Job], history: JobHistory) -> List[Job]:
    """Remove jobs already in history AND duplicates inside the current batch."""
    batch_keys: set = set()
    fresh: List[Job] = []
    for job in jobs:
        keys = job_keys(job)
        if history.contains_any(keys) or any(k in batch_keys for k in keys):
            continue
        batch_keys.update(keys)
        fresh.append(job)
    return fresh

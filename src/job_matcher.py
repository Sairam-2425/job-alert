"""AI job matching: OpenAI returns sub-scores + skills; the final 0-100 score is computed in code from
configurable weights so scoring is transparent and tunable."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List

from .job_fetcher import Job
from .llm import LLMError, chat_json
from .utils import PROMPTS_DIR, alnum

log = logging.getLogger(__name__)

SUB_SCORES = ("technical_skills", "experience", "education", "title_relevance", "location")
HIGH, MODERATE, LOW = "Highly relevant", "Moderately relevant", "Not relevant"


@dataclass
class MatchResult:
    job: Job
    score: int
    category: str
    sub_scores: Dict[str, int]
    matching_skills: List[str] = field(default_factory=list)
    missing_skills: List[str] = field(default_factory=list)
    summary: str = ""
    reason: str = ""


def categorize(score: int, scoring_cfg: dict) -> str:
    if score >= scoring_cfg["highly_relevant_min"]:
        return HIGH
    if score >= scoring_cfg["moderately_relevant_min"]:
        return MODERATE
    return LOW


def weighted_score(sub_scores: Dict[str, int], weights: Dict[str, float]) -> int:
    total_w = sum(weights.get(k, 0) for k in SUB_SCORES)
    if total_w <= 0:
        raise ValueError("scoring weights sum to zero")
    return round(sum(sub_scores[k] * weights.get(k, 0) for k in SUB_SCORES) / total_w)


def _clamp(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LLMError(f"Sub-score is not numeric: {value!r}")
    return max(0, min(100, int(round(value))))


def _str_list(value, limit: int = 10) -> List[str]:
    if not isinstance(value, list):
        return []
    return [s.strip() for s in value if isinstance(s, str) and s.strip()][:limit]


def _candidate_view(profile: dict) -> dict:
    keep = ("current_title", "summary", "total_experience_years", "seniority_level", "technical_skills",
            "experience", "education", "projects", "certifications", "preferred_roles", "preferred_locations",
            "remote_ok")
    return {k: profile.get(k) for k in keep}


def score_job(client, model: str, profile: dict, job: Job, config: dict) -> MatchResult:
    if client is None:  # no-AI (local) mode
        from .local_matcher import score_job_local
        return score_job_local(profile, job, config)
    system = (PROMPTS_DIR / "matching_prompt.txt").read_text(encoding="utf-8")
    limit = config["search"]["max_description_chars_for_ai"]
    payload = {
        "CANDIDATE": _candidate_view(profile),
        "JOB": {"title": job.title, "company": job.company, "location": job.location,
                "experience_requirement": job.experience, "description": job.description[:limit]},
    }
    raw = chat_json(client, model=model, system=system, user=json.dumps(payload, ensure_ascii=False),
                    attempts=config["openai"]["max_attempts"])

    missing_keys = [k for k in SUB_SCORES if k not in raw]
    if missing_keys:
        raise LLMError(f"Model response missing sub-scores: {missing_keys}")
    subs = {k: _clamp(raw[k]) for k in SUB_SCORES}
    score = weighted_score(subs, config["scoring"]["weights"])

    # Never credit the candidate with skills that are not in their profile.
    owned = {alnum(s): s for s in profile.get("technical_skills", [])}
    matching = [owned[alnum(s)] for s in _str_list(raw.get("matching_skills")) if alnum(s) in owned]

    return MatchResult(
        job=job,
        score=score,
        category=categorize(score, config["scoring"]),
        sub_scores=subs,
        matching_skills=matching,
        missing_skills=[s for s in _str_list(raw.get("missing_skills")) if alnum(s) not in owned],
        summary=str(raw.get("summary") or "").strip(),
        reason=str(raw.get("reason") or "").strip(),
    )

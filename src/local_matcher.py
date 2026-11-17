"""No-AI job scoring: transparent rules producing the same 5 sub-scores as the AI matcher."""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .job_fetcher import Job
from .job_matcher import MatchResult, categorize, weighted_score
from .local_analyzer import DEGREE_LEVELS
from .skills_catalog import find_skills
from .utils import alnum

STOP = {"senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate", "the", "a", "an", "and", "of", "i", "ii", "iii", "remote", "for", "in", "at"}


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9+#.]+", text.lower()) if t not in STOP and len(t) > 1}


def title_score(job_title: str, roles: List[str]) -> Tuple[int, str]:
    if not roles:
        return 50, "no preferred roles known"
    jt = _tokens(job_title)
    best, best_role = 0.0, roles[0]
    for role in roles:
        rt = _tokens(role)
        if not rt:
            continue
        if role.lower() in job_title.lower():
            return 100, f"title contains '{role}'"
        overlap = len(jt & rt) / len(rt)
        if overlap > best:
            best, best_role = overlap, role
    return round(100 * min(1.0, best)), f"{round(best * 100)}% word overlap with '{best_role}'"


def _job_years(experience: str) -> Tuple[Optional[int], Optional[int]]:
    m = re.match(r"(\d+)(?:-(\d+))? years|(\d+)\+ years", experience or "")
    if not m:
        return None, None
    if m.group(3):
        return int(m.group(3)), None
    return int(m.group(1)), int(m.group(2)) if m.group(2) else int(m.group(1))


def experience_score(candidate_years: Optional[float], job_exp: str) -> Tuple[int, str]:
    lo, hi = _job_years(job_exp)
    if lo is None:
        return 70, "job states no experience requirement"
    if candidate_years is None:
        return 60, f"job wants {job_exp}; candidate years unknown"
    if candidate_years < lo:
        return max(10, round(100 - 25 * (lo - candidate_years))), f"{candidate_years:g} yrs vs required {job_exp}"
    if hi is not None and candidate_years > hi + 2:
        return max(60, round(100 - 8 * (candidate_years - hi))), f"{candidate_years:g} yrs is above {job_exp}"
    return 100, f"{candidate_years:g} yrs fits {job_exp}"


def education_score(profile: dict, job_text: str) -> Tuple[int, str]:
    mentioned = {level for level, _, rx in DEGREE_LEVELS if level >= 2 and rx.search(job_text)}
    if not mentioned and re.search(r"\bdegree\b", job_text, re.I):
        mentioned = {2}
    if not mentioned:
        return 75, "no degree requirement stated"
    required = min(mentioned)
    have = max((e.get("level", 0) for e in profile.get("education", [])), default=0)
    if have == 0:
        return 50, "candidate education not detected"
    return (100, "education meets requirement") if have >= required else (40, "education below requirement")


def location_score(profile: dict, job: Job) -> Tuple[int, str]:
    loc = job.location.lower()
    prefs = [p.lower() for p in profile.get("preferred_locations") or []]
    remote = "remote" in loc or "remote" in job.title.lower() or "anywhere" in loc
    if prefs and any(p in loc or p in job.description.lower()[:1500] for p in prefs):
        return 100, "matches preferred location"
    if remote and profile.get("remote_ok", True):
        return 90, "remote role"
    if not prefs:
        return 70, "no location preference set"
    if loc in ("", "not specified"):
        return 60, "job location unknown"
    return 30, f"'{job.location}' is outside preferred locations"


def _summary(description: str, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", description).strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in sentences:
        if len(out) + len(s) > limit:
            break
        out += s + " "
    return (out or text[:limit]).strip()


def score_job_local(profile: dict, job: Job, config: dict) -> MatchResult:
    job_text = f"{job.title}\n{job.description}"
    owned = {alnum(s): s for s in profile.get("technical_skills", [])}
    job_skills = list(find_skills(job_text))
    matching = [owned[alnum(s)] for s in job_skills if alnum(s) in owned]
    missing = [s for s in job_skills if alnum(s) not in owned]
    if job_skills:
        tech = round(100 * len(matching) / len(job_skills))
        tech_note = f"{len(matching)}/{len(job_skills)} skills the job mentions are in your profile"
    else:
        tech, tech_note = 50, "no recognizable skills in the job text"

    roles = (profile.get("preferred_roles") or []) + ([profile["current_title"]] if profile.get("current_title") else [])
    title, title_note = title_score(job.title, roles)
    exp, exp_note = experience_score(profile.get("total_experience_years"), job.experience)
    edu, edu_note = education_score(profile, job_text)
    loc, loc_note = location_score(profile, job)

    subs = {"technical_skills": tech, "experience": exp, "education": edu, "title_relevance": title, "location": loc}
    score = weighted_score(subs, config["scoring"]["weights"])
    reason = (f"Skills {tech}: {tech_note}. Title {title}: {title_note}. Experience {exp}: {exp_note}. "
              f"Education {edu}: {edu_note}. Location {loc}: {loc_note}.")
    return MatchResult(job=job, score=score, category=categorize(score, config["scoring"]), sub_scores=subs,
                       matching_skills=matching[:10], missing_skills=missing[:10],
                       summary=_summary(job.description), reason=reason)

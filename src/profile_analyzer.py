"""Turn resume text into a structured, grounded candidate profile (data/candidate_profile.json)."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, List, Optional

from .database import load_json, save_json_atomic
from .llm import chat_json
from .resume_parser import ParsedResume, find_resume, parse_resume
from .utils import PROMPTS_DIR, alnum, env_or, utcnow_iso

log = logging.getLogger(__name__)

LIST_FIELDS = ["technical_skills", "experience", "education", "projects", "certifications",
               "preferred_roles", "preferred_locations", "search_keywords"]


def _supported(item: str, text_lower: str, text_alnum: str) -> bool:
    """True if the resume text actually contains this skill/certification (lenient on punctuation)."""
    item_l = item.lower().strip()
    if not item_l:
        return False
    if item_l in text_lower or alnum(item_l) in text_alnum:
        return True
    tokens = [t for t in re.findall(r"[a-z0-9+#]+", item_l) if len(t) > 2]
    return bool(tokens) and sum(t in text_lower for t in tokens) / len(tokens) >= 0.6


def ground_profile(profile: dict, resume_text: str) -> dict:
    """Drop skills/certifications the model produced that are not supported by the resume text."""
    low, al = resume_text.lower(), alnum(resume_text)
    for field in ("technical_skills", "certifications"):
        kept, dropped = [], []
        for item in profile.get(field) or []:
            (kept if isinstance(item, str) and _supported(item, low, al) else dropped).append(item)
        if dropped:
            log.warning("Removed unsupported %s from profile", field, extra={"ctx": {"dropped": dropped}})
        profile[field] = kept
    return profile


def normalize_profile(raw: dict) -> dict:
    profile = dict(raw)
    for field in LIST_FIELDS:
        value = profile.get(field)
        profile[field] = value if isinstance(value, list) else []
    for field in ("technical_skills", "certifications", "preferred_roles", "preferred_locations", "search_keywords"):
        profile[field] = [s.strip() for s in profile[field] if isinstance(s, str) and s.strip()]
    years = profile.get("total_experience_years")
    profile["total_experience_years"] = float(years) if isinstance(years, (int, float)) else None
    return profile


def analyze_resume(client, model: str, parsed: ParsedResume, attempts: int = 4) -> dict:
    system = (PROMPTS_DIR / "resume_prompt.txt").read_text(encoding="utf-8")
    user = f"<resume>\n{parsed.text[:30000]}\n</resume>"
    raw = chat_json(client, model=model, system=system, user=user, attempts=attempts)
    profile = ground_profile(normalize_profile(raw), parsed.text)
    if not profile["technical_skills"]:
        raise ValueError("No technical skills could be extracted from the resume")
    return profile


def apply_preferences(profile: dict, prefs: dict) -> dict:
    """config.json preferences override what was derived from the resume."""
    profile = dict(profile)
    if prefs.get("roles"):
        profile["preferred_roles"] = list(prefs["roles"])
    if prefs.get("locations"):
        profile["preferred_locations"] = list(prefs["locations"])
    profile["remote_ok"] = bool(prefs.get("remote_ok", True))
    return profile


def ensure_profile(client, config: dict, resume_dir: Path, profile_path: Path) -> dict:
    """
    - resume present and unchanged since last run -> reuse saved profile (no API cost)
    - resume present and new/changed              -> parse + analyze + save
    - no resume in repo but saved profile exists  -> reuse it (lets you keep the resume off GitHub)
    """
    model = env_or("OPENAI_MODEL", config["openai"]["model"])
    attempts = config["openai"]["max_attempts"]
    mode = "ai" if client is not None else "local"
    saved = load_json(profile_path, {})
    resume_path = find_resume(resume_dir)

    if resume_path is None:
        if saved.get("technical_skills"):
            log.info("No resume file found - using saved candidate profile")
            return apply_preferences(saved, config["preferences"])
        raise FileNotFoundError(
            f"No resume (.pdf/.docx) in '{resume_dir}' and no saved profile at {profile_path}"
        )

    parsed = parse_resume(resume_path)
    if saved.get("_resume_sha256") == parsed.sha256 and saved.get("technical_skills") and saved.get("_mode", "ai") == mode:
        log.info("Resume unchanged - reusing saved candidate profile")
        return apply_preferences(saved, config["preferences"])

    if client is None:
        from .local_analyzer import analyze_local
        log.info("Analyzing resume locally (no AI)")
        profile = analyze_local(parsed.text, config["preferences"])
        if not profile["technical_skills"]:
            raise ValueError("No known skills found in the resume. Add skills under preferences.extra_skills in config.json")
    else:
        log.info("Analyzing resume with the LLM", extra={"ctx": {"model": model}})
        profile = analyze_resume(client, model, parsed, attempts)
    profile["_mode"] = mode
    profile["_resume_sha256"] = parsed.sha256
    profile["_analyzed_at"] = utcnow_iso()
    profile["_resume_file"] = Path(parsed.path).name
    save_json_atomic(profile_path, profile)
    return apply_preferences(profile, config["preferences"])

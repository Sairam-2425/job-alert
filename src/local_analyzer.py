"""No-AI resume analysis: dictionary + heuristics. Everything comes straight from the resume text,
so nothing can be invented."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Optional

from .skills_catalog import find_skills

HEADINGS = {
    "skills": r"(?:technical\s+|key\s+|core\s+)?skills(?:\s*&\s*tools)?|technologies|tech\s+stack|tools",
    "experience": r"(?:work\s+|professional\s+|relevant\s+)?experience|employment(?:\s+history)?|work\s+history",
    "education": r"education(?:al)?(?:\s+qualifications?)?|academics?|academic\s+background",
    "projects": r"(?:personal\s+|academic\s+|key\s+)?projects",
    "certifications": r"certifications?|licenses?(?:\s*&\s*certifications?)?|courses|training",
    "summary": r"(?:professional\s+)?summary|objective|career\s+objective|profile|about\s+me",
    "other": r"achievements|awards|publications|interests|hobbies|languages|references|declaration",
}
_HEAD_RE = {k: re.compile(rf"^\s*(?:{v})\s*:?\s*$", re.I) for k, v in HEADINGS.items()}

ROLE_WORDS = r"engineer|developer|analyst|scientist|architect|consultant|administrator|designer|manager|lead|tester|programmer|specialist|intern|devops|sre"
ROLE_RE = re.compile(rf"\b(?:{ROLE_WORDS})\b", re.I)
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_RANGE = re.compile(
    rf"(?:{_MON}\s+)?((?:19|20)\d{{2}})\s*(?:-|–|—|to)\s*(?:{_MON}\s+)?((?:19|20)\d{{2}}|present|current|now|till\s+date|ongoing)",
    re.I)
_STATED_YEARS = re.compile(r"(\d{1,2})(?:\.\d)?\s*\+?\s*(?:years?|yrs?)(?:\s+of)?\s+(?:\w+\s+){0,3}?experience", re.I)

DEGREE_LEVELS = [  # (level, label, regex) - abbreviations are case-sensitive to avoid matching words like "be"/"ms"
    (4, "PhD", re.compile(r"\b(?:Ph\.?D|Doctorate|Doctor of Philosophy)\b", re.I)),
    (3, "Master's", re.compile(r"\b(?:Master|Masters|Master's)\b|\b(?:M\.?Tech|M\.?Sc|MBA|MCA|M\.?Eng|M\.?S|M\.?E)\b(?!\w)")),
    (2, "Bachelor's", re.compile(r"\b(?:Bachelor|Bachelors|Bachelor's)\b|\b(?:B\.?Tech|B\.?Sc|B\.?E|BCA|B\.?Com|B\.?A|B\.?S)\b(?!\w)")),
    (1, "Diploma", re.compile(r"\bDiploma\b", re.I)),
]


def split_sections(text: str) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {"_top": []}
    current = "_top"
    for line in text.splitlines():
        stripped = line.strip()
        matched = next((k for k, rx in _HEAD_RE.items() if len(stripped.split()) <= 5 and rx.match(stripped)), None)
        if matched:
            current = matched
            sections.setdefault(current, [])
        elif stripped:
            sections.setdefault(current, []).append(stripped)
    return sections


def _to_fraction(month: Optional[str], year: float) -> float:
    return year + (MONTHS.index(month.lower()[:3]) / 12 if month else 0)


def estimate_years(text: str, now: Optional[datetime] = None, range_text: Optional[str] = None) -> Optional[float]:
    """Prefer an explicit "N years of experience" statement anywhere in `text`; otherwise span the dated
    ranges found in `range_text` (the experience section, so education years are not counted)."""
    now = now or datetime.now()
    range_text = text if range_text is None else range_text
    stated = [int(m.group(1)) for m in _STATED_YEARS.finditer(text)]
    stated = [y for y in stated if 0 < y <= 40]
    if stated:
        return float(max(stated))
    starts, ends = [], []
    for m in _RANGE.finditer(range_text):
        s = _to_fraction(m.group(1), float(m.group(2)))
        e_raw = m.group(4).lower()
        e = now.year + (now.month - 1) / 12 if not e_raw[:2].isdigit() else _to_fraction(m.group(3), float(e_raw))
        if 0 <= e - s <= 40:
            starts.append(s)
            ends.append(e)
    return round(max(ends) - min(starts), 1) if starts else None


def _name(top: List[str]) -> Optional[str]:
    for line in top[:5]:
        if "@" in line or re.search(r"\d", line) or len(line.split()) > 4 or len(line) > 40:
            continue
        if not ROLE_RE.search(line):
            return line.title() if line.isupper() else line
    return None


def _titles(sections: Dict[str, List[str]]) -> List[str]:
    pool = sections.get("_top", [])[:12] + sections.get("experience", [])[:40]
    titles = []
    for line in pool:
        if ROLE_RE.search(line) and len(line.split()) <= 8 and not line.endswith("."):
            title = re.split(r"\s+(?:at|@|\||-|–|—)\s+|,\s", line)[0].strip(" -•*")
            if title and title.lower() not in {t.lower() for t in titles} and 1 <= len(title.split()) <= 6:
                titles.append(title)
    return titles


def education_entries(sections: Dict[str, List[str]], text: str) -> List[dict]:
    lines = sections.get("education") or [l for l in text.splitlines() if re.search(r"university|college|institute|school", l, re.I)]
    out = []
    for line in lines[:12]:
        for level, label, rx in DEGREE_LEVELS:
            if rx.search(line):
                year = re.search(r"(?:19|20)\d{2}", line)
                out.append({"degree": label, "level": level, "details": line[:160], "year": year.group(0) if year else None})
                break
    return out


def analyze_local(text: str, prefs: Optional[dict] = None) -> dict:
    prefs = prefs or {}
    sections = split_sections(text)
    skills = find_skills(text, prefs.get("extra_skills") or [])
    titles = _titles(sections)
    certs_block = sections.get("certifications", [])
    certs = [l.lstrip("•*-– ").strip() for l in certs_block if 3 < len(l) < 120][:10]
    if not certs:
        certs = [l.strip()[:120] for l in text.splitlines() if re.search(r"\bcertified\b|\bcertification\b", l, re.I)][:5]
    projects = []
    for line in sections.get("projects", []):
        if not line.startswith(("•", "-", "*", "–")) and len(line.split()) <= 14:
            projects.append({"name": line, "description": "", "technologies": []})
    exp_text = "\n".join(sections.get("experience", [])) or text
    years = estimate_years(text, range_text=exp_text)
    edu = education_entries(sections, text)
    return {
        "full_name": _name(sections.get("_top", [])),
        "current_title": titles[0] if titles else None,
        "summary": " ".join(sections.get("summary", [])[:3])[:400] or None,
        "total_experience_years": years,
        "seniority_level": None,
        "technical_skills": list(skills),
        "skill_mentions": skills,
        "experience": [{"title": t, "company": None, "start": None, "end": None, "summary": ""} for t in titles[:6]],
        "education": edu,
        "projects": projects[:8],
        "certifications": certs,
        "preferred_roles": titles[:3],
        "preferred_locations": [],
        "preferences_source": "derived_from_titles" if titles else "none",
        "search_keywords": list(skills)[:6],
    }

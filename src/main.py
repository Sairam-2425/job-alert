"""Entry point:  python -m src.main [--dry-run] [--config config.json]

Exit codes: 0 ok | 1 transient/runtime failure (workflow retries) | 3 configuration error (no retry)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import List

from .database import STATUS_NOTIFIED, STATUS_SCORED, HistoryError, JobHistory
from .duplicate_checker import filter_new, job_keys
from .email_sender import EmailDeliveryError, build_digest, send_digest
from .job_fetcher import fetch_all, is_recent, is_valid, prefilter
from .job_matcher import MatchResult, score_job
from .llm import FATAL_ERRORS, api_key, get_client
from .profile_analyzer import ensure_profile
from .resume_parser import ResumeError
from .utils import DATA_DIR, ROOT_DIR, ConfigError, env_or, load_config, setup_logging

log = logging.getLogger("main")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 3
MAX_CONSECUTIVE_AI_FAILURES = 5


def use_ai(config: dict) -> bool:
    """config.json -> ai.mode: "local" (no key needed) | "llm" | "auto" (LLM only if an API key is set)."""
    mode = config.get("ai", {}).get("mode", "auto")
    if mode == "local":
        return False
    if mode == "llm":
        return True
    return bool(api_key())


def _record(history: JobHistory, job, status: str, score) -> None:
    history.add(keys=job_keys(job), title=job.title, company=job.company, url=job.url, status=status, score=score)


def run(config: dict, *, dry_run: bool = False, history_path: Path = DATA_DIR / "seen_jobs.json",
        profile_path: Path = DATA_DIR / "candidate_profile.json", resume_dir: Path = None) -> int:
    resume_dir = resume_dir or ROOT_DIR / config["resume_dir"]
    history = JobHistory.load(history_path)
    log.info("History loaded", extra={"ctx": {"known_jobs": len(history), "dry_run": dry_run}})
    exit_code = EXIT_OK
    try:
        client = get_client(config["openai"]["timeout_seconds"], config["openai"].get("base_url")) if use_ai(config) else None
        log.info("Matching mode", extra={"ctx": {"mode": "ai" if client else "local (no AI)"}})
        model = env_or("OPENAI_MODEL", config["openai"]["model"])
        profile = ensure_profile(client, config, resume_dir, profile_path)

        # 1. discover
        jobs, failed, attempted = fetch_all(config, profile)
        if attempted == 0:
            log.error("No job sources are enabled in config.json")
            return EXIT_CONFIG
        if failed == attempted:
            log.error("Every job source failed")
            return EXIT_FAILED
        s = config["search"]

        # 2. validate -> recency -> de-duplicate -> keyword prefilter
        valid = [j for j in jobs if is_valid(j, s["min_description_chars"])]
        recent = [j for j in valid if is_recent(j, s["max_age_days"])]
        new_jobs = filter_new(recent, history)
        candidates = prefilter(new_jobs, profile, s["min_prefilter_score"], s["max_jobs_to_score"])
        log.info("Funnel", extra={"ctx": {"fetched": len(jobs), "valid": len(valid), "recent": len(recent),
                                          "new": len(new_jobs), "to_score": len(candidates)}})

        # 3. AI scoring (failures are skipped and NOT recorded, so the job is retried next run)
        relevant: List[MatchResult] = []
        consecutive_failures = 0
        for job in candidates:
            try:
                match = score_job(client, model, profile, job, config)
                consecutive_failures = 0
                if client is not None:
                    time.sleep(config["openai"].get("request_delay_seconds", 0))  # stay under provider rate limits
            except FATAL_ERRORS:
                log.exception("OpenAI rejected the request (check the API key and model name)")
                return EXIT_CONFIG
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                log.error("Scoring failed", extra={"ctx": {"title": job.title, "error": str(exc)}})
                if consecutive_failures >= MAX_CONSECUTIVE_AI_FAILURES:
                    log.error("Too many consecutive AI failures - aborting scoring")
                    exit_code = EXIT_FAILED
                    break
                continue
            if match.score >= config["scoring"]["notify_min_score"]:
                relevant.append(match)
            elif not dry_run:
                _record(history, job, STATUS_SCORED, match.score)  # don't pay to re-score it later
        if not dry_run:
            history.save()

        # 4. notify
        if not relevant:
            log.info("No new relevant jobs - no email sent")
            return exit_code
        relevant.sort(key=lambda m: m.score, reverse=True)
        batch = relevant[: config["email"]["max_jobs_per_digest"]]

        if dry_run:
            out = ROOT_DIR / "out"
            out.mkdir(exist_ok=True)
            subject, html, _ = build_digest(batch)
            (out / "digest_preview.html").write_text(html, encoding="utf-8")
            log.info("DRY RUN - email not sent", extra={"ctx": {"subject": subject, "preview": "out/digest_preview.html"}})
            return exit_code

        try:
            send_digest(batch, email_from=os.environ["EMAIL_FROM"], email_to=os.environ["EMAIL_TO"],
                        password=os.environ["EMAIL_PASSWORD"], host=config["email"]["smtp_host"],
                        port=config["email"]["smtp_port"], attempts=config["email"]["max_attempts"])
        except EmailDeliveryError as exc:
            log.error("Digest NOT delivered - jobs stay un-notified and will be retried", extra={"ctx": {"error": str(exc)}})
            return EXIT_FAILED

        # Only now - after confirmed delivery - are jobs marked as notified.
        for m in batch:
            _record(history, m.job, STATUS_NOTIFIED, m.score)
        log.info("Marked jobs as notified", extra={"ctx": {"count": len(batch)}})
        return exit_code
    finally:
        if not dry_run:
            removed = history.prune(config["history"]["retention_days"])
            history.save()
            log.info("History saved", extra={"ctx": {"known_jobs": len(history), "pruned": removed}})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AI job alert system")
    parser.add_argument("--dry-run", action="store_true", help="score jobs and write out/digest_preview.html; no email, no history changes")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    dry_run = args.dry_run or os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

    try:
        config = load_config(Path(args.config) if args.config else None)
        required = [] if dry_run else ["EMAIL_FROM", "EMAIL_TO", "EMAIL_PASSWORD"]
        missing = [name for name in required if not os.environ.get(name)]
        if config.get("ai", {}).get("mode") == "llm" and not api_key():
            missing.insert(0, "NVIDIA_API_KEY (or OPENAI_API_KEY)")
        if missing:
            raise ConfigError(f"Missing environment variables / GitHub Secrets: {', '.join(missing)}")
        return run(config, dry_run=dry_run)
    except (ConfigError, ResumeError, FileNotFoundError) as exc:
        log.error("Configuration problem: %s", exc)
        return EXIT_CONFIG
    except HistoryError as exc:
        log.error("History file problem: %s", exc)
        return EXIT_FAILED
    except Exception:  # noqa: BLE001
        log.exception("Unhandled error")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())

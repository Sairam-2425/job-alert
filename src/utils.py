"""Shared helpers: structured logging with secret redaction, retry/backoff, HTML cleanup, config."""
from __future__ import annotations

import html as htmllib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Tuple, Type, TypeVar

from bs4 import BeautifulSoup

T = TypeVar("T")

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
PROMPTS_DIR = ROOT_DIR / "prompts"

SECRET_ENV_VARS = ("NVIDIA_API_KEY", "OPENAI_API_KEY", "EMAIL_PASSWORD", "ADZUNA_APP_ID", "ADZUNA_APP_KEY")


# --------------------------------------------------------------------------- logging
def redact(text: str) -> str:
    """Replace the value of any known secret (or ${VAR} used by custom sources) with ***."""
    names = set(SECRET_ENV_VARS) | {k for k in os.environ if k.endswith(("_TOKEN", "_API_KEY", "_SECRET"))}
    for name in names:
        value = os.environ.get(name)
        if value and len(value) >= 6:
            text = text.replace(value, "***")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if ctx:
            payload["ctx"] = ctx
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Libraries can log request URLs/headers at DEBUG; keep them quiet.
    for noisy in ("urllib3", "httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- retry
def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    retry_on: Tuple[Type[BaseException], ...] = (Exception,),
    give_up_on: Tuple[Type[BaseException], ...] = (),
    what: str = "operation",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call fn(); on a transient error wait (exponential backoff + jitter) and retry."""
    log = logging.getLogger("retry")
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except give_up_on:
            raise
        except retry_on as exc:
            if attempt == attempts:
                log.error("%s failed after %d attempts: %s", what, attempts, exc)
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1)) + random.uniform(0, 1)
            log.warning("%s failed (attempt %d/%d): %s - retrying in %.1fs", what, attempt, attempts, exc, delay)
            sleep(delay)
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- text
def html_to_text(raw: Optional[str]) -> str:
    """Strip HTML (including HTML-escaped HTML as served by Greenhouse) to plain text."""
    if not raw:
        return ""
    text = str(raw)
    if "&lt;" in text and "<" not in text:
        text = htmllib.unescape(text)
    text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    text = htmllib.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


def alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9+#]", "", text.lower())


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- config
class ConfigError(Exception):
    pass


def load_config(path: Optional[Path] = None) -> dict:
    path = Path(path) if path else ROOT_DIR / "config.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    weights = cfg.get("scoring", {}).get("weights", {})
    if not weights or sum(weights.values()) <= 0:
        raise ConfigError("scoring.weights must contain positive numbers")
    return cfg


def env_or(name: str, default: str) -> str:
    """Environment variable with a default; empty strings count as unset (GitHub vars can be empty)."""
    return os.environ.get(name) or default

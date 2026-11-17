"""HTML digest + Gmail SMTP delivery (sender = secondary account, receiver = primary account)."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from typing import List, Optional, Tuple

from .job_matcher import HIGH, MODERATE, MatchResult
from .utils import retry_call

log = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    pass


def _safe_url(url: str) -> str:
    return escape(url, quote=True) if url.startswith(("http://", "https://")) else "#"


def _chips(items: List[str], bg: str, fg: str) -> str:
    if not items:
        return '<span style="color:#888">None identified</span>'
    return " ".join(
        f'<span style="display:inline-block;margin:2px 4px 2px 0;padding:2px 8px;border-radius:10px;'
        f'background:{bg};color:{fg};font-size:12px">{escape(s)}</span>' for s in items)


def _card_html(m: MatchResult) -> str:
    j = m.job
    color = {HIGH: "#1a7f37", MODERATE: "#b26a00"}.get(m.category, "#666")
    return f"""
<div style="border:1px solid #e1e4e8;border-radius:8px;padding:16px;margin:0 0 16px 0;background:#fff">
  <div style="float:right;text-align:center;min-width:64px">
    <div style="font-size:24px;font-weight:700;color:{color}">{m.score}%</div>
    <div style="font-size:11px;color:{color}">{escape(m.category)}</div>
  </div>
  <div style="font-size:17px;font-weight:600"><a href="{_safe_url(j.url)}" style="color:#0b5cad;text-decoration:none">{escape(j.title)}</a></div>
  <div style="color:#444;margin:2px 0 8px 0">{escape(j.company)} &middot; {escape(j.location)}</div>
  <div style="font-size:13px;color:#555"><b>Experience:</b> {escape(j.experience)}
     &nbsp;|&nbsp; <b>Source:</b> {escape(j.source.split(':')[0])}</div>
  <div style="margin-top:8px;font-size:13px"><b>Matching skills:</b> {_chips(m.matching_skills, '#dafbe1', '#116329')}</div>
  <div style="margin-top:4px;font-size:13px"><b>Missing skills:</b> {_chips(m.missing_skills, '#fff1d6', '#7a4b00')}</div>
  <p style="font-size:14px;margin:10px 0 6px 0">{escape(m.summary)}</p>
  <p style="font-size:12px;color:#666;margin:0 0 10px 0"><i>Why this score: {escape(m.reason)}</i></p>
  <a href="{_safe_url(j.url)}" style="display:inline-block;background:#0b5cad;color:#fff;padding:8px 14px;border-radius:6px;text-decoration:none;font-size:13px">View &amp; apply</a>
</div>"""


def build_digest(matches: List[MatchResult]) -> Tuple[str, str, str]:
    """Returns (subject, html_body, text_body). Matches are sorted best-first."""
    matches = sorted(matches, key=lambda m: m.score, reverse=True)
    top = matches[0]
    n = len(matches)
    subject = f"[Job Alert] {n} new job match{'es' if n != 1 else ''} - top {top.score}%: {top.job.title} @ {top.job.company}"
    cards = "".join(_card_html(m) for m in matches)
    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:#f6f8fa;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2328">
<div style="max-width:680px;margin:0 auto;padding:20px">
  <h2 style="margin:0 0 4px 0">{n} new job match{'es' if n != 1 else ''} for you</h2>
  <div style="color:#666;font-size:13px;margin-bottom:16px">Ranked by AI match score. Jobs you were already notified about are excluded.</div>
  {cards}
  <div style="color:#888;font-size:11px;text-align:center;margin-top:12px">Sent by your AI Job Alert System (GitHub Actions). No applications are submitted automatically.</div>
</div></body></html>"""
    lines = [f"{n} new job match(es)\n"]
    for m in matches:
        j = m.job
        lines += [f"{m.score}% ({m.category}) - {j.title} @ {j.company}, {j.location}",
                  f"  Experience: {j.experience}",
                  f"  Matching: {', '.join(m.matching_skills) or 'None identified'}",
                  f"  Missing: {', '.join(m.missing_skills) or 'None identified'}",
                  f"  {m.summary}", f"  Apply: {j.url}", ""]
    return subject, html, "\n".join(lines)


def _send_once(msg: EmailMessage, host: str, port: int, user: str, password: str) -> None:
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def send_digest(matches: List[MatchResult], *, email_from: str, email_to: str, password: str,
                host: str = "smtp.gmail.com", port: int = 465, attempts: int = 3) -> None:
    """Send one consolidated digest. Raises EmailDeliveryError if it could not be delivered."""
    subject, html, text = build_digest(matches)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, email_from, email_to
    msg["Date"], msg["Message-ID"] = formatdate(localtime=False), make_msgid()
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    try:
        retry_call(
            lambda: _send_once(msg, host, port, email_from, password),
            attempts=attempts, base_delay=5,
            retry_on=(smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, smtplib.SMTPResponseException,
                      TimeoutError, ConnectionError, OSError),
            give_up_on=(smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused),
            what="smtp.send",
        )
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailDeliveryError(
            "Gmail rejected the login. Use a 16-character App Password for EMAIL_PASSWORD "
            "(2-Step Verification must be on) and check EMAIL_FROM.") from exc
    except Exception as exc:  # noqa: BLE001
        raise EmailDeliveryError(f"Email delivery failed: {type(exc).__name__}: {exc}") from exc
    log.info("Digest email delivered", extra={"ctx": {"jobs": len(matches)}})

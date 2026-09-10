"""Outbound email.

Falls back to logging the message when no provider is configured. That is not
a placeholder: it is what lets a fresh deployment be logged into and tested
before any email account exists. The log line is deliberately loud.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


def send_email(to: str, subject: str, text: str, *, timeout: float = 15.0) -> bool:
    """Send one plain-text email. Returns whether a provider accepted it."""
    if not settings.resend_api_key:
        log.warning(
            "\n%s\nNo RESEND_API_KEY set, so this email was not sent.\n"
            "To: %s\nSubject: %s\n\n%s\n%s",
            "=" * 72, to, subject, text, "=" * 72,
        )
        return False

    try:
        response = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={"from": settings.mail_from, "to": [to], "subject": subject, "text": text},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        log.error("email to %s failed to send: %s", to, exc)
        return False

    if response.status_code >= 400:
        log.error("email provider rejected message to %s: %s", to, response.text[:400])
        return False

    log.info("sent %r to %s", subject, to)
    return True

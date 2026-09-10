"""Invite-only magic link authentication.

Two rules define the whole model:

1. An email address that is not in the ``invites`` table cannot log in. Holding
   the URL grants nothing. This is what stops an open link from turning into
   strangers spending your CPU and storage.
2. Login links are single-use and short-lived, and only their hash is stored,
   so a database leak does not hand over working links.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import db
from app.config import settings
from app.mailer import send_email

log = logging.getLogger(__name__)

SESSION_COOKIE = "clipstory_session"
_SESSION_SALT = "clipstory-session-v1"


class NotInvited(Exception):
    """The address is not on the allowlist."""


class InvalidToken(Exception):
    """The login link is unknown, expired, or already used."""


def normalise_email(email: str) -> str:
    return email.strip().lower()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_invited(email: str) -> bool:
    return db.query_one(
        "SELECT 1 FROM invites WHERE email = %s", (normalise_email(email),)
    ) is not None


def add_invite(email: str, invited_by: str) -> None:
    db.execute(
        """
        INSERT INTO invites (email, invited_by) VALUES (%s, %s)
        ON CONFLICT (email) DO NOTHING
        """,
        (normalise_email(email), invited_by),
    )
    log.info("invited %s (by %s)", normalise_email(email), invited_by)


def remove_invite(email: str) -> None:
    """Revoke access.

    The user row is left alone so their jobs and history survive; without an
    invite they simply cannot obtain a new session. Existing sessions are cut
    short by :func:`user_from_session`, which re-checks the allowlist on every
    request.
    """
    db.execute("DELETE FROM invites WHERE email = %s", (normalise_email(email),))
    log.info("revoked invite for %s", normalise_email(email))


def list_invites() -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT i.email,
               i.invited_by,
               i.created_at,
               u.is_admin,
               u.last_login_at
        FROM invites i
        LEFT JOIN users u ON u.email = i.email
        ORDER BY i.created_at
        """
    )


def request_login_link(email: str) -> str:
    """Create and send a magic link. Returns the URL, for logging in dev.

    Raises :class:`NotInvited` if the address is not on the allowlist. Callers
    should not reflect that distinction back to the browser, so that the
    allowlist cannot be enumerated by guessing addresses.
    """
    email = normalise_email(email)
    if not is_invited(email):
        raise NotInvited(email)

    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(minutes=settings.login_token_ttl_minutes)
    db.execute(
        "INSERT INTO login_tokens (token_hash, email, expires_at) VALUES (%s, %s, %s)",
        (_hash_token(token), email, expires_at),
    )

    url = f"{settings.base_url}/auth/verify?token={token}"
    send_email(
        email,
        "Your Clipstory sign-in link",
        f"Sign in to Clipstory:\n\n{url}\n\n"
        f"This link works once and expires in {settings.login_token_ttl_minutes} minutes.\n"
        "If you did not ask to sign in, ignore this message.",
    )
    return url


def consume_login_token(token: str) -> dict[str, Any]:
    """Validate a magic link and return the user row.

    The token is marked used inside the same statement that selects it, so two
    simultaneous clicks cannot both succeed.
    """
    row = db.query_one(
        """
        UPDATE login_tokens
        SET used_at = now()
        WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()
        RETURNING email
        """,
        (_hash_token(token),),
    )
    if row is None:
        raise InvalidToken("login link is invalid, expired, or already used")

    email = row["email"]
    # Re-check the allowlist here as well as at request time: an invite may
    # have been revoked in the minutes between the email being sent and the
    # link being clicked.
    if not is_invited(email):
        raise NotInvited(email)

    # ADMIN_EMAILS is applied here as well as at startup. Startup can only
    # promote rows that already exist, and a bootstrap admin has no row until
    # this, their first sign-in. Without this a fresh deployment would have
    # nobody able to reach the invite page.
    is_admin = email in settings.admin_emails
    user = db.query_one(
        """
        INSERT INTO users (email, last_login_at, is_admin) VALUES (%s, now(), %s)
        ON CONFLICT (email) DO UPDATE
        SET last_login_at = now(),
            is_admin = users.is_admin OR EXCLUDED.is_admin
        RETURNING id, email, is_admin
        """,
        (email, is_admin),
    )
    log.info("%s signed in", email)
    return user


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=_SESSION_SALT)


def issue_session(user: dict[str, Any]) -> str:
    return _serializer().dumps({"uid": user["id"], "email": user["email"]})


def user_from_session(cookie: str | None) -> dict[str, Any] | None:
    """Resolve a session cookie to a live user, or None.

    Re-reads the user and the allowlist on every request rather than trusting
    the cookie's contents, so revoking an invite takes effect immediately
    instead of whenever the cookie happens to expire.
    """
    if not cookie:
        return None
    try:
        payload = _serializer().loads(
            cookie, max_age=settings.session_ttl_days * 86400
        )
    except SignatureExpired:
        return None
    except BadSignature:
        log.warning("rejected a session cookie with a bad signature")
        return None

    user = db.query_one(
        "SELECT id, email, is_admin FROM users WHERE id = %s", (payload.get("uid"),)
    )
    if user is None or not is_invited(user["email"]):
        return None
    return user


def purge_expired_tokens() -> int:
    with db.connection() as conn:
        cursor = conn.execute(
            "DELETE FROM login_tokens WHERE expires_at < now() - interval '1 day'"
        )
        return cursor.rowcount or 0

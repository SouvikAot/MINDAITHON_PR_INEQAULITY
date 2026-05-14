"""
auth.py
Two-step authentication: password + SMTP OTP, session tokens.
"""
from __future__ import annotations
import os
import random
import sqlite3
import smtplib
import string
import uuid
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional, Dict, Any

from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = Path(__file__).resolve().parent / "database.db"
OTP_EXPIRY_MINUTES = 15
SESSION_EXPIRY_HOURS = 24 * 7  # 7 days


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------- #
# Schema
# ---------------------------------------------------------------------- #
def init_auth_tables() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_verified INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS otp_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            purpose TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------- #
# Users
# ---------------------------------------------------------------------- #
def create_user(name: str, email: str, password: str) -> str:
    user_id = uuid.uuid4().hex
    pw_hash = generate_password_hash(password)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO users (id, name, email, password_hash, is_verified, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (user_id, name.strip(), email.lower(), pw_hash, _now()),
    )
    conn.commit()
    conn.close()
    return user_id


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def delete_unverified_user(email: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM users WHERE email=? AND is_verified=0", (email.lower(),))
    conn.commit()
    conn.close()


def mark_user_verified(email: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_verified=1 WHERE email=?", (email.lower(),))
    conn.commit()
    conn.close()


def check_credentials(email: str, password: str) -> Optional[Dict[str, Any]]:
    user = get_user_by_email(email)
    if not user or not user["is_verified"]:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return user


# ---------------------------------------------------------------------- #
# OTP
# ---------------------------------------------------------------------- #
def _generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


def store_otp(email: str, purpose: str) -> str:
    code = _generate_otp()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
    ).isoformat()
    conn = sqlite3.connect(DB_PATH)
    # Invalidate any previous unused OTPs for this email + purpose
    conn.execute(
        "UPDATE otp_codes SET used=1 WHERE email=? AND purpose=? AND used=0",
        (email.lower(), purpose),
    )
    conn.execute(
        "INSERT INTO otp_codes (email, code, purpose, expires_at, used, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (email.lower(), code, purpose, expires_at, _now()),
    )
    conn.commit()
    conn.close()
    return code


def verify_otp(email: str, code: str, purpose: str) -> bool:
    now = _now()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """SELECT id FROM otp_codes
           WHERE email=? AND code=? AND purpose=? AND used=0 AND expires_at > ?
           ORDER BY id DESC LIMIT 1""",
        (email.lower(), code.strip(), purpose, now),
    )
    row = cur.fetchone()
    if row:
        conn.execute("UPDATE otp_codes SET used=1 WHERE id=?", (row[0],))
        conn.commit()
    conn.close()
    return row is not None


# ---------------------------------------------------------------------- #
# Sessions
# ---------------------------------------------------------------------- #
def create_session(user_id: str) -> str:
    token = uuid.uuid4().hex + uuid.uuid4().hex  # 64-char random token
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=SESSION_EXPIRY_HOURS)
    ).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token, user_id, expires_at, _now()),
    )
    conn.commit()
    conn.close()
    return token


def validate_session(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    now = _now()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT s.user_id, u.name, u.email
           FROM sessions s JOIN users u ON s.user_id = u.id
           WHERE s.token=? AND s.expires_at > ?""",
        (token, now),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------- #
# SMTP OTP email
# ---------------------------------------------------------------------- #
def send_otp_email(
    to_email: str, otp: str, purpose: str, name: str = ""
) -> Dict[str, Any]:
    # Dev mode: just log the OTP to console, don't require SMTP
    if os.getenv("AUTH_DEV_MODE", "false").lower() == "true":
        print(f"\n[AUTH DEV] OTP for {to_email} ({purpose}): {otp}\n")
        return {"success": True, "dev": True}

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM", smtp_user)

    if not all([smtp_host, smtp_user, smtp_pass]):
        return {
            "success": False,
            "error": (
                "SMTP not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD in .env, "
                "or set AUTH_DEV_MODE=true for development."
            ),
        }

    subject = (
        "Equity — Verify your email"
        if purpose == "register"
        else "Equity — Your login code"
    )
    greeting = f"Hi {name}," if name else "Hi,"
    action_text = (
        "complete your registration" if purpose == "register" else "sign in to your account"
    )

    plain = f"""Equity · PR Review Intelligence

{greeting}

Your verification code to {action_text}:

    {otp}

This code expires in {OTP_EXPIRY_MINUTES} minutes.
If you did not request this, you can safely ignore this email.

— Equity
"""

    html = f"""
<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:480px;margin:0 auto;
            background:#0b0d0f;border-radius:12px;overflow:hidden;">
  <div style="padding:28px 32px;background:#0b0d0f;">
    <div style="color:#e8743b;font-size:11px;letter-spacing:0.15em;text-transform:uppercase;
                margin-bottom:6px;">Equity · PR Review Intelligence</div>
    <h1 style="margin:0;font-size:22px;font-weight:500;color:#f5f4ee;">{subject}</h1>
  </div>
  <div style="background:#161a20;padding:32px;border-top:1px solid #262c35;">
    <p style="margin:0 0 20px;font-size:15px;line-height:1.6;color:#d8d6cf;">
      {greeting}<br/>Your verification code to
      <strong style="color:#f5f4ee;">{action_text}</strong>:
    </p>
    <div style="background:#1c2128;border:2px solid #e8743b;border-radius:10px;
                text-align:center;padding:28px;margin:0 0 24px;">
      <span style="font-family:monospace;font-size:40px;font-weight:700;
                   letter-spacing:0.25em;color:#e8743b;">{otp}</span>
    </div>
    <p style="font-size:13px;color:#6a6760;margin:0;">
      This code expires in {OTP_EXPIRY_MINUTES} minutes.<br/>
      If you didn't request this, ignore this email.
    </p>
  </div>
</div>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = to_email
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [to_email], msg.as_string())
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

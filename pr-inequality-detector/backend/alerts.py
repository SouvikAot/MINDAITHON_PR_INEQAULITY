"""
alerts.py
Detects sustained harsh-reviewer patterns and sends SMTP email alerts to a manager.
"""
from __future__ import annotations
import os
import smtplib
import sqlite3
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Dict, Any

from github_api import parse_iso


DB_PATH = Path(__file__).resolve().parent / "database.db"


# ----------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------
def init_alert_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reviewer TEXT NOT NULL,
            owner TEXT,
            repo TEXT,
            harsh_count INTEGER,
            day_span INTEGER,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            error TEXT
        )
    """)
    conn.commit()
    conn.close()


def was_recently_alerted(reviewer: str, cooldown_days: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
    cur = conn.execute(
        "SELECT id FROM alert_log WHERE reviewer = ? AND sent_at > ? AND status = 'sent'",
        (reviewer, cutoff),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def log_alert(reviewer: str, owner: str, repo: str, harsh_count: int,
              day_span: int, status: str, error: str = ""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO alert_log
           (reviewer, owner, repo, harsh_count, day_span, status, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (reviewer, owner, repo, harsh_count, day_span, status, error),
    )
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------
def detect_sustained_harsh_reviewers(
    prs: List[Dict[str, Any]],
    min_harsh: int = 5,
    min_days: int = 15,
) -> List[Dict[str, Any]]:
    """Find reviewers with >= min_harsh harsh comments spanning >= min_days."""
    from analytics import classify_tone

    incidents_by_reviewer: Dict[str, List[Dict[str, Any]]] = {}

    def _consider(text, reviewer, author, pr_number, pr_title, html_url, created_at):
        if not text or not text.strip() or not reviewer:
            return
        tone = classify_tone(text)
        if tone["tone"] not in ("hostile", "harsh", "dismissive"):
            return
        ts = parse_iso(created_at) if created_at else None
        incidents_by_reviewer.setdefault(reviewer, []).append({
            "pr_number": pr_number,
            "pr_title": pr_title,
            "author": author,
            "tone": tone["tone"],
            "excerpt": text.strip()[:200],
            "html_url": html_url,
            "created_at": ts,
        })

    for pr in prs:
        author = pr.get("author") or ""
        num = pr.get("number")
        title = pr.get("title", "")
        url = pr.get("html_url", "")
        for r in pr.get("reviews", []):
            _consider(r.get("body", ""), r.get("user", ""),
                      author, num, title, url, r.get("submitted_at"))
        for c in pr.get("review_comments_data", []):
            _consider(c.get("body", ""), c.get("user", ""),
                      author, num, title, url, c.get("created_at"))
        for c in pr.get("issue_comments_data", []):
            _consider(c.get("body", ""), c.get("user", ""),
                      author, num, title, url, c.get("created_at"))

    offenders = []
    for reviewer, incidents in incidents_by_reviewer.items():
        if len(incidents) < min_harsh:
            continue
        timestamps = [i["created_at"] for i in incidents if i["created_at"]]
        # Compute day span; if we have <2 timestamps, span is 0
        if len(timestamps) >= 2:
            day_span = (max(timestamps) - min(timestamps)).days
        else:
            day_span = 0
        if day_span < min_days:
            continue
        first_ts = min(timestamps).isoformat() if timestamps else "n/a"
        last_ts = max(timestamps).isoformat() if timestamps else "n/a"
        incidents.sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=timezone.utc))
        offenders.append({
            "reviewer": reviewer,
            "harsh_count": len(incidents),
            "day_span": day_span,
            "first_incident": first_ts,
            "last_incident": last_ts,
            "incidents": incidents,
        })
    offenders.sort(key=lambda x: x["harsh_count"], reverse=True)
    return offenders


# ----------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------
def _build_email_body(offender: Dict[str, Any], owner: str, repo: str):
    reviewer = offender["reviewer"]
    count = offender["harsh_count"]
    span = offender["day_span"]
    incidents = offender["incidents"][:10]

    plain = f"""Equity — Reviewer Behavior Alert

Reviewer @{reviewer} has shown a sustained pattern of harsh feedback
in {owner}/{repo} that warrants your review.

PATTERN
  - {count} harsh/hostile/dismissive comments
  - spanning {span} days
  - from {offender['first_incident'][:10]} to {offender['last_incident'][:10]}

This is an automated detection. Please verify context yourself before
taking action. The reviewer may have valid concerns expressed poorly,
or there may be an underlying interpersonal issue worth addressing.

RECENT INCIDENTS

"""
    for i, inc in enumerate(incidents, 1):
        plain += f"{i}. PR #{inc['pr_number']} ({inc['tone']})\n"
        plain += f"   author: {inc['author']}\n"
        plain += f"   said:   \"{inc['excerpt']}\"\n"
        plain += f"   link:   {inc['html_url']}\n\n"

    plain += f"""
RECOMMENDED ACTIONS

  1. Review the actual comments at the links above for context
  2. Consider a 1:1 with @{reviewer} about feedback delivery
  3. Check in with affected authors privately
  4. If the pattern is severe, involve HR

This alert was generated by Equity (PR Review Intelligence).
You are receiving it because you are configured as MANAGER_EMAIL.
"""

    rows = ""
    for inc in incidents:
        tone_color = {"hostile": "#dc2626", "harsh": "#d97706", "dismissive": "#a16207"}.get(inc["tone"], "#666")
        rows += f"""
        <tr>
          <td style="padding:10px; border-bottom:1px solid #eee; vertical-align:top;">
            <a href="{inc['html_url']}" style="color:#e8743b; text-decoration:none; font-weight:600;">#{inc['pr_number']}</a>
          </td>
          <td style="padding:10px; border-bottom:1px solid #eee; vertical-align:top;">
            <span style="background:{tone_color}; color:white; padding:2px 8px; border-radius:3px; font-size:11px; text-transform:uppercase;">{inc['tone']}</span>
          </td>
          <td style="padding:10px; border-bottom:1px solid #eee; vertical-align:top;">
            <em style="color:#444;">"{inc['excerpt']}"</em>
            <div style="color:#888; font-size:12px; margin-top:4px;">to @{inc['author']}</div>
          </td>
        </tr>
        """

    html = f"""
    <div style="font-family:-apple-system,Segoe UI,sans-serif; max-width:680px; margin:0 auto; color:#222;">
      <div style="background:#0b0d0f; color:#f5f4ee; padding:32px; border-radius:6px 6px 0 0;">
        <div style="color:#e8743b; font-size:11px; letter-spacing:0.15em; text-transform:uppercase; margin-bottom:8px;">Equity · Reviewer Behavior Alert</div>
        <h1 style="margin:0; font-size:26px; font-weight:500;">A reviewer needs your attention</h1>
      </div>
      <div style="background:white; padding:32px; border:1px solid #eee; border-top:none; border-radius:0 0 6px 6px;">
        <p style="font-size:15px; line-height:1.6; margin:0 0 24px 0;">
          Reviewer <strong>@{reviewer}</strong> in <strong>{owner}/{repo}</strong> has shown a sustained pattern of harsh feedback that warrants your attention.
        </p>
        <div style="background:#fef3c7; border-left:4px solid #d97706; padding:16px 20px; border-radius:4px; margin-bottom:24px;">
          <div style="font-weight:600; margin-bottom:8px;">The pattern</div>
          <ul style="margin:0; padding-left:20px; line-height:1.8;">
            <li><strong>{count}</strong> harsh/hostile/dismissive comments</li>
            <li>Spanning <strong>{span} days</strong></li>
            <li>From {offender['first_incident'][:10]} to {offender['last_incident'][:10]}</li>
          </ul>
        </div>
        <p style="font-size:14px; color:#555; line-height:1.6; margin-bottom:24px;">
          This is an automated detection. Please verify the context yourself before taking action.
        </p>
        <h3 style="font-size:13px; letter-spacing:0.1em; text-transform:uppercase; color:#888; margin:32px 0 12px 0;">Recent incidents</h3>
        <table style="width:100%; border-collapse:collapse; font-size:13px;">
          <thead><tr style="background:#fafafa;">
            <th style="padding:10px; text-align:left; font-size:11px; text-transform:uppercase; color:#666;">PR</th>
            <th style="padding:10px; text-align:left; font-size:11px; text-transform:uppercase; color:#666;">Tone</th>
            <th style="padding:10px; text-align:left; font-size:11px; text-transform:uppercase; color:#666;">Comment</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <h3 style="font-size:13px; letter-spacing:0.1em; text-transform:uppercase; color:#888; margin:32px 0 12px 0;">Recommended actions</h3>
        <ol style="line-height:1.8; color:#444;">
          <li>Review the actual comments at the links above for full context</li>
          <li>Consider a 1:1 with @{reviewer} about feedback delivery</li>
          <li>Check in with affected authors privately</li>
          <li>If the pattern is severe, involve HR</li>
        </ol>
        <div style="margin-top:32px; padding-top:20px; border-top:1px solid #eee; color:#999; font-size:12px;">
          Generated automatically by Equity (PR Review Intelligence).
        </div>
      </div>
    </div>
    """
    return plain, html


def send_alert_email(offender: Dict[str, Any], owner: str, repo: str) -> Dict[str, Any]:
    if os.getenv("ALERT_EMAIL_ENABLED", "false").lower() != "true":
        return {"success": False, "error": "ALERT_EMAIL_ENABLED is not true"}

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM", smtp_user)
    manager = os.getenv("MANAGER_EMAIL")

    if not all([smtp_host, smtp_user, smtp_pass, manager]):
        return {"success": False, "error": "SMTP credentials or MANAGER_EMAIL not configured"}

    plain_body, html_body = _build_email_body(offender, owner, repo)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Equity Alert] Reviewer @{offender['reviewer']} — sustained harsh feedback in {owner}/{repo}"
    msg["From"] = smtp_from
    msg["To"] = manager
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [manager], msg.as_string())
        return {"success": True, "error": ""}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ----------------------------------------------------------------------
# Top-level
# ----------------------------------------------------------------------
def process_alerts_for_analysis(prs: List[Dict[str, Any]],
                                owner: str, repo: str) -> Dict[str, Any]:
    init_alert_table()
    if os.getenv("ALERT_EMAIL_ENABLED", "false").lower() != "true":
        return {"enabled": False, "offenders_found": 0, "alerts_sent": 0}

    min_harsh = int(os.getenv("ALERT_MIN_HARSH_COMMENTS", "5"))
    min_days = int(os.getenv("ALERT_MIN_DAYS_SPAN", "15"))
    cooldown = int(os.getenv("ALERT_COOLDOWN_DAYS", "30"))

    offenders = detect_sustained_harsh_reviewers(prs, min_harsh, min_days)
    sent = skipped = failed = 0
    results = []

    for off in offenders:
        if was_recently_alerted(off["reviewer"], cooldown):
            log_alert(off["reviewer"], owner, repo, off["harsh_count"],
                      off["day_span"], "skipped_cooldown", "")
            skipped += 1
            results.append({"reviewer": off["reviewer"], "status": "skipped_cooldown"})
            continue
        result = send_alert_email(off, owner, repo)
        if result["success"]:
            log_alert(off["reviewer"], owner, repo, off["harsh_count"],
                      off["day_span"], "sent", "")
            sent += 1
            results.append({"reviewer": off["reviewer"], "status": "sent"})
            print(f"[ALERT SENT] reviewer=@{off['reviewer']} count={off['harsh_count']} days={off['day_span']}")
        else:
            log_alert(off["reviewer"], owner, repo, off["harsh_count"],
                      off["day_span"], "failed", result["error"])
            failed += 1
            results.append({"reviewer": off["reviewer"], "status": "failed",
                            "error": result["error"]})
            print(f"[ALERT FAILED] reviewer=@{off['reviewer']} error={result['error']}")

    return {
        "enabled": True,
        "offenders_found": len(offenders),
        "alerts_sent": sent,
        "alerts_skipped": skipped,
        "alerts_failed": failed,
        "results": results,
    }
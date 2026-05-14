"""
app.py
Flask backend that serves the frontend and the analytics API.
"""
from __future__ import annotations
from alerts import process_alerts_for_analysis
import os
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

import hashlib
import hmac
import urllib.parse

from flask import Flask, request, jsonify, send_from_directory, abort, g, Response, stream_with_context
from flask_cors import CORS
from alerts import process_alerts_for_analysis, send_alert_email, init_alert_table
import auth as _auth

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from github_api import GitHubAPI
from analytics import run_full_analytics
from ai import chat_with_analytics, ai_enhance_review_quality, ai_status


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DB_PATH = BACKEND_DIR / "database.db"


# ---------------------------------------------------------------------- #
# Database
# ---------------------------------------------------------------------- #
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    _auth.init_auth_tables()
    with _db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            repo TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            error TEXT,
            payload TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS saved_repos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            repo TEXT NOT NULL,
            label TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, owner, repo)
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS analysis_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            repo TEXT NOT NULL,
            analysis_id TEXT NOT NULL,
            health_score REAL,
            fairness_score REAL,
            psych_safety_score REAL,
            high_risk_count INTEGER,
            stale_count INTEGER,
            pr_count INTEGER,
            snapshot_at TEXT NOT NULL
        )
        """)
        c.commit()


def _save_analysis(row: Dict[str, Any]) -> None:
    with _db() as c:
        c.execute("""
        INSERT INTO analyses (id, owner, repo, status, progress, total,
                              error, payload, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status,
            progress=excluded.progress,
            total=excluded.total,
            error=excluded.error,
            payload=excluded.payload,
            updated_at=excluded.updated_at
        """, (
            row["id"], row["owner"], row["repo"], row["status"],
            row.get("progress", 0), row.get("total", 0),
            row.get("error"), row.get("payload"),
            row["created_at"], row["updated_at"],
        ))
        c.commit()


def _load_analysis(analysis_id: str) -> Optional[Dict[str, Any]]:
    with _db() as c:
        cur = c.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def _save_chat_message(analysis_id: str, role: str, content: str) -> None:
    with _db() as c:
        c.execute("""
        INSERT INTO chat_messages (analysis_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
        """, (analysis_id, role, content, datetime.now(timezone.utc).isoformat()))
        c.commit()


def _get_chat_history(analysis_id: str, limit: int = 12) -> list:
    with _db() as c:
        cur = c.execute("""
            SELECT role, content FROM chat_messages
            WHERE analysis_id = ?
            ORDER BY id DESC LIMIT ?
        """, (analysis_id, limit))
        rows = [dict(r) for r in cur.fetchall()]
        return list(reversed(rows))


# ---------------------------------------------------------------------- #
# Flask app
# ---------------------------------------------------------------------- #
app = Flask(__name__, static_folder=None)
CORS(app)

# In-memory progress tracker (mirror of DB for fast polling)
_progress_lock = threading.Lock()
_progress: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------- #
# Auth middleware — protects all /api/* routes except /api/auth/* and /api/health
# ---------------------------------------------------------------------- #
@app.before_request
def require_auth():
    if request.method == "OPTIONS":
        return
    path = request.path
    if not path.startswith("/api/"):
        return
    if path.startswith("/api/auth/") or path in ("/api/health", "/api/webhook/github"):
        return
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    user = _auth.validate_session(token)
    if not user:
        return jsonify({"error": "Authentication required", "code": "AUTH_REQUIRED"}), 401
    g.current_user = user


# ---------------------------------------------------------------------- #
# Auth routes
# ---------------------------------------------------------------------- #
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(silent=True) or {}
    name     = (data.get("name") or "").strip()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name or not email or not password:
        return jsonify({"error": "Name, email, and password are required."}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    existing = _auth.get_user_by_email(email)
    if existing and existing["is_verified"]:
        return jsonify({"error": "An account with this email already exists. Please sign in."}), 409

    # Replace any stale unverified record so the user can retry
    if existing and not existing["is_verified"]:
        _auth.delete_unverified_user(email)

    _auth.create_user(name, email, password)
    code   = _auth.store_otp(email, "register")
    result = _auth.send_otp_email(email, code, "register", name)

    if not result["success"]:
        return jsonify({"error": f"Could not send verification email: {result['error']}"}), 500

    return jsonify({"message": "Verification code sent to your email.", "email": email})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password are required."}), 400

    user = _auth.check_credentials(email, password)
    if not user:
        return jsonify({"error": "Invalid email or password."}), 401

    code   = _auth.store_otp(email, "login")
    result = _auth.send_otp_email(email, code, "login", user["name"])

    if not result["success"]:
        return jsonify({"error": f"Could not send login code: {result['error']}"}), 500

    return jsonify({"message": "Login code sent to your email.", "email": email})


@app.route("/api/auth/verify-otp", methods=["POST"])
def auth_verify_otp():
    data    = request.get_json(silent=True) or {}
    email   = (data.get("email") or "").strip().lower()
    code    = (data.get("code") or "").strip()
    purpose = (data.get("purpose") or "").strip()

    if not email or not code or purpose not in ("register", "login"):
        return jsonify({"error": "email, code, and purpose (register|login) are required."}), 400

    if not _auth.verify_otp(email, code, purpose):
        return jsonify({"error": "Invalid or expired verification code."}), 400

    if purpose == "register":
        _auth.mark_user_verified(email)

    user = _auth.get_user_by_email(email)
    if not user:
        return jsonify({"error": "User not found."}), 400

    token = _auth.create_session(user["id"])
    return jsonify({
        "token": token,
        "user": {"name": user["name"], "email": user["email"]},
    })


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if token:
        _auth.delete_session(token)
    return jsonify({"message": "Logged out."})


@app.route("/api/auth/me")
def auth_me():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    user  = _auth.validate_session(token)
    if not user:
        return jsonify({"error": "Not authenticated."}), 401
    return jsonify({"user": {"name": user["name"], "email": user["email"]}})


# ---------------------------------------------------------------------- #
# Static frontend
# ---------------------------------------------------------------------- #
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "landing.html")


@app.route("/app")
def app_page():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:path>")
def static_proxy(path):
    target = FRONTEND_DIR / path
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(FRONTEND_DIR, path)


# ---------------------------------------------------------------------- #
# Health + status
# ---------------------------------------------------------------------- #
@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "ai": ai_status(),
        "github_token_configured": bool(os.getenv("GITHUB_TOKEN")),
    })


# ---------------------------------------------------------------------- #
# Snapshot helper
# ---------------------------------------------------------------------- #
def _save_snapshot(owner: str, repo: str, analysis_id: str,
                   summary: Dict[str, Any]) -> None:
    with _db() as c:
        c.execute("""
        INSERT INTO analysis_snapshots
            (owner, repo, analysis_id, health_score, fairness_score,
             psych_safety_score, high_risk_count, stale_count, pr_count, snapshot_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            owner, repo, analysis_id,
            summary.get("team_health_score"),
            summary.get("fairness_score"),
            summary.get("psych_safety_score"),
            summary.get("high_risk_count"),
            summary.get("stale_count"),
            summary.get("total_prs"),
            datetime.now(timezone.utc).isoformat(),
        ))
        c.commit()


# ---------------------------------------------------------------------- #
# Background analysis worker
# ---------------------------------------------------------------------- #
def _run_analysis_job(analysis_id: str, owner: str, repo: str, max_prs: int,
                      label_filter: Optional[list] = None,
                      risk_weights: Optional[Dict[str, Any]] = None) -> None:
    now = lambda: datetime.now(timezone.utc).isoformat()
    try:
        gh = GitHubAPI()
        try:
            gh.validate_token()
        except Exception as e:
            with _progress_lock:
                _progress[analysis_id] = {"status": "error", "error": str(e),
                                          "progress": 0, "total": 0}
            _save_analysis({
                "id": analysis_id, "owner": owner, "repo": repo,
                "status": "error", "error": str(e),
                "progress": 0, "total": 0, "payload": None,
                "created_at": now(), "updated_at": now(),
            })
            return

        gh.get_repo_info(owner, repo)

        def progress_cb(idx: int, total: int, rate_limit: Optional[Dict] = None):
            with _progress_lock:
                _progress[analysis_id] = {
                    "status": "fetching", "progress": idx, "total": total,
                    "error": None, "rate_limit": rate_limit or {},
                }

        with _progress_lock:
            _progress[analysis_id] = {"status": "fetching", "progress": 0,
                                       "total": max_prs, "error": None, "rate_limit": {}}

        prs = gh.fetch_full_pr_dataset(owner, repo, max_prs=max_prs,
                                       progress=progress_cb)

        with _progress_lock:
            _progress[analysis_id] = {"status": "analyzing",
                                       "progress": len(prs),
                                       "total": len(prs), "error": None,
                                       "rate_limit": gh.get_rate_limit_status()}

        analytics = run_full_analytics(prs, label_filter=label_filter,
                                       risk_weights=risk_weights)

        # AI enhancement of a sample (best-effort, won't crash analysis)
        try:
            samples = analytics["quality"].get("samples", [])
            if samples:
                analytics["quality"]["ai_enhanced_samples"] = \
                    ai_enhance_review_quality(samples, max_samples=10)
        except Exception:
            analytics["quality"]["ai_enhanced_samples"] = []

        analytics["meta"] = {
            "owner": owner, "repo": repo,
            "fetched_at": now(),
            "pr_count": len(prs),
            "ai": ai_status(),
        }

        # ----- Manager alerts: detect sustained harsh patterns and email -----
        try:
            alert_result = process_alerts_for_analysis(prs, owner, repo)
            analytics["alerts"] = alert_result
            if alert_result.get("alerts_sent", 0) > 0:
                print(f"[ALERTS] Dispatched {alert_result['alerts_sent']} email(s) for {owner}/{repo}")
            elif alert_result.get("offenders_found", 0) > 0:
                print(f"[ALERTS] {alert_result['offenders_found']} offender(s) found, "
                      f"{alert_result.get('alerts_skipped', 0)} skipped (cooldown), "
                      f"{alert_result.get('alerts_failed', 0)} failed")
        except Exception as e:
            print(f"[ALERTS] Error: {e}")
            analytics["alerts"] = {"enabled": True, "error": str(e)}

        _save_analysis({
            "id": analysis_id, "owner": owner, "repo": repo,
            "status": "complete", "error": None,
            "progress": len(prs), "total": len(prs),
            "payload": json.dumps(analytics, default=str),
            "created_at": now(), "updated_at": now(),
        })
        try:
            _save_snapshot(owner, repo, analysis_id, analytics.get("summary", {}))
        except Exception:
            pass
        with _progress_lock:
            _progress[analysis_id] = {
                "status": "complete", "progress": len(prs),
                "total": len(prs), "error": None,
                "rate_limit": gh.get_rate_limit_status(),
            }
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        with _progress_lock:
            _progress[analysis_id] = {"status": "error", "error": err,
                                       "progress": 0, "total": 0}
        _save_analysis({
            "id": analysis_id, "owner": owner, "repo": repo,
            "status": "error", "error": err,
            "progress": 0, "total": 0, "payload": None,
            "created_at": now(), "updated_at": now(),
        })


# ---------------------------------------------------------------------- #
# API: start analysis
# ---------------------------------------------------------------------- #
@app.route("/api/analyze", methods=["POST"])
def start_analysis():
    if not os.getenv("GITHUB_TOKEN"):
        return jsonify({"error": "GITHUB_TOKEN is not configured on the server. "
                                  "Set it in the .env file and restart."}), 400
    data = request.get_json(silent=True) or {}
    repo_full = (data.get("repo") or "").strip()
    max_prs = int(data.get("max_prs") or 30)
    max_prs = max(5, min(max_prs, 100))
    label_filter = data.get("labels") or None  # list of label strings or None
    risk_weights = data.get("risk_weights") or None  # dict or None

    if isinstance(label_filter, list) and not label_filter:
        label_filter = None

    if "/" not in repo_full:
        return jsonify({"error": "Repository must be in the form 'owner/repo'."}), 400
    owner, repo = repo_full.split("/", 1)
    owner, repo = owner.strip(), repo.strip()
    if not owner or not repo:
        return jsonify({"error": "Repository must be in the form 'owner/repo'."}), 400

    analysis_id = uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc).isoformat()
    _save_analysis({
        "id": analysis_id, "owner": owner, "repo": repo,
        "status": "queued", "error": None,
        "progress": 0, "total": max_prs, "payload": None,
        "created_at": now, "updated_at": now,
    })
    with _progress_lock:
        _progress[analysis_id] = {"status": "queued", "progress": 0,
                                   "total": max_prs, "error": None, "rate_limit": {}}
    t = threading.Thread(target=_run_analysis_job,
                         args=(analysis_id, owner, repo, max_prs),
                         kwargs={"label_filter": label_filter, "risk_weights": risk_weights},
                         daemon=True)
    t.start()
    return jsonify({"analysis_id": analysis_id,
                    "owner": owner, "repo": repo, "max_prs": max_prs})


# ---------------------------------------------------------------------- #
# API: poll progress
# ---------------------------------------------------------------------- #
@app.route("/api/analyze/<analysis_id>/status")
def analysis_status(analysis_id):
    with _progress_lock:
        p = _progress.get(analysis_id)
    if p is None:
        row = _load_analysis(analysis_id)
        if row is None:
            return jsonify({"error": "unknown analysis id"}), 404
        return jsonify({
            "status": row["status"],
            "progress": row["progress"],
            "total": row["total"],
            "error": row["error"],
        })
    return jsonify(p)


# ---------------------------------------------------------------------- #
# API: results
# ---------------------------------------------------------------------- #
@app.route("/api/analyze/<analysis_id>/results")
def analysis_results(analysis_id):
    row = _load_analysis(analysis_id)
    if row is None:
        return jsonify({"error": "unknown analysis id"}), 404
    if row["status"] != "complete" or not row["payload"]:
        return jsonify({"status": row["status"], "error": row["error"]}), 202
    payload = json.loads(row["payload"])
    payload["analysis_id"] = analysis_id
    return jsonify(payload)


# ---------------------------------------------------------------------- #
# API: chat
# ---------------------------------------------------------------------- #
@app.route("/api/chat/<analysis_id>", methods=["POST"])
def chat(analysis_id):
    row = _load_analysis(analysis_id)
    if row is None or row["status"] != "complete" or not row["payload"]:
        return jsonify({"error": "Analysis not ready yet."}), 400

    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    payload = json.loads(row["payload"])
    history = _get_chat_history(analysis_id, limit=20)
    repo_label = f"{row['owner']}/{row['repo']}"

    result = chat_with_analytics(question, payload,
                                 repo_label=repo_label, history=history)
    _save_chat_message(analysis_id, "user", question)
    _save_chat_message(analysis_id, "assistant", result["answer"])
    return jsonify(result)


@app.route("/api/chat/<analysis_id>/history")
def chat_history(analysis_id):
    history = _get_chat_history(analysis_id, limit=50)
    return jsonify({"history": history})


# ---------------------------------------------------------------------- #
# API: list previous analyses
# ---------------------------------------------------------------------- #
@app.route("/api/analyses")
def list_analyses():
    with _db() as c:
        cur = c.execute("""
            SELECT id, owner, repo, status, created_at, updated_at
            FROM analyses
            ORDER BY datetime(created_at) DESC LIMIT 20
        """)
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"analyses": rows})


# ---------------------------------------------------------------------- #
# API: trend data for a repo
# ---------------------------------------------------------------------- #
@app.route("/api/repos/<owner>/<repo>/trend")
def repo_trend(owner, repo):
    with _db() as c:
        cur = c.execute("""
            SELECT analysis_id, health_score, fairness_score, psych_safety_score,
                   high_risk_count, stale_count, pr_count, snapshot_at
            FROM analysis_snapshots
            WHERE owner = ? AND repo = ?
            ORDER BY datetime(snapshot_at) ASC
            LIMIT 30
        """, (owner, repo))
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"owner": owner, "repo": repo, "snapshots": rows})


# ---------------------------------------------------------------------- #
# API: export analysis as Markdown
# ---------------------------------------------------------------------- #
@app.route("/api/analyze/<analysis_id>/export")
def export_analysis(analysis_id):
    row = _load_analysis(analysis_id)
    if row is None:
        return jsonify({"error": "unknown analysis id"}), 404
    if row["status"] != "complete" or not row["payload"]:
        return jsonify({"error": "Analysis not ready"}), 400
    data = json.loads(row["payload"])
    md = _build_markdown_report(row["owner"], row["repo"], data)
    return Response(md, mimetype="text/markdown",
                    headers={"Content-Disposition":
                             f'attachment; filename="{row["owner"]}_{row["repo"]}_equity.md"'})


def _build_markdown_report(owner: str, repo: str, data: Dict[str, Any]) -> str:
    s = data.get("summary", {})
    ineq = data.get("inequality", {})
    delays = data.get("delays", {})
    risk = data.get("risk", {})
    emotional = data.get("emotional", {})
    sla = data.get("sla", {})

    lines = [
        f"# Equity Report — {owner}/{repo}",
        f"",
        f"Generated: {data.get('meta', {}).get('fetched_at', 'unknown')}",
        f"PRs analyzed: {s.get('total_prs', 0)}",
        f"",
        f"---",
        f"",
        f"## Team Health Score: {s.get('team_health_score', '—')}/100",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Fairness Score | {s.get('fairness_score', '—')} |",
        f"| Psych Safety | {s.get('psych_safety_score', '—')} |",
        f"| High Risk PRs | {s.get('high_risk_count', 0)} |",
        f"| Stale PRs | {s.get('stale_count', 0)} |",
        f"| Unique Reviewers | {s.get('unique_reviewers', 0)} |",
        f"| Unique Authors | {s.get('unique_authors', 0)} |",
        f"",
        f"---",
        f"",
        f"## Reviewer Inequality (Gini: {ineq.get('gini', '—')})",
        f"",
    ]
    for r in (ineq.get("distribution") or [])[:10]:
        lines.append(f"- **{r['reviewer']}**: {r['review_count']} reviews")
    if ineq.get("overloaded"):
        lines += ["", "### Overloaded Reviewers"]
        for r in ineq["overloaded"]:
            lines.append(f"- {r['reviewer']} — {r['share_pct']}% of all reviews")

    lines += [
        "",
        "---",
        "",
        f"## PR Delays",
        f"",
        f"- Median first review: {delays.get('first_review_stats', {}).get('median', '—')}h",
        f"- P90 first review: {delays.get('first_review_stats', {}).get('p90', '—')}h",
        f"- Stale PRs: {len(delays.get('stale_prs', []))}",
        "",
        "---",
        "",
        f"## Risk Summary",
        f"",
        f"- High risk: {risk.get('summary', {}).get('high', 0)}",
        f"- Medium risk: {risk.get('summary', {}).get('medium', 0)}",
        f"- Low risk: {risk.get('summary', {}).get('low', 0)}",
        "",
    ]
    top_risk = [p for p in (risk.get("prs") or []) if p.get("risk_level") == "high"][:5]
    if top_risk:
        lines.append("### Top High-Risk PRs")
        for p in top_risk:
            lines.append(f"- [#{p['number']}]({p.get('html_url', '')}) {p['title']} (score: {p['risk_score']})")
            for reason in p.get("reasons", []):
                lines.append(f"  - {reason}")

    lines += [
        "",
        "---",
        "",
        f"## Psychological Safety Score: {emotional.get('team_psych_safety_score', '—')}/100",
        "",
    ]
    for rv in (emotional.get("reviewer_ei") or [])[:8]:
        lines.append(f"- {rv['reviewer']}: EI score {rv['ei_score']}")

    if sla.get("reviewers"):
        lines += [
            "",
            "---",
            "",
            f"## Reviewer SLA Compliance ({sla.get('sla_hours', 24)}h SLA)",
            f"Overall: {sla.get('overall_compliance_pct', '—')}%",
            "",
        ]
        for rv in sla["reviewers"][:8]:
            lines.append(
                f"- {rv['reviewer']}: {rv['compliance_pct']}% "
                f"({rv['sla_met']}/{rv['total_reviews']})"
            )

    lines += ["", "---", "", "*Generated by Equity — PR Review Intelligence*"]
    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# API: saved repos
# ---------------------------------------------------------------------- #
@app.route("/api/saved-repos", methods=["GET"])
def list_saved_repos():
    user_id = g.current_user["user_id"]
    with _db() as c:
        cur = c.execute(
            "SELECT id, owner, repo, label, created_at FROM saved_repos WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        )
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"saved_repos": rows})


@app.route("/api/saved-repos", methods=["POST"])
def add_saved_repo():
    user_id = g.current_user["user_id"]
    data = request.get_json(silent=True) or {}
    repo_full = (data.get("repo") or "").strip()
    label = (data.get("label") or "").strip() or None
    if "/" not in repo_full:
        return jsonify({"error": "repo must be owner/repo"}), 400
    owner, repo = repo_full.split("/", 1)
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO saved_repos (user_id, owner, repo, label, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, owner.strip(), repo.strip(), label,
                 datetime.now(timezone.utc).isoformat())
            )
            c.commit()
    except Exception:
        return jsonify({"error": "Already saved"}), 409
    return jsonify({"message": "Saved"}), 201


@app.route("/api/saved-repos/<int:repo_id>", methods=["DELETE"])
def delete_saved_repo(repo_id):
    user_id = g.current_user["user_id"]
    with _db() as c:
        c.execute("DELETE FROM saved_repos WHERE id = ? AND user_id = ?", (repo_id, user_id))
        c.commit()
    return jsonify({"message": "Deleted"})


# ---------------------------------------------------------------------- #
# API: webhook (GitHub)
# ---------------------------------------------------------------------- #
@app.route("/api/webhook/github", methods=["POST"])
def github_webhook():
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            secret.encode(), request.data, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return jsonify({"error": "Invalid signature"}), 401

    event = request.headers.get("X-GitHub-Event", "")
    payload = request.get_json(silent=True) or {}
    action = payload.get("action", "")

    if event == "pull_request" and action in ("opened", "closed", "reopened", "synchronize"):
        repo_obj = payload.get("repository", {})
        owner = (repo_obj.get("owner") or {}).get("login", "")
        repo = repo_obj.get("name", "")
        if owner and repo:
            analysis_id = uuid.uuid4().hex[:16]
            now = datetime.now(timezone.utc).isoformat()
            _save_analysis({
                "id": analysis_id, "owner": owner, "repo": repo,
                "status": "queued", "error": None,
                "progress": 0, "total": 30, "payload": None,
                "created_at": now, "updated_at": now,
            })
            with _progress_lock:
                _progress[analysis_id] = {"status": "queued", "progress": 0,
                                           "total": 30, "error": None, "rate_limit": {}}
            t = threading.Thread(target=_run_analysis_job,
                                 args=(analysis_id, owner, repo, 30), daemon=True)
            t.start()
            return jsonify({"analysis_id": analysis_id, "triggered": True})

    return jsonify({"received": True, "triggered": False})


# ---------------------------------------------------------------------- #
# API: multi-repo comparison
# ---------------------------------------------------------------------- #
@app.route("/api/compare", methods=["POST"])
def compare_repos():
    if not os.getenv("GITHUB_TOKEN"):
        return jsonify({"error": "GITHUB_TOKEN not configured"}), 400
    data = request.get_json(silent=True) or {}
    repos = data.get("repos") or []
    max_prs = max(5, min(int(data.get("max_prs") or 20), 50))

    if len(repos) < 2:
        return jsonify({"error": "Provide at least 2 repos"}), 400
    if len(repos) > 4:
        return jsonify({"error": "Max 4 repos for comparison"}), 400

    results = []
    for repo_full in repos:
        if "/" not in repo_full:
            continue
        owner, repo = repo_full.strip().split("/", 1)
        try:
            gh = GitHubAPI()
            prs = gh.fetch_full_pr_dataset(owner.strip(), repo.strip(), max_prs=max_prs)
            analytics = run_full_analytics(prs)
            results.append({
                "repo": repo_full,
                "owner": owner.strip(),
                "name": repo.strip(),
                "pr_count": len(prs),
                "summary": analytics["summary"],
                "gini": analytics["inequality"]["gini"],
                "top_reviewers": analytics["inequality"]["distribution"][:5],
                "risk_summary": analytics["risk"]["summary"],
                "sla_compliance": analytics["sla"]["overall_compliance_pct"],
            })
        except Exception as e:
            results.append({"repo": repo_full, "error": str(e)})

    return jsonify({"comparison": results})


# ---------------------------------------------------------------------- #
# API: SSE streaming chat
# ---------------------------------------------------------------------- #
@app.route("/api/chat/<analysis_id>/stream", methods=["GET"])
def chat_stream(analysis_id):
    question = request.args.get("q", "").strip()
    if not question:
        return jsonify({"error": "q param required"}), 400

    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not _auth.validate_session(token):
        return jsonify({"error": "Authentication required"}), 401

    row = _load_analysis(analysis_id)
    if row is None or row["status"] != "complete" or not row["payload"]:
        return jsonify({"error": "Analysis not ready"}), 400

    payload = json.loads(row["payload"])
    history = _get_chat_history(analysis_id, limit=20)
    repo_label = f"{row['owner']}/{row['repo']}"

    from ai import chat_with_analytics_stream
    _save_chat_message(analysis_id, "user", question)

    def generate():
        full_answer = []
        try:
            for chunk in chat_with_analytics_stream(question, payload, repo_label, history):
                full_answer.append(chunk)
                yield f"data: {json.dumps({'token': chunk})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if full_answer:
                _save_chat_message(analysis_id, "assistant", "".join(full_answer))
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------- #
# API: Google OAuth
# ---------------------------------------------------------------------- #
@app.route("/api/auth/google")
def google_oauth_start():
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    if not client_id:
        return jsonify({"error": "Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env"}), 501
    state = uuid.uuid4().hex
    redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI",
                              f"http://localhost:{os.getenv('PORT', '5000')}/api/auth/google/callback")
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
    })
    return jsonify({"url": f"https://accounts.google.com/o/oauth2/v2/auth?{params}"})


@app.route("/api/auth/google/callback")
def google_oauth_callback():
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    code = request.args.get("code", "")

    if not client_id or not client_secret:
        return jsonify({"error": "Google OAuth not configured"}), 501
    if not code:
        return jsonify({"error": "Missing authorization code"}), 400

    import requests as req_lib

    redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI",
                              f"http://localhost:{os.getenv('PORT', '5000')}/api/auth/google/callback")

    # Exchange authorization code for tokens
    token_resp = req_lib.post("https://oauth2.googleapis.com/token", data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=10)

    if not token_resp.ok:
        return jsonify({"error": f"Failed to get access token: {token_resp.text}"}), 400

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return jsonify({"error": "No access token returned from Google"}), 400

    # Fetch user profile from Google
    user_resp = req_lib.get("https://www.googleapis.com/oauth2/v2/userinfo",
                             headers={"Authorization": f"Bearer {access_token}"},
                             timeout=10)
    if not user_resp.ok:
        return jsonify({"error": "Failed to get Google user info"}), 400

    google_user = user_resp.json()
    email = google_user.get("email", "")
    name = google_user.get("name") or google_user.get("given_name", "Google User")

    if not email:
        return jsonify({"error": "Google account has no email address"}), 400

    existing = _auth.get_user_by_email(email)
    if not existing:
        _auth.create_user(name, email, uuid.uuid4().hex)
        _auth.mark_user_verified(email)
    elif not existing["is_verified"]:
        _auth.mark_user_verified(email)

    user = _auth.get_user_by_email(email)
    session_token = _auth.create_session(user["id"])

    # Redirect to the app page with token
    frontend_url = os.getenv("FRONTEND_URL", "/app")
    redirect_url = f"{frontend_url}?oauth_token={session_token}&name={urllib.parse.quote(name)}"
    from flask import redirect as flask_redirect
    return flask_redirect(redirect_url)


# ---------------------------------------------------------------------- #
# Entry point
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "5000"))
    print(f"\n  PR Inequality Detector running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
else:
    init_db()

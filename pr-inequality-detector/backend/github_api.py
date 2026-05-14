"""
github_api.py
GitHub REST API integration for fetching real PR data.
"""
import os
import time
import requests
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional


class GitHubAPI:
    """Wrapper around the GitHub REST API with rate-limit awareness."""

    BASE_URL = "https://api.github.com"

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self.session = requests.Session()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PR-Inequality-Detector/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers.update(headers)
        self._rate_limit: Dict[str, Any] = {
            "remaining": None, "limit": None, "reset": None, "used": None
        }

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Single request that respects GitHub rate limits."""
        for attempt in range(4):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            # Track rate-limit headers on every response
            if "X-RateLimit-Remaining" in resp.headers:
                self._rate_limit = {
                    "remaining": int(resp.headers.get("X-RateLimit-Remaining", 0)),
                    "limit":     int(resp.headers.get("X-RateLimit-Limit", 5000)),
                    "reset":     int(resp.headers.get("X-RateLimit-Reset", 0)),
                    "used":      int(resp.headers.get("X-RateLimit-Used", 0)),
                }
            # Hard rate-limit hit
            if resp.status_code == 403 and self._rate_limit.get("remaining") == 0:
                reset = self._rate_limit.get("reset") or int(time.time() + 30)
                wait = max(2, min(60, reset - int(time.time())))
                time.sleep(wait)
                continue
            # Secondary rate-limit / abuse detection
            if resp.status_code in (429, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            return resp
        return resp

    def get_rate_limit_status(self) -> Dict[str, Any]:
        """Return current rate-limit snapshot with ETA until reset."""
        rl = dict(self._rate_limit)
        reset = rl.get("reset")
        if reset:
            seconds_left = max(0, reset - int(time.time()))
            rl["seconds_until_reset"] = seconds_left
            rl["eta_minutes"] = round(seconds_left / 60, 1)
        return rl

    def _paginated(self, url: str, params: Optional[Dict] = None,
                   max_pages: int = 10) -> List[Dict[str, Any]]:
        """Iterate through paginated GitHub responses."""
        results: List[Dict[str, Any]] = []
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        while page <= max_pages:
            params["page"] = page
            resp = self._request("GET", url, params=params)
            if resp.status_code != 200:
                break
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            results.extend(data)
            if len(data) < params["per_page"]:
                break
            page += 1
        return results

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #
    def validate_token(self) -> Dict[str, Any]:
        """Return user info if token is valid, otherwise raise."""
        resp = self._request("GET", f"{self.BASE_URL}/user")
        if resp.status_code != 200:
            raise RuntimeError(
                f"GitHub token invalid or missing scopes (status {resp.status_code}). "
                "Make sure the token has 'repo' and 'read:org' scopes."
            )
        return resp.json()

    def get_repo_info(self, owner: str, repo: str) -> Dict[str, Any]:
        resp = self._request("GET", f"{self.BASE_URL}/repos/{owner}/{repo}")
        if resp.status_code == 404:
            raise RuntimeError(f"Repository {owner}/{repo} not found or no access.")
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to load repo (status {resp.status_code}).")
        return resp.json()

    def list_pull_requests(self, owner: str, repo: str, state: str = "all",
                           max_prs: int = 50) -> List[Dict[str, Any]]:
        """Return up to `max_prs` pull requests, newest first."""
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/pulls"
        params = {"state": state, "sort": "updated", "direction": "desc"}
        prs = self._paginated(url, params=params,
                              max_pages=max(1, (max_prs // 100) + 1))
        return prs[:max_prs]

    def get_pr_reviews(self, owner: str, repo: str,
                       pr_number: int) -> List[Dict[str, Any]]:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        return self._paginated(url, max_pages=3)

    def get_pr_review_comments(self, owner: str, repo: str,
                               pr_number: int) -> List[Dict[str, Any]]:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        return self._paginated(url, max_pages=3)

    def get_pr_issue_comments(self, owner: str, repo: str,
                              pr_number: int) -> List[Dict[str, Any]]:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        return self._paginated(url, max_pages=3)

    def get_pr_files(self, owner: str, repo: str,
                     pr_number: int) -> List[Dict[str, Any]]:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        return self._paginated(url, max_pages=2)

    # ------------------------------------------------------------------ #
    # Aggregator: full PR data for analytics
    # ------------------------------------------------------------------ #
    def fetch_full_pr_dataset(self, owner: str, repo: str,
                              max_prs: int = 30,
                              progress=None) -> List[Dict[str, Any]]:
        """
        Fetch a rich dataset for analytics. For each PR we collect:
          - core PR metadata
          - reviews (with reviewer + state + submitted_at)
          - review comments + issue comments
          - files changed (counts only)
        """
        prs = self.list_pull_requests(owner, repo, state="all", max_prs=max_prs)
        enriched: List[Dict[str, Any]] = []
        total = len(prs)

        for idx, pr in enumerate(prs, start=1):
            number = pr.get("number")
            try:
                reviews = self.get_pr_reviews(owner, repo, number)
            except Exception:
                reviews = []
            try:
                review_comments = self.get_pr_review_comments(owner, repo, number)
            except Exception:
                review_comments = []
            try:
                issue_comments = self.get_pr_issue_comments(owner, repo, number)
            except Exception:
                issue_comments = []

            requested_reviewers = [
                r.get("login") for r in (pr.get("requested_reviewers") or [])
                if r and r.get("login")
            ]

            enriched.append({
                "number": number,
                "title": pr.get("title", ""),
                "state": pr.get("state"),
                "draft": pr.get("draft", False),
                "merged_at": pr.get("merged_at"),
                "closed_at": pr.get("closed_at"),
                "created_at": pr.get("created_at"),
                "updated_at": pr.get("updated_at"),
                "author": (pr.get("user") or {}).get("login", "unknown"),
                "additions": pr.get("additions", 0),
                "deletions": pr.get("deletions", 0),
                "changed_files": pr.get("changed_files", 0),
                "comments": pr.get("comments", 0),
                "review_comments": pr.get("review_comments", 0),
                "html_url": pr.get("html_url", ""),
                "labels": [lbl.get("name") for lbl in (pr.get("labels") or []) if lbl],
                "requested_reviewers": requested_reviewers,
                "reviews": [
                    {
                        "user": (r.get("user") or {}).get("login", "unknown"),
                        "state": r.get("state"),
                        "submitted_at": r.get("submitted_at"),
                        "body": (r.get("body") or "")[:2000],
                    }
                    for r in reviews
                ],
                "review_comments_data": [
                    {
                        "user": (c.get("user") or {}).get("login", "unknown"),
                        "body": (c.get("body") or "")[:1000],
                        "created_at": c.get("created_at"),
                    }
                    for c in review_comments
                ],
                "issue_comments_data": [
                    {
                        "user": (c.get("user") or {}).get("login", "unknown"),
                        "body": (c.get("body") or "")[:1000],
                        "created_at": c.get("created_at"),
                    }
                    for c in issue_comments
                ],
            })

            if progress:
                try:
                    progress(idx, total, self.get_rate_limit_status())
                except Exception:
                    try:
                        progress(idx, total)
                    except Exception:
                        pass

        return enriched


def parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
    """Parse a GitHub ISO-8601 timestamp into a tz-aware datetime."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None

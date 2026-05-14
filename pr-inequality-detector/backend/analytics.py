"""
analytics.py
All analytics: reviewer fairness, PR delays, risk scoring, bias detection,
burnout detection, smart reviewer recommendation.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple
from collections import Counter, defaultdict
import math
import statistics

import pandas as pd

from github_api import parse_iso


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _hours_between(a: datetime, b: datetime) -> float:
    return abs((b - a).total_seconds()) / 3600.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _gini(values: List[float]) -> float:
    """Gini coefficient: 0 = perfect equality, 1 = max inequality."""
    if not values:
        return 0.0
    vals = sorted(float(v) for v in values)
    n = len(vals)
    s = sum(vals)
    if s == 0:
        return 0.0
    cum = 0.0
    for i, v in enumerate(vals, start=1):
        cum += i * v
    return (2 * cum) / (n * s) - (n + 1) / n


# ----------------------------------------------------------------------
# 1. Reviewer Inequality Detection
# ----------------------------------------------------------------------
def reviewer_inequality(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Distribution of reviews across reviewers, plus overload / inactive flags."""
    reviewer_counts: Counter = Counter()
    reviewer_pr_set: Dict[str, set] = defaultdict(set)
    reviewer_last_active: Dict[str, datetime] = {}
    requested_counts: Counter = Counter()

    for pr in prs:
        for r in pr.get("reviews", []):
            user = r.get("user")
            if not user or user == "unknown":
                continue
            reviewer_counts[user] += 1
            reviewer_pr_set[user].add(pr["number"])
            ts = parse_iso(r.get("submitted_at"))
            if ts and (user not in reviewer_last_active
                       or ts > reviewer_last_active[user]):
                reviewer_last_active[user] = ts
        for req in pr.get("requested_reviewers", []) or []:
            requested_counts[req] += 1

    rows: List[Dict[str, Any]] = []
    now = _now_utc()
    for user, count in reviewer_counts.items():
        last = reviewer_last_active.get(user)
        days_inactive = (now - last).days if last else None
        rows.append({
            "reviewer": user,
            "review_count": count,
            "prs_reviewed": len(reviewer_pr_set[user]),
            "last_review_days_ago": days_inactive,
            "times_requested": requested_counts.get(user, 0),
        })

    # Inactive reviewers: were requested but never submitted a review,
    # or last active >30 days ago.
    inactive: List[Dict[str, Any]] = []
    seen_active = {r["reviewer"] for r in rows}
    for user, req_count in requested_counts.items():
        if user not in seen_active:
            inactive.append({
                "reviewer": user,
                "times_requested": req_count,
                "review_count": 0,
                "reason": "Requested but never reviewed",
            })
    for r in rows:
        if r["last_review_days_ago"] is not None and r["last_review_days_ago"] > 30:
            inactive.append({
                "reviewer": r["reviewer"],
                "times_requested": r["times_requested"],
                "review_count": r["review_count"],
                "reason": f"No activity in {r['last_review_days_ago']} days",
            })

    counts_only = [r["review_count"] for r in rows]
    gini = _gini(counts_only) if counts_only else 0.0
    fairness_score = round((1.0 - gini) * 100, 1)

    # Overloaded: reviewers above the 80th percentile and >= 1.5x the median
    overloaded: List[Dict[str, Any]] = []
    if counts_only:
        try:
            p80 = statistics.quantiles(counts_only, n=5)[3] if len(counts_only) >= 5 else max(counts_only)
        except statistics.StatisticsError:
            p80 = max(counts_only)
        median = statistics.median(counts_only)
        threshold = max(p80, median * 1.5, 3)
        for r in rows:
            if r["review_count"] >= threshold and len(counts_only) > 1:
                overloaded.append({
                    "reviewer": r["reviewer"],
                    "review_count": r["review_count"],
                    "share_pct": round(100 * r["review_count"] / sum(counts_only), 1),
                })

    rows.sort(key=lambda x: x["review_count"], reverse=True)

    return {
        "distribution": rows,
        "overloaded": sorted(overloaded, key=lambda x: x["review_count"], reverse=True),
        "inactive": inactive,
        "gini": round(gini, 3),
        "fairness_score": fairness_score,
        "total_reviews": sum(counts_only),
        "unique_reviewers": len(rows),
    }


# ----------------------------------------------------------------------
# 2. PR Delay Analytics
# ----------------------------------------------------------------------
def pr_delay_analytics(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    now = _now_utc()

    for pr in prs:
        created = parse_iso(pr.get("created_at"))
        merged = parse_iso(pr.get("merged_at"))
        closed = parse_iso(pr.get("closed_at"))

        first_review_at = None
        first_approval_at = None
        for r in pr.get("reviews", []):
            ts = parse_iso(r.get("submitted_at"))
            if not ts:
                continue
            if first_review_at is None or ts < first_review_at:
                first_review_at = ts
            if r.get("state") == "APPROVED":
                if first_approval_at is None or ts < first_approval_at:
                    first_approval_at = ts

        time_to_first_review_h = (
            _hours_between(created, first_review_at)
            if created and first_review_at else None
        )
        time_to_approval_h = (
            _hours_between(created, first_approval_at)
            if created and first_approval_at else None
        )
        time_to_merge_h = (
            _hours_between(created, merged) if created and merged else None
        )

        is_open = pr.get("state") == "open"
        age_hours = _hours_between(created, now) if created else 0
        # Stale = open for >7 days OR open with no review for >3 days
        is_stale = False
        if is_open:
            if age_hours > 24 * 7:
                is_stale = True
            elif first_review_at is None and age_hours > 24 * 3:
                is_stale = True

        rows.append({
            "number": pr["number"],
            "title": pr["title"],
            "author": pr["author"],
            "state": pr["state"],
            "draft": pr.get("draft", False),
            "html_url": pr.get("html_url", ""),
            "created_at": pr.get("created_at"),
            "first_review_h": round(time_to_first_review_h, 2) if time_to_first_review_h is not None else None,
            "first_approval_h": round(time_to_approval_h, 2) if time_to_approval_h is not None else None,
            "time_to_merge_h": round(time_to_merge_h, 2) if time_to_merge_h is not None else None,
            "age_hours": round(age_hours, 2),
            "is_stale": is_stale,
            "is_open": is_open,
        })

    first_review_vals = [r["first_review_h"] for r in rows if r["first_review_h"] is not None]
    approval_vals = [r["first_approval_h"] for r in rows if r["first_approval_h"] is not None]
    merge_vals = [r["time_to_merge_h"] for r in rows if r["time_to_merge_h"] is not None]

    def _stats(vals: List[float]) -> Dict[str, Any]:
        if not vals:
            return {"avg": None, "median": None, "p90": None, "count": 0}
        s = sorted(vals)
        return {
            "avg": round(sum(s) / len(s), 2),
            "median": round(statistics.median(s), 2),
            "p90": round(s[max(0, math.ceil(len(s) * 0.9) - 1)], 2),
            "count": len(s),
        }

    stale = [r for r in rows if r["is_stale"]]
    stale.sort(key=lambda x: x["age_hours"], reverse=True)

    return {
        "prs": rows,
        "stale_prs": stale,
        "first_review_stats": _stats(first_review_vals),
        "approval_stats": _stats(approval_vals),
        "merge_stats": _stats(merge_vals),
    }


# ----------------------------------------------------------------------
# 3. AI Review Quality Analysis (rule-based; AI module enhances this)
# ----------------------------------------------------------------------
LOW_QUALITY_PATTERNS = [
    "lgtm", "looks good", "looks good to me", "+1", ":+1:", "👍", "ok", "okay",
    "approved", "approve", "ship it", "shipit", "fine", "good", "great",
    "nice", "sgtm", "lgtm 👍", "done", "ack", "wfm",
]


def classify_comment(text: str) -> Dict[str, Any]:
    """Classify a single review comment into a quality bucket."""
    if not text:
        return {"label": "empty", "score": 0, "length": 0}
    raw = text.strip()
    lowered = raw.lower()
    words = raw.split()
    length = len(words)

    has_question = "?" in raw
    has_code = "```" in raw or "`" in raw
    has_suggestion = any(k in lowered for k in
                         ["suggest", "consider", "what about", "could you",
                          "should we", "instead of", "rather than", "why not"])
    has_concern = any(k in lowered for k in
                      ["concern", "risk", "issue", "bug", "broken", "leak",
                       "race", "security", "vulnerab", "performance", "regress"])

    is_low = (length <= 4 and any(p == lowered or lowered.startswith(p)
                                  for p in LOW_QUALITY_PATTERNS))

    if is_low:
        label, score = "rubber_stamp", 1
    elif has_concern or has_code:
        label, score = "high_quality", 5
    elif has_suggestion or has_question:
        label, score = "constructive", 4
    elif length >= 25:
        label, score = "detailed", 4
    elif length >= 10:
        label, score = "moderate", 3
    else:
        label, score = "shallow", 2

    return {
        "label": label,
        "score": score,
        "length": length,
        "has_question": has_question,
        "has_code": has_code,
        "has_suggestion": has_suggestion,
        "has_concern": has_concern,
    }


# def review_quality_analysis(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
#     bucket_counts: Counter = Counter()
#     per_pr: Dict[int, Dict[str, Any]] = {}
#     rubber_stamp_approvals: List[Dict[str, Any]] = []
#     reviewer_avg: Dict[str, List[int]] = defaultdict(list)
#     samples: List[Dict[str, Any]] = []

#     for pr in prs:
#         pr_scores: List[int] = []
#         comments_count = 0

#         # Review-body classification + rubber-stamp detection
#         for r in pr.get("reviews", []):
#             cls = classify_comment(r.get("body", "") or "")
#             if r.get("state") == "APPROVED" and (
#                 cls["label"] in ("rubber_stamp", "empty", "shallow")
#             ):
#                 rubber_stamp_approvals.append({
#                     "pr_number": pr["number"],
#                     "pr_title": pr["title"],
#                     "reviewer": r.get("user"),
#                     "body_excerpt": (r.get("body") or "").strip()[:120] or "(no body)",
#                     "html_url": pr.get("html_url", ""),
#                 })
#             if r.get("body") and r.get("body").strip():
#                 bucket_counts[cls["label"]] += 1
#                 pr_scores.append(cls["score"])
#                 if r.get("user"):
#                     reviewer_avg[r["user"]].append(cls["score"])
#                 comments_count += 1
#                 if len(samples) < 25:
#                     samples.append({
#                         "pr_number": pr["number"],
#                         "reviewer": r.get("user"),
#                         "label": cls["label"],
#                         "score": cls["score"],
#                         "excerpt": (r.get("body") or "")[:200],
#                     })

#         # Inline review comments + issue comments
#         for c in pr.get("review_comments_data", []) + pr.get("issue_comments_data", []):
#             cls = classify_comment(c.get("body", "") or "")
#             if cls["label"] == "empty":
#                 continue
#             bucket_counts[cls["label"]] += 1
#             pr_scores.append(cls["score"])
#             if c.get("user"):
#                 reviewer_avg[c["user"]].append(cls["score"])
#             comments_count += 1

#         per_pr[pr["number"]] = {
#             "avg_score": round(sum(pr_scores) / len(pr_scores), 2) if pr_scores else 0,
#             "comments_count": comments_count,
#         }

#     reviewer_quality = [
#         {
#             "reviewer": user,
#             "avg_quality_score": round(sum(scores) / len(scores), 2),
#             "total_comments": len(scores),
#         }
#         for user, scores in reviewer_avg.items()
#     ]
#     reviewer_quality.sort(key=lambda x: x["avg_quality_score"], reverse=True)

#     total = sum(bucket_counts.values())
#     overall_avg = (
#         sum(c["avg_score"] for c in per_pr.values() if c["avg_score"] > 0)
#         / max(1, sum(1 for c in per_pr.values() if c["avg_score"] > 0))
#     )

#     return {
#         "buckets": dict(bucket_counts),
#         "total_classified": total,
#         "overall_avg_score": round(overall_avg, 2),
#         "rubber_stamp_approvals": rubber_stamp_approvals,
#         "reviewer_quality": reviewer_quality,
#         "per_pr": per_pr,
#         "samples": samples,
#     }
def review_quality_analysis(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
    bucket_counts: Counter = Counter()
    per_pr: Dict[int, Dict[str, Any]] = {}
    rubber_stamp_approvals: List[Dict[str, Any]] = []
    reviewer_avg: Dict[str, List[int]] = defaultdict(list)
    samples: List[Dict[str, Any]] = []

    for pr in prs:
        pr_scores: List[int] = []
        comments_count = 0

        # Review-body classification + rubber-stamp detection
        for r in pr.get("reviews", []):
            cls = classify_comment(r.get("body", "") or "")
            if r.get("state") == "APPROVED" and (
                cls["label"] in ("rubber_stamp", "empty", "shallow")
            ):
                rubber_stamp_approvals.append({
                    "pr_number": pr["number"],
                    "pr_title": pr["title"],
                    "reviewer": r.get("user"),
                    "body_excerpt": (r.get("body") or "").strip()[:120] or "(no body)",
                    "html_url": pr.get("html_url", ""),
                })
            if r.get("body") and r.get("body").strip():
                bucket_counts[cls["label"]] += 1
                pr_scores.append(cls["score"])
                if r.get("user"):
                    reviewer_avg[r["user"]].append(cls["score"])
                comments_count += 1
                samples.append({
                    "pr_number": pr["number"],
                    "reviewer": r.get("user"),
                    "label": cls["label"],
                    "score": cls["score"],
                    "excerpt": (r.get("body") or "")[:200],
                    "source": "review",
                })

        # Inline review comments + issue comments
        review_comments = pr.get("review_comments_data", [])
        issue_comments = pr.get("issue_comments_data", [])
        for c in review_comments + issue_comments:
            cls = classify_comment(c.get("body", "") or "")
            if cls["label"] == "empty":
                continue
            bucket_counts[cls["label"]] += 1
            pr_scores.append(cls["score"])
            if c.get("user"):
                reviewer_avg[c["user"]].append(cls["score"])
            comments_count += 1
            samples.append({
                "pr_number": pr["number"],
                "reviewer": c.get("user"),
                "label": cls["label"],
                "score": cls["score"],
                "excerpt": (c.get("body") or "")[:200],
                "source": "inline" if c in review_comments else "discussion",
            })

        per_pr[pr["number"]] = {
            "avg_score": round(sum(pr_scores) / len(pr_scores), 2) if pr_scores else 0,
            "comments_count": comments_count,
        }

    reviewer_quality = [
        {
            "reviewer": user,
            "avg_quality_score": round(sum(scores) / len(scores), 2),
            "total_comments": len(scores),
        }
        for user, scores in reviewer_avg.items()
    ]
    reviewer_quality.sort(key=lambda x: x["avg_quality_score"], reverse=True)

    total = sum(bucket_counts.values())
    overall_avg = (
        sum(c["avg_score"] for c in per_pr.values() if c["avg_score"] > 0)
        / max(1, sum(1 for c in per_pr.values() if c["avg_score"] > 0))
    )

    return {
        "buckets": dict(bucket_counts),
        "total_classified": total,
        "overall_avg_score": round(overall_avg, 2),
        "rubber_stamp_approvals": rubber_stamp_approvals,
        "reviewer_quality": reviewer_quality,
        "per_pr": per_pr,
        "samples": samples,
    }

# ----------------------------------------------------------------------
# 4. PR Risk Scoring
# ----------------------------------------------------------------------
DEFAULT_RISK_WEIGHTS: Dict[str, Any] = {
    "very_large_diff_threshold": 1000,
    "very_large_diff_score": 35,
    "large_diff_threshold": 400,
    "large_diff_score": 20,
    "few_approvers_score": 20,
    "fast_merge_hours": 1,
    "fast_merge_score": 25,
    "no_comments_score": 15,
    "many_files_threshold": 30,
    "many_files_score": 10,
    "no_reviews_score": 25,
}


def pr_risk_scoring(prs: List[Dict[str, Any]],
                    risk_weights: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    w = {**DEFAULT_RISK_WEIGHTS, **(risk_weights or {})}
    rows: List[Dict[str, Any]] = []

    for pr in prs:
        created = parse_iso(pr.get("created_at"))
        merged = parse_iso(pr.get("merged_at"))
        approvers = {r["user"] for r in pr.get("reviews", [])
                     if r.get("state") == "APPROVED" and r.get("user")}
        unique_reviewers = {r["user"] for r in pr.get("reviews", []) if r.get("user")}
        size = (pr.get("additions", 0) or 0) + (pr.get("deletions", 0) or 0)
        review_comment_count = (
            len(pr.get("review_comments_data", []))
            + len(pr.get("issue_comments_data", []))
        )

        time_to_merge_h = (
            _hours_between(created, merged) if created and merged else None
        )

        reasons: List[str] = []
        score = 0
        if size > w["very_large_diff_threshold"]:
            score += w["very_large_diff_score"]
            reasons.append(f"Very large diff ({size} lines)")
        elif size > w["large_diff_threshold"]:
            score += w["large_diff_score"]
            reasons.append(f"Large diff ({size} lines)")

        if pr.get("merged_at") and len(approvers) <= 1:
            score += w["few_approvers_score"]
            reasons.append("Merged with ≤1 approver")

        if time_to_merge_h is not None and time_to_merge_h < w["fast_merge_hours"] and size > 100:
            score += w["fast_merge_score"]
            reasons.append(f"Approved in <{w['fast_merge_hours']}h with {size} lines changed")
        elif time_to_merge_h is not None and time_to_merge_h < 4 and size > 300:
            score += 15
            reasons.append("Fast merge on a substantial change")

        if review_comment_count == 0 and size > 50 and pr.get("merged_at"):
            score += w["no_comments_score"]
            reasons.append("No review comments at all")

        if (pr.get("changed_files") or 0) > w["many_files_threshold"]:
            score += w["many_files_score"]
            reasons.append(f"{pr['changed_files']} files touched")

        if not unique_reviewers and pr.get("merged_at"):
            score += w["no_reviews_score"]
            reasons.append("Merged with no reviews recorded")

        score = min(score, 100)
        if score >= 60:
            level = "high"
        elif score >= 30:
            level = "medium"
        else:
            level = "low"

        rows.append({
            "number": pr["number"],
            "title": pr["title"],
            "author": pr["author"],
            "html_url": pr.get("html_url", ""),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", 0),
            "approvers": sorted(approvers),
            "unique_reviewers": sorted(unique_reviewers),
            "merged_at": pr.get("merged_at"),
            "state": pr.get("state"),
            "risk_score": score,
            "risk_level": level,
            "reasons": reasons,
            "time_to_merge_h": round(time_to_merge_h, 2) if time_to_merge_h is not None else None,
        })

    rows.sort(key=lambda x: x["risk_score"], reverse=True)

    summary = {
        "high": sum(1 for r in rows if r["risk_level"] == "high"),
        "medium": sum(1 for r in rows if r["risk_level"] == "medium"),
        "low": sum(1 for r in rows if r["risk_level"] == "low"),
    }

    return {"prs": rows, "summary": summary}


# ----------------------------------------------------------------------
# 5. Bias / Favoritism Detection
# ----------------------------------------------------------------------
def bias_detection(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
    # author -> reviewer approvals counter
    author_reviewer: Dict[str, Counter] = defaultdict(Counter)
    reviewer_authors: Dict[str, Counter] = defaultdict(Counter)
    pair_total: Counter = Counter()

    for pr in prs:
        author = pr.get("author")
        if not author:
            continue
        approvers = {r["user"] for r in pr.get("reviews", [])
                     if r.get("state") == "APPROVED" and r.get("user")}
        for a in approvers:
            if a == author:
                continue
            author_reviewer[author][a] += 1
            reviewer_authors[a][author] += 1
            pair_total[(author, a)] += 1

    # Favoritism: reviewer approves the same author disproportionately
    favoritism: List[Dict[str, Any]] = []
    for author, ctr in author_reviewer.items():
        total = sum(ctr.values())
        if total < 3:
            continue
        for reviewer, n in ctr.items():
            share = n / total
            if n >= 3 and share >= 0.6:
                favoritism.append({
                    "author": author,
                    "reviewer": reviewer,
                    "approvals": n,
                    "author_total_approvals": total,
                    "share_pct": round(share * 100, 1),
                })
    favoritism.sort(key=lambda x: x["share_pct"], reverse=True)

    # Repeated approval groups
    group_counter: Counter = Counter()
    for pr in prs:
        approvers = tuple(sorted({r["user"] for r in pr.get("reviews", [])
                                  if r.get("state") == "APPROVED" and r.get("user")}))
        if len(approvers) >= 2:
            group_counter[approvers] += 1
    repeated_groups = [
        {"reviewers": list(group), "occurrences": count}
        for group, count in group_counter.most_common(10) if count >= 2
    ]

    # Isolated authors: authors only ever reviewed by 1 reviewer (>=3 approvals)
    isolated_authors: List[Dict[str, Any]] = []
    for author, ctr in author_reviewer.items():
        if len(ctr) == 1 and sum(ctr.values()) >= 3:
            reviewer, n = next(iter(ctr.items()))
            isolated_authors.append({
                "author": author,
                "sole_reviewer": reviewer,
                "approvals": n,
            })

    return {
        "favoritism": favoritism,
        "repeated_groups": repeated_groups,
        "isolated_authors": isolated_authors,
    }


# ----------------------------------------------------------------------
# 6. Smart Reviewer Recommendation
# ----------------------------------------------------------------------
def smart_reviewer_recommendation(
    prs: List[Dict[str, Any]],
    inequality: Dict[str, Any],
) -> Dict[str, Any]:
    """For each open PR, suggest balanced reviewers based on workload + history."""
    workload = {r["reviewer"]: r["review_count"]
                for r in inequality.get("distribution", [])}
    if not workload:
        return {"recommendations": []}

    # Recent activity within 14 days
    cutoff = _now_utc() - timedelta(days=14)
    recent_load: Counter = Counter()
    for pr in prs:
        for r in pr.get("reviews", []):
            ts = parse_iso(r.get("submitted_at"))
            if ts and ts >= cutoff and r.get("user"):
                recent_load[r["user"]] += 1

    avg_load = sum(workload.values()) / max(1, len(workload))

    recommendations: List[Dict[str, Any]] = []
    for pr in prs:
        if pr.get("state") != "open":
            continue
        candidates = []
        for reviewer, total in workload.items():
            if reviewer == pr.get("author"):
                continue
            recent = recent_load.get(reviewer, 0)
            # Lower score = better candidate (less loaded, but still active)
            balance_score = (recent * 1.5) + max(0, total - avg_load)
            candidates.append({
                "reviewer": reviewer,
                "total_reviews": total,
                "recent_reviews": recent,
                "balance_score": round(balance_score, 2),
            })
        candidates.sort(key=lambda x: x["balance_score"])
        recommendations.append({
            "pr_number": pr["number"],
            "pr_title": pr["title"],
            "html_url": pr.get("html_url", ""),
            "author": pr.get("author"),
            "suggested_reviewers": candidates[:3],
        })

    return {"recommendations": recommendations}


# ----------------------------------------------------------------------
# 7. Burnout Detection
# ----------------------------------------------------------------------
def burnout_detection(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Detect late-night and weekend review patterns per reviewer."""
    activity: Dict[str, List[datetime]] = defaultdict(list)
    for pr in prs:
        for r in pr.get("reviews", []):
            ts = parse_iso(r.get("submitted_at"))
            if ts and r.get("user"):
                activity[r["user"]].append(ts)
        for c in pr.get("review_comments_data", []) + pr.get("issue_comments_data", []):
            ts = parse_iso(c.get("created_at"))
            if ts and c.get("user"):
                activity[c["user"]].append(ts)

    rows: List[Dict[str, Any]] = []
    for user, timestamps in activity.items():
        if not timestamps:
            continue
        late_night = sum(1 for t in timestamps if t.hour >= 22 or t.hour < 6)
        weekend = sum(1 for t in timestamps if t.weekday() >= 5)
        total = len(timestamps)
        late_pct = round(100 * late_night / total, 1)
        weekend_pct = round(100 * weekend / total, 1)

        risk_signals = []
        if late_pct >= 25:
            risk_signals.append(f"{late_pct}% late-night activity")
        if weekend_pct >= 25:
            risk_signals.append(f"{weekend_pct}% weekend activity")
        if total >= 15:
            risk_signals.append(f"High volume ({total} actions)")

        # Burnout score
        score = 0
        score += min(40, late_pct)
        score += min(30, weekend_pct)
        score += min(30, total * 1.2) if total > 10 else 0
        rows.append({
            "reviewer": user,
            "total_actions": total,
            "late_night_pct": late_pct,
            "weekend_pct": weekend_pct,
            "burnout_score": round(min(100, score), 1),
            "signals": risk_signals,
        })

    rows.sort(key=lambda x: x["burnout_score"], reverse=True)
    at_risk = [r for r in rows if r["burnout_score"] >= 50]

    return {"reviewers": rows, "at_risk": at_risk}


# ----------------------------------------------------------------------
# 8. Emotional Intelligence — comment tone & mental health impact
# ----------------------------------------------------------------------

HOSTILE_PATTERNS = [
    "stupid", "idiot", "dumb", "moron", "garbage", "trash", "awful",
    "terrible", "horrible", "useless", "pathetic", "ridiculous",
    "are you serious", "are you stupid", "wtf",
    "did you even", "do you even", "did you test", "didnt you test",
    "shit", "crap", "nonsense", "embarrassing",
    "incompetent", "lazy", "amateur",
]

HARSH_PATTERNS = [
    "wrong", "broken", "broke it", "you broke", "you missed",
    "you forgot", "you didnt", "you didn't", "dont do", "don't do",
    "never do", "stop doing", "redo this", "rewrite this", "scrap this",
    "start over", "still wrong", "still broken", "as i told you",
    "i told you", "clearly wrong", "unnecessary",
]

DISMISSIVE_PATTERNS = [
    "nope", "wont merge", "won't merge", "not merging",
    "rejected", "denied", "bad idea", "wont work", "won't work",
    "doesnt work", "doesn't work", "pointless", "waste of time",
    "not happening", "absolutely not",
]

CONSTRUCTIVE_TONE_PATTERNS = [
    "consider", "what about", "could you", "could we", "what if",
    "have you tried", "suggest", "recommend", "instead of",
    "rather than", "why not", "perhaps", "maybe", "might want to",
    "an alternative", "another approach",
]

SUPPORTIVE_PATTERNS = [
    "great work", "great job", "nice work", "nice job", "well done",
    "love this", "love it", "awesome", "fantastic", "brilliant",
    "elegant", "clever", "clean approach", "thanks for", "appreciate",
    "i learned", "good catch", "good point",
    "makes sense", "fair point", "you're right", "youre right",
]


def classify_tone(text: str) -> Dict[str, Any]:
    """Classify the emotional tone of a single comment."""
    if not text or not text.strip():
        return {"tone": "neutral", "intensity": 0}
    lowered = text.lower()
    if any(p in lowered for p in HOSTILE_PATTERNS):
        return {"tone": "hostile", "intensity": 4}
    if any(p in lowered for p in DISMISSIVE_PATTERNS):
        return {"tone": "dismissive", "intensity": 3}
    if any(p in lowered for p in HARSH_PATTERNS):
        return {"tone": "harsh", "intensity": 2}
    if any(p in lowered for p in SUPPORTIVE_PATTERNS):
        return {"tone": "supportive", "intensity": -2}
    if any(p in lowered for p in CONSTRUCTIVE_TONE_PATTERNS):
        return {"tone": "constructive", "intensity": -1}
    return {"tone": "neutral", "intensity": 0}


def emotional_intelligence_analysis(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Score reviewers for psychological safety and flag at-risk authors."""
    tone_buckets: Counter = Counter()
    reviewer_tones: Dict[str, Counter] = defaultdict(Counter)
    author_received: Dict[str, Counter] = defaultdict(Counter)
    flagged_comments: List[Dict[str, Any]] = []

    def _process(comment_text, reviewer, author, pr_number, pr_title, html_url, source):
        if not comment_text or not comment_text.strip():
            return
        tone = classify_tone(comment_text)
        label = tone["tone"]
        tone_buckets[label] += 1
        if reviewer:
            reviewer_tones[reviewer][label] += 1
        if author and reviewer != author:
            author_received[author][label] += 1
        if label in ("hostile", "harsh", "dismissive"):
            flagged_comments.append({
                "pr_number": pr_number,
                "pr_title": pr_title,
                "reviewer": reviewer,
                "author": author,
                "tone": label,
                "excerpt": comment_text.strip()[:180],
                "source": source,
                "html_url": html_url,
            })

    for pr in prs:
        author = pr.get("author") or ""
        num = pr.get("number")
        title = pr.get("title", "")
        url = pr.get("html_url", "")
        for r in pr.get("reviews", []):
            _process(r.get("body", ""), r.get("user", ""),
                     author, num, title, url, "review")
        for c in pr.get("review_comments_data", []):
            _process(c.get("body", ""), c.get("user", ""),
                     author, num, title, url, "inline")
        for c in pr.get("issue_comments_data", []):
            _process(c.get("body", ""), c.get("user", ""),
                     author, num, title, url, "discussion")

    reviewer_ei: List[Dict[str, Any]] = []
    for reviewer, counts in reviewer_tones.items():
        total = sum(counts.values())
        if total == 0:
            continue
        hostile_pct = 100 * counts["hostile"] / total
        harsh_pct = 100 * counts["harsh"] / total
        dismissive_pct = 100 * counts["dismissive"] / total
        supportive_pct = 100 * counts["supportive"] / total
        ei_score = 100 - (hostile_pct * 4) - (harsh_pct * 2) \
                   - (dismissive_pct * 1.5) + (supportive_pct * 0.5)
        ei_score = max(0, min(100, ei_score))

        if ei_score < 40:
            level = "concerning"
        elif ei_score < 60:
            level = "needs_work"
        elif ei_score < 80:
            level = "okay"
        else:
            level = "psychologically_safe"

        reviewer_ei.append({
            "reviewer": reviewer,
            "ei_score": round(ei_score, 1),
            "level": level,
            "total_comments": total,
            "tone_breakdown": dict(counts),
            "hostile_pct": round(hostile_pct, 1),
            "harsh_pct": round(harsh_pct, 1),
            "supportive_pct": round(supportive_pct, 1),
        })
    reviewer_ei.sort(key=lambda x: x["ei_score"], reverse=True)

    at_risk_authors: List[Dict[str, Any]] = []
    for author, counts in author_received.items():
        total = sum(counts.values())
        if total < 3:
            continue
        negative = counts["hostile"] + counts["harsh"] + counts["dismissive"]
        negative_pct = 100 * negative / total
        if negative_pct >= 30:
            at_risk_authors.append({
                "author": author,
                "negative_pct": round(negative_pct, 1),
                "total_comments_received": total,
                "hostile": counts["hostile"],
                "harsh": counts["harsh"],
                "dismissive": counts["dismissive"],
                "supportive": counts["supportive"],
            })
    at_risk_authors.sort(key=lambda x: x["negative_pct"], reverse=True)

    total_classified = sum(tone_buckets.values())
    negative_total = (tone_buckets["hostile"] + tone_buckets["harsh"]
                      + tone_buckets["dismissive"])
    team_psych_safety = (
        round(100 - (100 * negative_total / total_classified), 1)
        if total_classified else 100.0
    )

    return {
        "team_psych_safety_score": team_psych_safety,
        "tone_buckets": dict(tone_buckets),
        "total_classified": total_classified,
        "reviewer_ei": reviewer_ei,
        "at_risk_authors": at_risk_authors,
        "flagged_comments": flagged_comments[:50],
    }


# ----------------------------------------------------------------------
# 9. Reviewer SLA Tracking
# ----------------------------------------------------------------------
def reviewer_sla_analysis(prs: List[Dict[str, Any]],
                           sla_hours: float = 24.0) -> Dict[str, Any]:
    """Track reviewer response time compliance vs. a configurable SLA threshold."""
    reviewer_responses: Dict[str, List[Dict]] = defaultdict(list)

    for pr in prs:
        created = parse_iso(pr.get("created_at"))
        if not created:
            continue
        for r in pr.get("reviews", []):
            reviewer = r.get("user")
            if not reviewer or reviewer == "unknown":
                continue
            submitted = parse_iso(r.get("submitted_at"))
            if not submitted:
                continue
            response_h = _hours_between(created, submitted)
            reviewer_responses[reviewer].append({
                "pr_number": pr["number"],
                "response_hours": round(response_h, 2),
                "met_sla": response_h <= sla_hours,
            })

    rows: List[Dict[str, Any]] = []
    for reviewer, responses in reviewer_responses.items():
        total = len(responses)
        met = sum(1 for r in responses if r["met_sla"])
        avg_h = round(sum(r["response_hours"] for r in responses) / total, 2) if total else None
        rows.append({
            "reviewer": reviewer,
            "total_reviews": total,
            "sla_met": met,
            "sla_missed": total - met,
            "compliance_pct": round(100 * met / total, 1) if total else 0,
            "avg_response_hours": avg_h,
        })

    rows.sort(key=lambda x: x["compliance_pct"])
    overall_total = sum(r["total_reviews"] for r in rows)
    overall_met = sum(r["sla_met"] for r in rows)
    return {
        "reviewers": rows,
        "overall_compliance_pct": round(100 * overall_met / overall_total, 1) if overall_total else 100.0,
        "sla_hours": sla_hours,
        "total_reviews_analyzed": overall_total,
    }


# ----------------------------------------------------------------------
# 10. Author Analytics
# ----------------------------------------------------------------------
def author_analytics(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-author metrics: PR volume, merge rate, wait time, comment quality received."""
    author_prs: Dict[str, List] = defaultdict(list)
    author_wait_times: Dict[str, List[float]] = defaultdict(list)
    author_quality_scores: Dict[str, List[int]] = defaultdict(list)

    for pr in prs:
        author = pr.get("author")
        if not author:
            continue
        author_prs[author].append(pr)

        created = parse_iso(pr.get("created_at"))
        first_review_at: Optional[datetime] = None
        for r in pr.get("reviews", []):
            reviewer = r.get("user")
            if reviewer and reviewer != author:
                ts = parse_iso(r.get("submitted_at"))
                if ts and (first_review_at is None or ts < first_review_at):
                    first_review_at = ts
        if created and first_review_at:
            author_wait_times[author].append(_hours_between(created, first_review_at))

        for c in pr.get("review_comments_data", []) + pr.get("issue_comments_data", []):
            if c.get("user") and c.get("user") != author:
                cls = classify_comment(c.get("body", "") or "")
                if cls["label"] != "empty":
                    author_quality_scores[author].append(cls["score"])

    rows: List[Dict[str, Any]] = []
    for author, pr_list in author_prs.items():
        total = len(pr_list)
        merged = sum(1 for p in pr_list if p.get("merged_at"))
        open_count = sum(1 for p in pr_list if p.get("state") == "open")
        wt = author_wait_times.get(author, [])
        qs = author_quality_scores.get(author, [])
        rows.append({
            "author": author,
            "total_prs": total,
            "merged": merged,
            "open": open_count,
            "closed_unmerged": total - merged - open_count,
            "merge_rate_pct": round(100 * merged / total, 1) if total else 0,
            "avg_wait_for_review_h": round(sum(wt) / len(wt), 2) if wt else None,
            "avg_quality_received": round(sum(qs) / len(qs), 2) if qs else None,
            "comments_received": len(qs),
            "total_additions": sum(p.get("additions", 0) or 0 for p in pr_list),
            "total_deletions": sum(p.get("deletions", 0) or 0 for p in pr_list),
        })

    rows.sort(key=lambda x: x["total_prs"], reverse=True)
    return {"authors": rows, "total_authors": len(rows)}


# # ----------------------------------------------------------------------
# # Top-level analytics aggregator
# # ----------------------------------------------------------------------
# def run_full_analytics(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
#     inequality = reviewer_inequality(prs)
#     delays = pr_delay_analytics(prs)
#     quality = review_quality_analysis(prs)
#     risk = pr_risk_scoring(prs)
#     bias = bias_detection(prs)
#     burnout = burnout_detection(prs)
#     recs = smart_reviewer_recommendation(prs, inequality)

#     # Composite team health score (0-100, higher = healthier)
#     fairness = inequality["fairness_score"]
#     quality_score = (quality["overall_avg_score"] / 5.0) * 100 if quality["overall_avg_score"] else 50
#     rubber_pct = (
#         100 * len(quality["rubber_stamp_approvals"])
#         / max(1, sum(1 for pr in prs if pr.get("merged_at")))
#     )
#     risk_pct = 100 * risk["summary"]["high"] / max(1, len(prs))
#     review_speed = delays["first_review_stats"].get("median") or 24
#     speed_score = max(0, 100 - min(100, review_speed * 1.5))

#     health_score = round(
#         0.30 * fairness +
#         0.25 * quality_score +
#         0.15 * (100 - rubber_pct) +
#         0.15 * (100 - min(100, risk_pct * 3)) +
#         0.15 * speed_score, 1
#     )

#     summary = {
#         "total_prs": len(prs),
#         "open_prs": sum(1 for pr in prs if pr.get("state") == "open"),
#         "merged_prs": sum(1 for pr in prs if pr.get("merged_at")),
#         "closed_unmerged": sum(1 for pr in prs
#                                if pr.get("state") == "closed" and not pr.get("merged_at")),
#         "unique_authors": len({pr["author"] for pr in prs if pr.get("author")}),
#         "unique_reviewers": inequality["unique_reviewers"],
#         "team_health_score": health_score,
#         "fairness_score": fairness,
#         "review_quality_score": round(quality_score, 1),
#         "stale_count": len(delays["stale_prs"]),
#         "high_risk_count": risk["summary"]["high"],
#     }

#     return {
#         "summary": summary,
#         "inequality": inequality,
#         "delays": delays,
#         "quality": quality,
#         "risk": risk,
#         "bias": bias,
#         "burnout": burnout,
#         "recommendations": recs,
#     }

def run_full_analytics(prs: List[Dict[str, Any]],
                        label_filter: Optional[List[str]] = None,
                        risk_weights: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if label_filter:
        labels_set = {lbl.lower() for lbl in label_filter}
        prs = [p for p in prs if any(
            (l or "").lower() in labels_set for l in (p.get("labels") or [])
        )]

    inequality = reviewer_inequality(prs)
    delays = pr_delay_analytics(prs)
    quality = review_quality_analysis(prs)
    risk = pr_risk_scoring(prs, risk_weights=risk_weights)
    bias = bias_detection(prs)
    burnout = burnout_detection(prs)
    recs = smart_reviewer_recommendation(prs, inequality)
    emotional = emotional_intelligence_analysis(prs)
    author = author_analytics(prs)
    sla = reviewer_sla_analysis(prs)

    fairness = inequality["fairness_score"]
    quality_score = (quality["overall_avg_score"] / 5.0) * 100 if quality["overall_avg_score"] else 50
    rubber_pct = (
        100 * len(quality["rubber_stamp_approvals"])
        / max(1, sum(1 for pr in prs if pr.get("merged_at")))
    )
    risk_pct = 100 * risk["summary"]["high"] / max(1, len(prs))
    review_speed = delays["first_review_stats"].get("median") or 24
    speed_score = max(0, 100 - min(100, review_speed * 1.5))
    psych_safety = emotional["team_psych_safety_score"]

    health_score = round(
        0.25 * fairness +
        0.25 * psych_safety +
        0.15 * quality_score +
        0.10 * (100 - rubber_pct) +
        0.10 * (100 - min(100, risk_pct * 3)) +
        0.15 * speed_score, 1
    )

    summary = {
        "total_prs": len(prs),
        "open_prs": sum(1 for pr in prs if pr.get("state") == "open"),
        "merged_prs": sum(1 for pr in prs if pr.get("merged_at")),
        "closed_unmerged": sum(1 for pr in prs
                               if pr.get("state") == "closed" and not pr.get("merged_at")),
        "unique_authors": len({pr["author"] for pr in prs if pr.get("author")}),
        "unique_reviewers": inequality["unique_reviewers"],
        "team_health_score": health_score,
        "fairness_score": fairness,
        "review_quality_score": round(quality_score, 1),
        "psych_safety_score": psych_safety,
        "stale_count": len(delays["stale_prs"]),
        "high_risk_count": risk["summary"]["high"],
        "at_risk_authors_count": len(emotional["at_risk_authors"]),
    }

    return {
        "summary": summary,
        "inequality": inequality,
        "delays": delays,
        "quality": quality,
        "risk": risk,
        "bias": bias,
        "burnout": burnout,
        "emotional": emotional,
        "recommendations": recs,
        "author": author,
        "sla": sla,
    }
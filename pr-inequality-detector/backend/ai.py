"""
ai.py
AI integration: enhanced review-quality scoring + chat assistant.
Supports Anthropic Claude, OpenAI, Ollama (local), or rule-based fallback.
"""
from __future__ import annotations
import os
import re
import json
from typing import List, Dict, Any, Optional

import requests


# ---------------------------------------------------------------------- #
# Provider detection
# ---------------------------------------------------------------------- #
def _provider() -> str:
    forced = os.getenv("AI_PROVIDER", "").lower().strip()

    if forced == "anthropic":
        if os.getenv("ANTHROPIC_API_KEY"):
            return "anthropic"
    elif forced == "openai":
        key = os.getenv("OPENAI_API_KEY", "")
        if key and key.startswith("sk-"):
            return "openai"
    elif forced == "ollama":
        return "ollama"
    elif forced == "rule-based":
        return "rule-based"

    # Auto-detect in priority order: Anthropic → OpenAI → Ollama → rule-based
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    key = os.getenv("OPENAI_API_KEY", "")
    if key and key.startswith("sk-"):
        return "openai"
    return "rule-based"


# ---------------------------------------------------------------------- #
# Anthropic Claude helper (primary provider)
# ---------------------------------------------------------------------- #
def _merge_consecutive_roles(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Merge consecutive same-role messages (required by Anthropic API)."""
    merged: List[Dict[str, str]] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append({"role": m["role"], "content": m["content"]})
    if not merged or merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "(begin)"})
    return merged


def _claude_chat(messages: List[Dict[str, str]],
                 model: Optional[str] = None,
                 max_tokens: int = 1200,
                 temperature: float = 0.2) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    # Separate system messages from conversation messages
    system_parts: List[str] = []
    chat_messages: List[Dict[str, str]] = []
    for m in messages:
        if m["role"] == "system":
            system_parts.append(m["content"])
        else:
            chat_messages.append({"role": m["role"], "content": m["content"]})

    system_text = "\n\n".join(system_parts).strip()

    merged = _merge_consecutive_roles(chat_messages)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_text,
            messages=merged,
            temperature=temperature,
        )
        return resp.content[0].text
    except ImportError:
        # Direct REST fallback if SDK not installed
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_text,
            "messages": merged,
        }
        r = requests.post(url, headers=headers, json=body, timeout=60)
        r.raise_for_status()
        return r.json()["content"][0]["text"]


# ---------------------------------------------------------------------- #
# OpenAI helper
# ---------------------------------------------------------------------- #
def _openai_chat(messages: List[Dict[str, str]],
                 model: str = "gpt-4o-mini",
                 max_tokens: int = 1200,
                 temperature: float = 0.2) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""
    except Exception:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}",
                   "Content-Type": "application/json"}
        body = {"model": model, "messages": messages,
                "max_tokens": max_tokens, "temperature": temperature}
        r = requests.post(url, headers=headers, json=body, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------- #
# Ollama helper
# ---------------------------------------------------------------------- #
def _ollama_chat(messages: List[Dict[str, str]],
                 model: Optional[str] = None,
                 max_tokens: int = 1200,
                 temperature: float = 0.2) -> str:
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    model = model or os.getenv("OLLAMA_MODEL", "llama3.2")
    url = f"{host}/api/chat"
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    r = requests.post(url, json=body, timeout=120)
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content", "") or ""


# ---------------------------------------------------------------------- #
# Public: provider info + status
# ---------------------------------------------------------------------- #
def ai_status() -> Dict[str, Any]:
    p = _provider()
    info: Dict[str, Any] = {"provider": p, "available": p != "rule-based"}
    if p == "anthropic":
        info["model"] = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    elif p == "openai":
        info["model"] = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    elif p == "ollama":
        info["model"] = os.getenv("OLLAMA_MODEL", "llama3.2")
        info["host"] = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    return info


# ---------------------------------------------------------------------- #
# Enhanced review-quality scoring
# ---------------------------------------------------------------------- #
QUALITY_SYS_PROMPT = (
    "You are a senior staff engineer rating the quality of code-review comments. "
    "For each comment, output a JSON array with one object per input, in the same "
    "order. Each object has: index (int), score (1-5 int, higher=more substantive), "
    "label (one of: rubber_stamp, shallow, moderate, constructive, detailed, "
    "high_quality), reason (short, <=15 words). Return ONLY a JSON array, no prose."
)


def ai_enhance_review_quality(samples: List[Dict[str, Any]],
                              max_samples: int = 50) -> List[Dict[str, Any]]:
    """Re-score a sample of review comments using an LLM. Falls back gracefully."""
    if not samples:
        return []
    provider = _provider()
    take = samples[:max_samples]

    if provider == "rule-based":
        return [
            {**s, "ai_score": s.get("score"),
             "ai_label": s.get("label"), "ai_reason": "rule-based"}
            for s in take
        ]

    user_payload = [
        {"index": i, "comment": s.get("excerpt", "")[:600]}
        for i, s in enumerate(take)
    ]
    messages = [
        {"role": "system", "content": QUALITY_SYS_PROMPT},
        {"role": "user",
         "content": "Rate these review comments:\n" + json.dumps(user_payload)},
    ]

    try:
        if provider == "anthropic":
            raw = _claude_chat(messages, max_tokens=1000, temperature=0.1)
        elif provider == "openai":
            raw = _openai_chat(messages, max_tokens=1000, temperature=0.1)
        else:
            raw = _ollama_chat(messages, max_tokens=1000, temperature=0.1)

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1:
            raise ValueError("No JSON array in LLM response")
        parsed = json.loads(cleaned[start:end + 1])
        by_index = {item.get("index"): item for item in parsed
                    if isinstance(item, dict)}
        out = []
        for i, s in enumerate(take):
            ai_obj = by_index.get(i, {})
            out.append({
                **s,
                "ai_score": ai_obj.get("score", s.get("score")),
                "ai_label": ai_obj.get("label", s.get("label")),
                "ai_reason": ai_obj.get("reason", "n/a"),
            })
        return out
    except Exception as e:
        return [
            {**s, "ai_score": s.get("score"),
             "ai_label": s.get("label"),
             "ai_reason": f"AI fallback ({type(e).__name__})"}
            for s in take
        ]


# ---------------------------------------------------------------------- #
# Chat assistant — comprehensive system prompt
# ---------------------------------------------------------------------- #
CHAT_SYS_PROMPT = """You are Equity — an expert engineering analytics assistant embedded in a GitHub PR analysis platform. You are a real conversational AI, not a FAQ bot. You can answer ANY question the user asks.

## What you know
You have deep expertise in:
- Reviewer workload fairness and inequality (Gini coefficient, overloaded reviewers)
- PR delays, stale PRs, time-to-first-review, approval turnaround
- Review quality (rubber-stamp approvals, constructive vs shallow comments)
- PR risk scoring (large diffs, single reviewer, fast approvals)
- Bias and favoritism detection (reviewer-author patterns, isolated authors)
- Reviewer burnout (late-night and weekend activity patterns)
- Emotional intelligence and psychological safety (tone analysis, at-risk authors)
- Smart reviewer recommendations for open PRs
- Team health composite score breakdown
- Software engineering best practices, code review culture, GitHub workflows, team dynamics

## How to handle any question
- **Specific data questions** ("Who is overloaded?", "Show risky PRs"): Ground your answer in the ANALYTICS CONTEXT. Cite real names and numbers.
- **Comparison questions** ("How does Alice compare to Bob?", "Which area is worst?"): Cross-reference multiple sections of context to compare.
- **Trend/pattern questions** ("Are things improving?", "What's the biggest problem?"): Synthesize across inequality, delays, risk, burnout, and emotional data.
- **General knowledge questions** ("What is a Gini coefficient?", "How should we do code reviews?"): Answer from your expertise, then relate to the repo's data where relevant.
- **Follow-up questions** ("Tell me more", "Why?", "What about X?"): Use conversation history to maintain full context.
- **Vague questions** ("Is our team healthy?", "Anything concerning?"): Pick the most important signals from context and surface them proactively.
- **Action/advice questions** ("What should we fix first?", "How do we improve?"): Prioritize by impact, be concrete and specific.
- **Off-topic questions**: Answer helpfully from general knowledge and gently bridge back to the repo if relevant.

## Response rules
1. **Always answer** — never say "I can't answer that." If data is unavailable, use general knowledge.
2. Lead with the direct answer in 1–2 sentences.
3. Use markdown: **bold** for names/numbers, bullet lists for multiple items, `code` for PR numbers/identifiers.
4. For specific claims, cite the source ("PR #42 has a risk score of 78 because…").
5. Never dump raw JSON. Translate everything to plain English.
6. Keep answers focused and actionable — no filler.
7. For follow-ups, reference what was discussed earlier — you have conversation memory.
"""


# ---------------------------------------------------------------------- #
# Context condensation — richer than before, includes emotional data
# ---------------------------------------------------------------------- #
def _condense_context(analytics: Dict[str, Any], repo_label: str) -> Dict[str, Any]:
    """Trim the analytics payload to fit comfortably in a chat context window."""
    inequality = analytics.get("inequality", {})
    delays     = analytics.get("delays", {})
    quality    = analytics.get("quality", {})
    risk       = analytics.get("risk", {})
    bias       = analytics.get("bias", {})
    burnout    = analytics.get("burnout", {})
    recs       = analytics.get("recommendations", {})
    summary    = analytics.get("summary", {})
    emotional  = analytics.get("emotional", {})

    reviewer_quality: List[Dict] = quality.get("reviewer_quality") or []

    return {
        "repo": repo_label,
        "summary": summary,
        "inequality": {
            "fairness_score": inequality.get("fairness_score"),
            "gini": inequality.get("gini"),
            "unique_reviewers": inequality.get("unique_reviewers"),
            "distribution": inequality.get("distribution", [])[:20],
            "overloaded": inequality.get("overloaded", [])[:10],
            "inactive": inequality.get("inactive", [])[:10],
        },
        "delays": {
            "first_review_stats": delays.get("first_review_stats"),
            "approval_stats":     delays.get("approval_stats"),
            "merge_stats":        delays.get("merge_stats"),
            "stale_prs": [
                {k: pr[k] for k in ("number", "title", "author", "age_hours") if k in pr}
                for pr in delays.get("stale_prs", [])[:15]
            ],
        },
        "quality": {
            "buckets":               quality.get("buckets"),
            "overall_avg_score":     quality.get("overall_avg_score"),
            "rubber_stamp_count":    len(quality.get("rubber_stamp_approvals", [])),
            "rubber_stamp_examples": quality.get("rubber_stamp_approvals", [])[:8],
            "top_reviewers":         reviewer_quality[:8],
            "bottom_reviewers":      reviewer_quality[-5:] if len(reviewer_quality) > 5 else [],
        },
        "risk": {
            "summary": risk.get("summary"),
            "top_risky": [
                {k: pr[k] for k in
                 ("number", "title", "author", "risk_score", "risk_level", "reasons")
                 if k in pr}
                for pr in risk.get("prs", [])[:12]
            ],
        },
        "bias": {
            "favoritism":       bias.get("favoritism", [])[:8],
            "isolated_authors": bias.get("isolated_authors", [])[:8],
            "repeated_groups":  bias.get("repeated_groups", [])[:5],
        },
        "burnout": {
            "at_risk":       burnout.get("at_risk", [])[:10],
            "all_reviewers": burnout.get("reviewers", [])[:15],
        },
        "emotional": {
            "team_psych_safety_score": emotional.get("team_psych_safety_score"),
            "tone_buckets":            emotional.get("tone_buckets"),
            "reviewer_ei":             emotional.get("reviewer_ei", [])[:10],
            "at_risk_authors":         emotional.get("at_risk_authors", [])[:8],
            "flagged_comments":        emotional.get("flagged_comments", [])[:10],
        },
        "recommendations": {
            "open_pr_suggestions": recs.get("recommendations", [])[:8],
        },
    }


# ---------------------------------------------------------------------- #
# Rule-based chat — handler functions (one per intent)
# ---------------------------------------------------------------------- #

def _h_concepts(q: str, ctx: Dict[str, Any]) -> Optional[str]:
    """Explain analytics concepts by name."""
    if "gini" in q:
        g = ctx["inequality"]["gini"]
        level = ("well-balanced" if (g or 1) < 0.3
                 else "moderate concentration" if (g or 0) < 0.6
                 else "highly concentrated")
        return (f"**Gini coefficient** measures inequality (0=equal, 1=one person does everything).\n"
                f"This repo scores **{g}** — {level}.")
    if "health score" in q or "team health" in q:
        s = ctx["summary"]
        return (f"**Team Health Score** (0–100) composite:\n"
                f"• Fairness 25%: **{s.get('fairness_score')}/100** | "
                f"Psych safety 25%: **{s.get('psych_safety_score')}/100**\n"
                f"• Review quality 15%: **{s.get('review_quality_score')}/100** | "
                f"Review speed 15% | Risk rate 10% | Rubber-stamp rate 10%\n"
                f"Current: **{s.get('team_health_score')}/100**")
    if "burnout" in q and "score" in q:
        return ("**Burnout score** (0–100) measures off-hours review stress:\n"
                "• Late-night reviews (22:00–06:00) and weekend reviews raise the score\n"
                "• Reviewers ≥ 50 are flagged at-risk\n"
                f"Currently {len(ctx['burnout']['at_risk'])} reviewer(s) are at risk.")
    if "rubber stamp" in q or "rubber-stamp" in q:
        n = ctx["quality"]["rubber_stamp_count"]
        return (f"A **rubber-stamp approval** is 'LGTM' / '+1' with no substantive comment.\n"
                f"This repo has **{n}** rubber-stamp approvals in the analyzed window.")
    if "risk score" in q or "pr risk" in q:
        return ("**PR risk score** (0–100):\n"
                "• Large diff (>400 lines=+20, >1000=+35) | Single reviewer (+20)\n"
                "• Fast approval on big diff (<1h/>100 lines=+25) | No comments (+15)\n"
                "• File spread >30 (+10)\n"
                "≥60=high · 30–59=medium · <30=low")
    if "ei score" in q or "emotional intelligence" in q:
        psych = ctx.get("emotional", {}).get("team_psych_safety_score", "N/A")
        return (f"**EI score** (0–100) — how constructive a reviewer's language is:\n"
                "• 80–100=safe · 60–79=mostly safe · 40–59=borderline · 0–39=at-risk\n"
                f"Team safety score: **{psych}/100**")
    if "fairness" in q:
        f_s = ctx["inequality"]["fairness_score"]
        g = ctx["inequality"]["gini"]
        return (f"**Fairness score** (0–100) derived from the Gini coefficient of review load.\n"
                f"100=perfectly equal; 0=one person does everything.\n"
                f"This repo: **{f_s}/100** (Gini {g})")
    return None


def _h_help(ctx: Dict[str, Any]) -> str:
    health = ctx["summary"].get("team_health_score", "N/A")
    repo = ctx.get("repo", "this repo")
    return (
        f"I can answer questions about **{repo}** (health: **{health}/100**):\n\n"
        "• **Workload** — 'Who is overloaded?', 'Show review distribution'\n"
        "• **Delays** — 'Which PRs are stale?', 'Average review time'\n"
        "• **Quality** — 'Best reviewers', 'Rubber-stamp approvals'\n"
        "• **Risk** — 'Risky PRs', 'List high-risk PRs'\n"
        "• **Bias** — 'Favoritism patterns', 'Isolated authors'\n"
        "• **Burnout** — 'Who is burning out?', 'Late-night patterns'\n"
        "• **Emotional safety** — 'Psychological safety score', 'Tone analysis'\n"
        "• **Recommendations** — 'Suggest reviewers for open PRs'\n"
        "• **Concepts** — 'What is the Gini coefficient?', 'Explain the health score'\n"
        "• **Actions** — 'What should we improve?', 'Top action items'"
    )


def _h_health(ctx: Dict[str, Any]) -> str:
    s = ctx["summary"]
    return (
        f"**{ctx['repo']}** — Team Health: **{s.get('team_health_score')}/100**\n\n"
        f"• PRs: {s.get('total_prs')} ({s.get('open_prs')} open · "
        f"{s.get('merged_prs')} merged · {s.get('closed_unmerged', 0)} closed-unmerged)\n"
        f"• {s.get('unique_authors')} authors · {s.get('unique_reviewers')} reviewers\n"
        f"• Fairness **{s.get('fairness_score')}/100** | "
        f"Quality **{s.get('review_quality_score')}/100** | "
        f"Psych safety **{s.get('psych_safety_score')}/100**\n"
        f"• High-risk PRs **{s.get('high_risk_count')}** | "
        f"Stale **{s.get('stale_count')}** | "
        f"At-risk authors **{s.get('at_risk_authors_count', 0)}**"
    )


def _h_workload(ctx: Dict[str, Any]) -> str:
    ov = ctx["inequality"]["overloaded"]
    dist = ctx["inequality"]["distribution"][:6]
    if not ov:
        lines = [f"No reviewers flagged as overloaded in **{ctx['repo']}** — load is balanced."]
        if dist:
            lines.append("\nTop reviewers by volume:")
            for d in dist:
                lines.append(f"• **{d['reviewer']}** — {d['review_count']} reviews")
        return "\n".join(lines)
    lines = [f"**{len(ov)} overloaded reviewer(s)** (above 80th percentile):"]
    for o in ov[:6]:
        lines.append(f"• **{o['reviewer']}** — {o['review_count']} reviews "
                     f"({o.get('share_pct', '?')}% of all reviews)")
    return "\n".join(lines)


def _h_inactive(ctx: Dict[str, Any]) -> str:
    inactive = ctx["inequality"]["inactive"]
    if not inactive:
        return f"No reviewers flagged as inactive in **{ctx['repo']}** currently."
    lines = [f"**{len(inactive)} inactive reviewer(s):**"]
    for r in inactive[:8]:
        lines.append(f"• **{r['reviewer']}** — {r.get('reason', 'inactive')} "
                     f"({r.get('times_requested', 0)}× requested, "
                     f"{r.get('review_count', 0)} done)")
    return "\n".join(lines)


def _h_fairness(ctx: Dict[str, Any]) -> str:
    f_s = ctx["inequality"]["fairness_score"]
    gini = ctx["inequality"]["gini"]
    level = ("balanced" if (gini or 1) < 0.35
             else "moderate concentration" if (gini or 0) < 0.6
             else "highly concentrated")
    lines = [f"**Fairness: {f_s}/100** | Gini: **{gini}** ({level})"]
    for d in ctx["inequality"]["distribution"][:10]:
        lines.append(f"• **{d['reviewer']}** — {d['review_count']} reviews")
    return "\n".join(lines)


def _h_stale(ctx: Dict[str, Any]) -> str:
    stale = ctx["delays"]["stale_prs"]
    stats = ctx["delays"]["first_review_stats"] or {}
    parts = []
    for key in ("median", "avg", "p90"):
        v = stats.get(key)
        if v is not None:
            parts.append(f"{key} **{round(v, 1)}h**")
    timing = ("Time to first review: " + ", ".join(parts) + ".") if parts else ""
    if not stale:
        return (timing + "\n\nNo stale PRs currently.") if timing else "No stale PRs currently."
    lines = [timing, f"\n**{len(stale)} stale PR(s):**"]
    for s in stale[:8]:
        days = round(s["age_hours"] / 24, 1)
        lines.append(f"• PR **#{s['number']}** _{s['title'][:55]}_ "
                     f"by {s['author']} — **{days} days**")
    return "\n".join(lines)


def _h_timing(ctx: Dict[str, Any]) -> str:
    d = ctx["delays"]
    first = d.get("first_review_stats") or {}
    appr  = d.get("approval_stats") or {}
    merge = d.get("merge_stats") or {}
    lines = ["**PR timing statistics:**"]
    if first.get("median") is not None:
        lines.append(f"• First review: median **{round(first['median'], 1)}h** · "
                     f"avg {round(first.get('avg') or 0, 1)}h · "
                     f"p90 {round(first.get('p90') or 0, 1)}h")
    if appr.get("median") is not None:
        lines.append(f"• Approval: median **{round(appr['median'], 1)}h**")
    if merge.get("median") is not None:
        lines.append(f"• Merge: median **{round(merge['median'], 1)}h**")
    return "\n".join(lines) if len(lines) > 1 else "No timing data available yet."


def _h_risk(ctx: Dict[str, Any]) -> str:
    risky = ctx["risk"]["top_risky"]
    summ  = ctx["risk"]["summary"] or {}
    if not risky:
        return "No high-risk PRs detected in the analyzed window."
    lines = [f"Risk: **{summ.get('high', 0)} high** · "
             f"{summ.get('medium', 0)} medium · {summ.get('low', 0)} low\n"]
    for r in risky[:6]:
        reasons = ", ".join(r.get("reasons", [])[:2]) or "multiple signals"
        lines.append(f"• PR **#{r['number']}** _{r['title'][:55]}_ "
                     f"— **{r['risk_score']}/100** [{r['risk_level']}] ({reasons})")
    return "\n".join(lines)


def _h_quality(ctx: Dict[str, Any]) -> str:
    top     = ctx["quality"]["top_reviewers"]
    avg     = ctx["quality"]["overall_avg_score"]
    buckets = ctx["quality"]["buckets"] or {}
    lines   = [f"Overall review quality: **{avg}/5** (1=rubber stamp → 5=high quality)"]
    if buckets:
        lines.append("\nDistribution:")
        for label, count in sorted(buckets.items(), key=lambda x: -x[1]):
            lines.append(f"  • {label}: **{count}**")
    if top:
        lines.append("\nTop quality reviewers:")
        for r in top[:5]:
            name = r.get("reviewer") or r.get("user", "?")
            lines.append(f"• **{name}** — avg {r.get('avg_score', '?')}/5 "
                         f"({r.get('review_count', 0)} reviews)")
    return "\n".join(lines)


def _h_rubber_stamp(ctx: Dict[str, Any]) -> str:
    n   = ctx["quality"]["rubber_stamp_count"]
    ex  = ctx["quality"]["rubber_stamp_examples"]
    avg = ctx["quality"]["overall_avg_score"]
    lines = [f"**{n} rubber-stamp approval(s)** (no substantive review content)."]
    if avg:
        lines.append(f"Overall review quality: **{avg}/5**")
    for e in ex[:5]:
        lines.append(f"• PR **#{e['pr_number']}** by {e.get('reviewer', '?')}: "
                     f"\"{e.get('body_excerpt', '')[:60]}\"")
    return "\n".join(lines)


def _h_bias(ctx: Dict[str, Any]) -> str:
    fav    = ctx["bias"]["favoritism"]
    iso    = ctx["bias"]["isolated_authors"]
    groups = ctx["bias"].get("repeated_groups", [])
    lines: List[str] = []
    if fav:
        lines.append("**Favoritism patterns:**")
        for f in fav[:5]:
            lines.append(f"• **{f['reviewer']}** approves **{f['author']}**'s PRs "
                         f"{f.get('share_pct', '?')}% of the time "
                         f"({f.get('approvals', 0)} of {f.get('author_total_approvals', 0)})")
    if iso:
        lines.append("\n**Isolated authors** (only one reviewer ever approves them):")
        for i in iso[:5]:
            lines.append(f"• **{i['author']}** — sole reviewer **{i['sole_reviewer']}** "
                         f"({i.get('approvals', 0)} times)")
    if groups:
        lines.append("\n**Repeated approval groups:**")
        for g in groups[:3]:
            lines.append(f"• {', '.join(g.get('reviewers', []))} — {g.get('count', 0)} times")
    return "\n".join(lines) if lines else "No strong bias signals detected."


def _h_burnout(ctx: Dict[str, Any]) -> str:
    at_risk = ctx["burnout"]["at_risk"]
    if not at_risk:
        return "No reviewers at burnout-risk threshold (score ≥ 50)."
    lines = [f"**{len(at_risk)} reviewer(s) with burnout signals** (score ≥ 50/100):"]
    for r in at_risk[:6]:
        lines.append(f"• **{r['reviewer']}** — **{r['burnout_score']}/100** "
                     f"(late-night {r.get('late_night_pct', 0)}%, "
                     f"weekends {r.get('weekend_pct', 0)}%)")
    return "\n".join(lines)


def _h_emotional(ctx: Dict[str, Any]) -> str:
    ei      = ctx.get("emotional", {})
    psych   = ei.get("team_psych_safety_score")
    rev_ei  = ei.get("reviewer_ei", [])
    authors = ei.get("at_risk_authors", [])
    flagged = ei.get("flagged_comments", [])
    tone_b  = ei.get("tone_buckets") or {}
    lines   = [f"**Team Psychological Safety: {psych}/100**"]
    if tone_b:
        lines.append("\nTone distribution:")
        for tone, count in sorted(tone_b.items(), key=lambda x: -x[1]):
            lines.append(f"  • {tone}: {count}")
    if rev_ei:
        lines.append("\nReviewer EI scores:")
        for r in rev_ei[:6]:
            tb  = r.get("tone_breakdown", {})
            pos = tb.get("constructive", 0) + tb.get("supportive", 0)
            lines.append(f"• **{r['reviewer']}** — **{r['ei_score']}/100** "
                         f"({tb.get('hostile', 0)} hostile · {pos} positive)")
    if authors:
        lines.append("\nAt-risk authors (mostly negative feedback):")
        for a in authors[:4]:
            lines.append(f"• **{a['author']}** — {a.get('negative_pct', '?')}% negative "
                         f"({a.get('total_comments', 0)} comments)")
    if flagged:
        lines.append(f"\n{len(flagged)} flagged comments (hostile/harsh/dismissive).")
    return "\n".join(lines)


def _h_at_risk_authors(ctx: Dict[str, Any]) -> str:
    at_risk = ctx.get("emotional", {}).get("at_risk_authors", [])
    if not at_risk:
        return "No authors flagged for predominantly negative feedback."
    lines = [f"**{len(at_risk)} author(s) receiving disproportionate negative feedback:**"]
    for a in at_risk[:6]:
        lines.append(f"• **{a['author']}** — {a.get('negative_pct', '?')}% negative "
                     f"({a.get('total_comments', 0)} comments)")
    return "\n".join(lines)


def _h_recommendations(ctx: Dict[str, Any]) -> str:
    recs = ctx["recommendations"]["open_pr_suggestions"]
    if not recs:
        return "No open PRs need reviewer suggestions right now."
    lines = ["**Suggested reviewers for open PRs** (workload-balanced):"]
    for r in recs[:6]:
        sugg = ", ".join(s["reviewer"] for s in r.get("suggested_reviewers", [])[:3])
        lines.append(f"• PR **#{r['pr_number']}** _{r.get('pr_title', '')[:50]}_ → **{sugg}**")
    return "\n".join(lines)


def _h_improvements(ctx: Dict[str, Any]) -> str:
    s    = ctx["summary"]
    repo = ctx.get("repo", "this repository")
    issues: List[str] = []
    if (s.get("fairness_score") or 100) < 70:
        names = ", ".join(o["reviewer"] for o in ctx["inequality"]["overloaded"][:3]) or "some reviewers"
        issues.append(f"**Redistribute reviews** (fairness {s.get('fairness_score')}/100) — "
                      f"load-balance away from {names}.")
    if (s.get("high_risk_count") or 0) > 3:
        issues.append(f"**Review {s['high_risk_count']} high-risk PRs** — require 2+ reviewers on large diffs.")
    if len(ctx["delays"]["stale_prs"]) > 2:
        issues.append(f"**Triage {len(ctx['delays']['stale_prs'])} stale PRs** — set a 24h review SLA.")
    if (s.get("review_quality_score") or 100) < 60:
        issues.append("**Raise review quality** — ban LGTM-only approvals on diffs >100 lines.")
    if (s.get("psych_safety_score") or 100) < 70:
        issues.append(f"**Improve psychological safety** ({s.get('psych_safety_score')}/100) — "
                      "coach reviewers toward constructive language.")
    if (s.get("at_risk_authors_count") or 0) > 0:
        issues.append(f"**Support {s['at_risk_authors_count']} at-risk developer(s)** receiving negative feedback.")
    burnout_ar = ctx["burnout"]["at_risk"]
    if burnout_ar:
        names = ", ".join(r["reviewer"] for r in burnout_ar[:3])
        issues.append(f"**Address burnout** for {names} — reviewing heavily during off-hours.")
    if not issues:
        return (f"**{repo}** looks healthy (score {s.get('team_health_score')}/100). "
                "Keep monitoring review distribution and PR quality.")
    lines = [f"**Key improvements for {repo}** (health {s.get('team_health_score')}/100):\n"]
    lines.extend(f"{i+1}. {issue}" for i, issue in enumerate(issues))
    return "\n".join(lines)


def _h_pr_lookup(q: str, ctx: Dict[str, Any]) -> Optional[str]:
    m = re.search(r"#(\d+)|pr[# ]+(\d+)|pull[# ]+request[# ]+(\d+)", q)
    if not m:
        return None
    pr_num = int(next(g for g in m.groups() if g is not None))
    for pr in ctx["risk"]["top_risky"]:
        if pr.get("number") == pr_num:
            reasons = ", ".join(pr.get("reasons", []))
            return (f"**PR #{pr_num}** _{pr.get('title', '')}_ by {pr.get('author', 'unknown')}\n"
                    f"• Risk: **{pr['risk_score']}/100** ({pr['risk_level']}) — "
                    f"{reasons or 'no specific flags'}")
    for pr in ctx["delays"]["stale_prs"]:
        if pr.get("number") == pr_num:
            days = round(pr["age_hours"] / 24, 1)
            return (f"**PR #{pr_num}** _{pr.get('title', '')}_ by {pr.get('author', 'unknown')}\n"
                    f"• Status: **Stale** — open {days} days without review")
    return (f"PR **#{pr_num}** is not in the top-risk or stale lists — "
            "appears within normal parameters.")


def _h_reviewer_lookup(q: str, ctx: Dict[str, Any]) -> Optional[str]:
    dist    = ctx["inequality"]["distribution"]
    ov      = ctx["inequality"]["overloaded"]
    bo_list = ctx["burnout"]["all_reviewers"]
    ei_list = ctx.get("emotional", {}).get("reviewer_ei", [])
    rq_list = ctx["quality"]["top_reviewers"]
    for candidate in dist[:20]:
        if candidate["reviewer"].lower() not in q:
            continue
        found = candidate["reviewer"]
        is_ov = any(o["reviewer"] == found for o in ov)
        bo = next((r for r in bo_list if r["reviewer"] == found), None)
        ei = next((r for r in ei_list if r["reviewer"] == found), None)
        rq = next((r for r in rq_list if r.get("reviewer", r.get("user")) == found), None)
        lines = [f"**{found}** — {candidate['review_count']} reviews "
                 f"({'⚠ overloaded' if is_ov else 'normal load'})"]
        if rq:
            lines.append(f"• Quality: **{rq.get('avg_score', '?')}/5**")
        if bo:
            lines.append(f"• Burnout: **{bo['burnout_score']}/100** "
                         f"(late-night {bo.get('late_night_pct', 0)}%, "
                         f"weekends {bo.get('weekend_pct', 0)}%)")
        if ei:
            lines.append(f"• EI: **{ei['ei_score']}/100** ({ei.get('level', 'unknown')})")
        return "\n".join(lines)
    return None


# Keyword → handler mapping (checked in order)
_INTENT_MAP: List[tuple] = [
    (("what is", "explain", "what does", "what are", "define", "how does"),
     lambda q, ctx: _h_concepts(q, ctx)),
    (("help", "what can you", "what do you know", "capabilities", "what questions"),
     lambda q, ctx: _h_help(ctx)),
    (("health", "summary", "overview", "overall", "how is the team",
      "how are we doing", "team status", "dashboard"),
     lambda q, ctx: _h_health(ctx)),
    (("overload", "too many", "most reviews", "burdened", "busiest",
      "heavy load", "review load", "who reviews most", "top reviewer"),
     lambda q, ctx: _h_workload(ctx)),
    (("inactive", "never review", "ghost", "absent", "not reviewing",
      "dormant", "who stopped", "unresponsive"),
     lambda q, ctx: _h_inactive(ctx)),
    (("fair", "equal", "distribution", "gini", "spread",
      "balanced", "inequality", "unequal"),
     lambda q, ctx: _h_fairness(ctx)),
    (("stale", "slow", "delay", "waiting", "no review",
      "without review", "stuck", "old pr", "pending"),
     lambda q, ctx: _h_stale(ctx)),
    (("how long", "time to review", "review time", "approval time",
      "merge time", "turnaround", "how fast", "how slow"),
     lambda q, ctx: _h_timing(ctx)),
    (("risky", "risk", "dangerous", "large pr", "big pr",
      "unsafe", "high risk", "critical pr"),
     lambda q, ctx: _h_risk(ctx)),
    (("quality", "review score", "best reviewer", "worst reviewer",
      "quality rank", "constructive", "detailed review"),
     lambda q, ctx: _h_quality(ctx)),
    (("rubber", "lgtm", "low quality", "low-quality", "shallow",
      "stamp", "trivial approval", "+1"),
     lambda q, ctx: _h_rubber_stamp(ctx)),
    (("favorit", "bias", "isolated", "unfair", "preference", "clique",
      "nepotism", "always approve", "one reviewer"),
     lambda q, ctx: _h_bias(ctx)),
    (("burnout", "burn out", "burning out", "exhausted", "late night",
      "weekend", "overwork", "stressed", "off hours", "after hours"),
     lambda q, ctx: _h_burnout(ctx)),
    (("emotional", "ei score", "psych", "psychological", "safety",
      "toxic", "hostile", "harsh", "tone", "sentiment",
      "negative comment", "abusive", "constructive feedback"),
     lambda q, ctx: _h_emotional(ctx)),
    (("at risk", "at-risk", "negative feedback", "harsh feedback",
      "who is suffering", "treated poorly"),
     lambda q, ctx: _h_at_risk_authors(ctx)),
    (("recommend", "suggest", "who should review", "assign",
      "reviewer for", "who to review", "pick reviewer"),
     lambda q, ctx: _h_recommendations(ctx)),
    (("improve", "better", "fix", "action", "what should", "priorit",
      "advice", "issues to address", "problems"),
     lambda q, ctx: _h_improvements(ctx)),
]


def _h_full_summary(ctx: Dict[str, Any]) -> str:
    """Comprehensive catch-all summary covering all analytics dimensions."""
    s = ctx["summary"]
    repo = ctx.get("repo", "this repository")
    lines = [f"**{repo}** — Team Health: **{s.get('team_health_score')}/100**\n"]

    # Inequality
    gini = ctx["inequality"]["gini"]
    ov = ctx["inequality"]["overloaded"]
    lines.append(f"**Workload fairness:** {ctx['inequality']['fairness_score']}/100 (Gini {gini})"
                 + (f" — {len(ov)} overloaded" if ov else " — balanced"))

    # Delays
    stale = ctx["delays"]["stale_prs"]
    first = ctx["delays"].get("first_review_stats") or {}
    if first.get("median") is not None:
        lines.append(f"**Review speed:** median first-review {round(first['median'], 1)}h"
                     + (f", {len(stale)} stale PRs" if stale else ""))

    # Risk
    risk_s = ctx["risk"]["summary"] or {}
    lines.append(f"**Risk:** {risk_s.get('high', 0)} high · {risk_s.get('medium', 0)} medium · "
                 f"{risk_s.get('low', 0)} low")

    # Quality
    avg = ctx["quality"]["overall_avg_score"]
    rs = ctx["quality"]["rubber_stamp_count"]
    lines.append(f"**Review quality:** {avg}/5 avg · {rs} rubber-stamp approval(s)")

    # Emotional
    psych = ctx["emotional"].get("team_psych_safety_score")
    at_risk = ctx["emotional"].get("at_risk_authors", [])
    if psych is not None:
        lines.append(f"**Psych safety:** {psych}/100"
                     + (f" · {len(at_risk)} author(s) receiving harsh feedback" if at_risk else ""))

    # Burnout
    bo = ctx["burnout"]["at_risk"]
    if bo:
        lines.append(f"**Burnout signals:** {len(bo)} reviewer(s) at risk — "
                     + ", ".join(r["reviewer"] for r in bo[:3]))

    return "\n".join(lines)


def _score_intents(q: str) -> List[tuple]:
    """Score each intent by how many of its keywords appear in q. Return sorted list."""
    scores = []
    for keywords, handler in _INTENT_MAP:
        hits = sum(1 for k in keywords if k in q)
        if hits:
            scores.append((hits, handler))
    scores.sort(key=lambda x: -x[0])
    return scores


def _rule_based_chat(question: str, ctx: Dict[str, Any]) -> str:
    q = question.lower().strip()

    # PR-specific lookup (has its own regex detection)
    pr_answer = _h_pr_lookup(q, ctx)
    if pr_answer:
        return pr_answer

    # Reviewer-specific lookup (name mentioned)
    rv_answer = _h_reviewer_lookup(q, ctx)
    if rv_answer:
        return rv_answer

    # Score all intents and pick the best match
    scored = _score_intents(q)
    if scored:
        result = scored[0][1](q, ctx)
        if result:
            return result

    # Generic analytics catch-all for any question we couldn't match
    # Try to determine what dimension the user is curious about
    combined = _h_full_summary(ctx)
    repo = ctx.get("repo", "this repository")

    # If the question sounds like it wants a specific answer, acknowledge
    question_words = ("who", "which", "what", "when", "why", "how", "show", "list", "give", "tell")
    if any(q.startswith(w) for w in question_words):
        return (
            f"Here's what I found for **{repo}**:\n\n"
            + combined
            + "\n\nFor a specific breakdown, try asking about workload, delays, risk, "
              "quality, burnout, bias, or psychological safety."
        )

    return combined


# ---------------------------------------------------------------------- #
# Main chat entry point
# ---------------------------------------------------------------------- #
def chat_with_analytics(question: str,
                        analytics: Dict[str, Any],
                        repo_label: str = "the repository",
                        history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    ctx = _condense_context(analytics, repo_label)
    provider = _provider()

    if provider == "rule-based":
        return {"answer": _rule_based_chat(question, ctx), "provider": provider}

    messages: List[Dict[str, str]] = [{"role": "system", "content": CHAT_SYS_PROMPT}]
    messages.append({
        "role": "system",
        "content": "ANALYTICS CONTEXT (JSON):\n" + json.dumps(ctx, default=str)[:16000],
    })
    for h in (history or [])[-20:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"][:3000]})
    messages.append({"role": "user", "content": question})

    try:
        if provider == "anthropic":
            text = _claude_chat(messages, max_tokens=2000, temperature=0.3)
        elif provider == "openai":
            model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            text = _openai_chat(messages, model=model, max_tokens=2000, temperature=0.3)
        else:
            text = _ollama_chat(messages, max_tokens=2000, temperature=0.3)

        if not text.strip():
            text = _rule_based_chat(question, ctx)
            provider = "rule-based-fallback"
        return {"answer": text.strip(), "provider": provider}
    except Exception as e:
        return {
            "answer": _rule_based_chat(question, ctx)
                      + f"\n\n_(AI provider unavailable: {type(e).__name__})_",
            "provider": "rule-based-fallback",
        }


# ---------------------------------------------------------------------- #
# Streaming chat entry point (yields text chunks for SSE)
# ---------------------------------------------------------------------- #
def chat_with_analytics_stream(question: str,
                                analytics: Dict[str, Any],
                                repo_label: str = "the repository",
                                history: Optional[List[Dict[str, str]]] = None):
    """Generator that yields text chunks. Used for SSE streaming."""
    import time

    ctx = _condense_context(analytics, repo_label)
    provider = _provider()

    # For rule-based, simulate streaming by word-chunking
    if provider == "rule-based":
        answer = _rule_based_chat(question, ctx)
        for word in answer.split(" "):
            yield word + " "
            time.sleep(0.015)
        return

    messages: List[Dict[str, str]] = [{"role": "system", "content": CHAT_SYS_PROMPT}]
    messages.append({
        "role": "system",
        "content": "ANALYTICS CONTEXT (JSON):\n" + json.dumps(ctx, default=str)[:16000],
    })
    for h in (history or [])[-20:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"][:3000]})
    messages.append({"role": "user", "content": question})

    try:
        if provider == "anthropic":
            yield from _claude_stream(messages)
        elif provider == "openai":
            yield from _openai_stream(messages)
        else:
            # Ollama: non-streaming fallback with word chunking
            text = _ollama_chat(messages, max_tokens=2000, temperature=0.3)
            for word in text.split(" "):
                yield word + " "
                time.sleep(0.01)
    except Exception:
        answer = _rule_based_chat(question, ctx)
        for word in answer.split(" "):
            yield word + " "
            time.sleep(0.015)


def _claude_stream(messages: List[Dict[str, str]]):
    """Stream tokens from Anthropic Claude."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    chat_msgs = _merge_consecutive_roles([m for m in messages if m["role"] != "system"])
    if not chat_msgs:
        chat_msgs = [{"role": "user", "content": "Hello"}]

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        with client.messages.stream(
            model=model,
            max_tokens=2000,
            system="\n\n".join(system_parts),
            messages=chat_msgs,
            temperature=0.3,
        ) as stream:
            for text in stream.text_stream:
                yield text
    except Exception:
        # Fallback: streaming via REST
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model, "max_tokens": 1200, "temperature": 0.2,
                "system": "\n\n".join(system_parts),
                "messages": chat_msgs,
                "stream": True,
            },
            stream=True, timeout=60,
        )
        for line in resp.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8") if isinstance(line, bytes) else line
            if line.startswith("data:"):
                try:
                    chunk = json.loads(line[5:].strip())
                    if chunk.get("type") == "content_block_delta":
                        yield chunk.get("delta", {}).get("text", "")
                except Exception:
                    pass


def _openai_stream(messages: List[Dict[str, str]]):
    """Stream tokens from OpenAI."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        with client.chat.completions.create(
            model=model, messages=messages, max_tokens=1200,
            temperature=0.2, stream=True,
        ) as stream:
            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content
    except Exception:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": messages,
                  "max_tokens": 1200, "temperature": 0.2, "stream": True},
            stream=True, timeout=60,
        )
        for line in resp.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8") if isinstance(line, bytes) else line
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[6:])
                    content = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                    if content:
                        yield content
                except Exception:
                    pass

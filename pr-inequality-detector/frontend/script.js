/* =============================================================
   Equity — frontend logic
   ============================================================= */

const API = "";   // same origin
const charts = {};   // chart instances we can destroy on re-render
let currentAnalysisId = null;
let currentRepo = null;   // "owner/repo" of last analysis
let _savedRepos = [];
let _activeLabels = new Set();

/* ---------- DOM helpers (defined early — used throughout auth + app) ---------- */
const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const escapeHtml = (s = "") =>
  String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

const fmtHours = (h) => {
  if (h === null || h === undefined) return "—";
  if (h < 1) return `${Math.round(h * 60)}m`;
  if (h < 48) return `${h.toFixed(1)}h`;
  return `${(h / 24).toFixed(1)}d`;
};

/* =============================================================
   AUTH STATE
   ============================================================= */
const TOKEN_KEY = "equity_auth_token";
let _authToken = localStorage.getItem(TOKEN_KEY) || null;
let _pendingEmail = "";   // email waiting for OTP verification

/* Attach the Bearer token to every protected API call */
async function authFetch(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (_authToken) headers["Authorization"] = `Bearer ${_authToken}`;
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401) {
    // Session expired or invalid — show login
    _authToken = null;
    localStorage.removeItem(TOKEN_KEY);
    showAuthOverlay("login");
    throw new Error("Session expired. Please sign in again.");
  }
  return res;
}

/* ----- Overlay show / hide ----- */
function showAuthOverlay(panel = "login") {
  document.body.classList.add("auth-open");
  $("#authOverlay").classList.remove("hidden");
  showAuthPanel(panel === "register" ? "panelRegister" : "panelLogin");
}

function hideAuthOverlay() {
  document.body.classList.remove("auth-open");
  $("#authOverlay").classList.add("hidden");
}

function showAuthPanel(id) {
  ["panelLogin", "panelLoginOtp", "panelRegister", "panelRegisterOtp"].forEach(p => {
    const el = document.getElementById(p);
    if (el) el.classList.toggle("hidden", p !== id);
  });
  // Auto-focus first visible input
  const panel = document.getElementById(id);
  if (panel) {
    const input = panel.querySelector("input");
    if (input) setTimeout(() => input.focus(), 60);
  }
}

/* =============================================================
   THEME TOGGLE
   ============================================================= */
(function initTheme() {
  const saved = localStorage.getItem("equity_theme") || "dark";
  document.documentElement.setAttribute("data-theme", saved);
  const btn = document.getElementById("themeToggle");
  if (btn) btn.textContent = saved === "dark" ? "☀" : "🌙";
})();

document.getElementById("themeToggle")?.addEventListener("click", () => {
  const html = document.documentElement;
  const current = html.getAttribute("data-theme");
  const next = current === "dark" ? "light" : "dark";
  html.setAttribute("data-theme", next);
  localStorage.setItem("equity_theme", next);
  const btn = document.getElementById("themeToggle");
  if (btn) btn.textContent = next === "dark" ? "☀" : "🌙";
  // Update chart colors for the new theme
  updateChartTheme(next);
});

function updateChartTheme(theme) {
  const isDark = theme === "dark";
  const colors = isDark
    ? { bg: "#0b0d0f", line: "#262c35", text: "#9b988e", text0: "#f5f4ee" }
    : { bg: "#f8f7f2", line: "#c8c5b8", text: "#5a5750", text0: "#1a1815" };
  if (typeof Chart !== "undefined") {
    Chart.defaults.borderColor = colors.line;
    Chart.defaults.color = colors.text;
    Chart.defaults.plugins.tooltip.backgroundColor = isDark ? "#0b0d0f" : "#f8f7f2";
    Chart.defaults.plugins.tooltip.borderColor = colors.line;
    Chart.defaults.plugins.tooltip.titleColor = colors.text0;
    Chart.defaults.plugins.tooltip.bodyColor = colors.text;
  }
}

/* =============================================================
   GOOGLE OAUTH
   ============================================================= */
document.getElementById("googleLoginBtn")?.addEventListener("click", async () => {
  try {
    const res = await fetch(`${API}/api/auth/google`);
    const data = await res.json();
    if (data.url) {
      window.location.href = data.url;
    } else {
      setAuthError("loginError", data.error || "Google OAuth not configured on server.");
    }
  } catch (e) {
    setAuthError("loginError", "Could not reach server.");
  }
});

// Handle OAuth redirect back with token
(function handleOAuthReturn() {
  const params = new URLSearchParams(window.location.search);
  const oauthToken = params.get("oauth_token");
  const oauthName = params.get("name");
  if (oauthToken) {
    _authToken = oauthToken;
    localStorage.setItem(TOKEN_KEY, oauthToken);
    window.history.replaceState({}, "", "/");
    onAuthSuccess({ name: decodeURIComponent(oauthName || "User") });
  }
})();

function setAuthError(elId, msg) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = msg;
  el.hidden = !msg;
}

function setAuthBusy(btnId, busy, label = "Continue") {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  btn.disabled = busy;
  btn.querySelector("span").textContent = busy ? "Please wait…" : label;
}

/* ----- Session check on boot ----- */
async function initAuth() {
  if (!_authToken) { showAuthOverlay("login"); return; }
  try {
    const res = await fetch(`${API}/api/auth/me`, {
      headers: { "Authorization": `Bearer ${_authToken}` },
    });
    if (!res.ok) throw new Error("invalid");
    const data = await res.json();
    onAuthSuccess(data.user, false);   // already logged in, don't hide/show
  } catch {
    _authToken = null;
    localStorage.removeItem(TOKEN_KEY);
    showAuthOverlay("login");
  }
}

/* ----- Called after successful OTP verification ----- */
function onAuthSuccess(user, animate = true) {
  _authToken = user._token || _authToken;  // token already stored before calling
  hideAuthOverlay();

  // Populate user pill in topbar
  const pill    = $("#userPill");
  const avatar  = $("#userAvatar");
  const nameEl  = $("#userDisplayName");
  if (pill && avatar && nameEl) {
    avatar.textContent = (user.name || user.email || "?")[0].toUpperCase();
    nameEl.textContent = user.name || user.email;
    pill.classList.remove("hidden");
  }

  // Refresh data that now needs auth
  refreshAIStatus();
  loadRecent();
  loadSavedRepos();
}

/* ==================== REGISTER FLOW ==================== */
$("#registerForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  setAuthError("registerError", "");
  setAuthBusy("registerBtn", true, "Send verification code");

  const name     = $("#regName").value.trim();
  const email    = $("#regEmail").value.trim();
  const password = $("#regPassword").value;

  try {
    const res = await fetch(`${API}/api/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, email, password }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Registration failed.");
    _pendingEmail = data.email;
    $("#regOtpSub").innerHTML =
      `We sent a 6-digit code to <strong>${escapeHtml(_pendingEmail)}</strong>.<br/>It expires in 15 minutes.`;
    $("#regOtpCode").value = "";
    setAuthError("regOtpError", "");
    showAuthPanel("panelRegisterOtp");
  } catch (err) {
    setAuthError("registerError", err.message);
  } finally {
    setAuthBusy("registerBtn", false, "Send verification code");
  }
});

$("#registerOtpForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  setAuthError("regOtpError", "");
  setAuthBusy("regOtpBtn", true, "Verify & Create account");

  const code = $("#regOtpCode").value.trim();
  try {
    const res = await fetch(`${API}/api/auth/verify-otp`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: _pendingEmail, code, purpose: "register" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Verification failed.");
    _authToken = data.token;
    localStorage.setItem(TOKEN_KEY, _authToken);
    onAuthSuccess(data.user);
  } catch (err) {
    setAuthError("regOtpError", err.message);
  } finally {
    setAuthBusy("regOtpBtn", false, "Verify & Create account");
  }
});

/* ==================== LOGIN FLOW ==================== */
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  setAuthError("loginError", "");
  setAuthBusy("loginBtn", true, "Continue");

  const email    = $("#loginEmail").value.trim();
  const password = $("#loginPassword").value;

  try {
    const res = await fetch(`${API}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Login failed.");
    _pendingEmail = data.email;
    $("#loginOtpSub").innerHTML =
      `We sent a 6-digit code to <strong>${escapeHtml(_pendingEmail)}</strong>.<br/>It expires in 15 minutes.`;
    $("#loginOtpCode").value = "";
    setAuthError("loginOtpError", "");
    showAuthPanel("panelLoginOtp");
  } catch (err) {
    setAuthError("loginError", err.message);
  } finally {
    setAuthBusy("loginBtn", false, "Continue");
  }
});

$("#loginOtpForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  setAuthError("loginOtpError", "");
  setAuthBusy("loginOtpBtn", true, "Verify & Sign in");

  const code = $("#loginOtpCode").value.trim();
  try {
    const res = await fetch(`${API}/api/auth/verify-otp`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: _pendingEmail, code, purpose: "login" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Verification failed.");
    _authToken = data.token;
    localStorage.setItem(TOKEN_KEY, _authToken);
    onAuthSuccess(data.user);
  } catch (err) {
    setAuthError("loginOtpError", err.message);
  } finally {
    setAuthBusy("loginOtpBtn", false, "Verify & Sign in");
  }
});

/* ==================== PANEL SWITCHES ==================== */
$("#switchToRegister").addEventListener("click", () => {
  setAuthError("loginError", "");
  showAuthPanel("panelRegister");
});
$("#switchToLogin").addEventListener("click", () => {
  setAuthError("registerError", "");
  showAuthPanel("panelLogin");
});
$("#loginOtpBack").addEventListener("click", () => showAuthPanel("panelLogin"));
$("#regOtpBack").addEventListener("click",   () => showAuthPanel("panelRegister"));

/* ==================== OTP: digits only + auto-submit ==================== */
["loginOtpCode", "regOtpCode"].forEach(id => {
  document.getElementById(id)?.addEventListener("input", function () {
    this.value = this.value.replace(/\D/g, "").slice(0, 6);
    if (this.value.length === 6) this.closest("form").requestSubmit();
  });
});

/* ==================== LOGOUT ==================== */
$("#logoutBtn")?.addEventListener("click", async () => {
  try {
    await authFetch(`${API}/api/auth/logout`, { method: "POST" });
  } catch {}
  _authToken = null;
  localStorage.removeItem(TOKEN_KEY);
  $("#userPill")?.classList.add("hidden");
  // Reset dashboard
  $("#dashboard").hidden = true;
  currentAnalysisId = null;
  showAuthOverlay("login");
});

/* ---------- Chart.js global theme ---------- */
const COLORS = {
  ink0:   "#f5f4ee",
  ink1:   "#d8d6cf",
  ink2:   "#9b988e",
  ink3:   "#6a6760",
  line:   "#262c35",
  bg2:    "#161a20",
  bg3:    "#1c2128",
  accent: "#e8743b",
  accentSoft: "#c75a25",
  good:   "#6ec196",
  warn:   "#e8b03b",
  bad:    "#e5614c",
  info:   "#6ea3d3",
};

if (typeof Chart !== "undefined") {
  Chart.defaults.color = COLORS.ink2;
  Chart.defaults.font.family = "'Inter Tight', -apple-system, sans-serif";
  Chart.defaults.font.size = 12;
  Chart.defaults.borderColor = COLORS.line;
  Chart.defaults.plugins.legend.labels.color = COLORS.ink1;
  Chart.defaults.plugins.tooltip.backgroundColor = "#0b0d0f";
  Chart.defaults.plugins.tooltip.borderColor = COLORS.line;
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.titleColor = COLORS.ink0;
  Chart.defaults.plugins.tooltip.bodyColor = COLORS.ink1;
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.plugins.tooltip.cornerRadius = 6;
}


/* ---------- AI status pill ---------- */
async function refreshAIStatus() {
  try {
    const r = await authFetch(`${API}/api/health`);
    const data = await r.json();
    const pill = $("#aiStatusPill");
    const text = $("#aiStatusText");
    if (!data.github_token_configured) {
      pill.classList.remove("ok"); pill.classList.add("bad");
      text.textContent = "GitHub token missing";
      return;
    }
    pill.classList.remove("bad");
    if (data.ai && data.ai.available) {
      pill.classList.add("ok");
      text.textContent = `AI · ${data.ai.provider}${data.ai.model ? " · " + data.ai.model : ""}`;
    } else {
      pill.classList.add("warn");
      text.textContent = "AI · rule-based";
    }
  } catch (e) {
    $("#aiStatusText").textContent = "offline";
    $("#aiStatusPill").classList.add("bad");
  }
}

/* ---------- Recent analyses ---------- */
async function loadRecent() {
  try {
    const r = await authFetch(`${API}/api/analyses`);
    const data = await r.json();
    const items = (data.analyses || []).filter(a => a.status === "complete");
    if (!items.length) return;
    $("#recentRow").hidden = false;
    const wrap = $("#recentChips");
    wrap.innerHTML = "";
    items.slice(0, 6).forEach(a => {
      const c = document.createElement("button");
      c.className = "recent-chip";
      c.textContent = `${a.owner}/${a.repo}`;
      c.addEventListener("click", () => loadResults(a.id));
      wrap.appendChild(c);
    });
  } catch {}
}

/* ---------- Analyze flow ---------- */
$("#analyzeForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const repo = $("#repoInput").value.trim();
  const max  = parseInt($("#prCount").value, 10);
  if (!repo.includes("/")) {
    showError("Repository must be in the form 'owner/repo'.");
    return;
  }
  $("#analyzeBtn").disabled = true;
  hideError();
  $("#dashboard").hidden = true;
  showProgress("Starting analysis…", 0, max);

  currentRepo = repo;
  // Collect risk weights
  const riskWeights = {};
  const wLarge = parseFloat(document.getElementById("wLargeDiff")?.value);
  const wVeryLarge = parseFloat(document.getElementById("wVeryLargeDiff")?.value);
  const wFastH = parseFloat(document.getElementById("wFastMergeHours")?.value);
  const wFiles = parseFloat(document.getElementById("wManyFiles")?.value);
  if (!isNaN(wLarge))    riskWeights.large_diff_threshold = wLarge;
  if (!isNaN(wVeryLarge)) riskWeights.very_large_diff_threshold = wVeryLarge;
  if (!isNaN(wFastH))    riskWeights.fast_merge_hours = wFastH;
  if (!isNaN(wFiles))    riskWeights.many_files_threshold = wFiles;

  const labels = _activeLabels.size > 0 ? Array.from(_activeLabels) : undefined;

  try {
    const r = await authFetch(`${API}/api/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repo, max_prs: max,
        risk_weights: Object.keys(riskWeights).length ? riskWeights : undefined,
        labels,
      }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Failed to start analysis.");
    currentAnalysisId = data.analysis_id;
    pollStatus(data.analysis_id);
  } catch (err) {
    hideProgress();
    showError(err.message || String(err));
    $("#analyzeBtn").disabled = false;
  }
});

async function pollStatus(id) {
  let tries = 0;
  while (true) {
    await sleep(1300);
    try {
      const r = await authFetch(`${API}/api/analyze/${id}/status`);
      const data = await r.json();
      const rl = data.rate_limit || null;
      if (data.status === "queued") {
        showProgress("Queued…", 0, parseInt($("#prCount").value, 10), rl);
      } else if (data.status === "fetching") {
        showProgress(`Fetching pull requests from GitHub…`,
                     data.progress, data.total, rl);
      } else if (data.status === "analyzing") {
        showProgress("Crunching analytics…", data.progress, data.total, rl);
      } else if (data.status === "complete") {
        await loadResults(id);
        return;
      } else if (data.status === "error") {
        hideProgress();
        showError(data.error || "Unknown error.");
        $("#analyzeBtn").disabled = false;
        return;
      }
    } catch (e) {
      tries += 1;
      if (tries > 5) {
        hideProgress();
        showError("Lost connection to the server.");
        $("#analyzeBtn").disabled = false;
        return;
      }
    }
  }
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function showProgress(label, progress, total, rateLimit = null) {
  $("#progressShell").hidden = false;
  $("#progressLabel").textContent = label;
  $("#progressCount").textContent = `${progress} / ${total}`;
  const pct = total > 0 ? Math.min(100, (progress / total) * 100) : 5;
  $("#progressFill").style.width = `${pct}%`;
  $("#progressMeta").textContent =
    progress === 0
      ? "This usually takes 10-60s depending on PR count."
      : `Processed ${progress} of ${total} pull requests.`;

  const rlRow = $("#rateLimitRow");
  if (rateLimit && rateLimit.limit) {
    rlRow.hidden = false;
    const remaining = rateLimit.remaining ?? rateLimit.limit;
    const limit = rateLimit.limit;
    const pctLeft = Math.round(100 * remaining / limit);
    const fill = $("#rateLimitFill");
    fill.style.width = `${pctLeft}%`;
    fill.className = "rate-limit-fill" +
      (pctLeft < 10 ? " critical" : pctLeft < 25 ? " warn" : "");
    $("#rateLimitText").textContent = `API: ${remaining}/${limit} left`;
    const eta = rateLimit.eta_minutes;
    $("#rateLimitEta").textContent = (eta && remaining < 100)
      ? `resets in ${eta}m` : "";
  } else {
    rlRow.hidden = true;
  }
}
function hideProgress() { $("#progressShell").hidden = true; }
function showError(msg) {
  $("#errorShell").hidden = false;
  $("#errorBody").textContent = msg;
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function hideError() { $("#errorShell").hidden = true; }

/* ---------- Load + render results ---------- */
async function loadResults(id) {
  hideError();
  $("#analyzeBtn").disabled = false;
  currentAnalysisId = id;
  try {
    const r = await authFetch(`${API}/api/analyze/${id}/results`);
    if (r.status === 202) {
      showProgress("Analysis still running…", 0, 0);
      pollStatus(id);
      return;
    }
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Failed to load results.");
    hideProgress();
    renderAll(data);
    $("#dashboard").hidden = false;
    loadChatHistory(id);
    loadRecent();
    document.getElementById("section-overview")
      .scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    showError(e.message || String(e));
  }
}

/* ---------- Master render ---------- */
function renderAll(data) {
  const meta = data.meta || {};
  $("#overviewMeta").innerHTML = `
    <div><strong>${escapeHtml(meta.owner || "")}/${escapeHtml(meta.repo || "")}</strong></div>
    <div>${meta.pr_count || 0} PRs analyzed · ${
      meta.fetched_at ? new Date(meta.fetched_at).toLocaleString() : ""
    }</div>
  `;

  // Export button
  const exportBtn = $("#exportBtn");
  if (exportBtn && currentAnalysisId) {
    exportBtn.href = `${API}/api/analyze/${currentAnalysisId}/export`;
    exportBtn.download = `${(meta.owner || "repo")}_${(meta.repo || "equity")}_report.md`;
    exportBtn.hidden = false;
  }

  // Show save current repo button
  const saveBtn = $("#saveCurrentRepoBtn");
  if (saveBtn && currentRepo) {
    saveBtn.hidden = false;
  }

  renderOverview(data.summary);
  renderFairness(data.inequality, data.sla);
  renderDelays(data.delays);
  renderEmotional(data.emotional);
  renderRisk(data.risk);
  renderBias(data.bias);
  renderBurnout(data.burnout);
  renderAuthor(data.author);
  renderRecs(data.recommendations);
  resetChat();

  // Load trend async
  if (currentRepo) {
    loadTrend(currentRepo);
  }
}

/* ---------- 1. Overview ---------- */
function renderOverview(s) {
  if (!s) return;
  $("#kpiHealth").textContent = s.team_health_score ?? "—";
  $("#kpiHealthBar").style.width = `${Math.min(100, s.team_health_score || 0)}%`;
  $("#kpiHealthFoot").textContent =
    "Composite of fairness, tone, risk, and review speed";

  $("#kpiFairness").textContent = s.fairness_score ?? "—";
  $("#kpiFairnessFoot").textContent =
    `${s.unique_reviewers ?? 0} reviewers · ${s.unique_authors ?? 0} authors`;

  const ps = s.psych_safety_score ?? "—";
  $("#kpiPsychSafety").textContent = ps;
  if (typeof ps === "number") {
    $("#kpiPsychSafetyFoot").textContent =
      ps >= 80 ? "Healthy feedback culture"
      : ps >= 60 ? "Some sharp edges"
      : "Action needed";
  } else {
    $("#kpiPsychSafetyFoot").textContent = "tone score / 100";
  }

  // Median first review time (was missing before)
  const m = s.median_first_review_h;
  $("#kpiFirstReview").textContent =
    (m === null || m === undefined) ? "—" : fmtHours(m);

  $("#kpiRisk").textContent = s.high_risk_count ?? 0;
  $("#kpiRiskFoot").textContent = `of ${s.total_prs ?? 0} PRs`;

  $("#kpiStale").textContent = s.stale_count ?? 0;
}

/* ---------- 2. Fairness + SLA ---------- */
function renderFairness(ineq, sla) {
  if (!ineq) return;
  $("#fairnessGiniTag").textContent = `Gini ${ineq.gini ?? "—"}`;

  const dist = (ineq.distribution || []).slice(0, 14);
  destroy("reviewers");
  charts.reviewers = new Chart($("#chartReviewers"), {
    type: "bar",
    data: {
      labels: dist.map(d => d.reviewer),
      datasets: [{
        label: "Reviews",
        data: dist.map(d => d.review_count),
        backgroundColor: dist.map((_, i) =>
          i === 0 ? COLORS.accent :
          i < 3   ? COLORS.accentSoft : COLORS.bg3),
        borderColor: dist.map((_, i) =>
          i === 0 ? COLORS.accent :
          i < 3   ? COLORS.accentSoft : COLORS.line),
        borderWidth: 1,
        borderRadius: 4,
      }],
    },
    options: {
      indexAxis: "y",
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: COLORS.line, drawBorder: false }, ticks: { color: COLORS.ink3 } },
        y: { grid: { display: false }, ticks: { color: COLORS.ink1 } },
      },
    },
  });

  const ovEl = $("#overloadedList");
  if (!ineq.overloaded || !ineq.overloaded.length) {
    ovEl.innerHTML = `<li class="empty">No overloaded reviewers detected.</li>`;
  } else {
    ovEl.innerHTML = ineq.overloaded.map(o => `
      <li>
        <div>
          <div class="list-name">${escapeHtml(o.reviewer)}</div>
          <div class="list-sub">${o.share_pct}% of all reviews</div>
        </div>
        <div class="list-num">${o.review_count}</div>
      </li>
    `).join("");
  }

  const inEl = $("#inactiveList");
  if (!ineq.inactive || !ineq.inactive.length) {
    inEl.innerHTML = `<li class="empty">No inactive reviewers detected.</li>`;
  } else {
    inEl.innerHTML = ineq.inactive.slice(0, 8).map(o => `
      <li>
        <div>
          <div class="list-name">${escapeHtml(o.reviewer)}</div>
          <div class="list-sub">${escapeHtml(o.reason || "Inactive")}</div>
        </div>
        <div class="list-num">${o.times_requested || 0}×</div>
      </li>
    `).join("");
  }

  // SLA rendering
  if (sla) {
    const tag = $("#slaOverallTag");
    if (tag) tag.textContent = `${sla.overall_compliance_pct}% compliant · ${sla.sla_hours}h SLA`;
    const slaEl = $("#slaList");
    if (slaEl) {
      if (!sla.reviewers || !sla.reviewers.length) {
        slaEl.innerHTML = `<div class="empty-state">No SLA data available.</div>`;
      } else {
        slaEl.innerHTML = sla.reviewers.slice(0, 12).map(r => {
          const pct = r.compliance_pct;
          const color = pct >= 80 ? "var(--good)" : pct >= 50 ? "var(--warn)" : "var(--bad)";
          return `
            <div class="sla-row">
              <div>
                <strong style="color:var(--ink-0)">${escapeHtml(r.reviewer)}</strong>
                <div style="font-family:var(--font-mono);font-size:11px;color:var(--ink-3);margin-top:2px">
                  avg ${r.avg_response_hours ? r.avg_response_hours + 'h' : '—'} · ${r.sla_met}/${r.total_reviews} on time
                </div>
              </div>
              <div class="sla-compliance-bar">
                <div class="sla-compliance-fill" style="width:${pct}%;background:${color}"></div>
              </div>
              <div style="font-family:var(--font-mono);font-size:13px;color:${color};font-weight:500">
                ${pct}%
              </div>
            </div>`;
        }).join("");
      }
    }
  }
}

/* ---------- 3. Delays ---------- */
function renderDelays(d) {
  if (!d) return;
  const vals = (d.prs || [])
    .map(p => p.first_review_h)
    .filter(v => v !== null && v !== undefined);
  // Bucket into ranges (hours)
  const buckets = [
    { label: "<1h",      max: 1,    count: 0 },
    { label: "1-4h",     max: 4,    count: 0 },
    { label: "4-12h",    max: 12,   count: 0 },
    { label: "12-24h",   max: 24,   count: 0 },
    { label: "1-3 days", max: 72,   count: 0 },
    { label: "3-7 days", max: 168,  count: 0 },
    { label: ">7 days",  max: 1e9,  count: 0 },
  ];
  vals.forEach(v => {
    for (const b of buckets) { if (v <= b.max) { b.count += 1; break; } }
  });

  destroy("firstReview");
  charts.firstReview = new Chart($("#chartFirstReview"), {
    type: "bar",
    data: {
      labels: buckets.map(b => b.label),
      datasets: [{
        label: "PRs",
        data: buckets.map(b => b.count),
        backgroundColor: COLORS.accent,
        borderRadius: 4,
      }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: COLORS.ink2 } },
        y: { grid: { color: COLORS.line }, ticks: { color: COLORS.ink3, precision: 0 } },
      },
    },
  });

  const renderStat = (lbl, st, unit) => {
    if (!st || st.count === 0) {
      return `<div class="stat-block">
        <div class="label">${lbl}</div>
        <div class="value">—</div>
        <div class="sub">no data</div>
      </div>`;
    }
    return `<div class="stat-block">
      <div class="label">${lbl}</div>
      <div class="value">${fmtHours(st.median)}</div>
      <div class="sub">avg ${fmtHours(st.avg)} · p90 ${fmtHours(st.p90)} · n=${st.count}</div>
    </div>`;
  };
  $("#delayStats").innerHTML =
    renderStat("First review", d.first_review_stats) +
    renderStat("First approval", d.approval_stats) +
    renderStat("Time to merge", d.merge_stats) +
    renderStat("Stale PRs", { median: d.stale_prs?.length || 0, avg: 0, p90: 0, count: d.stale_prs?.length || 0 });

  const stale = d.stale_prs || [];
  $("#staleCountTag").textContent = `${stale.length} stale`;
  const tbody = $("#staleTable tbody");
  if (!stale.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No stale PRs.</td></tr>`;
  } else {
    tbody.innerHTML = stale.slice(0, 12).map(s => `
      <tr>
        <td><span class="pr-num">#${s.number}</span></td>
        <td><span class="pr-title">${escapeHtml(s.title)}</span></td>
        <td><span class="author-tag">${escapeHtml(s.author)}</span></td>
        <td>${(s.age_hours / 24).toFixed(1)}d</td>
        <td><span class="state-pill state-${s.state}">${s.state}</span></td>
        <td>${s.html_url ? `<a class="row-link" href="${s.html_url}" target="_blank" rel="noopener">view</a>` : ""}</td>
      </tr>
    `).join("");
  }
}

/* ---------- 4. Quality ---------- */
// function renderQuality(q) {
//   if (!q) return;
//   $("#qualityAvgTag").textContent = `avg ${q.overall_avg_score ?? "—"} / 5`;

//   const order = ["rubber_stamp", "shallow", "moderate", "constructive", "detailed", "high_quality"];
//   const labels = ["Rubber stamp", "Shallow", "Moderate", "Constructive", "Detailed", "High quality"];
//   const colors = [COLORS.bad, "#a76b4a", "#9b988e", COLORS.info, COLORS.good, COLORS.accent];
//   const counts = order.map(k => (q.buckets || {})[k] || 0);

//   destroy("qualityBuckets");
//   charts.qualityBuckets = new Chart($("#chartQualityBuckets"), {
//     type: "doughnut",
//     data: {
//       labels: labels,
//       datasets: [{
//         data: counts,
//         backgroundColor: colors,
//         borderColor: COLORS.bg2,
//         borderWidth: 2,
//       }],
//     },
//     options: {
//       maintainAspectRatio: false,
//       cutout: "62%",
//       plugins: {
//         legend: { position: "right", labels: { color: COLORS.ink1, padding: 12, font: { size: 12 } } },
//       },
//     },
//   });

//   const tq = (q.reviewer_quality || []).filter(r => r.total_comments >= 2).slice(0, 8);
//   if (!tq.length) {
//     $("#topQualityList").innerHTML = `<li class="empty">Not enough comments to score reviewers.</li>`;
//   } else {
//     $("#topQualityList").innerHTML = tq.map(r => `
//       <li>
//         <div>
//           <div class="list-name">${escapeHtml(r.reviewer)}</div>
//           <div class="list-sub">${r.total_comments} comments</div>
//         </div>
//         <div class="list-num">${r.avg_quality_score.toFixed(2)}</div>
//       </li>
//     `).join("");
//   }

//   const rs = q.rubber_stamp_approvals || [];
//   $("#rubberCountTag").textContent = `${rs.length} found`;
//   const tbody = $("#rubberTable tbody");
//   if (!rs.length) {
//     tbody.innerHTML = `<tr><td colspan="4" class="empty-state">No rubber-stamp approvals detected — nice.</td></tr>`;
//   } else {
//     tbody.innerHTML = rs.slice(0, 10).map(r => `
//       <tr>
//         <td><span class="pr-num">#${r.pr_number}</span></td>
//         <td><span class="author-tag">${escapeHtml(r.reviewer || "—")}</span></td>
//         <td><em style="color: var(--ink-2)">"${escapeHtml(r.body_excerpt || "(no body)")}"</em></td>
//         <td>${r.html_url ? `<a class="row-link" href="${r.html_url}" target="_blank" rel="noopener">view</a>` : ""}</td>
//       </tr>
//     `).join("");
//   }

//   const ai = q.ai_enhanced_samples || [];
//   if (ai.length) {
//     $("#aiSamplesCard").hidden = false;
//     $("#aiSamplesTag").textContent = `${ai.length} graded`;
//     $("#aiSamplesTable tbody").innerHTML = ai.map(s => `
//       <tr>
//         <td><span class="pr-num">#${s.pr_number}</span></td>
//         <td><span class="author-tag">${escapeHtml(s.reviewer || "—")}</span></td>
//         <td><span class="state-pill state-merged">${escapeHtml(s.ai_label || s.label || "—")}</span></td>
//         <td><strong>${s.ai_score ?? s.score ?? "—"}</strong>/5</td>
//         <td style="color: var(--ink-2);">${escapeHtml((s.excerpt || "").slice(0, 160))}</td>
//       </tr>
//     `).join("");
//   } else {
//     $("#aiSamplesCard").hidden = true;
//   }
// }
/* ---------- 3. Emotional Intelligence ---------- */
function renderEmotional(e) {
  if (!e) return;

  const score = e.team_psych_safety_score ?? 100;
  $("#psychSafetyScore").textContent = score;

  let safetyLabel, safetyColor;
  if (score >= 80) { safetyLabel = "Healthy"; safetyColor = "var(--good)"; }
  else if (score >= 60) { safetyLabel = "Mild concerns"; safetyColor = "var(--warn)"; }
  else { safetyLabel = "Action needed"; safetyColor = "var(--bad)"; }
  const tag = $("#psychSafetyTag");
  tag.textContent = safetyLabel;
  tag.style.color = safetyColor;

  // Tone breakdown
  const toneOrder = ["supportive", "constructive", "neutral", "harsh", "dismissive", "hostile"];
  const toneEmoji = {
    supportive: "🟢", constructive: "🌱", neutral: "⚪",
    harsh: "🟠", dismissive: "🟡", hostile: "🔴"
  };
  const buckets = e.tone_buckets || {};
  const total = e.total_classified || 1;
  $("#toneBreakdown").innerHTML = toneOrder
    .filter(t => buckets[t])
    .map(t => {
      const pct = ((buckets[t] / total) * 100).toFixed(1);
      return `
        <div class="list-row">
          <div><span style="margin-right:8px">${toneEmoji[t]}</span><strong style="text-transform:capitalize">${t}</strong></div>
          <div><span class="metric">${buckets[t]}</span> <span style="color:var(--ink-3); font-size:12px">(${pct}%)</span></div>
        </div>`;
    }).join("") || `<div style="color: var(--ink-3); font-size: 13px;">No comments classified yet.</div>`;

  // Reviewer EI rankings
  const reviewers = e.reviewer_ei || [];
  $("#reviewerEIList").innerHTML = reviewers.length
    ? reviewers.slice(0, 10).map(r => {
        let color = "var(--good)";
        if (r.ei_score < 40) color = "var(--bad)";
        else if (r.ei_score < 60) color = "var(--warn)";
        else if (r.ei_score < 80) color = "var(--ink-2)";
        return `
          <div class="list-row">
            <div>
              <strong>${escapeHtml(r.reviewer)}</strong>
              <div style="color: var(--ink-3); font-size: 11px; font-family: var(--font-mono); margin-top: 2px;">
                ${r.total_comments} comments · ${r.harsh_pct}% harsh · ${r.supportive_pct}% supportive
              </div>
            </div>
            <div style="font-family: var(--font-display); font-size: 22px; color: ${color}; font-weight: 400;">
              ${r.ei_score}
            </div>
          </div>`;
      }).join("")
    : `<div style="color: var(--ink-3); font-size: 13px;">No reviewer data yet.</div>`;

  // At-risk authors
  const atRisk = e.at_risk_authors || [];
  $("#atRiskAuthorsTag").textContent = `${atRisk.length} found`;
  $("#atRiskAuthorsList").innerHTML = atRisk.length
    ? atRisk.map(a => `
        <div class="list-row" style="padding: 14px 0; border-bottom: 1px dashed var(--bg-3);">
          <div>
            <strong>${escapeHtml(a.author)}</strong>
            <div style="color: var(--ink-3); font-size: 11px; font-family: var(--font-mono); margin-top: 4px;">
              ${a.total_comments_received} comments received
              · 🔴 ${a.hostile} hostile
              · 🟠 ${a.harsh} harsh
              · 🟡 ${a.dismissive} dismissive
            </div>
          </div>
          <div style="font-family: var(--font-display); font-size: 22px; color: var(--bad); font-weight: 400;">
            ${a.negative_pct}%
          </div>
        </div>`).join("")
    : `<div style="color: var(--ink-3); font-size: 13px; padding: 8px 0;">No authors at risk — good signal.</div>`;

  // Flagged comments
  const flagged = e.flagged_comments || [];
  $("#flaggedCommentsTag").textContent = `${flagged.length} flagged`;
  const tbody = $("#flaggedCommentsTable tbody");
  if (flagged.length) {
    tbody.innerHTML = flagged.map(f => {
      const toneColors = { hostile: "var(--bad)", harsh: "#d97706", dismissive: "#a16207" };
      return `
        <tr>
          <td><span class="pr-num">#${f.pr_number}</span></td>
          <td><span class="author-tag">${escapeHtml(f.reviewer || "—")}</span></td>
          <td><span class="state-pill" style="background:${toneColors[f.tone] || 'var(--bg-3)'}; color: white;">${escapeHtml(f.tone)}</span></td>
          <td style="color: var(--ink-2); font-style: italic;">"${escapeHtml(f.excerpt)}"</td>
        </tr>`;
    }).join("");
  } else {
    tbody.innerHTML = `<tr><td colspan="4" style="color: var(--ink-3); padding: 16px 0;">No flagged comments — feedback culture looks healthy.</td></tr>`;
  }
}
/* ---------- 5. Risk ---------- */
function renderRisk(r) {
  if (!r) return;
  const summary = r.summary || { high: 0, medium: 0, low: 0 };

  destroy("risk");
  charts.risk = new Chart($("#chartRisk"), {
    type: "doughnut",
    data: {
      labels: ["High", "Medium", "Low"],
      datasets: [{
        data: [summary.high, summary.medium, summary.low],
        backgroundColor: [COLORS.bad, COLORS.warn, COLORS.good],
        borderColor: COLORS.bg2,
        borderWidth: 2,
      }],
    },
    options: {
      maintainAspectRatio: false,
      cutout: "70%",
      plugins: {
        legend: { position: "bottom", labels: { color: COLORS.ink1, padding: 14 } },
      },
    },
  });

  const list = $("#riskList");
  const top = (r.prs || []).filter(p => p.risk_score > 0).slice(0, 8);
  if (!top.length) {
    list.innerHTML = `<div class="empty-state">No risky PRs found in the analyzed window.</div>`;
    return;
  }
  list.innerHTML = top.map(pr => `
    <div class="risk-item ${pr.risk_level}">
      <div>
        <div class="risk-score">${pr.risk_score}</div>
        <div class="risk-score-label">${pr.risk_level}</div>
      </div>
      <div class="risk-body">
        <div class="risk-title">#${pr.number} · ${escapeHtml(pr.title)}</div>
        <div class="risk-meta">
          by <strong style="color:var(--ink-1)">${escapeHtml(pr.author)}</strong>
          · ${pr.additions || 0}+/${pr.deletions || 0}− across ${pr.changed_files || 0} files
          · approvers: ${pr.approvers && pr.approvers.length ? pr.approvers.map(escapeHtml).join(", ") : "<em>none</em>"}
        </div>
        <div class="risk-reasons">
          ${(pr.reasons || []).map(r => `<span class="risk-reason">${escapeHtml(r)}</span>`).join("")}
        </div>
      </div>
      <div>${pr.html_url ? `<a class="row-link" href="${pr.html_url}" target="_blank" rel="noopener">view PR ↗</a>` : ""}</div>
    </div>
  `).join("");
}

/* ---------- 6. Bias ---------- */
function renderBias(b) {
  if (!b) return;
  const fav = b.favoritism || [];
  const favEl = $("#favoritismList");
  if (!fav.length) {
    favEl.innerHTML = `<li class="empty">No strong favoritism patterns detected.</li>`;
  } else {
    favEl.innerHTML = fav.slice(0, 8).map(f => `
      <li>
        <div>
          <div class="list-name">${escapeHtml(f.reviewer)} → ${escapeHtml(f.author)}</div>
          <div class="list-sub">${f.approvals} of ${f.author_total_approvals} approvals</div>
        </div>
        <div class="list-num">${f.share_pct}%</div>
      </li>
    `).join("");
  }

  const groups = b.repeated_groups || [];
  const grpEl = $("#repeatedGroupsList");
  if (!groups.length) {
    grpEl.innerHTML = `<li class="empty">No repeated approval groups.</li>`;
  } else {
    grpEl.innerHTML = groups.map(g => `
      <li>
        <div>
          <div class="list-name">${g.reviewers.map(escapeHtml).join(" · ")}</div>
          <div class="list-sub">approved together</div>
        </div>
        <div class="list-num">${g.occurrences}×</div>
      </li>
    `).join("");
  }

  const iso = b.isolated_authors || [];
  const isoEl = $("#isolatedList");
  if (!iso.length) {
    isoEl.innerHTML = `<li class="empty">No isolated authors detected.</li>`;
  } else {
    isoEl.innerHTML = iso.map(a => `
      <li>
        <div>
          <div class="list-name">${escapeHtml(a.author)}</div>
          <div class="list-sub">only reviewed by ${escapeHtml(a.sole_reviewer)}</div>
        </div>
        <div class="list-num">${a.approvals}×</div>
      </li>
    `).join("");
  }
}

/* ---------- 7. Burnout ---------- */
function renderBurnout(b) {
  if (!b) return;
  const top = (b.reviewers || []).slice(0, 12);

  destroy("burnout");
  charts.burnout = new Chart($("#chartBurnout"), {
    type: "bar",
    data: {
      labels: top.map(r => r.reviewer),
      datasets: [
        {
          label: "Late-night %",
          data: top.map(r => r.late_night_pct),
          backgroundColor: COLORS.accent,
          borderRadius: 4,
        },
        {
          label: "Weekend %",
          data: top.map(r => r.weekend_pct),
          backgroundColor: COLORS.info,
          borderRadius: 4,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { position: "top", labels: { color: COLORS.ink1 } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: COLORS.ink2 } },
        y: { grid: { color: COLORS.line }, ticks: { color: COLORS.ink3, callback: v => v + "%" } },
      },
    },
  });

  const at = b.at_risk || [];
  if (!at.length) {
    $("#burnoutList").innerHTML = `<li class="empty">No burnout signals detected.</li>`;
  } else {
    $("#burnoutList").innerHTML = at.slice(0, 8).map(r => `
      <li>
        <div>
          <div class="list-name">${escapeHtml(r.reviewer)}</div>
          <div class="list-sub">${(r.signals || []).join(" · ") || "elevated activity"}</div>
        </div>
        <div class="list-num">${r.burnout_score}</div>
      </li>
    `).join("");
  }
}

/* ---------- 8. Recommendations ---------- */
function renderRecs(r) {
  if (!r) return;
  const recs = r.recommendations || [];
  const list = $("#recList");
  if (!recs.length) {
    list.innerHTML = `<div class="empty-state">No open PRs in the analyzed window.</div>`;
    return;
  }
  list.innerHTML = recs.map(rec => `
    <div class="rec-item">
      <div class="rec-head">
        <div>
          <div class="rec-pr-title">#${rec.pr_number} · ${escapeHtml(rec.pr_title)}</div>
          <div class="rec-meta">opened by ${escapeHtml(rec.author || "—")}</div>
        </div>
        ${rec.html_url ? `<a class="row-link" href="${rec.html_url}" target="_blank" rel="noopener">view PR ↗</a>` : ""}
      </div>
      <div class="rec-suggestions">
        ${(rec.suggested_reviewers || []).map(s => `
          <span class="rec-suggestion">
            <span class="name">${escapeHtml(s.reviewer)}</span>
            <span class="meta">${s.recent_reviews}r · 14d</span>
          </span>
        `).join("") || `<span class="empty-state">No candidates available.</span>`}
      </div>
    </div>
  `).join("");
}

/* ---------- 9. Author Analytics ---------- */
function renderAuthor(authorData) {
  const grid = $("#authorGrid");
  if (!grid) return;
  if (!authorData || !authorData.authors || !authorData.authors.length) {
    grid.innerHTML = `<div class="empty-state">No author data available.</div>`;
    return;
  }
  const tag = $("#authorCountTag");
  if (tag) tag.textContent = `${authorData.total_authors} authors`;

  grid.innerHTML = authorData.authors.slice(0, 20).map(a => {
    const mergeColor = a.merge_rate_pct >= 80 ? "var(--good)"
                     : a.merge_rate_pct >= 50 ? "var(--warn)" : "var(--bad)";
    return `
      <div class="author-card">
        <div class="author-card-name">${escapeHtml(a.author)}</div>
        <div class="author-stat-row">
          <span>Total PRs</span><strong>${a.total_prs}</strong>
        </div>
        <div class="author-stat-row">
          <span>Merge rate</span>
          <strong style="color:${mergeColor}">${a.merge_rate_pct}%</strong>
        </div>
        <div class="author-stat-row">
          <span>Avg wait for review</span>
          <strong>${a.avg_wait_for_review_h ? fmtHours(a.avg_wait_for_review_h) : "—"}</strong>
        </div>
        <div class="author-stat-row">
          <span>Avg comment quality</span>
          <strong>${a.avg_quality_received ? a.avg_quality_received.toFixed(1) + "/5" : "—"}</strong>
        </div>
        <div class="author-stat-row">
          <span>Lines (+ / −)</span>
          <strong>${a.total_additions || 0}/${a.total_deletions || 0}</strong>
        </div>
      </div>`;
  }).join("");
}

/* ---------- 10. Trend ---------- */
async function loadTrend(repoFull) {
  if (!repoFull || !repoFull.includes("/")) return;
  const [owner, repo] = repoFull.split("/", 2);
  try {
    const r = await authFetch(`${API}/api/repos/${owner}/${repo}/trend`);
    const data = await r.json();
    renderTrend(data.snapshots || []);
  } catch {}
}

function renderTrend(snapshots) {
  const empty = $("#trendEmpty");
  const tag = $("#trendSnapTag");
  if (tag) tag.textContent = `${snapshots.length} snapshot${snapshots.length !== 1 ? "s" : ""}`;

  if (!snapshots.length || snapshots.length < 2) {
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;

  const labels = snapshots.map(s => {
    const d = new Date(s.snapshot_at);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  });

  destroy("trend");
  charts.trend = new Chart($("#chartTrend"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Health Score",
          data: snapshots.map(s => s.health_score),
          borderColor: COLORS.accent,
          backgroundColor: "rgba(232,116,59,0.08)",
          tension: 0.35,
          fill: true,
          pointRadius: 5,
          pointHoverRadius: 7,
        },
        {
          label: "Fairness",
          data: snapshots.map(s => s.fairness_score),
          borderColor: COLORS.good,
          tension: 0.35,
          fill: false,
          pointRadius: 4,
          borderDash: [5, 4],
        },
        {
          label: "Psych Safety",
          data: snapshots.map(s => s.psych_safety_score),
          borderColor: COLORS.info,
          tension: 0.35,
          fill: false,
          pointRadius: 4,
          borderDash: [3, 3],
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { position: "top" } },
      scales: {
        x: { grid: { color: COLORS.line }, ticks: { color: COLORS.ink2 } },
        y: {
          min: 0, max: 100,
          grid: { color: COLORS.line },
          ticks: { color: COLORS.ink3 },
        },
      },
    },
  });
}

/* ---------- 10. Chat ---------- */
function resetChat() {
  $("#chatThread").innerHTML =
    `<div class="chat-empty">Ask me anything — reviewer fairness, stale PRs, burnout risk, bias patterns, or general code-review advice.</div>`;
  const inp = $("#chatInput");
  if (inp) { inp.value = ""; resizeChatInput(); }
  const suggestions = document.querySelector(".chat-suggestions");
  if (suggestions) { suggestions.hidden = false; suggestions.style.opacity = ""; }
}

async function loadChatHistory(id) {
  try {
    const r = await authFetch(`${API}/api/chat/${id}/history`);
    const data = await r.json();
    const items = data.history || [];
    if (!items.length) return;
    $("#chatThread").innerHTML = "";
    items.forEach(m => appendChatMessage(m.role, m.content));
    scrollChatToBottom();
  } catch {}
}

function appendChatMessage(role, content, providerLabel = null) {
  const thread = $("#chatThread");
  const empty = thread.querySelector(".chat-empty");
  if (empty) empty.remove();

  const wrap = document.createElement("div");
  wrap.className = `chat-msg ${role}`;
  const inner = document.createElement("div");

  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  bubble.innerHTML = formatChatMarkdown(content);
  inner.appendChild(bubble);

  if (providerLabel && role === "assistant") {
    const meta = document.createElement("div");
    meta.className = "chat-meta";
    meta.textContent = `via ${providerLabel}`;
    inner.appendChild(meta);
  }
  wrap.appendChild(inner);
  thread.appendChild(wrap);
  return wrap;
}

function appendTyping() {
  const thread = $("#chatThread");
  const wrap = document.createElement("div");
  wrap.className = "chat-msg assistant";
  wrap.id = "chatTyping";
  wrap.innerHTML = `
    <div class="chat-bubble" style="padding:6px 16px;">
      <div class="chat-typing"><span></span><span></span><span></span></div>
    </div>`;
  thread.appendChild(wrap);
  scrollChatToBottom();
}
function removeTyping() { document.getElementById("chatTyping")?.remove(); }
function scrollChatToBottom() {
  const t = $("#chatThread");
  t.scrollTop = t.scrollHeight;
}

function formatChatMarkdown(text) {
  // Fenced code blocks first (before escaping ruins them)
  const codeBlocks = [];
  text = text.replace(/```[\w]*\n?([\s\S]*?)```/g, (_, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push(code.trim());
    return `\x00CODE${idx}\x00`;
  });

  // Escape HTML in non-code parts
  let safe = escapeHtml(text);

  // Restore code blocks as <pre><code>
  safe = safe.replace(/\x00CODE(\d+)\x00/g, (_, i) =>
    `<pre style="background:var(--bg-2);border-radius:6px;padding:10px 14px;overflow-x:auto;margin:8px 0;font-size:12px;"><code style="font-family:var(--font-mono);">${escapeHtml(codeBlocks[+i])}</code></pre>`
  );

  // Inline code
  safe = safe.replace(/`([^`]+)`/g,
    '<code style="font-family:var(--font-mono);background:var(--bg-2);padding:1px 6px;border-radius:4px;font-size:0.9em;">$1</code>');

  // Headers (##, #)
  safe = safe.replace(/^### (.+)$/gm, '<h4 style="margin:10px 0 4px;font-size:13px;color:var(--ink-1);">$1</h4>');
  safe = safe.replace(/^## (.+)$/gm, '<h3 style="margin:12px 0 6px;font-size:14px;color:var(--ink-1);">$1</h3>');
  safe = safe.replace(/^# (.+)$/gm, '<h2 style="margin:12px 0 6px;font-size:15px;color:var(--ink-1);">$1</h2>');

  // Bold and italic
  safe = safe.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
  safe = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  safe = safe.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');

  // Horizontal rule
  safe = safe.replace(/^---+$/gm, '<hr style="border:none;border-top:1px solid var(--bg-3);margin:10px 0;">');

  // Unordered lists — gather consecutive bullet lines into a <ul>
  safe = safe.replace(/((?:^[•\-\*] .+\n?)+)/gm, (block) => {
    const items = block.trim().split('\n').map(line =>
      `<li style="margin:2px 0;">${line.replace(/^[•\-\*] /, '').trim()}</li>`
    ).join('');
    return `<ul style="margin:6px 0 6px 18px;padding:0;">${items}</ul>`;
  });

  // Ordered lists
  safe = safe.replace(/((?:^\d+\. .+\n?)+)/gm, (block) => {
    const items = block.trim().split('\n').map(line =>
      `<li style="margin:2px 0;">${line.replace(/^\d+\. /, '').trim()}</li>`
    ).join('');
    return `<ol style="margin:6px 0 6px 18px;padding:0;">${items}</ol>`;
  });

  // Line breaks (but not inside block elements)
  safe = safe.replace(/\n{2,}/g, '<br><br>');
  safe = safe.replace(/\n/g, '<br>');

  return safe;
}

// Chat form submit is handled by the unified SSE/regular handler below.

function hideChatSuggestions() {
  const suggestions = document.querySelector(".chat-suggestions");
  if (suggestions && !suggestions.hidden) {
    suggestions.style.transition = "opacity 0.2s";
    suggestions.style.opacity = "0";
    setTimeout(() => { suggestions.hidden = true; suggestions.style.opacity = ""; }, 200);
  }
}

$$(".chat-suggestions .chip").forEach(b => {
  b.addEventListener("click", () => {
    $("#chatInput").value = b.dataset.q;
    $("#chatForm").dispatchEvent(new Event("submit"));
  });
});

/* ---------- nav highlight on scroll ---------- */
const navLinks = $$(".topnav a");
const sections = navLinks
  .map(a => document.querySelector(a.getAttribute("href")))
  .filter(Boolean);
const obs = new IntersectionObserver((entries) => {
  entries.forEach(e => {
    if (e.isIntersecting) {
      navLinks.forEach(a => a.classList.toggle("active",
        a.getAttribute("href") === `#${e.target.id}`));
    }
  });
}, { rootMargin: "-40% 0px -55% 0px", threshold: 0 });
sections.forEach(s => obs.observe(s));

/* ---------- helpers ---------- */
function destroy(name) {
  if (charts[name]) {
    try { charts[name].destroy(); } catch {}
    charts[name] = null;
  }
}

/* =============================================================
   SAVED REPOS
   ============================================================= */
async function loadSavedRepos() {
  try {
    const r = await authFetch(`${API}/api/saved-repos`);
    const data = await r.json();
    _savedRepos = data.saved_repos || [];
    renderSavedRepos();
  } catch {}
}

function renderSavedRepos() {
  const bar = $("#savedReposBar");
  const chips = $("#savedRepoChips");
  if (!bar || !chips) return;

  if (!_savedRepos.length) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  chips.innerHTML = _savedRepos.map(s => `
    <span class="saved-chip" data-repo="${escapeHtml(s.owner + '/' + s.repo)}">
      ${escapeHtml(s.owner)}/${escapeHtml(s.repo)}
      <button class="saved-chip-del" data-id="${s.id}" title="Remove">×</button>
    </span>
  `).join("");

  chips.querySelectorAll(".saved-chip").forEach(chip => {
    chip.addEventListener("click", (e) => {
      if (e.target.classList.contains("saved-chip-del")) return;
      const repo = chip.dataset.repo;
      $("#repoInput").value = repo;
      $("#analyzeForm").dispatchEvent(new Event("submit"));
    });
  });
  chips.querySelectorAll(".saved-chip-del").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      try {
        await authFetch(`${API}/api/saved-repos/${id}`, { method: "DELETE" });
        await loadSavedRepos();
      } catch {}
    });
  });
}

$("#saveCurrentRepoBtn")?.addEventListener("click", async () => {
  if (!currentRepo) return;
  try {
    await authFetch(`${API}/api/saved-repos`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo: currentRepo }),
    });
    await loadSavedRepos();
    const btn = $("#saveCurrentRepoBtn");
    if (btn) { btn.textContent = "✓ Saved"; setTimeout(() => { btn.textContent = "+ Save current"; }, 2000); }
  } catch {}
});

/* =============================================================
   RISK WEIGHTS PANEL TOGGLE
   ============================================================= */
document.getElementById("riskWeightsToggle")?.addEventListener("click", () => {
  const panel = document.getElementById("riskWeightsPanel");
  if (panel) panel.classList.toggle("open");
});

/* =============================================================
   MULTI-REPO COMPARE
   ============================================================= */
document.getElementById("compareBtn")?.addEventListener("click", async () => {
  const repos = [
    document.getElementById("compareRepo1")?.value.trim(),
    document.getElementById("compareRepo2")?.value.trim(),
    document.getElementById("compareRepo3")?.value.trim(),
  ].filter(Boolean);

  if (repos.length < 2) {
    document.getElementById("compareResult").innerHTML =
      `<div class="empty-state">Enter at least 2 repos to compare.</div>`;
    return;
  }

  const btn = document.getElementById("compareBtn");
  btn.disabled = true;
  btn.querySelector("span").textContent = "Analyzing…";
  document.getElementById("compareResult").innerHTML =
    `<div class="empty-state">Fetching data for ${repos.length} repos…</div>`;

  try {
    const r = await authFetch(`${API}/api/compare`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repos, max_prs: 20 }),
    });
    const data = await r.json();
    renderCompare(data.comparison || []);
  } catch (e) {
    document.getElementById("compareResult").innerHTML =
      `<div class="empty-state">Error: ${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.querySelector("span").textContent = "Compare";
  }
});

function renderCompare(items) {
  const el = document.getElementById("compareResult");
  if (!items.length) {
    el.innerHTML = `<div class="empty-state">No results.</div>`;
    return;
  }
  const rows = items.map(item => {
    if (item.error) {
      return `<tr>
        <td><strong>${escapeHtml(item.repo)}</strong></td>
        <td colspan="5" style="color:var(--bad);font-family:var(--font-mono);font-size:12px">${escapeHtml(item.error)}</td>
      </tr>`;
    }
    const s = item.summary || {};
    const health = s.team_health_score ?? "—";
    const fair = s.fairness_score ?? "—";
    const psych = s.psych_safety_score ?? "—";
    const risk = (item.risk_summary?.high ?? 0) + "H " + (item.risk_summary?.medium ?? 0) + "M";
    const sla = item.sla_compliance ?? "—";
    return `<tr>
      <td><strong style="color:var(--ink-0)">${escapeHtml(item.repo)}</strong>
          <div style="font-family:var(--font-mono);font-size:10.5px;color:var(--ink-3)">${item.pr_count} PRs</div>
      </td>
      <td>
        <div class="score-bar-wrap">
          <strong style="font-family:var(--font-display);font-size:18px">${health}</strong>
          <div class="score-bar"><div class="score-bar-fill" style="width:${health || 0}%"></div></div>
        </div>
      </td>
      <td><span style="font-family:var(--font-mono)">${fair}</span></td>
      <td><span style="font-family:var(--font-mono)">${psych}</span></td>
      <td><span style="font-family:var(--font-mono);font-size:12px">${risk}</span></td>
      <td><span style="font-family:var(--font-mono)">${typeof sla === "number" ? sla + "%" : sla}</span></td>
    </tr>`;
  }).join("");

  el.innerHTML = `
    <div class="table-wrap">
      <table class="compare-table">
        <thead>
          <tr>
            <th>Repository</th><th>Health</th><th>Fairness</th>
            <th>Psych Safety</th><th>Risk</th><th>SLA</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

/* =============================================================
   SSE STREAMING CHAT (replaces non-streaming when available)
   ============================================================= */
let _useSSE = true;  // toggle to false for non-streaming fallback

async function sendChatSSE(q) {
  if (!currentAnalysisId) {
    appendChatMessage("assistant", "Run an analysis first, then I can answer.");
    return;
  }
  hideChatSuggestions();
  $("#chatInput").value = "";
  resizeChatInput();
  appendChatMessage("user", q);
  scrollChatToBottom();

  const thread = $("#chatThread");
  const empty = thread.querySelector(".chat-empty");
  if (empty) empty.remove();

  const wrap = document.createElement("div");
  wrap.className = "chat-msg assistant";
  const inner = document.createElement("div");
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  bubble.innerHTML = `<div class="chat-typing"><span></span><span></span><span></span></div>`;
  inner.appendChild(bubble);
  wrap.appendChild(inner);
  thread.appendChild(wrap);
  scrollChatToBottom();

  const url = `${API}/api/chat/${currentAnalysisId}/stream?q=${encodeURIComponent(q)}`;
  let fullText = "";

  try {
    const es = new EventSource(url);
    // EventSource doesn't support custom headers — send token via cookie or skip for now
    // We fall back if EventSource fails due to auth
    let started = false;

    es.onmessage = (event) => {
      if (event.data === "[DONE]") {
        es.close();
        return;
      }
      try {
        const chunk = JSON.parse(event.data);
        if (chunk.error) {
          es.close();
          return;
        }
        if (!started) {
          bubble.innerHTML = "";
          started = true;
        }
        fullText += chunk.token || "";
        bubble.innerHTML = formatChatMarkdown(fullText);
        scrollChatToBottom();
      } catch {}
    };

    es.onerror = () => {
      es.close();
      if (!started) {
        // EventSource failed (likely auth) - fall back to regular POST
        bubble.remove();
        inner.remove();
        wrap.remove();
        sendChatRegular(q, true);
      } else {
        $("#chatInput").focus();
      }
    };
  } catch {
    wrap.remove();
    sendChatRegular(q, true);
  }
}

async function sendChatRegular(q, skipAppendUser = false) {
  hideChatSuggestions();
  if (!skipAppendUser) {
    appendChatMessage("user", q);
    scrollChatToBottom();
  }
  appendTyping();
  try {
    const r = await authFetch(`${API}/api/chat/${currentAnalysisId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    const data = await r.json();
    removeTyping();
    if (!r.ok) {
      appendChatMessage("assistant", `Error: ${data.error || "Chat failed."}`);
    } else {
      appendChatMessage("assistant", data.answer, data.provider);
    }
    scrollChatToBottom();
  } catch (e) {
    removeTyping();
    appendChatMessage("assistant", `Error: ${e.message || e}`);
  } finally {
    $("#chatInput").focus();
  }
}

/* Auto-resize textarea as user types */
function resizeChatInput() {
  const el = $("#chatInput");
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 140) + "px";
}
$("#chatInput").addEventListener("input", resizeChatInput);

/* Enter = submit, Shift+Enter = newline */
$("#chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chatForm").dispatchEvent(new Event("submit"));
  }
});

/* Unified chat form handler — uses SSE streaming when available, falls back to regular */
document.getElementById("chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#chatInput").value.trim();
  if (!q) return;
  if (!currentAnalysisId) {
    appendChatMessage("assistant", "Run an analysis first, then I can answer.");
    return;
  }
  if (_useSSE) {
    sendChatSSE(q);
  } else {
    $("#chatInput").value = "";
    resizeChatInput();
    sendChatRegular(q);
  }
});

/* =============================================================
   LABEL FILTER (populated from PR labels after analysis)
   ============================================================= */
function populateLabelFilter(prsData) {
  const allLabels = new Set();
  (prsData || []).forEach(pr => {
    (pr.labels || []).forEach(l => { if (l) allLabels.add(l); });
  });
  const row = $("#labelFilterRow");
  const chips = $("#labelChips");
  const hint = $("#labelFilterHint");
  if (!row || !chips) return;

  if (!allLabels.size) {
    row.hidden = true;
    return;
  }
  row.hidden = false;
  if (hint) hint.textContent = "Click to filter next run by label";
  chips.innerHTML = Array.from(allLabels).slice(0, 12).map(l => `
    <button class="label-filter-chip" data-label="${escapeHtml(l)}">${escapeHtml(l)}</button>
  `).join("");

  chips.querySelectorAll(".label-filter-chip").forEach(chip => {
    chip.addEventListener("click", () => {
      const lbl = chip.dataset.label;
      if (_activeLabels.has(lbl)) {
        _activeLabels.delete(lbl);
        chip.classList.remove("active");
      } else {
        _activeLabels.add(lbl);
        chip.classList.add("active");
      }
    });
  });
}

/* ---------- boot ---------- */
initAuth();   // checks token → shows auth overlay or resumes session

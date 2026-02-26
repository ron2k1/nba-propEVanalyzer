const $ = (id) => document.getElementById(id);

const healthBadge = $("healthBadge");
const gamesContainer = $("gamesContainer");
const propForm = $("propForm");
const propResult = $("propResult");
const sweepResult = $("sweepResult");
const playerIdInput = $("playerId");
const playerNameInput = $("playerName");
const playersList = $("playersList");
const autoSweepBtn = $("autoSweepBtn");
const sweepTopNInput = $("sweepTopN");
const parlayBtn = $("parlayBtn");
const parlayLegs = $("parlayLegs");
const parlayResult = $("parlayResult");
const oddsResult = $("oddsResult");
const loadOddsBtn = $("loadOddsBtn");
const loadLiveOddsBtn = $("loadLiveOddsBtn");
const propsPresetBtn = $("propsPresetBtn");
const oddsMarketWarning = $("oddsMarketWarning");
const trackingForm = $("trackingForm");
const trackingDateInput = $("trackingDate");
const trackingLimitInput = $("trackingLimit");
const loadBestTodayBtn = $("loadBestTodayBtn");
const settleYesterdayBtn = $("settleYesterdayBtn");
const loadResultsYesterdayBtn = $("loadResultsYesterdayBtn");
const trackingResult = $("trackingResult");
const starterAccuracyForm = $("starterAccuracyForm");
const starterAccDateInput = $("starterAccDate");
const starterAccBookmakersInput = $("starterAccBookmakers");
const starterAccRegionsInput = $("starterAccRegions");
const starterAccSportInput = $("starterAccSport");
const starterAccModelInput = $("starterAccModel");
const runStarterAccuracyBtn = $("runStarterAccuracyBtn");
const starterAccuracyResult = $("starterAccuracyResult");
let playersLoaded = false;
const playersById = new Map();
const playersByNormName = new Map();
const PROP_MARKETS_PRESET = [
  "player_points",
  "player_rebounds",
  "player_assists",
  "player_threes",
  "player_blocks",
  "player_steals",
  "player_turnovers",
  "player_points_rebounds_assists",
  "player_points_rebounds",
  "player_points_assists",
  "player_rebounds_assists",
].join(",");

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll("\"", "&quot;")
    .replaceAll("'", "&#39;");
}

function toUpperTrim(value) {
  return String(value ?? "").trim().toUpperCase();
}

function fmt(value, digits = 2) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : "n/a";
}

function pct(value, digits = 1) {
  const num = Number(value);
  return Number.isFinite(num) ? `${(num * 100).toFixed(digits)}%` : "n/a";
}

function pctAlreadyPercent(value, digits = 2) {
  const num = Number(value);
  return Number.isFinite(num) ? `${num.toFixed(digits)}%` : "n/a";
}

async function apiGet(path) {
  const res = await fetch(path, { cache: "no-store" });
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

function showError(container, message, data) {
  const details = data ? `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>` : "";
  container.innerHTML = `<p class="error">${escapeHtml(message)}</p>${details}`;
}

function statusPill(value) {
  const raw = String(value || "pending").toLowerCase();
  const cls = ["win", "loss", "push"].includes(raw) ? raw : "pending";
  return `<span class="status-pill ${cls}">${escapeHtml(raw.toUpperCase())}</span>`;
}

function parseMarketsCsv(marketsCsv) {
  return String(marketsCsv || "")
    .split(",")
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);
}

function hasAnyPlayerPropMarket(marketsCsv) {
  return parseMarketsCsv(marketsCsv).some((m) => m.startsWith("player_"));
}

function normalizeName(name) {
  return String(name || "")
    .toLowerCase()
    .replace(/[^a-z0-9 ]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

async function loadPlayersIndex() {
  if (playersLoaded) {
    return;
  }
  try {
    const data = await apiGet("/api/players");
    if (!data || data.success !== true || !Array.isArray(data.players)) {
      return;
    }

    playersById.clear();
    playersByNormName.clear();
    playersList.innerHTML = "";

    data.players.forEach((p) => {
      const id = Number(p.id ?? p.playerId);
      const name = String(p.name || "").trim();
      if (!Number.isFinite(id) || id <= 0 || !name) {
        return;
      }
      const entry = { id, name };
      playersById.set(id, entry);

      const norm = normalizeName(name);
      const arr = playersByNormName.get(norm) || [];
      arr.push(entry);
      playersByNormName.set(norm, arr);

      const option = document.createElement("option");
      option.value = `${name} (${id})`;
      playersList.appendChild(option);
    });

    playersLoaded = true;
    syncPlayerNameFromId();
  } catch {
    // Keep UI functional even if players list fails.
  }
}

function resolvePlayerIdFromName(nameInput) {
  const raw = String(nameInput || "").trim();
  if (!raw) {
    return { id: null };
  }

  const idMatch = raw.match(/\((\d+)\)\s*$/);
  if (idMatch) {
    const id = Number(idMatch[1]);
    if (Number.isFinite(id) && id > 0) {
      return { id };
    }
  }

  const cleaned = raw.replace(/\(\d+\)\s*$/, "").trim();
  const norm = normalizeName(cleaned);
  if (!norm) {
    return { id: null };
  }

  const exact = playersByNormName.get(norm) || [];
  if (exact.length === 1) {
    return { id: exact[0].id };
  }
  if (exact.length > 1) {
    return { id: null, ambiguous: true, candidates: exact.slice(0, 6) };
  }

  const prefix = [];
  for (const [key, values] of playersByNormName.entries()) {
    if (key.startsWith(norm)) {
      prefix.push(...values);
    }
  }
  if (prefix.length === 1) {
    return { id: prefix[0].id };
  }
  if (prefix.length > 1) {
    return { id: null, ambiguous: true, candidates: prefix.slice(0, 6) };
  }

  return { id: null };
}

function syncPlayerNameFromId() {
  if (!playersLoaded) {
    return;
  }
  const id = Number(playerIdInput.value);
  if (!Number.isFinite(id) || id <= 0) {
    return;
  }
  const entry = playersById.get(id);
  if (entry) {
    playerNameInput.value = `${entry.name} (${entry.id})`;
  }
}

function syncPlayerIdFromName() {
  if (!playersLoaded) {
    return;
  }
  const resolved = resolvePlayerIdFromName(playerNameInput.value);
  if (resolved.id) {
    playerIdInput.value = resolved.id;
    const entry = playersById.get(resolved.id);
    if (entry) {
      playerNameInput.value = `${entry.name} (${entry.id})`;
    }
  }
}

function applyGamePrefill(teamAbbr, opponentAbbr, isHome) {
  $("playerTeamAbbr").value = toUpperTrim(teamAbbr);
  $("opponentAbbr").value = toUpperTrim(opponentAbbr);
  $("isHome").checked = Boolean(isHome);
}

function renderGames(payload) {
  gamesContainer.innerHTML = "";
  if (!payload || payload.success !== true) {
    showError(gamesContainer, payload?.error || "Failed to load games.", payload);
    return;
  }

  if (!Array.isArray(payload.games) || payload.games.length === 0) {
    gamesContainer.innerHTML = "<p>No games returned.</p>";
    return;
  }

  const template = $("gameCardTemplate");
  payload.games.forEach((g) => {
    const clone = template.content.cloneNode(true);
    const matchup = clone.querySelector(".matchup");
    const meta = clone.querySelector(".meta");
    const homeBtn = clone.querySelector(".use-home");
    const awayBtn = clone.querySelector(".use-away");

    const home = g.homeTeam?.abbreviation || "HOME";
    const away = g.awayTeam?.abbreviation || "AWAY";
    matchup.textContent = `${away} @ ${home}`;
    meta.textContent = `${g.status || "Scheduled"} | Date: ${payload.date || "n/a"}${payload.isStale ? " | stale" : ""}`;

    homeBtn.addEventListener("click", () => applyGamePrefill(home, away, true));
    awayBtn.addEventListener("click", () => applyGamePrefill(away, home, false));

    gamesContainer.appendChild(clone);
  });
}

function renderLlmAnalysis(llm) {
  if (!llm) return "";

  function providerTag(provider) {
    return provider ? `<span class="llm-provider-tag">${escapeHtml(provider)}</span>` : "";
  }

  function injuryCard(s) {
    if (!s || !s.success) {
      return `<div class="llm-card">
        <div class="llm-card-title">Injury Signal ${providerTag(s?.provider)}</div>
        <div class="llm-card-error">${escapeHtml(s?.error || "No news signals")}</div>
      </div>`;
    }
    const adjPct = Number(s.adjustmentPct) * 100;
    const sign = adjPct >= 0 ? "+" : "";
    const adjColor = adjPct > 0 ? "color:var(--ok)" : adjPct < 0 ? "color:var(--bad)" : "";
    return `<div class="llm-card">
      <div class="llm-card-title">Injury Signal ${providerTag(s.provider)}</div>
      <div class="llm-card-value" style="${adjColor}">${sign}${adjPct.toFixed(1)}%</div>
      <div class="llm-card-reasoning">${escapeHtml(s.reasoning || "")}</div>
      <div class="llm-card-meta">Confidence: ${pct(s.confidence)}</div>
    </div>`;
  }

  function matchupCard(s) {
    if (!s || !s.success) {
      return `<div class="llm-card">
        <div class="llm-card-title">Matchup Context ${providerTag(s?.provider)}</div>
        <div class="llm-card-error">${escapeHtml(s?.error || "Unavailable")}</div>
      </div>`;
    }
    const mod = Number(s.modifier);
    const modSign = mod >= 1 ? "color:var(--ok)" : "color:var(--bad)";
    return `<div class="llm-card">
      <div class="llm-card-title">Matchup Context ${providerTag(s.provider)}</div>
      <div class="llm-card-value" style="${modSign}">${fmt(mod, 2)}x</div>
      <div class="llm-card-reasoning">${escapeHtml(s.reasoning || "")}</div>
      <div class="llm-card-meta">Confidence: ${pct(s.confidence)}</div>
    </div>`;
  }

  function lineCard(s) {
    if (!s || !s.success) {
      return `<div class="llm-card">
        <div class="llm-card-title">Line Reasoning ${providerTag(s?.provider)}</div>
        <div class="llm-card-error">${escapeHtml(s?.error || "Unavailable")}</div>
      </div>`;
    }
    const verdict = String(s.verdict || "neutral").toLowerCase();
    return `<div class="llm-card verdict-${verdict}">
      <div class="llm-card-title">Line Reasoning ${providerTag(s.provider)}</div>
      <div class="llm-card-value">${verdict.toUpperCase()} &nbsp;${escapeHtml(String(s.sharpnessScore ?? "?"))}/10</div>
      <div class="llm-card-reasoning">${escapeHtml(s.reasoning || "")}</div>
    </div>`;
  }

  return `
    <div class="llm-section">
      <h4>LLM Analysis</h4>
      <div class="llm-cards">
        ${injuryCard(llm.injurySignal)}
        ${matchupCard(llm.matchupContext)}
        ${lineCard(llm.lineReasoning)}
      </div>
    </div>`;
}

function renderProp(payload) {
  if (!payload || payload.success !== true) {
    showError(propResult, payload?.error || "Prop EV request failed.", payload);
    return;
  }

  const proj = payload.projection || {};
  const ev = payload.ev || {};
  const over = ev.over || {};
  const under = ev.under || {};
  const adj = proj.adjustments || {};

  propResult.innerHTML = `
    <div class="metric-grid">
      <article class="metric">
        <h4>Projection</h4>
        <p>${fmt(proj.projection, 1)}</p>
      </article>
      <article class="metric">
        <h4>Line</h4>
        <p>${fmt(payload.line, 1)}</p>
      </article>
      <article class="metric">
        <h4>Stdev</h4>
        <p>${fmt(ev.stdev, 2)}</p>
      </article>
      <article class="metric ev-over">
        <h4>Over Prob</h4>
        <p>${pct(ev.probOver)}</p>
      </article>
      <article class="metric ev-under">
        <h4>Under Prob</h4>
        <p>${pct(ev.probUnder)}</p>
      </article>
      <article class="metric">
        <h4>Push Prob</h4>
        <p>${pct(ev.probPush || 0)}</p>
      </article>
      <article class="metric ev-over">
        <h4>Over EV%</h4>
        <p>${fmt(over.evPercent, 2)} | ${escapeHtml(over.verdict || "n/a")}</p>
      </article>
      <article class="metric ev-under">
        <h4>Under EV%</h4>
        <p>${fmt(under.evPercent, 2)} | ${escapeHtml(under.verdict || "n/a")}</p>
      </article>
      <article class="metric">
        <h4>Games Sample</h4>
        <p>${escapeHtml(payload.gamesPlayed ?? "n/a")}</p>
      </article>
      <article class="metric">
        <h4>Distribution</h4>
        <p>${escapeHtml(ev.distributionMode || "normal")}</p>
      </article>
      ${payload.referenceBook ? `
      <article class="metric ev-over">
        <h4>Reference Book</h4>
        <p>${escapeHtml(payload.referenceBook.book || "n/a")} | No-Vig O ${pctAlreadyPercent(payload.referenceBook.noVigOver * 100)}</p>
      </article>` : ""}
    </div>
    ${renderLlmAnalysis(payload.llmAnalysis)}
    <h4>Adjustments</h4>
    <pre>${escapeHtml(JSON.stringify(adj, null, 2))}</pre>
    <h4>Raw Result</h4>
    <pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>
  `;
}

function renderSweep(payload) {
  if (!payload || payload.success !== true) {
    showError(sweepResult, payload?.error || "Auto sweep failed.", payload);
    return;
  }

  const best = payload.bestRecommendation || {};
  const rows = (Array.isArray(payload.rankedOffers) ? payload.rankedOffers : []).map((r) => `
    <tr>
      <td>${escapeHtml(r.bookmaker || "n/a")}</td>
      <td>${fmt(r.line, 1)}</td>
      <td>${escapeHtml(String(r.overOdds ?? "n/a"))}</td>
      <td>${escapeHtml(String(r.underOdds ?? "n/a"))}</td>
      <td>${escapeHtml(String(r.bestSide || "n/a").toUpperCase())}</td>
      <td>${fmt(r.bestEvPct, 2)}%</td>
      <td>${fmt(r.evOverPct, 2)}%</td>
      <td>${fmt(r.evUnderPct, 2)}%</td>
    </tr>
  `).join("");

  sweepResult.innerHTML = `
    <div class="metric-grid">
      <article class="metric">
        <h4>Player</h4>
        <p>${escapeHtml(payload.playerName || "n/a")} (${escapeHtml(payload.playerId ?? "n/a")})</p>
      </article>
      <article class="metric">
        <h4>Projection</h4>
        <p>${fmt(payload.projectionValue, 1)} (${escapeHtml(String(payload.stat || "").toUpperCase())})</p>
      </article>
      <article class="metric">
        <h4>Best Book / Line</h4>
        <p>${escapeHtml(best.bookmaker || "n/a")} @ ${fmt(best.line, 1)}</p>
      </article>
      <article class="metric">
        <h4>Best Side</h4>
        <p>${escapeHtml(String(best.bestSide || "n/a").toUpperCase())} | EV ${fmt(best.bestEvPct, 2)}%</p>
      </article>
      <article class="metric">
        <h4>Best Odds</h4>
        <p>O ${escapeHtml(String(best.overOdds ?? "n/a"))} / U ${escapeHtml(String(best.underOdds ?? "n/a"))}</p>
      </article>
      <article class="metric">
        <h4>Offers Scored</h4>
        <p>${escapeHtml(payload.offerCount ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Event</h4>
        <p>${escapeHtml(`${payload.eventAwayTeam || "?"} @ ${payload.eventHomeTeam || "?"}`)}</p>
      </article>
      <article class="metric">
        <h4>Market</h4>
        <p>${escapeHtml(payload.marketKey || "n/a")}</p>
      </article>
    </div>
    <h4>Ranked Line Offers</h4>
    <div class="odds-table-wrap">
      <table class="odds-table">
        <thead>
          <tr>
            <th>Book</th>
            <th>Line</th>
            <th>Over</th>
            <th>Under</th>
            <th>Best Side</th>
            <th>Best EV%</th>
            <th>Over EV%</th>
            <th>Under EV%</th>
          </tr>
        </thead>
        <tbody>
          ${rows || "<tr><td colspan='8'>No scored offers.</td></tr>"}
        </tbody>
      </table>
    </div>
    <h4>Raw Result</h4>
    <pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>
  `;
}

function renderParlay(payload) {
  if (!payload || payload.success !== true) {
    showError(parlayResult, payload?.error || "Parlay EV request failed.", payload);
    return;
  }

  parlayResult.innerHTML = `
    <div class="metric-grid">
      <article class="metric">
        <h4>Joint Prob</h4>
        <p>${pct(payload.jointProb)}</p>
      </article>
      <article class="metric">
        <h4>Naive Joint</h4>
        <p>${pct(payload.naiveJointProb)}</p>
      </article>
      <article class="metric">
        <h4>Correlation Impact</h4>
        <p>${fmt(payload.correlationImpact, 2)} pts</p>
      </article>
      <article class="metric">
        <h4>Parlay Odds</h4>
        <p>Dec ${fmt(payload.parlayDecOdds, 3)} | Am ${escapeHtml(payload.parlayAmericanOdds)}</p>
      </article>
      <article class="metric">
        <h4>EV%</h4>
        <p>${fmt(payload.evPercent, 2)} | ${escapeHtml(payload.verdict || "n/a")}</p>
      </article>
      <article class="metric">
        <h4>Kelly / Half</h4>
        <p>${fmt(payload.kellyFraction, 4)} / ${fmt(payload.halfKelly, 4)}</p>
      </article>
    </div>
    <h4>Raw Result</h4>
    <pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>
  `;
}

function renderOdds(payload) {
  if (!payload || payload.success !== true) {
    showError(oddsResult, payload?.error || "Odds request failed.", payload);
    return;
  }

  const discrepancies = Array.isArray(payload.discrepancies) ? payload.discrepancies.slice(0, 30) : [];
  const quota = payload.quota || {};
  const rows = discrepancies.map((d) => `
    <tr>
      <td>${escapeHtml(`${d.awayTeam || "?"} @ ${d.homeTeam || "?"}`)}</td>
      <td>${escapeHtml(d.market || "n/a")}</td>
      <td>${escapeHtml(`${d.outcome || "n/a"}${d.point !== null && d.point !== undefined ? ` (${d.point})` : ""}`)}</td>
      <td>${escapeHtml(String(d.bestPrice ?? "n/a"))} (${escapeHtml(d.bestBookmaker || "n/a")})</td>
      <td>${escapeHtml(String(d.worstPrice ?? "n/a"))} (${escapeHtml(d.worstBookmaker || "n/a")})</td>
      <td>${escapeHtml(fmt(d.valueGapPct, 2))}%</td>
    </tr>
  `).join("");

  oddsResult.innerHTML = `
    <div class="metric-grid">
      <article class="metric">
        <h4>Mode</h4>
        <p>${escapeHtml(payload.mode || "n/a")}</p>
      </article>
      <article class="metric">
        <h4>Events</h4>
        <p>${escapeHtml(payload.eventCount ?? "0")}</p>
      </article>
      <article class="metric">
        <h4>Discrepancies</h4>
        <p>${escapeHtml((payload.discrepancies || []).length)}</p>
      </article>
      <article class="metric">
        <h4>Quota Remaining</h4>
        <p>${escapeHtml(quota.remaining ?? "n/a")}</p>
      </article>
      <article class="metric">
        <h4>Quota Used</h4>
        <p>${escapeHtml(quota.used ?? "n/a")}</p>
      </article>
      <article class="metric">
        <h4>Quota Last</h4>
        <p>${escapeHtml(quota.last ?? "n/a")}</p>
      </article>
    </div>
    <h4>Top Line Discrepancies</h4>
    <div class="odds-table-wrap">
      <table class="odds-table">
        <thead>
          <tr>
            <th>Event</th>
            <th>Market</th>
            <th>Outcome</th>
            <th>Best</th>
            <th>Worst</th>
            <th>Gap</th>
          </tr>
        </thead>
        <tbody>
          ${rows || "<tr><td colspan='6'>No discrepancies in current payload.</td></tr>"}
        </tbody>
      </table>
    </div>
    <h4>Raw Result</h4>
    <pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>
  `;
}

function buildTrackingQuery(includeLimit = true) {
  const params = new URLSearchParams();
  if (includeLimit) {
    const limit = Math.max(1, Number(trackingLimitInput.value) || 15);
    params.set("limit", String(limit));
  }
  const date = trackingDateInput.value.trim();
  if (date) {
    params.set("date", date);
  }
  return params.toString();
}

function renderTrackingBest(payload) {
  if (!payload || payload.success !== true) {
    showError(trackingResult, payload?.error || "Failed to load best EV picks.", payload);
    return;
  }

  const top = Array.isArray(payload.top) ? payload.top : [];
  const rows = top.map((r) => `
    <tr>
      <td>${escapeHtml(r.playerName || `${r.playerId || "?"}`)}</td>
      <td>${escapeHtml(String(r.playerId ?? "n/a"))}</td>
      <td>${escapeHtml(String(r.stat || "n/a").toUpperCase())}</td>
      <td>${escapeHtml(String(r.recommendedSide || "n/a").toUpperCase())}</td>
      <td>${fmt(r.line, 1)}</td>
      <td>${fmt(r.projection, 1)}</td>
      <td>${fmt(r.recommendedEvPct, 2)}%</td>
      <td>${escapeHtml(String(r.recommendedOdds ?? "n/a"))}</td>
      <td>${statusPill(r.result || (r.settled ? "pending" : "pending"))}</td>
    </tr>
  `).join("");

  trackingResult.innerHTML = `
    <div class="metric-grid">
      <article class="metric">
        <h4>Date</h4>
        <p>${escapeHtml(payload.date || "n/a")}</p>
      </article>
      <article class="metric">
        <h4>Logged</h4>
        <p>${escapeHtml(payload.entriesLogged ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Unique</h4>
        <p>${escapeHtml(payload.entriesUnique ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Ranked</h4>
        <p>${escapeHtml(payload.rankedCount ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Positive Edge</h4>
        <p>${escapeHtml(payload.positiveEdgeCount ?? 0)}</p>
      </article>
    </div>
    <h4>Top EV Plays</h4>
    <div class="odds-table-wrap">
      <table class="odds-table">
        <thead>
          <tr>
            <th>Player</th>
            <th>ID</th>
            <th>Stat</th>
            <th>Side</th>
            <th>Line</th>
            <th>Proj</th>
            <th>EV%</th>
            <th>Odds</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          ${rows || "<tr><td colspan='9'>No ranked entries yet.</td></tr>"}
        </tbody>
      </table>
    </div>
  `;
}

function renderTrackingResults(payload, settlePayload = null) {
  if (!payload || payload.success !== true) {
    showError(trackingResult, payload?.error || "Failed to load results.", payload);
    return;
  }

  const summary = payload.summary || {};
  const clvSection = (summary.clvSampleSize > 0) ? `
    <h4>Closing Line Value</h4>
    <div class="metric-grid">
      <article class="metric">
        <h4>CLV Sample</h4>
        <p>${escapeHtml(summary.clvSampleSize ?? 0)}</p>
      </article>
      <article class="metric ev-over">
        <h4>Avg CLV Line</h4>
        <p>${fmt(summary.avgClvLine, 3)}</p>
      </article>
      <article class="metric ev-over">
        <h4>Avg CLV Odds%</h4>
        <p>${fmt(summary.avgClvOddsPct, 2)}%</p>
      </article>
      <article class="metric">
        <h4>+CLV Count</h4>
        <p>${escapeHtml(summary.positiveClvCount ?? 0)} / ${escapeHtml(summary.clvSampleSize ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>+CLV Rate</h4>
        <p>${pctAlreadyPercent(summary.positiveClvPct)}</p>
      </article>
    </div>
  ` : "";
  const rows = (Array.isArray(payload.results) ? payload.results : []).map((r) => `
    <tr>
      <td>${escapeHtml(r.playerName || `${r.playerId || "?"}`)}</td>
      <td>${escapeHtml(String(r.stat || "n/a").toUpperCase())}</td>
      <td>${escapeHtml(String(r.side || "n/a").toUpperCase())}</td>
      <td>${fmt(r.line, 1)}</td>
      <td>${fmt(r.actualStat, 1)}</td>
      <td>${fmt(r.recommendedEvPct, 2)}%</td>
      <td>${escapeHtml(String(r.odds ?? "n/a"))}</td>
      <td>${statusPill(r.result)}</td>
      <td>${fmt(r.pnl1u, 2)}</td>
    </tr>
  `).join("");

  let settleBanner = "";
  if (settlePayload) {
    settleBanner = `
      <h4>Settlement Run</h4>
      <div class="metric-grid">
        <article class="metric">
          <h4>Date</h4>
          <p>${escapeHtml(settlePayload.date || "n/a")}</p>
        </article>
        <article class="metric">
          <h4>Pending</h4>
          <p>${escapeHtml(settlePayload.pendingCount ?? 0)}</p>
        </article>
        <article class="metric">
          <h4>Settled Now</h4>
          <p>${escapeHtml(settlePayload.settledNow ?? 0)}</p>
        </article>
        <article class="metric">
          <h4>Unresolved</h4>
          <p>${escapeHtml(settlePayload.unresolved ?? 0)}</p>
        </article>
      </div>
    `;
  }

  trackingResult.innerHTML = `
    ${settleBanner}
    ${clvSection}
    <div class="metric-grid">
      <article class="metric">
        <h4>Date</h4>
        <p>${escapeHtml(payload.date || "n/a")}</p>
      </article>
      <article class="metric">
        <h4>Graded</h4>
        <p>${escapeHtml(summary.gradedCount ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Wins / Losses / Pushes</h4>
        <p>${escapeHtml(summary.wins ?? 0)} / ${escapeHtml(summary.losses ?? 0)} / ${escapeHtml(summary.pushes ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Hit Rate (No Push)</h4>
        <p>${pctAlreadyPercent(summary.hitRateNoPushPct)}</p>
      </article>
      <article class="metric">
        <h4>Total PnL (1u)</h4>
        <p>${fmt(summary.pnlUnits, 2)}</p>
      </article>
      <article class="metric">
        <h4>ROI / Bet</h4>
        <p>${pctAlreadyPercent(summary.roiPctPerBet)}</p>
      </article>
      <article class="metric">
        <h4>Unsettled</h4>
        <p>${escapeHtml(payload.unsettledCount ?? 0)}</p>
      </article>
    </div>
    <h4>Graded Picks</h4>
    <div class="odds-table-wrap">
      <table class="odds-table">
        <thead>
          <tr>
            <th>Player</th>
            <th>Stat</th>
            <th>Side</th>
            <th>Line</th>
            <th>Actual</th>
            <th>EV%</th>
            <th>Odds</th>
            <th>Result</th>
            <th>PnL</th>
          </tr>
        </thead>
        <tbody>
          ${rows || "<tr><td colspan='9'>No graded picks for this date yet.</td></tr>"}
        </tbody>
      </table>
    </div>
  `;
}

function renderStarterAccuracy(payload) {
  if (!payload || payload.success !== true) {
    showError(starterAccuracyResult, payload?.error || "Starter accuracy run failed.", payload);
    return;
  }

  const byStat = payload.byStat || {};
  const byStatRows = Object.entries(byStat).map(([stat, s]) => `
    <tr>
      <td>${escapeHtml(String(stat || "").toUpperCase())}</td>
      <td>${escapeHtml(s.leans ?? 0)}</td>
      <td>${escapeHtml(s.wins ?? 0)}</td>
      <td>${escapeHtml(s.losses ?? 0)}</td>
      <td>${escapeHtml(s.pushes ?? 0)}</td>
      <td>${pctAlreadyPercent(s.hitRateNoPushPct)}</td>
      <td>${fmt(s.pnlUnits, 2)}</td>
      <td>${pctAlreadyPercent(s.roiPctPerBet)}</td>
    </tr>
  `).join("");

  const topRows = (Array.isArray(payload.sampleTopByEv) ? payload.sampleTopByEv : []).slice(0, 12).map((r) => `
    <tr>
      <td>${escapeHtml(r.playerName || `${r.playerId || "?"}`)}</td>
      <td>${escapeHtml(r.teamAbbr || "")}</td>
      <td>${escapeHtml(String(r.stat || "").toUpperCase())}</td>
      <td>${escapeHtml(String(r.side || "").toUpperCase())}</td>
      <td>${fmt(r.line, 1)}</td>
      <td>${fmt(r.projection, 1)}</td>
      <td>${fmt(r.actual, 1)}</td>
      <td>${fmt(r.evPct, 2)}%</td>
      <td>${escapeHtml(String(r.odds ?? "n/a"))}</td>
      <td>${statusPill(r.outcome)}</td>
      <td>${fmt(r.pnl1u, 2)}</td>
    </tr>
  `).join("");

  starterAccuracyResult.innerHTML = `
    <div class="metric-grid">
      <article class="metric">
        <h4>Date</h4>
        <p>${escapeHtml(payload.targetDate || "n/a")}</p>
      </article>
      <article class="metric">
        <h4>Games Final</h4>
        <p>${escapeHtml(payload.gamesFinal ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Starters Seen</h4>
        <p>${escapeHtml(payload.startersSeen ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Starter Markets</h4>
        <p>${escapeHtml(payload.starterStatMarketsWithLines ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>EV Leans</h4>
        <p>${escapeHtml(payload.evLeansPlaced ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Record</h4>
        <p>${escapeHtml(payload.wins ?? 0)} / ${escapeHtml(payload.losses ?? 0)} / ${escapeHtml(payload.pushes ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Hit Rate (No Push)</h4>
        <p>${pctAlreadyPercent(payload.hitRateNoPushPct)}</p>
      </article>
      <article class="metric">
        <h4>PnL (1u)</h4>
        <p>${fmt(payload.pnlUnits, 2)}</p>
      </article>
      <article class="metric">
        <h4>ROI / Bet</h4>
        <p>${pctAlreadyPercent(payload.roiPctPerBet)}</p>
      </article>
      <article class="metric">
        <h4>Projection Errors</h4>
        <p>${escapeHtml(payload.projectionErrors ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Missing Odds Events</h4>
        <p>${escapeHtml(payload.missingEventOdds ?? 0)}</p>
      </article>
      <article class="metric">
        <h4>Runtime</h4>
        <p>${fmt(payload.runtimeSec, 1)}s</p>
      </article>
    </div>
    <h4>By Stat</h4>
    <div class="odds-table-wrap">
      <table class="odds-table">
        <thead>
          <tr>
            <th>Stat</th>
            <th>Leans</th>
            <th>Wins</th>
            <th>Losses</th>
            <th>Pushes</th>
            <th>Hit Rate</th>
            <th>PnL</th>
            <th>ROI</th>
          </tr>
        </thead>
        <tbody>
          ${byStatRows || "<tr><td colspan='8'>No stat rows.</td></tr>"}
        </tbody>
      </table>
    </div>
    <h4>Top EV Sample</h4>
    <div class="odds-table-wrap">
      <table class="odds-table">
        <thead>
          <tr>
            <th>Player</th>
            <th>Team</th>
            <th>Stat</th>
            <th>Side</th>
            <th>Line</th>
            <th>Proj</th>
            <th>Actual</th>
            <th>EV%</th>
            <th>Odds</th>
            <th>Outcome</th>
            <th>PnL</th>
          </tr>
        </thead>
        <tbody>
          ${topRows || "<tr><td colspan='11'>No EV leans found.</td></tr>"}
        </tbody>
      </table>
    </div>
    <h4>Raw Result</h4>
    <pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>
  `;
}

async function runStarterAccuracy() {
  starterAccuracyResult.innerHTML = "<p>Running starter EV accuracy. This can take a few minutes...</p>";
  const params = new URLSearchParams();
  const d = starterAccDateInput.value.trim();
  if (d) {
    params.set("date", d);
  }
  params.set("bookmakers", starterAccBookmakersInput.value.trim() || "draftkings,fanduel");
  params.set("regions", starterAccRegionsInput.value.trim() || "us");
  params.set("sport", starterAccSportInput.value.trim() || "basketball_nba");
  params.set("modelVariant", starterAccModelInput.value.trim() || "full");

  try {
    const data = await apiGet(`/api/starter_accuracy?${params.toString()}`);
    renderStarterAccuracy(data);
  } catch (err) {
    showError(starterAccuracyResult, `Failed to run starter accuracy: ${err.message}`);
  }
}

async function loadBestToday(silent = false) {
  if (!silent) {
    trackingResult.innerHTML = "<p>Loading top EV plays...</p>";
  }
  const query = buildTrackingQuery(true);
  const endpoint = query ? `/api/best_today?${query}` : "/api/best_today";
  try {
    const data = await apiGet(endpoint);
    renderTrackingBest(data);
  } catch (err) {
    if (!silent) {
      showError(trackingResult, `Failed to load best EV picks: ${err.message}`);
    }
  }
}

async function loadResults(settlePayload = null) {
  trackingResult.innerHTML = "<p>Loading results...</p>";
  const query = buildTrackingQuery(true);
  const endpoint = query ? `/api/results_yesterday?${query}` : "/api/results_yesterday";
  try {
    const data = await apiGet(endpoint);
    renderTrackingResults(data, settlePayload);
  } catch (err) {
    showError(trackingResult, `Failed to load results: ${err.message}`);
  }
}

async function settleDate() {
  trackingResult.innerHTML = "<p>Settling picks for selected date...</p>";
  const query = buildTrackingQuery(false);
  const endpoint = query ? `/api/settle_yesterday?${query}` : "/api/settle_yesterday";
  try {
    const settleData = await apiGet(endpoint);
    if (!settleData || settleData.success !== true) {
      return showError(trackingResult, settleData?.error || "Settlement failed.", settleData);
    }
    await loadResults(settleData);
  } catch (err) {
    showError(trackingResult, `Failed to settle picks: ${err.message}`);
  }
}

function updateOddsMarketWarning() {
  if (!oddsMarketWarning) {
    return;
  }
  const markets = $("oddsMarkets").value.trim();
  if (!hasAnyPlayerPropMarket(markets)) {
    oddsMarketWarning.hidden = true;
    oddsMarketWarning.textContent = "";
    return;
  }

  oddsMarketWarning.hidden = false;
  oddsMarketWarning.textContent =
    "Player prop markets are not supported on this odds endpoint. Use Auto Sweep Best Line for props.";
}

function applyPropsPreset() {
  $("oddsMarkets").value = PROP_MARKETS_PRESET;
  updateOddsMarketWarning();
}

async function loadOdds(liveMode = false) {
  oddsResult.innerHTML = `<p>Loading ${liveMode ? "live" : "pregame"} odds...</p>`;
  updateOddsMarketWarning();
  const params = new URLSearchParams({
    regions: $("oddsRegions").value.trim() || "us",
    markets: $("oddsMarkets").value.trim() || "h2h,spreads,totals",
    bookmakers: $("oddsBookmakers").value.trim(),
    sport: $("oddsSport").value.trim() || "basketball_nba",
  });
  if (liveMode) {
    params.set("maxEvents", String(Number($("oddsMaxEvents").value) || 8));
  }
  const endpoint = liveMode ? "/api/odds_live" : "/api/odds";

  try {
    const data = await apiGet(`${endpoint}?${params.toString()}`);
    renderOdds(data);
  } catch (err) {
    showError(oddsResult, `Failed to fetch odds: ${err.message}`);
  }
}

async function checkHealth() {
  try {
    const data = await apiGet("/api/health");
    if (data.success) {
      healthBadge.className = "badge ok";
      healthBadge.textContent = "API Ready";
    } else {
      healthBadge.className = "badge bad";
      healthBadge.textContent = "API Error";
    }
  } catch (err) {
    healthBadge.className = "badge bad";
    healthBadge.textContent = "API Offline";
  }
}

async function loadGames() {
  gamesContainer.innerHTML = "<p>Loading games...</p>";
  try {
    const data = await apiGet("/api/games");
    renderGames(data);
  } catch (err) {
    showError(gamesContainer, `Failed to fetch games: ${err.message}`);
  }
}

async function resolvePlayerContextForRequest(containerForError) {
  if (!playersLoaded) {
    await loadPlayersIndex();
  }

  let resolvedId = Number(playerIdInput.value);
  if (!Number.isFinite(resolvedId) || resolvedId <= 0) {
    resolvedId = null;
  }

  const rawName = playerNameInput.value.trim();
  if (rawName && playersLoaded) {
    const resolvedByName = resolvePlayerIdFromName(rawName);
    if (resolvedByName.ambiguous) {
      const opts = (resolvedByName.candidates || [])
        .map((x) => `${x.name} (${x.id})`)
        .join(", ");
      showError(
        containerForError,
        `Player name is ambiguous. Select a suggestion or type Player ID. Matches: ${opts}`
      );
      return null;
    }
    if (resolvedByName.id) {
      if (resolvedId && resolvedId !== resolvedByName.id) {
        showError(
          containerForError,
          `Player name and Player ID do not match. Name resolves to ${resolvedByName.id}, but ID field has ${resolvedId}.`
        );
        return null;
      }
      resolvedId = resolvedByName.id;
    }
  }

  if (!resolvedId && !rawName) {
    showError(
      containerForError,
      "Enter a valid Player ID or type/select a Player Name."
    );
    return null;
  }

  if (resolvedId) {
    playerIdInput.value = resolvedId;
  }
  if (playersLoaded && resolvedId) {
    syncPlayerNameFromId();
  }

  return {
    playerId: resolvedId || null,
    playerName: rawName || "",
  };
}

function renderLiveProjection(payload) {
  const liveResult = $("liveResult");
  if (!payload || payload.success !== true) {
    showError(liveResult, payload?.error || "Live projection failed.", payload);
    return;
  }

  const pace = Number(payload.gamePacePct);
  const paceBar = Number.isFinite(pace)
    ? `<div class="pace-bar-wrap"><div class="pace-bar" style="width:${Math.min(pace, 100).toFixed(1)}%"></div></div><span>${pace.toFixed(1)}% through projected mins</span>`
    : "";

  const stats = payload.liveStats || {};
  const statRows = [
    ["PTS", stats.PTS], ["REB", stats.REB], ["AST", stats.AST],
    ["STL", stats.STL], ["BLK", stats.BLK], ["TOV", stats.TOV], ["FG3M", stats.FG3M],
  ].map(([k, v]) => `<tr><td>${k}</td><td>${v ?? "n/a"}</td></tr>`).join("");

  const periodLabel = payload.gameStatus === 3 ? "Final" : `Q${payload.period ?? "?"}`;

  liveResult.innerHTML = `
    <div class="metric-grid">
      <article class="metric ev-over">
        <h4>Live Projection</h4>
        <p>${fmt(payload.liveProjection, 1)}</p>
      </article>
      <article class="metric">
        <h4>Current ${String(payload.stat || "").toUpperCase()}</h4>
        <p>${fmt(payload.currentStat, 1)}</p>
      </article>
      <article class="metric">
        <h4>Pregame Proj</h4>
        <p>${fmt(payload.pregameProjection, 1)}</p>
      </article>
      <article class="metric">
        <h4>Mins Played</h4>
        <p>${fmt(payload.minsPlayed, 1)} / ${fmt(payload.projectedMinutes, 1)}</p>
      </article>
      <article class="metric">
        <h4>Remaining Mins</h4>
        <p>${fmt(payload.remainingMins, 1)}</p>
      </article>
      <article class="metric">
        <h4>Per-Min Rate</h4>
        <p>${fmt(payload.perMinRate, 4)}</p>
      </article>
      <article class="metric">
        <h4>Period</h4>
        <p>${escapeHtml(periodLabel)}</p>
      </article>
    </div>
    <div class="pace-row">${paceBar}</div>
    <h4>Live Stat Line</h4>
    <table class="odds-table">
      <thead><tr><th>Stat</th><th>Current</th></tr></thead>
      <tbody>${statRows}</tbody>
    </table>
  `;
}

propForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  propResult.innerHTML = "<p>Running prop EV...</p>";
  sweepResult.innerHTML = "";

  const playerCtx = await resolvePlayerContextForRequest(propResult);
  if (!playerCtx) {
    return;
  }

  const payload = {
    playerId: playerCtx.playerId,
    playerName: playerCtx.playerName,
    playerTeamAbbr: toUpperTrim($("playerTeamAbbr").value),
    opponentAbbr: toUpperTrim($("opponentAbbr").value),
    isHome: $("isHome").checked,
    stat: $("stat").value,
    line: Number($("line").value),
    overOdds: Number($("overOdds").value),
    underOdds: Number($("underOdds").value),
    isB2b: $("isB2b").checked,
    referenceBook: $("referenceBook").value.trim(),
  };

  try {
    const data = await apiPost("/api/prop_ev", payload);
    renderProp(data);
    if (data && data.success === true) {
      loadBestToday(true);
    }
  } catch (err) {
    showError(propResult, `Failed to run prop EV: ${err.message}`);
  }
});

autoSweepBtn.addEventListener("click", async () => {
  sweepResult.innerHTML = "<p>Running auto line sweep...</p>";

  const playerCtx = await resolvePlayerContextForRequest(sweepResult);
  if (!playerCtx) {
    return;
  }

  const playerTeamAbbr = toUpperTrim($("playerTeamAbbr").value);
  if (!playerTeamAbbr) {
    return showError(sweepResult, "Player Team is required for auto sweep.");
  }

  const payload = {
    playerId: playerCtx.playerId,
    playerName: playerCtx.playerName,
    playerTeamAbbr,
    opponentAbbr: toUpperTrim($("opponentAbbr").value),
    isHome: $("isHome").checked,
    stat: $("stat").value,
    isB2b: $("isB2b").checked,
    regions: $("oddsRegions").value.trim() || "us",
    bookmakers: $("oddsBookmakers").value.trim(),
    sport: $("oddsSport").value.trim() || "basketball_nba",
    topN: Math.max(1, Number(sweepTopNInput.value) || 15),
  };

  try {
    const data = await apiPost("/api/auto_sweep", payload);
    renderSweep(data);
    if (data && data.success === true) {
      loadBestToday(true);
    }
  } catch (err) {
    showError(sweepResult, `Failed to run auto sweep: ${err.message}`);
  }
});

playerIdInput.addEventListener("change", syncPlayerNameFromId);
playerNameInput.addEventListener("change", syncPlayerIdFromName);
playerNameInput.addEventListener("blur", syncPlayerIdFromName);

// Live projection player sync
function syncLiveNameFromId() {
  if (!playersLoaded) return;
  const id = Number($("livePlayerId").value);
  if (!Number.isFinite(id) || id <= 0) return;
  const entry = playersById.get(id);
  if (entry) $("livePlayerName").value = `${entry.name} (${entry.id})`;
}
function syncLiveIdFromName() {
  if (!playersLoaded) return;
  const resolved = resolvePlayerIdFromName($("livePlayerName").value);
  if (resolved.id) {
    $("livePlayerId").value = resolved.id;
    const entry = playersById.get(resolved.id);
    if (entry) $("livePlayerName").value = `${entry.name} (${entry.id})`;
  }
}
$("livePlayerId").addEventListener("change", syncLiveNameFromId);
$("livePlayerName").addEventListener("change", syncLiveIdFromName);
$("livePlayerName").addEventListener("blur", syncLiveIdFromName);

$("liveForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const liveResult = $("liveResult");
  liveResult.innerHTML = "<p>Fetching live boxscore...</p>";

  if (!playersLoaded) await loadPlayersIndex();

  let resolvedId = Number($("livePlayerId").value);
  if (!Number.isFinite(resolvedId) || resolvedId <= 0) resolvedId = null;

  const rawName = $("livePlayerName").value.trim();
  if (rawName && playersLoaded) {
    const r = resolvePlayerIdFromName(rawName);
    if (r.ambiguous) {
      const opts = (r.candidates || []).map((x) => `${x.name} (${x.id})`).join(", ");
      return showError(liveResult, `Ambiguous player name. Matches: ${opts}`);
    }
    if (r.id) resolvedId = r.id;
  }

  if (!resolvedId && !rawName) {
    return showError(liveResult, "Enter a Player ID or Player Name.");
  }

  const playerTeamAbbr = toUpperTrim($("livePlayerTeam").value);
  if (!playerTeamAbbr) return showError(liveResult, "Player Team is required.");

  const payload = {
    playerId: resolvedId || null,
    playerName: rawName || "",
    playerTeamAbbr,
    opponentAbbr: toUpperTrim($("liveOpponent").value),
    isHome: $("liveIsHome").checked,
    stat: $("liveStat").value,
  };

  try {
    const data = await apiPost("/api/live_projection", payload);
    renderLiveProjection(data);
  } catch (err) {
    showError(liveResult, `Live projection failed: ${err.message}`);
  }
});

parlayBtn.addEventListener("click", async () => {
  parlayResult.innerHTML = "<p>Running parlay EV...</p>";
  let legs;
  try {
    legs = JSON.parse(parlayLegs.value);
  } catch (err) {
    return showError(parlayResult, `Leg JSON parse failed: ${err.message}`);
  }

  try {
    const data = await apiPost("/api/parlay_ev", { legs });
    renderParlay(data);
  } catch (err) {
    showError(parlayResult, `Failed to run parlay EV: ${err.message}`);
  }
});

$("loadGamesBtn").addEventListener("click", loadGames);
loadOddsBtn.addEventListener("click", () => loadOdds(false));
loadLiveOddsBtn.addEventListener("click", () => loadOdds(true));
propsPresetBtn.addEventListener("click", applyPropsPreset);
$("oddsMarkets").addEventListener("input", updateOddsMarketWarning);
trackingForm.addEventListener("submit", (event) => {
  event.preventDefault();
  loadBestToday();
});
loadBestTodayBtn.addEventListener("click", () => loadBestToday());
settleYesterdayBtn.addEventListener("click", () => settleDate());
loadResultsYesterdayBtn.addEventListener("click", () => loadResults());
starterAccuracyForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runStarterAccuracy();
});
runStarterAccuracyBtn.addEventListener("click", () => runStarterAccuracy());

checkHealth();
loadGames();
loadPlayersIndex();
loadBestToday();
updateOddsMarketWarning();

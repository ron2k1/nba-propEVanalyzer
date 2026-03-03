// Analyze tab — Prop EV form, auto sweep, LLM analysis
import { apiGet, apiPost, escapeHtml, fmt, pct, pctAlready, toUpperTrim, showError, DEFAULT_BOOKMAKERS } from './api.js';

export default function () {
  return {
    // Form state
    playerId: '',
    playerName: '',
    playerTeamAbbr: '',
    opponentAbbr: '',
    stat: 'pts',
    line: 24.5,
    overOdds: -110,
    underOdds: -110,
    isHome: true,
    isB2b: false,
    referenceBook: '',
    minutesMult: '',
    sweepTopN: 15,

    // Results
    propLoading: false,
    propResult: null,
    propError: '',

    sweepLoading: false,
    sweepResult: null,
    sweepError: '',

    init() {
      window.addEventListener('prefill-game', (e) => {
        const d = e.detail;
        this.playerTeamAbbr = d.teamAbbr || '';
        this.opponentAbbr = d.opponent || '';
        this.isHome = !!d.isHome;
      });
    },

    async resolvePlayer(errorTarget) {
      const ps = Alpine.store('players');
      if (!ps.loaded) await ps.load();

      let resolvedId = Number(this.playerId);
      if (!Number.isFinite(resolvedId) || resolvedId <= 0) resolvedId = null;

      const rawName = this.playerName.trim();
      if (rawName && ps.loaded) {
        const r = ps.resolve(rawName);
        if (r.ambiguous) {
          const opts = (r.candidates || []).map(x => `${x.name} (${x.id})`).join(', ');
          this[errorTarget] = `Ambiguous player name. Matches: ${opts}`;
          return null;
        }
        if (r.id) {
          if (resolvedId && resolvedId !== r.id) {
            this[errorTarget] = `Name resolves to ${r.id}, but ID field has ${resolvedId}.`;
            return null;
          }
          resolvedId = r.id;
        }
      }

      if (!resolvedId && !rawName) {
        this[errorTarget] = 'Enter a Player ID or select a Player Name.';
        return null;
      }

      if (resolvedId) {
        this.playerId = resolvedId;
        const name = ps.nameForId(resolvedId);
        if (name) this.playerName = name;
      }

      return { playerId: resolvedId || null, playerName: rawName || '' };
    },

    async runPropEv() {
      this.propLoading = true;
      this.propError = '';
      this.propResult = null;
      this.sweepResult = null;
      this.sweepError = '';

      const ctx = await this.resolvePlayer('propError');
      if (!ctx) { this.propLoading = false; return; }

      try {
        const data = await apiPost('/api/prop_ev', {
          playerId: ctx.playerId,
          playerName: ctx.playerName,
          playerTeamAbbr: toUpperTrim(this.playerTeamAbbr),
          opponentAbbr: toUpperTrim(this.opponentAbbr),
          isHome: this.isHome,
          stat: this.stat,
          line: Number(this.line),
          overOdds: Number(this.overOdds),
          underOdds: Number(this.underOdds),
          isB2b: this.isB2b,
          referenceBook: this.referenceBook.trim(),
          minutesMultiplier: this.minutesMult !== '' ? Number(this.minutesMult) : null,
        });
        if (!data || data.success !== true) {
          this.propError = data?.error || 'Prop EV request failed.';
          return;
        }
        this.propResult = data;
      } catch (err) {
        this.propError = `Failed: ${err.message}`;
      } finally {
        this.propLoading = false;
      }
    },

    async runAutoSweep() {
      this.sweepLoading = true;
      this.sweepError = '';
      this.sweepResult = null;

      const ctx = await this.resolvePlayer('sweepError');
      if (!ctx) { this.sweepLoading = false; return; }

      if (!this.playerTeamAbbr.trim()) {
        this.sweepError = 'Player Team is required for auto sweep.';
        this.sweepLoading = false;
        return;
      }

      try {
        const data = await apiPost('/api/auto_sweep', {
          playerId: ctx.playerId,
          playerName: ctx.playerName,
          playerTeamAbbr: toUpperTrim(this.playerTeamAbbr),
          opponentAbbr: toUpperTrim(this.opponentAbbr),
          isHome: this.isHome,
          stat: this.stat,
          isB2b: this.isB2b,
          regions: 'us',
          bookmakers: DEFAULT_BOOKMAKERS,
          sport: 'basketball_nba',
          topN: Math.max(1, Number(this.sweepTopN) || 15),
        });
        if (!data || data.success !== true) {
          this.sweepError = data?.error || 'Auto sweep failed.';
          return;
        }
        this.sweepResult = data;
      } catch (err) {
        this.sweepError = `Failed: ${err.message}`;
      } finally {
        this.sweepLoading = false;
      }
    },

    // Render helpers for complex LLM cards
    renderLlmHtml() {
      const llm = this.propResult?.llmAnalysis;
      if (!llm) return '';

      function providerTag(provider) {
        return provider ? `<span class="llm-provider-tag">${escapeHtml(provider)}</span>` : '';
      }

      function injuryCard(s) {
        if (!s || !s.success) {
          return `<div class="llm-card"><div class="llm-card-title">Injury Signal ${providerTag(s?.provider)}</div><div class="llm-card-error">${escapeHtml(s?.error || 'No news signals')}</div></div>`;
        }
        const adjPct = Number(s.adjustmentPct) * 100;
        const sign = adjPct >= 0 ? '+' : '';
        const adjColor = adjPct > 0 ? 'color:var(--ok)' : adjPct < 0 ? 'color:var(--bad)' : '';
        return `<div class="llm-card"><div class="llm-card-title">Injury Signal ${providerTag(s.provider)}</div><div class="llm-card-value" style="${adjColor}">${sign}${adjPct.toFixed(1)}%</div><div class="llm-card-reasoning">${escapeHtml(s.reasoning || '')}</div><div class="llm-card-meta">Confidence: ${pct(s.confidence)}</div></div>`;
      }

      function matchupCard(s) {
        if (!s || !s.success) {
          return `<div class="llm-card"><div class="llm-card-title">Matchup Context ${providerTag(s?.provider)}</div><div class="llm-card-error">${escapeHtml(s?.error || 'Unavailable')}</div></div>`;
        }
        const mod = Number(s.modifier);
        const modColor = mod >= 1 ? 'color:var(--ok)' : 'color:var(--bad)';
        return `<div class="llm-card"><div class="llm-card-title">Matchup Context ${providerTag(s.provider)}</div><div class="llm-card-value" style="${modColor}">${fmt(mod, 2)}x</div><div class="llm-card-reasoning">${escapeHtml(s.reasoning || '')}</div><div class="llm-card-meta">Confidence: ${pct(s.confidence)}</div></div>`;
      }

      function lineCard(s) {
        if (!s || !s.success) {
          return `<div class="llm-card"><div class="llm-card-title">Line Reasoning ${providerTag(s?.provider)}</div><div class="llm-card-error">${escapeHtml(s?.error || 'Unavailable')}</div></div>`;
        }
        const verdict = String(s.verdict || 'neutral').toLowerCase();
        return `<div class="llm-card verdict-${verdict}"><div class="llm-card-title">Line Reasoning ${providerTag(s.provider)}</div><div class="llm-card-value">${verdict.toUpperCase()} ${escapeHtml(String(s.sharpnessScore ?? '?'))}/10</div><div class="llm-card-reasoning">${escapeHtml(s.reasoning || '')}</div></div>`;
      }

      return `<div class="llm-cards">${injuryCard(llm.injurySignal)}${matchupCard(llm.matchupContext)}${lineCard(llm.lineReasoning)}</div>`;
    },

    renderSweepTableHtml() {
      if (!this.sweepResult) return '';
      const offers = Array.isArray(this.sweepResult.rankedOffers) ? this.sweepResult.rankedOffers : [];
      if (!offers.length) return '<p>No scored offers.</p>';
      const rows = offers.map(r =>
        `<tr><td>${escapeHtml(r.bookmaker || 'n/a')}</td><td>${fmt(r.line, 1)}</td><td>${r.overOdds ?? 'n/a'}</td><td>${r.underOdds ?? 'n/a'}</td><td>${String(r.bestSide || 'n/a').toUpperCase()}</td><td>${fmt(r.bestEvPct, 2)}%</td><td>${fmt(r.evOverPct, 2)}%</td><td>${fmt(r.evUnderPct, 2)}%</td></tr>`
      ).join('');
      return `<div class="odds-table-wrap"><table class="odds-table"><thead><tr><th>Book</th><th>Line</th><th>Over</th><th>Under</th><th>Side</th><th>Best EV%</th><th>Over EV%</th><th>Under EV%</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    },

    // Expose helpers
    fmt, pct, pctAlready, escapeHtml,
  };
}

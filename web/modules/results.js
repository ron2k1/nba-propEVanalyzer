// Results tab — settlement, results_yesterday, CLV, paper_summary
import { apiGet, escapeHtml, fmt, pct, pctAlready, statusPill } from './api.js';

export default function () {
  return {
    // Controls
    trackingDate: '',
    trackingLimit: 15,

    // Results
    resultsLoading: false,
    resultsData: null,
    settleData: null,
    resultsError: '',

    // Paper Summary
    summaryLoading: false,
    summaryData: null,
    summaryError: '',
    summaryWindow: 14,

    async loadBestToday() {
      this.resultsLoading = true;
      this.resultsError = '';
      this.resultsData = null;
      this.settleData = null;
      try {
        const params = new URLSearchParams({ limit: String(this.trackingLimit) });
        if (this.trackingDate) params.set('date', this.trackingDate);
        const data = await apiGet(`/api/best_today?${params}`);
        if (!data || data.success !== true) {
          this.resultsError = data?.error || 'Failed to load best EV picks.';
          return;
        }
        this.resultsData = { mode: 'best', ...data };
      } catch (err) {
        this.resultsError = `Failed: ${err.message}`;
      } finally {
        this.resultsLoading = false;
      }
    },

    async settleDate() {
      this.resultsLoading = true;
      this.resultsError = '';
      try {
        const params = new URLSearchParams();
        if (this.trackingDate) params.set('date', this.trackingDate);
        const settle = await apiGet(`/api/settle_yesterday?${params}`);
        if (!settle || settle.success !== true) {
          this.resultsError = settle?.error || 'Settlement failed.';
          this.resultsLoading = false;
          return;
        }
        this.settleData = settle;
        // Auto-load results after settle
        await this.loadResults();
      } catch (err) {
        this.resultsError = `Failed: ${err.message}`;
        this.resultsLoading = false;
      }
    },

    async loadResults() {
      this.resultsLoading = true;
      this.resultsError = '';
      try {
        const params = new URLSearchParams({ limit: String(this.trackingLimit) });
        if (this.trackingDate) params.set('date', this.trackingDate);
        const data = await apiGet(`/api/results_yesterday?${params}`);
        if (!data || data.success !== true) {
          this.resultsError = data?.error || 'Failed to load results.';
          return;
        }
        this.resultsData = { mode: 'results', ...data };
      } catch (err) {
        this.resultsError = `Failed: ${err.message}`;
      } finally {
        this.resultsLoading = false;
      }
    },

    async loadPaperSummary() {
      this.summaryLoading = true;
      this.summaryError = '';
      this.summaryData = null;
      try {
        const data = await apiGet(`/api/paper_summary?windowDays=${this.summaryWindow}`);
        if (!data || data.success !== true) {
          this.summaryError = data?.error || 'Paper summary failed.';
          return;
        }
        this.summaryData = data;
      } catch (err) {
        this.summaryError = `Failed: ${err.message}`;
      } finally {
        this.summaryLoading = false;
      }
    },

    // Computed helpers
    bestRows() {
      if (!this.resultsData || this.resultsData.mode !== 'best') return [];
      return Array.isArray(this.resultsData.top) ? this.resultsData.top : [];
    },

    resultRows() {
      if (!this.resultsData || this.resultsData.mode !== 'results') return [];
      return Array.isArray(this.resultsData.results) ? this.resultsData.results : [];
    },

    summary() {
      return this.resultsData?.summary || {};
    },

    hasClv() {
      return (this.resultsData?.summary?.clvSampleSize || 0) > 0;
    },

    renderResultsTableHtml() {
      if (!this.resultsData) return '';
      if (this.resultsData.mode === 'best') {
        const rows = this.bestRows().map(r =>
          `<tr><td>${escapeHtml(r.playerName || r.playerId || '?')}</td><td>${String(r.stat || '').toUpperCase()}</td><td>${String(r.recommendedSide || '').toUpperCase()}</td><td>${fmt(r.line, 1)}</td><td>${fmt(r.projection, 1)}</td><td>${fmt(r.recommendedEvPct, 2)}%</td><td>${r.recommendedOdds ?? 'n/a'}</td><td>${statusPill(r.result || 'pending')}</td></tr>`
        ).join('');
        return `<table class="odds-table"><thead><tr><th>Player</th><th>Stat</th><th>Side</th><th>Line</th><th>Proj</th><th>EV%</th><th>Odds</th><th>Status</th></tr></thead><tbody>${rows || '<tr><td colspan="8">No ranked entries yet.</td></tr>'}</tbody></table>`;
      }
      // Results mode
      const rows = this.resultRows().map(r =>
        `<tr><td>${escapeHtml(r.playerName || r.playerId || '?')}</td><td>${String(r.stat || '').toUpperCase()}</td><td>${String(r.side || '').toUpperCase()}</td><td>${fmt(r.line, 1)}</td><td>${fmt(r.actualStat, 1)}</td><td>${fmt(r.recommendedEvPct, 2)}%</td><td>${r.odds ?? 'n/a'}</td><td>${statusPill(r.result)}</td><td>${fmt(r.pnl1u, 2)}</td></tr>`
      ).join('');
      return `<table class="odds-table"><thead><tr><th>Player</th><th>Stat</th><th>Side</th><th>Line</th><th>Actual</th><th>EV%</th><th>Odds</th><th>Result</th><th>PnL</th></tr></thead><tbody>${rows || '<tr><td colspan="9">No graded picks.</td></tr>'}</tbody></table>`;
    },

    renderSummaryHtml() {
      if (!this.summaryData) return '';
      const d = this.summaryData;
      const gate = d.gate || {};
      const metrics = gate.metrics || {};
      const byStat = d.byStat || {};
      const leans = gate.model_leans || {};
      const research = gate.research_stats || {};
      const edgeAt = gate.edge_at_emission || {};

      let statRows = '';
      for (const [stat, s] of Object.entries(byStat)) {
        statRows += `<tr><td>${escapeHtml(stat.toUpperCase())}</td><td>${s.signals ?? 0}</td><td>${s.wins ?? 0}</td><td>${s.losses ?? 0}</td><td>${pctAlready(s.hitRate)}</td><td>${fmt(s.roi, 2)}%</td></tr>`;
      }

      // Research stats rows
      let researchRows = '';
      for (const [stat, s] of Object.entries(research)) {
        researchRows += `<tr><td>${escapeHtml(stat.toUpperCase())}</td><td>${s.count ?? 0}</td><td>${s.wins ?? 0}</td><td>${(s.count ?? 0) - (s.wins ?? 0)}</td><td>${pctAlready(s.hitRate)}</td><td>${fmt(s.pnl, 2)}u</td></tr>`;
      }

      const policyEdge = edgeAt.policy || {};
      const allEdge = edgeAt.all || {};
      const wl = (gate.config || {}).stat_whitelist || [];

      return `
        <div class="gate-badge ${gate.gatePass ? 'gate-pass' : 'gate-fail'}">
          <span class="gate-label">${gate.gatePass ? 'GATE PASS' : 'GATE FAIL'}</span>
          <span style="font-size:0.75em;opacity:0.7;margin-left:8px">${escapeHtml(gate.reason || '')}</span>
        </div>
        <h4 style="margin-top:14px">Policy-Qualified (${escapeHtml(wl.join(', '))})</h4>
        <div class="metric-grid" style="margin-top:8px">
          <article class="metric"><h4>Sample</h4><p>${metrics.sample ?? 0} / 50</p></article>
          <article class="metric ev-over"><h4>Hit Rate</h4><p>${pctAlready(metrics.hit_rate)}</p></article>
          <article class="metric ev-over"><h4>ROI</h4><p>${pctAlready(metrics.roi)}</p></article>
          <article class="metric"><h4>+CLV %</h4><p>${pctAlready(metrics.positive_clv_pct, 1)}</p></article>
          <article class="metric"><h4>Avg Edge</h4><p>${pctAlready(policyEdge.avgEdge)}</p></article>
          <article class="metric"><h4>Window</h4><p>${gate.windowDays ?? 14}d</p></article>
        </div>
        <h4 style="margin-top:14px">Edge at Emission</h4>
        <div class="metric-grid" style="margin-top:8px">
          <article class="metric"><h4>Avg</h4><p>${pctAlready(policyEdge.avgEdge)}</p></article>
          <article class="metric"><h4>Min</h4><p>${pctAlready(policyEdge.minEdge)}</p></article>
          <article class="metric"><h4>Max</h4><p>${pctAlready(policyEdge.maxEdge)}</p></article>
        </div>
        ${statRows ? `<h4 style="margin-top:14px">By Stat (Policy)</h4><div class="odds-table-wrap"><table class="odds-table"><thead><tr><th>Stat</th><th>Signals</th><th>Wins</th><th>Losses</th><th>Hit Rate</th><th>ROI</th></tr></thead><tbody>${statRows}</tbody></table></div>` : ''}
        <h4 style="margin-top:14px">Model Leans (All Eligible Stats)</h4>
        <div class="metric-grid" style="margin-top:8px">
          <article class="metric"><h4>Sample</h4><p>${leans.sample ?? 0}</p></article>
          <article class="metric ev-over"><h4>Hit Rate</h4><p>${pctAlready(leans.hitRate)}</p></article>
          <article class="metric ev-over"><h4>ROI</h4><p>${pctAlready(leans.roi)}</p></article>
          <article class="metric"><h4>PnL</h4><p>${fmt(leans.pnl, 2)}u</p></article>
          <article class="metric"><h4>Avg Edge</h4><p>${pctAlready(leans.avgEdge)}</p></article>
        </div>
        ${researchRows ? `<h4 style="margin-top:14px">Research Stats (Not in Whitelist)</h4><div class="odds-table-wrap"><table class="odds-table"><thead><tr><th>Stat</th><th>Signals</th><th>Wins</th><th>Losses</th><th>Hit Rate</th><th>PnL</th></tr></thead><tbody>${researchRows}</tbody></table></div>` : ''}
      `;
    },

    fmt, pct, pctAlready, escapeHtml, statusPill,
  };
}

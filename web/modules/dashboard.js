// Dashboard tab — games grid + GO-LIVE gate badge
import { apiGet, escapeHtml, toUpperTrim, toast } from './api.js';

export default function () {
  return {
    // Health
    healthStatus: 'checking',
    healthText: 'Checking API...',

    // Games
    games: [],
    gamesDate: '',
    gamesStale: false,
    gamesLoading: false,
    gamesError: '',

    // Gate
    gate: null,
    gateLoading: false,
    gateError: '',

    // F9: Auto-refresh
    autoRefresh: false,
    lastRefresh: null,
    _refreshTimer: null,

    async init() {
      this.checkHealth();
      // Auto-settle finished games before loading data
      this.autoSettle();
      this.loadGames();
      this.loadGate();
      this.lastRefresh = new Date().toLocaleTimeString();
    },

    async autoSettle() {
      try {
        const r = await apiGet('/api/auto_settle');
        if (r && r.settledNow > 0) {
          toast(`Auto-settled ${r.settledNow} entries`, 'ok');
        }
      } catch { /* silent — non-critical */ }
    },

    async checkHealth() {
      try {
        const data = await apiGet('/api/health');
        if (data.success) {
          this.healthStatus = 'ok';
          this.healthText = 'API Ready';
        } else {
          this.healthStatus = 'bad';
          this.healthText = 'API Error';
        }
      } catch {
        this.healthStatus = 'bad';
        this.healthText = 'API Offline';
      }
    },

    async loadGames() {
      this.gamesLoading = true;
      this.gamesError = '';
      try {
        const data = await apiGet('/api/games');
        if (!data || data.success !== true) {
          this.gamesError = data?.error || 'Failed to load games.';
          return;
        }
        this.games = Array.isArray(data.games) ? data.games : [];
        this.gamesDate = data.date || '';
        this.gamesStale = !!data.isStale;
      } catch (err) {
        this.gamesError = `Failed to fetch games: ${err.message}`;
      } finally {
        this.gamesLoading = false;
      }
    },

    async loadGate() {
      this.gateLoading = true;
      this.gateError = '';
      try {
        const data = await apiGet('/api/journal_gate?windowDays=14');
        if (!data || data.error) {
          this.gateError = data?.error || 'Gate check unavailable.';
          return;
        }
        // Normalize: API returns {gatePass, metrics, ...} not {gate: {gatePass, ...}}
        this.gate = {
          gate: { gatePass: data.gatePass, sample: data.metrics?.sample, roi: data.metrics?.roi, positive_clv_pct: data.metrics?.positive_clv_pct },
          totalSignals: data.metrics?.sample ?? 0,
          roi: (data.metrics?.roi ?? 0) * 100,
          positiveClvPct: data.metrics?.positive_clv_pct ?? 0,
          reason: data.reason || '',
        };
      } catch (err) {
        this.gateError = `Gate check failed: ${err.message}`;
      } finally {
        this.gateLoading = false;
      }
    },

    // F4: Click game card -> jump to Analyze with prefill
    analyzeGame(g, isHome) {
      const home = this.homeAbbr(g);
      const away = this.awayAbbr(g);
      this.prefill(home, away, isHome);
      Alpine.store('tab').set('analyze');
    },

    prefill(home, away, isHome) {
      // Dispatch to analyze tab's form
      window.dispatchEvent(new CustomEvent('prefill-game', {
        detail: { teamAbbr: toUpperTrim(isHome ? home : away), opponent: toUpperTrim(isHome ? away : home), isHome }
      }));
    },

    // F9: Toggle auto-refresh (60s interval)
    toggleAutoRefresh() {
      this.autoRefresh = !this.autoRefresh;
      if (this._refreshTimer) { clearInterval(this._refreshTimer); this._refreshTimer = null; }
      if (this.autoRefresh) {
        this._refreshTimer = setInterval(async () => {
          await this.autoSettle();
          await this.loadGames();
          await this.loadGate();
          this.lastRefresh = new Date().toLocaleTimeString();
          toast('Dashboard refreshed', 'ok');
        }, 60000);
      }
    },

    homeAbbr(g) { return g.homeTeam?.abbreviation || 'HOME'; },
    awayAbbr(g) { return g.awayTeam?.abbreviation || 'AWAY'; },
    gameMeta(g) {
      let s = g.status || 'Scheduled';
      if (this.gamesDate) s += ` | ${this.gamesDate}`;
      if (this.gamesStale) s += ' | stale';
      return s;
    },
  };
}

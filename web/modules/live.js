// Live tab — Live projection + Parlay EV builder
import { apiPost, escapeHtml, fmt, pct } from './api.js';

export default function () {
  return {
    // Live Projection
    livePlayerId: '',
    livePlayerName: '',
    livePlayerTeam: '',
    liveOpponent: '',
    liveStat: 'pts',
    liveIsHome: false,
    liveLoading: false,
    liveResult: null,
    liveError: '',

    // Parlay
    parlayLegs: `[
  {"playerId":2544,"playerTeam":"LAL","stat":"pts","line":25.5,"side":"over","probOver":0.58,"overOdds":-110,"underOdds":-110},
  {"playerId":2544,"playerTeam":"LAL","stat":"reb","line":8.5,"side":"under","probOver":0.52,"overOdds":-115,"underOdds":-105}
]`,
    parlayLoading: false,
    parlayResult: null,
    parlayError: '',

    async runLiveProjection() {
      this.liveLoading = true;
      this.liveError = '';
      this.liveResult = null;

      const ps = Alpine.store('players');
      if (!ps.loaded) await ps.load();

      let resolvedId = Number(this.livePlayerId);
      if (!Number.isFinite(resolvedId) || resolvedId <= 0) resolvedId = null;

      const rawName = this.livePlayerName.trim();
      if (rawName && ps.loaded) {
        const r = ps.resolve(rawName);
        if (r.ambiguous) {
          this.liveError = `Ambiguous name. Matches: ${(r.candidates || []).map(x => `${x.name} (${x.id})`).join(', ')}`;
          this.liveLoading = false;
          return;
        }
        if (r.id) resolvedId = r.id;
      }

      if (!resolvedId && !rawName) {
        this.liveError = 'Enter a Player ID or Player Name.';
        this.liveLoading = false;
        return;
      }

      if (!this.livePlayerTeam.trim()) {
        this.liveError = 'Player Team is required.';
        this.liveLoading = false;
        return;
      }

      try {
        const data = await apiPost('/api/live_projection', {
          playerId: resolvedId || null,
          playerName: rawName || '',
          playerTeamAbbr: this.livePlayerTeam.trim().toUpperCase(),
          opponentAbbr: this.liveOpponent.trim().toUpperCase(),
          isHome: this.liveIsHome,
          stat: this.liveStat,
        });
        if (!data || data.success !== true) {
          this.liveError = data?.error || 'Live projection failed.';
          return;
        }
        this.liveResult = data;
      } catch (err) {
        this.liveError = `Failed: ${err.message}`;
      } finally {
        this.liveLoading = false;
      }
    },

    async runParlay() {
      this.parlayLoading = true;
      this.parlayError = '';
      this.parlayResult = null;

      let legs;
      try {
        legs = JSON.parse(this.parlayLegs);
      } catch (err) {
        this.parlayError = `Leg JSON parse failed: ${err.message}`;
        this.parlayLoading = false;
        return;
      }

      try {
        const data = await apiPost('/api/parlay_ev', { legs });
        if (!data || data.success !== true) {
          this.parlayError = data?.error || 'Parlay EV failed.';
          return;
        }
        this.parlayResult = data;
      } catch (err) {
        this.parlayError = `Failed: ${err.message}`;
      } finally {
        this.parlayLoading = false;
      }
    },

    // Live stat table HTML builder
    liveStatRows() {
      if (!this.liveResult) return '';
      const stats = this.liveResult.liveStats || {};
      return [['PTS', stats.PTS], ['REB', stats.REB], ['AST', stats.AST],
        ['STL', stats.STL], ['BLK', stats.BLK], ['TOV', stats.TOV], ['FG3M', stats.FG3M]]
        .map(([k, v]) => `<tr><td>${k}</td><td>${v ?? 'n/a'}</td></tr>`).join('');
    },

    paceWidth() {
      const p = Number(this.liveResult?.gamePacePct);
      return Number.isFinite(p) ? Math.min(p, 100).toFixed(1) : '0';
    },

    periodLabel() {
      if (!this.liveResult) return '';
      return this.liveResult.gameStatus === 3 ? 'Final' : `Q${this.liveResult.period ?? '?'}`;
    },

    fmt, pct, escapeHtml,
  };
}

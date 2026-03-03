// Reference tab — Sportsbook Odds + Starter EV Accuracy
import { apiGet, escapeHtml, fmt, pct, pctAlready, statusPill, DEFAULT_BOOKMAKERS, PROP_MARKETS_PRESET } from './api.js';

export default function () {
  return {
    // Odds
    oddsRegions: 'us',
    oddsMarkets: 'h2h,spreads,totals',
    oddsBookmakers: DEFAULT_BOOKMAKERS,
    oddsSport: 'basketball_nba',
    oddsMaxEvents: 8,
    oddsLoading: false,
    oddsResult: null,
    oddsError: '',
    oddsMarketWarning: '',

    // Starter Accuracy
    starterDate: '',
    starterBookmakers: DEFAULT_BOOKMAKERS,
    starterRegions: 'us',
    starterSport: 'basketball_nba',
    starterModel: 'full',
    starterLoading: false,
    starterResult: null,
    starterError: '',

    applyPropsPreset() {
      this.oddsMarkets = PROP_MARKETS_PRESET;
      this.checkMarketWarning();
    },

    checkMarketWarning() {
      const hasProps = this.oddsMarkets.split(',').some(m => m.trim().toLowerCase().startsWith('player_'));
      this.oddsMarketWarning = hasProps
        ? 'Player prop markets are not supported on this odds endpoint. Use Auto Sweep for props.'
        : '';
    },

    async loadOdds(live = false) {
      this.oddsLoading = true;
      this.oddsError = '';
      this.oddsResult = null;
      this.checkMarketWarning();

      const params = new URLSearchParams({
        regions: this.oddsRegions || 'us',
        markets: this.oddsMarkets || 'h2h,spreads,totals',
        bookmakers: this.oddsBookmakers || DEFAULT_BOOKMAKERS,
        sport: this.oddsSport || 'basketball_nba',
      });
      if (live) params.set('maxEvents', String(this.oddsMaxEvents || 8));

      try {
        const data = await apiGet(`/api/${live ? 'odds_live' : 'odds'}?${params}`);
        if (!data || data.success !== true) {
          this.oddsError = data?.error || 'Odds request failed.';
          return;
        }
        this.oddsResult = data;
      } catch (err) {
        this.oddsError = `Failed: ${err.message}`;
      } finally {
        this.oddsLoading = false;
      }
    },

    async runStarterAccuracy() {
      this.starterLoading = true;
      this.starterError = '';
      this.starterResult = null;
      try {
        const params = new URLSearchParams({
          bookmakers: this.starterBookmakers || DEFAULT_BOOKMAKERS,
          regions: this.starterRegions || 'us',
          sport: this.starterSport || 'basketball_nba',
          modelVariant: this.starterModel || 'full',
        });
        if (this.starterDate) params.set('date', this.starterDate);

        const data = await apiGet(`/api/starter_accuracy?${params}`);
        if (!data || data.success !== true) {
          this.starterError = data?.error || 'Starter accuracy failed.';
          return;
        }
        this.starterResult = data;
      } catch (err) {
        this.starterError = `Failed: ${err.message}`;
      } finally {
        this.starterLoading = false;
      }
    },

    renderOddsTableHtml() {
      if (!this.oddsResult) return '';
      const disc = Array.isArray(this.oddsResult.discrepancies) ? this.oddsResult.discrepancies.slice(0, 30) : [];
      if (!disc.length) return '<p>No discrepancies in current payload.</p>';
      const rows = disc.map(d =>
        `<tr><td>${escapeHtml(`${d.awayTeam || '?'} @ ${d.homeTeam || '?'}`)}</td><td>${escapeHtml(d.market || 'n/a')}</td><td>${escapeHtml(`${d.outcome || 'n/a'}${d.point != null ? ` (${d.point})` : ''}`)}</td><td>${d.bestPrice ?? 'n/a'} (${escapeHtml(d.bestBookmaker || 'n/a')})</td><td>${d.worstPrice ?? 'n/a'} (${escapeHtml(d.worstBookmaker || 'n/a')})</td><td>${fmt(d.valueGapPct, 2)}%</td></tr>`
      ).join('');
      return `<table class="odds-table"><thead><tr><th>Event</th><th>Market</th><th>Outcome</th><th>Best</th><th>Worst</th><th>Gap</th></tr></thead><tbody>${rows}</tbody></table>`;
    },

    renderStarterTableHtml() {
      if (!this.starterResult) return '';
      const byStat = this.starterResult.byStat || {};
      const byStatRows = Object.entries(byStat).map(([stat, s]) =>
        `<tr><td>${stat.toUpperCase()}</td><td>${s.leans ?? 0}</td><td>${s.wins ?? 0}</td><td>${s.losses ?? 0}</td><td>${s.pushes ?? 0}</td><td>${pctAlready(s.hitRateNoPushPct)}</td><td>${fmt(s.pnlUnits, 2)}</td><td>${pctAlready(s.roiPctPerBet)}</td></tr>`
      ).join('');

      const topRows = (this.starterResult.sampleTopByEv || []).slice(0, 12).map(r =>
        `<tr><td>${escapeHtml(r.playerName || r.playerId || '?')}</td><td>${escapeHtml(r.teamAbbr || '')}</td><td>${String(r.stat || '').toUpperCase()}</td><td>${String(r.side || '').toUpperCase()}</td><td>${fmt(r.line, 1)}</td><td>${fmt(r.projection, 1)}</td><td>${fmt(r.actual, 1)}</td><td>${fmt(r.evPct, 2)}%</td><td>${r.odds ?? 'n/a'}</td><td>${statusPill(r.outcome)}</td><td>${fmt(r.pnl1u, 2)}</td></tr>`
      ).join('');

      return `
        <h4>By Stat</h4>
        <table class="odds-table"><thead><tr><th>Stat</th><th>Leans</th><th>Wins</th><th>Losses</th><th>Pushes</th><th>Hit Rate</th><th>PnL</th><th>ROI</th></tr></thead><tbody>${byStatRows || '<tr><td colspan="8">No stat rows.</td></tr>'}</tbody></table>
        <h4 style="margin-top:14px">Top EV Sample</h4>
        <table class="odds-table"><thead><tr><th>Player</th><th>Team</th><th>Stat</th><th>Side</th><th>Line</th><th>Proj</th><th>Actual</th><th>EV%</th><th>Odds</th><th>Outcome</th><th>PnL</th></tr></thead><tbody>${topRows || '<tr><td colspan="11">No EV leans found.</td></tr>'}</tbody></table>
      `;
    },

    fmt, pct, pctAlready, escapeHtml, statusPill,
  };
}

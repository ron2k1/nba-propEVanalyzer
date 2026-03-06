// Picks tab — best_today table + top_picks + best parlay + LLM rundown
import { apiGet, escapeHtml, fmt, pct, statusPill, toast, evColorClass, exportCsv } from './api.js';

export default function () {
  return {
    // Best Today
    bestLoading: false,
    bestResult: null,
    bestError: '',
    bestLimit: 15,

    // F5: Sort state
    bestSortCol: 'recommendedEvPct',
    bestSortDir: 'desc',

    // F6: Filter state
    bestFilter: '',

    // F8: Expand state
    expandedRowKey: null,

    // Top Picks
    topLoading: false,
    topResult: null,
    topError: '',
    topLimit: 5,

    // LLM Rundown
    rundownLoading: false,
    rundownResult: null,
    rundownError: '',

    async init() {
      this.loadBestToday();
    },

    async loadBestToday() {
      this.bestLoading = true;
      this.bestError = '';
      try {
        const data = await apiGet(`/api/best_today?limit=${this.bestLimit}`);
        if (!data || data.success !== true) {
          this.bestError = data?.error || 'Failed to load best EV picks.';
          return;
        }
        this.bestResult = data;
      } catch (err) {
        this.bestError = `Failed: ${err.message}`;
      } finally {
        this.bestLoading = false;
      }
    },

    async loadTopPicks() {
      this.topLoading = true;
      this.topError = '';
      try {
        const data = await apiGet(`/api/top_picks?limit=${this.topLimit}`);
        if (!data || data.success !== true) {
          this.topError = data?.error || 'Failed to load top picks.';
          return;
        }
        this.topResult = data;
      } catch (err) {
        this.topError = `Failed: ${err.message}`;
      } finally {
        this.topLoading = false;
      }
    },

    async loadRundown() {
      this.rundownLoading = true;
      this.rundownError = '';
      this.rundownResult = null;
      try {
        const data = await apiGet('/api/lean_rundown?limit=10', { timeoutMs: 120_000 });
        if (!data || data.success !== true) {
          this.rundownError = data?.error || 'LLM rundown failed.';
          return;
        }
        this.rundownResult = data;
      } catch (err) {
        this.rundownError = `Rundown failed: ${err.message}`;
      } finally {
        this.rundownLoading = false;
      }
    },

    bestRows() {
      if (!this.bestResult) return [];
      const rows = this.bestResult.topOffers || this.bestResult.top || [];
      return Array.isArray(rows) ? rows : [];
    },

    qualifiedRows() {
      return this.bestRows().filter(r => r.policyQualified);
    },

    leanRows() {
      if (!this.bestResult) return [];
      // Prefer dedicated modelLeans array from API; fall back to filtering topOffers
      const leans = this.bestResult.modelLeans;
      if (Array.isArray(leans) && leans.length > 0) {
        return leans.filter(r => !r.policyPass);
      }
      return this.bestRows().filter(r => !r.policyQualified && (r.recommendedEvPct || 0) > 0);
    },

    topRows() {
      if (!this.topResult) return [];
      const picks = this.topResult.picks || this.topResult.top || [];
      return Array.isArray(picks) ? picks : [];
    },

    reasonTag(reason) {
      if (!reason) return '';
      return `<span class="lean-reason">${escapeHtml(reason)}</span>`;
    },

    // F5: Sort by column
    sortBy(col) {
      if (this.bestSortCol === col) {
        this.bestSortDir = this.bestSortDir === 'asc' ? 'desc' : 'asc';
      } else {
        this.bestSortCol = col;
        this.bestSortDir = 'desc';
      }
    },

    sortArrow(col) {
      if (this.bestSortCol !== col) return '';
      return this.bestSortDir === 'asc' ? ' \u25B2' : ' \u25BC';
    },

    // F5+F6: Sorted and filtered best rows
    sortedBestRows() {
      let rows = this.bestRows();
      const f = this.bestFilter.toLowerCase();
      if (f) {
        rows = rows.filter(r =>
          (r.playerName || '').toLowerCase().includes(f) ||
          (r.stat || '').toLowerCase().includes(f)
        );
      }
      const col = this.bestSortCol;
      const dir = this.bestSortDir === 'asc' ? 1 : -1;
      return [...rows].sort((a, b) => {
        const av = a[col], bv = b[col];
        if (av == null && bv == null) return 0;
        if (av == null) return 1;
        if (bv == null) return -1;
        if (typeof av === 'string') return av.localeCompare(bv) * dir;
        return (av - bv) * dir;
      });
    },

    // F8: Row expand
    rowKey(r) {
      return (r.playerId || '') + ':' + (r.stat || '') + ':' + (r.line || '');
    },
    toggleRow(r) {
      const k = this.rowKey(r);
      this.expandedRowKey = this.expandedRowKey === k ? null : k;
    },
    isExpanded(r) {
      return this.expandedRowKey === this.rowKey(r);
    },

    // F10: Copy picks to clipboard
    copyPicks() {
      const rows = this.sortedBestRows();
      if (!rows.length) { toast('No picks to copy', 'bad'); return; }
      const text = rows.map(r =>
        `${r.playerName || '?'} | ${(r.stat || '').toUpperCase()} ${(r.recommendedSide || '').toUpperCase()} ${r.line} | EV ${(r.recommendedEvPct || 0).toFixed(1)}% | ${r.recommendedOdds || 'n/a'}`
      ).join('\n');
      navigator.clipboard.writeText(text).then(() => toast('Picks copied to clipboard', 'ok'));
    },

    // F11: Export picks CSV
    exportPicksCsv() {
      exportCsv(this.sortedBestRows(), [
        { label: 'Player', key: r => r.playerName || r.playerId || '' },
        { label: 'Stat', key: r => (r.stat || '').toUpperCase() },
        { label: 'Side', key: r => (r.recommendedSide || '').toUpperCase() },
        { label: 'Line', key: 'line' },
        { label: 'Projection', key: 'projection' },
        { label: 'EV%', key: 'recommendedEvPct' },
        { label: 'Odds', key: r => r.recommendedOdds || '' },
      ], 'picks_' + (this.bestResult?.date || 'today') + '.csv');
      toast('CSV exported', 'ok');
    },

    // F14: Click player -> jump to Analyze with all fields prefilled
    analyzePick(r) {
      window.dispatchEvent(new CustomEvent('prefill-pick', {
        detail: {
          playerName: r.playerName || '',
          playerId: r.playerId || '',
          stat: r.stat || 'pts',
          line: r.line,
          overOdds: r.overOdds ?? -110,
          underOdds: r.underOdds ?? -110,
          playerTeamAbbr: r.playerTeamAbbr || r.teamAbbr || '',
          opponentAbbr: r.opponentAbbr || '',
          isHome: r.isHome ?? true,
        }
      }));
      Alpine.store('tab').set('analyze');
    },

    // Helpers exposed to template
    fmt, pct, statusPill, escapeHtml, evColorClass,
  };
}

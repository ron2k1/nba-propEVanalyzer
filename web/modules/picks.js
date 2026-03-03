// Picks tab — best_today table + top_picks + best parlay
import { apiGet, escapeHtml, fmt, pct, statusPill } from './api.js';

export default function () {
  return {
    // Best Today
    bestLoading: false,
    bestResult: null,
    bestError: '',
    bestLimit: 15,

    // Top Picks
    topLoading: false,
    topResult: null,
    topError: '',
    topLimit: 5,

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

    bestRows() {
      if (!this.bestResult) return [];
      const rows = this.bestResult.topOffers || this.bestResult.top || [];
      return Array.isArray(rows) ? rows : [];
    },

    qualifiedRows() {
      return this.bestRows().filter(r => r.policyQualified);
    },

    leanRows() {
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

    // Helpers exposed to template
    fmt, pct, statusPill, escapeHtml,
  };
}

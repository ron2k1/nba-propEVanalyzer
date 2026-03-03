// Lines tab — collect_lines, roster_sweep, daily_ops pipeline
import { apiGet, escapeHtml, fmt } from './api.js';

export default function () {
  return {
    // Shared lock — prevents overlapping long-running requests
    busy: false,

    // Collect Lines
    collectResult: null,
    collectError: '',
    collectBooks: 'betmgm,draftkings,fanduel',
    collectStats: 'pts,reb,ast,pra',

    // Roster Sweep
    sweepResult: null,
    sweepError: '',

    // Daily Ops
    opsSteps: [],
    opsError: '',

    async collectLines() {
      if (this.busy) return;
      this.busy = true;
      this.collectError = '';
      this.collectResult = null;
      try {
        const params = new URLSearchParams({
          books: this.collectBooks,
          stats: this.collectStats,
        });
        const data = await apiGet(`/api/collect_lines?${params}`);
        if (!data || data.success !== true) {
          this.collectError = data?.error || 'Collect lines failed.';
          return;
        }
        this.collectResult = data;
      } catch (err) {
        this.collectError = `Collect lines failed: ${err.message}`;
      } finally {
        this.busy = false;
      }
    },

    async rosterSweep() {
      if (this.busy) return;
      this.busy = true;
      this.sweepError = '';
      this.sweepResult = null;
      try {
        const data = await apiGet('/api/roster_sweep', { timeoutMs: 600_000 });
        if (!data || data.success !== true) {
          this.sweepError = data?.error || 'Roster sweep failed.';
          return;
        }
        this.sweepResult = data;
      } catch (err) {
        this.sweepError = `Roster sweep failed: ${err.message}`;
      } finally {
        this.busy = false;
      }
    },

    async runDailyOps() {
      if (this.busy) return;
      this.busy = true;
      this.opsError = '';
      this.opsSteps = [
        { name: 'Collect Lines', status: 'running', result: null },
        { name: 'Roster Sweep', status: 'pending', result: null },
        { name: 'Build Closes', status: 'pending', result: null },
      ];
      try {
        // Step 1: Collect Lines
        const cl = await apiGet(`/api/collect_lines?books=${this.collectBooks}&stats=${this.collectStats}`);
        this.opsSteps[0].status = cl?.success ? 'done' : 'error';
        this.opsSteps[0].result = cl;

        // Step 2: Roster Sweep
        this.opsSteps[1].status = 'running';
        const rs = await apiGet('/api/roster_sweep', { timeoutMs: 600_000 });
        this.opsSteps[1].status = rs?.success ? 'done' : 'error';
        this.opsSteps[1].result = rs;

        // Step 3: Build Closes
        this.opsSteps[2].status = 'running';
        const data = await apiGet('/api/daily_ops?dryRun=false', { timeoutMs: 600_000 });
        this.opsSteps[2].status = data?.success ? 'done' : 'error';
        this.opsSteps[2].result = data;
      } catch (err) {
        this.opsError = `Pipeline failed: ${err.message}`;
      } finally {
        this.busy = false;
      }
    },
  };
}

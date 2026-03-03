// Lines tab — collect_lines, roster_sweep, daily_ops pipeline
import { apiGet, escapeHtml, fmt } from './api.js';

export default function () {
  return {
    // Collect Lines
    collectLoading: false,
    collectResult: null,
    collectError: '',
    collectBooks: 'betmgm,draftkings,fanduel',
    collectStats: 'pts,reb,ast,pra',

    // Roster Sweep
    sweepLoading: false,
    sweepResult: null,
    sweepError: '',

    // Daily Ops
    opsLoading: false,
    opsSteps: [],
    opsError: '',

    async collectLines() {
      this.collectLoading = true;
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
        this.collectLoading = false;
      }
    },

    async rosterSweep() {
      this.sweepLoading = true;
      this.sweepError = '';
      this.sweepResult = null;
      try {
        const data = await apiGet('/api/roster_sweep');
        if (!data || data.success !== true) {
          this.sweepError = data?.error || 'Roster sweep failed.';
          return;
        }
        this.sweepResult = data;
      } catch (err) {
        this.sweepError = `Roster sweep failed: ${err.message}`;
      } finally {
        this.sweepLoading = false;
      }
    },

    async runDailyOps() {
      this.opsLoading = true;
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
        const rs = await apiGet('/api/roster_sweep');
        this.opsSteps[1].status = rs?.success ? 'done' : 'error';
        this.opsSteps[1].result = rs;

        // Step 3: Build Closes
        this.opsSteps[2].status = 'running';
        const data = await apiGet('/api/daily_ops?dryRun=false');
        this.opsSteps[2].status = data?.success ? 'done' : 'error';
        this.opsSteps[2].result = data;
      } catch (err) {
        this.opsError = `Pipeline failed: ${err.message}`;
      } finally {
        this.opsLoading = false;
      }
    },

    stepIcon(status) {
      if (status === 'done') return 'done';
      if (status === 'error') return 'error';
      if (status === 'running') return 'running';
      return 'pending';
    },
  };
}

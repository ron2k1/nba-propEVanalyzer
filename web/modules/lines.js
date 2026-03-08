// Lines tab - collect_lines, roster_sweep, daily_ops pipeline
import { apiGet } from './api.js';

const LONG_TIMEOUT_MS = 1_800_000;
const STATUS_POLL_MS = 2_000;

function defaultOpsSteps() {
  return [
    { name: 'Collect Lines', status: 'pending', result: null },
    { name: 'Roster Sweep', status: 'pending', result: null },
    { name: 'Best Today', status: 'pending', result: null },
  ];
}

function opsStepsFromResult(stepMap = {}) {
  return [
    { name: 'Collect Lines', status: stepMap.collect_lines?.success ? 'done' : stepMap.collect_lines ? 'error' : 'pending', result: stepMap.collect_lines || null },
    {
      name: 'Roster Sweep',
      status: stepMap.roster_sweep?.success || stepMap.roster_sweep?.skipped ? 'done' : stepMap.roster_sweep ? 'error' : 'pending',
      result: stepMap.roster_sweep || null,
    },
    { name: 'Best Today', status: stepMap.best_today?.success || stepMap.best_today?.skipped ? 'done' : stepMap.best_today ? 'error' : 'pending', result: stepMap.best_today || null },
  ];
}

export default function () {
  return {
    busy: false,
    requestInFlight: false,
    serverBusy: false,
    statusTimer: null,
    pipelineStatus: null,

    collectResult: null,
    collectError: '',
    collectBooks: 'betmgm,draftkings,fanduel',
    collectStats: 'pts,reb,ast,pra',

    sweepResult: null,
    sweepError: '',

    opsSteps: [],
    opsError: '',

    syncBusy() {
      this.busy = !!(this.requestInFlight || this.serverBusy);
    },

    applyPipelineStatus(status) {
      if (!status || status.success !== true) return;
      this.pipelineStatus = status;
      this.serverBusy = !!status.busy;
      if (Array.isArray(status.steps) && status.steps.length) {
        this.opsSteps = status.steps;
      }
      this.syncBusy();
    },

    async refreshPipelineStatus() {
      try {
        const status = await apiGet('/api/pipeline_status', { timeoutMs: 10_000 });
        this.applyPipelineStatus(status);
      } catch (_) {
        // Ignore transient polling failures.
      } finally {
        this.syncBusy();
      }
    },

    startPolling() {
      if (this.statusTimer) return;
      this.statusTimer = setInterval(() => {
        void this.refreshPipelineStatus();
      }, STATUS_POLL_MS);
    },

    stopPollingIfIdle() {
      if (this.requestInFlight || this.serverBusy) return;
      if (this.statusTimer) {
        clearInterval(this.statusTimer);
        this.statusTimer = null;
      }
    },

    async init() {
      await this.refreshPipelineStatus();
      if (this.serverBusy) {
        this.startPolling();
      }
    },

    async collectLines() {
      if (this.busy) return;
      this.requestInFlight = true;
      this.syncBusy();
      this.collectError = '';
      this.collectResult = null;
      this.startPolling();
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
        this.requestInFlight = false;
        await this.refreshPipelineStatus();
        this.stopPollingIfIdle();
      }
    },

    async rosterSweep() {
      if (this.busy) return;
      this.requestInFlight = true;
      this.syncBusy();
      this.sweepError = '';
      this.sweepResult = null;
      this.startPolling();
      try {
        const data = await apiGet('/api/roster_sweep', { timeoutMs: LONG_TIMEOUT_MS });
        if (!data || data.success !== true) {
          this.sweepError = data?.error || 'Roster sweep failed.';
          return;
        }
        this.sweepResult = data;
      } catch (err) {
        this.sweepError = `Roster sweep failed: ${err.message}`;
      } finally {
        this.requestInFlight = false;
        await this.refreshPipelineStatus();
        this.stopPollingIfIdle();
      }
    },

    async runDailyOps() {
      if (this.busy) return;
      this.requestInFlight = true;
      this.syncBusy();
      this.opsError = '';
      this.opsSteps = defaultOpsSteps();
      this.opsSteps[0].status = 'running';
      this.startPolling();
      try {
        const data = await apiGet('/api/daily_ops?dryRun=false', { timeoutMs: LONG_TIMEOUT_MS });
        if (!data || data.success !== true) {
          this.opsError = data?.error || 'Pipeline failed.';
          return;
        }
        if (data?.steps) {
          this.opsSteps = opsStepsFromResult(data.steps);
        }
      } catch (err) {
        this.opsError = `Pipeline failed: ${err.message}`;
      } finally {
        this.requestInFlight = false;
        await this.refreshPipelineStatus();
        this.stopPollingIfIdle();
      }
    },

    async cancelPipeline() {
      try {
        await apiGet('/api/pipeline_cancel', { timeoutMs: 15_000 });
      } catch (_) {
        // Best effort cancellation.
      } finally {
        await this.refreshPipelineStatus();
        this.stopPollingIfIdle();
      }
    },
  };
}

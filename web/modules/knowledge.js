// Knowledge tab — LightRAG health, ingest, and query
import { apiGet, escapeHtml } from './api.js';

export default function () {
  return {
    // Health
    healthy: null,
    healthError: '',
    healthLoading: false,
    healthUrl: '',

    // Ingest
    ingestSource: 'all',
    ingestForce: false,
    ingestLoading: false,
    ingestOutput: '',
    ingestError: '',

    // Query
    queryText: '',
    queryLoading: false,
    queryResponse: '',
    queryError: '',

    init() {
      this.$watch('$store.tab.current', (tab) => {
        if (tab === 'knowledge') this.checkHealth();
      });
    },

    async checkHealth() {
      this.healthLoading = true;
      this.healthError = '';
      this.healthy = null;
      try {
        const data = await apiGet('/api/lightrag_health');
        if (data && data.success === true) {
          this.healthy = true;
          this.healthUrl = data.url || '';
        } else {
          this.healthy = false;
          this.healthError = data?.error || 'LightRAG not reachable.';
        }
      } catch (err) {
        this.healthy = false;
        this.healthError = `Health check failed: ${err.message}`;
      } finally {
        this.healthLoading = false;
      }
    },

    async runIngest() {
      this.ingestLoading = true;
      this.ingestOutput = '';
      this.ingestError = '';
      try {
        const params = new URLSearchParams({
          source: this.ingestSource || 'all',
        });
        if (this.ingestForce) params.set('force', 'true');

        const res = await fetch(`/api/lightrag_ingest?${params}`, { cache: 'no-store' });
        if (res.status === 409) {
          this.ingestError = 'Ingest already running — please wait.';
          return;
        }
        const data = await res.json();
        this.ingestOutput = data?.output || '';
        if (data?.success === false) {
          this.ingestError = data?.error || (data?.output ? '' : 'Ingest failed.');
        }
      } catch (err) {
        this.ingestError = `Ingest request failed: ${err.message}`;
      } finally {
        this.ingestLoading = false;
      }
    },

    async runQuery() {
      if (!this.queryText.trim()) return;
      this.queryLoading = true;
      this.queryResponse = '';
      this.queryError = '';
      try {
        const data = await apiGet('/api/lightrag_query?q=' + encodeURIComponent(this.queryText.trim()));
        if (data && data.success === true) {
          this.queryResponse = data.response || '(empty response)';
        } else {
          this.queryError = data?.error || 'Query failed.';
        }
      } catch (err) {
        this.queryError = `Query failed: ${err.message}`;
      } finally {
        this.queryLoading = false;
      }
    },

    escapeHtml,
  };
}

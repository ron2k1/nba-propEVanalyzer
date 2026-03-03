// NBA Prop Engine — Alpine.js orchestrator
// Each tab is a separate ES module in web/modules/
// Alpine auto-start is disabled in vendor build; we call Alpine.start() here.

import playersStore from './modules/players.js';
import dashboardComponent from './modules/dashboard.js';
import linesComponent from './modules/lines.js';
import picksComponent from './modules/picks.js';
import analyzeComponent from './modules/analyze.js';
import liveComponent from './modules/live.js';
import resultsComponent from './modules/results.js';
import referenceComponent from './modules/reference.js';
import knowledgeComponent from './modules/knowledge.js';

// Wait for Alpine global to exist (set by the defer CDN/vendor script)
function boot() {
  if (typeof Alpine === 'undefined') {
    // Alpine hasn't loaded yet — wait and retry
    setTimeout(boot, 10);
    return;
  }

  // Register shared stores
  Alpine.store('players', playersStore);
  Alpine.store('tab', {
    current: 'dashboard',
    set(name) { this.current = name; },
  });

  // Register tab components
  Alpine.data('dashboard', dashboardComponent);
  Alpine.data('lines', linesComponent);
  Alpine.data('picks', picksComponent);
  Alpine.data('analyze', analyzeComponent);
  Alpine.data('live', liveComponent);
  Alpine.data('results', resultsComponent);
  Alpine.data('reference', referenceComponent);
  Alpine.data('knowledge', knowledgeComponent);

  // Start Alpine (auto-start was removed from vendor build)
  Alpine.start();

  // Load player index after Alpine is running
  Alpine.store('players').load();
}

// Modules execute after parsing; Alpine defer script may or may not be ready.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}

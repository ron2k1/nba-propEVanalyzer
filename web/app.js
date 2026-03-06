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
  Alpine.store('toasts', { items: [] });

  // F2: Keyboard shortcuts — 1-7 switch tabs, Escape blurs input
  const tabIds = ['dashboard', 'lines', 'picks', 'analyze', 'live', 'results', 'reference'];
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    const idx = parseInt(e.key) - 1;
    if (idx >= 0 && idx < tabIds.length) {
      Alpine.store('tab').set(tabIds[idx]);
    }
  });

  // F13: x-counter directive — animate number count-up (600ms ease-out cubic)
  Alpine.directive('counter', (el, { expression }, { evaluate }) => {
    const target = Number(evaluate(expression));
    if (!Number.isFinite(target)) { el.textContent = expression; return; }
    const duration = 600;
    const start = performance.now();
    const isFloat = !Number.isInteger(target);
    const step = (now) => {
      const t = Math.min((now - start) / duration, 1);
      const ease = 1 - Math.pow(1 - t, 3);
      el.textContent = isFloat ? (target * ease).toFixed(1) : Math.round(target * ease);
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });

  // Register tab components
  Alpine.data('dashboard', dashboardComponent);
  Alpine.data('lines', linesComponent);
  Alpine.data('picks', picksComponent);
  Alpine.data('analyze', analyzeComponent);
  Alpine.data('live', liveComponent);
  Alpine.data('results', resultsComponent);
  Alpine.data('reference', referenceComponent);

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

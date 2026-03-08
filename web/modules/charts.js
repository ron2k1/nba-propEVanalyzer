// Charts module — Chart.js renderers for distribution, line movement, edge scatter, calibration
import { apiGet, fmt } from './api.js';

const DARK_THEME = {
  color: '#a1a1aa',
  borderColor: 'rgba(255,255,255,0.06)',
  font: { family: "Bahnschrift, 'Trebuchet MS', system-ui, sans-serif" },
};

const BRAND = '#22d3ee';
const BRAND_ALPHA = 'rgba(34,211,238,0.25)';
const ACCENT = '#f59e0b';
const ACCENT_ALPHA = 'rgba(245,158,11,0.25)';
const OK = '#34d399';
const BAD = '#f87171';
const MUTED = '#71717a';

function baseOptions(title) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 400 },
    plugins: {
      legend: { labels: { color: DARK_THEME.color, font: DARK_THEME.font } },
      title: {
        display: !!title,
        text: title || '',
        color: '#e4e4e7',
        font: { ...DARK_THEME.font, size: 13, weight: 600 },
      },
      tooltip: {
        backgroundColor: '#161618',
        titleColor: '#fafafa',
        bodyColor: '#a1a1aa',
        borderColor: 'rgba(255,255,255,0.10)',
        borderWidth: 1,
      },
    },
    scales: {
      x: { ticks: { color: DARK_THEME.color }, grid: { color: DARK_THEME.borderColor } },
      y: { ticks: { color: DARK_THEME.color }, grid: { color: DARK_THEME.borderColor } },
    },
  };
}

function destroyChart(canvasId) {
  const existing = Chart.getChart(canvasId);
  if (existing) existing.destroy();
}

// ── Chart 1: Probability Distribution ──
// Normal bell curve or Poisson PMF with book line overlay.
export function renderDistribution(canvasId, ev) {
  if (!ev) return;
  destroyChart(canvasId);
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  const mu = Number(ev.projection);
  const sigma = Number(ev.stdev) || 1;
  const line = Number(ev.line);
  const mode = ev.distributionMode || 'normal';

  let labels, rawData, calData;

  if (mode === 'poisson') {
    const maxK = Math.max(Math.ceil(mu * 2.5), 10);
    labels = Array.from({ length: maxK + 1 }, (_, i) => i);
    rawData = labels.map(k => {
      let logP = k * Math.log(mu) - mu;
      for (let i = 1; i <= k; i++) logP -= Math.log(i);
      return Math.exp(logP);
    });
    calData = null; // No calibrated overlay for Poisson
  } else {
    const lo = Math.floor(mu - 4 * sigma);
    const hi = Math.ceil(mu + 4 * sigma);
    const step = Math.max(0.5, (hi - lo) / 80);
    labels = [];
    rawData = [];
    for (let x = lo; x <= hi; x += step) {
      labels.push(x.toFixed(1));
      const z = (x - mu) / sigma;
      rawData.push(Math.exp(-0.5 * z * z) / (sigma * Math.sqrt(2 * Math.PI)));
    }

    // Calibrated overlay if raw vs calibrated differ
    const pOverRaw = ev.probOverRaw;
    const pOver = ev.probOver;
    if (pOverRaw != null && pOver != null && Math.abs(pOverRaw - pOver) > 0.005) {
      // Approximate calibrated sigma from the calibrated P(over)
      // inv_cdf approximation not needed — just show the raw/cal bars in Chart 4
      calData = null;
    } else {
      calData = null;
    }
  }

  const datasets = [{
    label: mode === 'poisson' ? 'Poisson PMF' : 'Normal PDF',
    data: rawData,
    borderColor: BRAND,
    backgroundColor: BRAND_ALPHA,
    fill: true,
    pointRadius: 0,
    tension: 0.4,
    borderWidth: 2,
  }];

  const opts = baseOptions('Probability Distribution');
  // Vertical annotation line at book line
  opts.plugins.annotation = {
    annotations: {
      bookLine: {
        type: 'line',
        xMin: labels.findIndex(l => Number(l) >= line),
        xMax: labels.findIndex(l => Number(l) >= line),
        borderColor: ACCENT,
        borderWidth: 2,
        borderDash: [6, 3],
        label: {
          display: true,
          content: `Line: ${line}`,
          color: ACCENT,
          font: { size: 11 },
          position: 'start',
        },
      },
    },
  };
  // Simpler approach: use plugin for vertical line
  delete opts.plugins.annotation;

  new Chart(canvas, {
    type: mode === 'poisson' ? 'bar' : 'line',
    data: { labels, datasets },
    options: {
      ...opts,
      plugins: {
        ...opts.plugins,
        legend: { display: false },
      },
      scales: {
        ...opts.scales,
        x: {
          ...opts.scales.x,
          title: { display: true, text: mode === 'poisson' ? 'Count' : 'Value', color: DARK_THEME.color },
        },
        y: {
          ...opts.scales.y,
          title: { display: true, text: 'Probability', color: DARK_THEME.color },
        },
      },
    },
    plugins: [{
      id: 'bookLine',
      afterDraw(chart) {
        const xAxis = chart.scales.x;
        let xPixel;
        if (mode === 'poisson') {
          xPixel = xAxis.getPixelForValue(line);
        } else {
          // Find closest label index
          let idx = labels.findIndex(l => Number(l) >= line);
          if (idx < 0) idx = labels.length - 1;
          xPixel = xAxis.getPixelForValue(idx);
        }
        const ctx = chart.ctx;
        ctx.save();
        ctx.beginPath();
        ctx.setLineDash([6, 3]);
        ctx.strokeStyle = ACCENT;
        ctx.lineWidth = 2;
        ctx.moveTo(xPixel, chart.chartArea.top);
        ctx.lineTo(xPixel, chart.chartArea.bottom);
        ctx.stroke();
        ctx.font = '11px ' + DARK_THEME.font.family;
        ctx.fillStyle = ACCENT;
        ctx.fillText(`Line: ${line}`, xPixel + 4, chart.chartArea.top + 14);
        ctx.restore();
      },
    }],
  });
}

// ── Chart 2: Line Movement Timeline ──
export async function renderLineMovement(canvasId, player, stat, date) {
  destroyChart(canvasId);
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  try {
    const params = new URLSearchParams({ player, stat });
    if (date) params.set('date', date);
    const data = await apiGet(`/api/line_movement?${params}`);
    if (!data?.success || !data.books) return;

    const colors = [BRAND, ACCENT, OK, BAD, MUTED, '#a78bfa', '#fb923c'];
    const datasets = [];
    let colorIdx = 0;

    for (const [book, snaps] of Object.entries(data.books)) {
      const sorted = snaps.sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
      datasets.push({
        label: book,
        data: sorted.map(s => ({
          x: s.timestamp ? new Date(s.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '?',
          y: s.line,
        })),
        borderColor: colors[colorIdx % colors.length],
        backgroundColor: 'transparent',
        pointRadius: 3,
        pointHoverRadius: 5,
        tension: 0.3,
        borderWidth: 2,
      });
      colorIdx++;
    }

    // Flatten all timestamps for x-axis labels
    const allLabels = [...new Set(
      datasets.flatMap(ds => ds.data.map(d => d.x))
    )].sort();

    // Restructure datasets to use common labels
    for (const ds of datasets) {
      const byTime = Object.fromEntries(ds.data.map(d => [d.x, d.y]));
      ds.data = allLabels.map(t => byTime[t] ?? null);
    }

    new Chart(canvas, {
      type: 'line',
      data: { labels: allLabels, datasets },
      options: {
        ...baseOptions('Line Movement'),
        spanGaps: true,
        scales: {
          x: {
            ticks: { color: DARK_THEME.color, maxTicksLimit: 12 },
            grid: { color: DARK_THEME.borderColor },
            title: { display: true, text: 'Time', color: DARK_THEME.color },
          },
          y: {
            ticks: { color: DARK_THEME.color },
            grid: { color: DARK_THEME.borderColor },
            title: { display: true, text: 'Line', color: DARK_THEME.color },
          },
        },
      },
    });
  } catch (e) {
    console.warn('Line movement chart failed:', e);
  }
}

// ── Chart 3: Edge/EV Scatter ──
export function renderEdgeScatter(canvasId, rows) {
  destroyChart(canvasId);
  const canvas = document.getElementById(canvasId);
  if (!canvas || !rows?.length) return;

  const STAT_COLORS = {
    pts: BRAND, reb: OK, ast: ACCENT, pra: '#a78bfa',
    stl: '#fb923c', blk: BAD, fg3m: '#38bdf8', tov: MUTED,
  };

  const points = rows
    .filter(r => r.recommendedEvPct != null && r.projection != null && r.line != null)
    .map(r => ({
      x: Number(r.projection) - Number(r.line),
      y: Number(r.recommendedEvPct),
      r: Math.max(4, Math.min(14, (Number(r.confidence ?? 0.6)) * 16)),
      stat: (r.stat || 'pts').toLowerCase(),
      label: `${r.playerName || '?'} ${(r.stat || '').toUpperCase()} ${r.line}`,
    }));

  // Group by stat for legend
  const byStat = {};
  for (const p of points) {
    if (!byStat[p.stat]) byStat[p.stat] = [];
    byStat[p.stat].push(p);
  }

  const datasets = Object.entries(byStat).map(([stat, pts]) => ({
    label: stat.toUpperCase(),
    data: pts,
    backgroundColor: (STAT_COLORS[stat] || MUTED) + '99',
    borderColor: STAT_COLORS[stat] || MUTED,
    borderWidth: 1,
  }));

  new Chart(canvas, {
    type: 'bubble',
    data: { datasets },
    options: {
      ...baseOptions('Displacement vs EV% (size = confidence)'),
      scales: {
        x: {
          ticks: { color: DARK_THEME.color },
          grid: { color: DARK_THEME.borderColor },
          title: { display: true, text: 'Projection \u2212 Line', color: DARK_THEME.color },
        },
        y: {
          ticks: { color: DARK_THEME.color },
          grid: { color: DARK_THEME.borderColor },
          title: { display: true, text: 'EV %', color: DARK_THEME.color },
        },
      },
      plugins: {
        ...baseOptions().plugins,
        tooltip: {
          ...baseOptions().plugins.tooltip,
          callbacks: {
            label: (ctx) => {
              const p = ctx.raw;
              return `${p.label}: Disp ${p.x.toFixed(1)}, EV ${p.y.toFixed(1)}%`;
            },
          },
        },
      },
    },
  });
}

// ── Chart 4: Calibration Before/After ──
export function renderCalibration(canvasId, ev) {
  destroyChart(canvasId);
  const canvas = document.getElementById(canvasId);
  if (!canvas || !ev) return;

  const rawOver = ev.probOverRaw;
  const calOver = ev.probOver;
  const rawUnder = ev.probUnderRaw;
  const calUnder = ev.probUnder;

  if (rawOver == null || calOver == null) return;

  new Chart(canvas, {
    type: 'bar',
    data: {
      labels: ['P(Over)', 'P(Under)'],
      datasets: [
        {
          label: 'Raw Model',
          data: [(rawOver * 100).toFixed(1), (rawUnder * 100).toFixed(1)],
          backgroundColor: MUTED + '99',
          borderColor: MUTED,
          borderWidth: 1,
        },
        {
          label: 'Calibrated',
          data: [(calOver * 100).toFixed(1), (calUnder * 100).toFixed(1)],
          backgroundColor: BRAND_ALPHA,
          borderColor: BRAND,
          borderWidth: 1,
        },
      ],
    },
    options: {
      ...baseOptions('Raw vs Calibrated Probability'),
      scales: {
        x: {
          ticks: { color: DARK_THEME.color },
          grid: { color: DARK_THEME.borderColor },
        },
        y: {
          ticks: { color: DARK_THEME.color, callback: v => v + '%' },
          grid: { color: DARK_THEME.borderColor },
          title: { display: true, text: 'Probability %', color: DARK_THEME.color },
          min: 0,
          max: 100,
        },
      },
    },
  });
}

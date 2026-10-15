/* Shared chart helpers for the control panel and the saved reports. */
(function () {
  const PHASE_TINT = {
    idle: "--phase-idle", ramp_up: "--phase-ramp", spike: "--phase-spike",
    peak: "--phase-peak", steady: "--phase-steady", cooldown: "--phase-cool",
  };
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function fmtTime(sec) {
    if (sec == null || isNaN(sec)) return "";
    sec = Math.max(0, Math.round(sec));
    const m = Math.floor(sec / 60), s = sec % 60;
    return m + ":" + String(s).padStart(2, "0");
  }
  const bands = {
    id: "phaseBands",
    beforeDatasetsDraw(chart, _a, opts) {
      const stages = (opts && opts.stages) || [];
      if (!stages.length) return;
      const { ctx, chartArea, scales } = chart;
      const x = scales.x;
      ctx.save();
      for (const s of stages) {
        const x0 = Math.max(chartArea.left, x.getPixelForValue(s.start));
        const x1 = Math.min(chartArea.right, x.getPixelForValue(s.end));
        if (x1 <= x0) continue;
        ctx.fillStyle = cssVar(PHASE_TINT[s.phase] || "--phase-idle");
        ctx.fillRect(x0, chartArea.top, x1 - x0, chartArea.bottom - chartArea.top);
        if (opts.labels && x1 - x0 > 46) {
          ctx.fillStyle = cssVar("--text-faint");
          ctx.font = "10px " + cssVar("--font-ui");
          ctx.fillText(String(s.label || s.phase).slice(0, Math.floor((x1 - x0) / 6)), x0 + 4, chartArea.top + 11);
        }
      }
      if (opts.cursor != null) {
        const xc = x.getPixelForValue(opts.cursor);
        if (xc >= chartArea.left && xc <= chartArea.right) {
          ctx.strokeStyle = cssVar("--text-faint");
          ctx.setLineDash([3, 3]);
          ctx.beginPath(); ctx.moveTo(xc, chartArea.top); ctx.lineTo(xc, chartArea.bottom); ctx.stroke();
        }
      }
      ctx.restore();
    },
  };

  function lineChart(canvas, cfg) {
    const grid = cssVar("--grid"), text = cssVar("--text-muted");
    const datasets = cfg.series.map((s) => ({
      label: s.label, data: s.data || [], borderColor: cssVar(s.color), backgroundColor: cssVar(s.color),
      borderWidth: s.width || 1.6, pointRadius: 0, tension: 0.25, yAxisID: s.axis || "y",
      borderDash: s.dash || [], spanGaps: true, fill: false,
    }));
    const scales = {
      x: {
        type: "linear", min: cfg.xmin, max: cfg.xmax, grid: { color: grid }, border: { display: false },
        ticks: { color: text, maxTicksLimit: 8, callback: (v) => fmtTime(v), font: { size: 10 } },
      },
      y: {
        beginAtZero: true, grid: { color: grid }, border: { display: false }, suggestedMax: cfg.suggestedMax,
        max: cfg.max, title: { display: !!cfg.yTitle, text: cfg.yTitle, color: text, font: { size: 10 } },
        ticks: { color: text, maxTicksLimit: 5, font: { size: 10 } },
      },
    };
    if (cfg.y2) {
      scales.y2 = {
        position: "right", beginAtZero: true, grid: { display: false }, border: { display: false },
        title: { display: true, text: cfg.y2, color: text, font: { size: 10 } },
        ticks: { color: text, maxTicksLimit: 5, font: { size: 10 } },
      };
    }
    return new Chart(canvas, {
      type: "line",
      data: { datasets },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "bottom", labels: { color: text, boxWidth: 10, boxHeight: 2, font: { size: 11 } } },
          tooltip: { callbacks: { title: (items) => (items.length ? fmtTime(items[0].parsed.x) : "") } },
          phaseBands: { stages: cfg.stages || [], labels: cfg.bandLabels !== false, cursor: null },
        },
        scales,
      },
      plugins: [bands],
    });
  }

  window.DCCharts = { lineChart, fmtTime, cssVar };
})();

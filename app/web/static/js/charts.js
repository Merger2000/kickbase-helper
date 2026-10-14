// Charts on the player page: market value (line) and points per matchday (bars).
// One series each, so no legend; colours come from CSS tokens and follow dark/light mode.
(() => {
  const dataElement = document.getElementById("chart-data");
  if (!dataElement || !window.Chart) return;
  const data = JSON.parse(dataElement.textContent);

  const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const moneyShort = (value) => {
    const amount = Math.abs(value);
    const sign = value < 0 ? "−" : "";
    if (amount >= 1e6) return `${sign}${(amount / 1e6).toLocaleString("de-DE", { maximumFractionDigits: 1 })} Mio.`;
    if (amount >= 1e3) return `${sign}${Math.round(amount / 1e3).toLocaleString("de-DE")} Tsd.`;
    return `${sign}${amount.toLocaleString("de-DE")} €`;
  };
  const moneyFull = (value) => `${value.toLocaleString("de-DE")} €`;
  const withAlpha = (hex, alpha) => {
    const n = parseInt(hex.replace("#", ""), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  };
  const shortDate = (iso) => {
    const [, month, day] = iso.split("-");
    return `${Number(day)}.${Number(month)}.`;
  };

  // Value label at the end of the line (labels stay in text colour, never the series colour).
  const endLabel = {
    id: "endLabel",
    afterDatasetsDraw(chart) {
      const meta = chart.getDatasetMeta(0);
      const last = meta.data[meta.data.length - 1];
      if (!last) return;
      const { ctx } = chart;
      ctx.save();
      ctx.font = `600 12px ${token("--font")}`;
      ctx.fillStyle = token("--text");
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      ctx.fillText(moneyShort(data.values[data.values.length - 1]), last.x, last.y - 8);
      ctx.restore();
    },
  };

  let charts = [];

  function baseOptions() {
    const grid = token("--line");
    const muted = token("--muted");
    Chart.defaults.font.family = token("--font");
    Chart.defaults.color = muted;
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      layout: { padding: { top: 22, right: 4 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: token("--surface-raised"),
          titleColor: muted,
          bodyColor: token("--text"),
          borderColor: token("--line-strong"),
          borderWidth: 1,
          padding: 10,
          displayColors: false,
          bodyFont: { weight: "600", size: 14 },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          border: { color: grid },
          ticks: { maxRotation: 0, autoSkipPadding: 16 },
        },
        y: {
          grid: { color: grid, lineWidth: 1 },
          border: { display: false },
          ticks: { maxTicksLimit: 5 },
        },
      },
    };
  }

  function marketValueChart() {
    const canvas = document.getElementById("chart-market-value");
    if (!canvas || data.values.length < 2) return null;
    const series = token("--chart-series");
    const surface = token("--bg");
    const options = baseOptions();
    options.scales.x.ticks.callback = function (value) {
      return shortDate(this.getLabelForValue(value));
    };
    options.scales.x.ticks.maxTicksLimit = 4;
    options.scales.y.ticks.callback = (value) => moneyShort(value);
    options.plugins.tooltip.callbacks = {
      title: (items) => shortDate(items[0].label),
      label: (item) => moneyFull(item.parsed.y),
    };
    const lastIndex = data.values.length - 1;
    return new Chart(canvas, {
      type: "line",
      data: {
        labels: data.days,
        datasets: [
          {
            data: data.values,
            borderColor: series,
            borderWidth: 2,
            borderJoinStyle: "round",
            borderCapStyle: "round",
            backgroundColor: withAlpha(series, 0.1),
            fill: true,
            tension: 0.25,
            pointRadius: (ctx) => (ctx.dataIndex === lastIndex ? 4 : 0),
            pointHoverRadius: 5,
            pointBackgroundColor: series,
            pointBorderColor: surface,
            pointBorderWidth: 2,
            pointHitRadius: 12,
          },
        ],
      },
      options,
      plugins: [endLabel],
    });
  }

  function pointsChart() {
    const canvas = document.getElementById("chart-points");
    if (!canvas || !data.matchdays.length) return null;
    const series = token("--chart-series");
    const options = baseOptions();
    options.interaction = { mode: "nearest", axis: "x", intersect: false };
    options.scales.x.ticks.callback = function (value) {
      return `${this.getLabelForValue(value)}.`;
    };
    options.scales.y.grace = "12%";
    options.scales.y.grid.color = (ctx) => (ctx.tick.value === 0 ? token("--line-strong") : token("--line"));
    options.plugins.tooltip.callbacks = {
      title: (items) => {
        const i = items[0].dataIndex;
        const opponent = data.opponents[i] ? ` gegen ${data.opponents[i]}` : "";
        return `${data.matchdays[i]}. Spieltag${opponent}`;
      },
      label: (item) => (data.played[item.dataIndex] ? `${item.parsed.y} Punkte` : "nicht eingesetzt"),
    };
    return new Chart(canvas, {
      type: "bar",
      data: {
        labels: data.matchdays,
        datasets: [
          {
            data: data.points,
            backgroundColor: series,
            hoverBackgroundColor: withAlpha(series, 0.75),
            maxBarThickness: 24,
            borderRadius: 4,
            borderSkipped: "start",
            categoryPercentage: 0.8,
            barPercentage: 0.9,
          },
        ],
      },
      options,
    });
  }

  // Team page: per-matchday placement (1 = top). Reads the separate
  // #matchday-chart-data element so the page can ship both charts side by side
  // without colliding on a single #chart-data payload.
  function placementChart() {
    const canvas = document.getElementById("chart-placement");
    if (!canvas) return null;
    const payloadEl = document.getElementById("matchday-chart-data");
    if (!payloadEl) return null;
    const payload = JSON.parse(payloadEl.textContent);
    if (!payload.matchdays || payload.matchdays.length < 2) return null;
    const series = token("--chart-series");
    const surface = token("--bg");
    const options = baseOptions();
    options.scales.x.ticks.callback = function (value) {
      return `${this.getLabelForValue(value)}.`;
    };
    options.scales.x.ticks.maxTicksLimit = 6;
    options.scales.y.reverse = true;
    options.scales.y.min = 1;
    options.scales.y.ticks.callback = (value) => Math.round(value);
    options.scales.y.ticks.stepSize = 1;
    options.plugins.tooltip.callbacks = {
      title: (items) => `${payload.matchdays[items[0].dataIndex]}. Spieltag`,
      label: (item) => `Platz ${item.parsed.y}`,
    };
    const lastIndex = payload.placements.length - 1;
    return new Chart(canvas, {
      type: "line",
      data: {
        labels: payload.matchdays,
        datasets: [
          {
            data: payload.placements,
            borderColor: series,
            borderWidth: 2,
            borderJoinStyle: "round",
            borderCapStyle: "round",
            backgroundColor: withAlpha(series, 0.1),
            fill: true,
            tension: 0.25,
            pointRadius: (ctx) => (ctx.dataIndex === lastIndex ? 4 : 0),
            pointHoverRadius: 5,
            pointBackgroundColor: series,
            pointBorderColor: surface,
            pointBorderWidth: 2,
            pointHitRadius: 12,
          },
        ],
      },
      options,
    });
  }

  function render() {
    charts.forEach((chart) => chart.destroy());
    charts = [marketValueChart(), pointsChart(), placementChart()].filter(Boolean);
  }

  render();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
})();

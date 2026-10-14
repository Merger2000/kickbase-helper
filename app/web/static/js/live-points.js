/* GET only; a hidden/offline page makes no polling requests. Server cache is authoritative. */
(() => {
  let inFlight = false;
  let lastCheck = Date.now();
  document.addEventListener("htmx:beforeRequest", (event) => {
    if (event.detail.target?.id === "live-points") inFlight = true;
  });
  document.addEventListener("htmx:afterRequest", (event) => {
    if (event.detail.target?.id === "live-points") {
      inFlight = false;
      lastCheck = Date.now();
    }
  });
  setInterval(() => {
    const panel = document.getElementById("live-points");
    if (!panel || panel.dataset.autoRefresh !== "true" || document.hidden ||
        !navigator.onLine || inFlight || !window.htmx) return;
    const seconds = Math.max(600, Number(panel.dataset.interval) || 600);
    if (Date.now() - lastCheck < seconds * 1000) return;
    lastCheck = Date.now();
    window.htmx.trigger(panel, "refresh-points");
  }, 1000);
})();

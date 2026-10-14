// Pull-to-refresh for the installed PWA (iOS has no native gesture in standalone mode):
// pulling down at the top of the page triggers the refresh button.
(() => {
  const indicator = document.getElementById("pull-indicator");
  const button = document.getElementById("refresh");
  if (!indicator || !button) return;

  const THRESHOLD = 72;
  let startY = null;
  let armed = false;

  const reset = () => {
    startY = null;
    armed = false;
    indicator.classList.remove("is-visible", "is-armed");
    indicator.style.removeProperty("--pull");
  };

  window.addEventListener(
    "touchstart",
    (event) => {
      startY = window.scrollY <= 0 && event.touches.length === 1 ? event.touches[0].clientY : null;
    },
    { passive: true },
  );

  window.addEventListener(
    "touchmove",
    (event) => {
      if (startY === null) return;
      const distance = event.touches[0].clientY - startY;
      if (distance <= 0) return reset();
      armed = distance > THRESHOLD;
      indicator.classList.toggle("is-visible", distance > 16);
      indicator.classList.toggle("is-armed", armed);
      indicator.style.setProperty("--pull", `${Math.min(distance, 120)}px`);
    },
    { passive: true },
  );

  window.addEventListener("touchend", () => {
    if (armed && !button.classList.contains("htmx-request")) button.click();
    reset();
  });
  window.addEventListener("touchcancel", reset);
})();

/* Keep the GET form compact; prices become editable after selecting the purchase. */
(() => {
  const sync = () => {
    document.querySelectorAll(".simulation-offer").forEach((row) => {
      const selected = row.querySelector('input[name="kauf"]');
      const price = row.querySelector('.simulation-price input');
      if (selected && price) price.disabled = !selected.checked;
    });
  };
  document.addEventListener("change", sync);
  document.addEventListener("htmx:afterSwap", sync);
  sync();
})();

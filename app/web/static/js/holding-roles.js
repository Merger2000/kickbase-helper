// An edited/failed role is not a newly saved preference.
document.addEventListener("change", (event) => {
  const editor = event.target.closest(".holding-edit");
  if (editor) editor.querySelector(".holding-edit__saved")?.remove();
});

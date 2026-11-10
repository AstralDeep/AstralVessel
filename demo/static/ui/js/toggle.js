/* design@631e72808224 js/toggle.js - copied by design-add. Local edits are allowed; `design-add --check` reports them. */
// Flips aria-checked on any <button class="toggle" role="switch"> when clicked
// and fires a "change" event the page can listen for. Not needed with the React wrapper.
document.addEventListener('click', function (e) {
  var b = e.target.closest && e.target.closest('button.toggle[role="switch"]');
  if (!b) return;
  b.setAttribute('aria-checked', b.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
  b.dispatchEvent(new Event('change', { bubbles: true }));
});

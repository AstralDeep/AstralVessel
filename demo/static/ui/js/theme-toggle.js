/* design@631e72808224 js/theme-toggle.js - copied by design-add. Local edits are allowed; `design-add --check` reports them. */
// Theme toggle. Any <button data-theme-toggle> switches between light and dark,
// stores the choice, and labels itself with the theme it will switch to.
// Load in <head> so a stored choice applies before first paint.
(function () {
  var KEY = 'ds-theme';
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem(KEY);
    if (saved === 'light' || saved === 'dark') root.setAttribute('data-theme', saved);
  } catch (e) {}

  function current() {
    var t = root.getAttribute('data-theme');
    if (t === 'light' || t === 'dark') return t;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function label() {
    var next = current() === 'dark' ? 'Light' : 'Dark';
    document.querySelectorAll('[data-theme-toggle]').forEach(function (b) {
      b.textContent = next;
      b.setAttribute('aria-label', 'Switch to ' + next.toLowerCase() + ' theme');
    });
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-theme-toggle]');
    if (!b) return;
    var next = current() === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem(KEY, next); } catch (err) {}
    label();
  });

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', label);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', label);
  else label();
})();

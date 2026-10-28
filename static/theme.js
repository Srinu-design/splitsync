(function () {
  'use strict';

  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let choice = null;
  try {
    const saved = localStorage.getItem('splitsync.theme');
    if (saved === 'light' || saved === 'dark') choice = saved;
  } catch (_) {}

  function applyTheme() {
    const theme = choice || (system.matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      const label = 'Switch to ' + (theme === 'dark' ? 'light' : 'dark') + ' mode';
      button.setAttribute('aria-label', label);
      button.title = label;
    });
  }

  applyTheme();
  system.addEventListener('change', applyTheme);
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      button.addEventListener('click', () => {
        choice = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        try { localStorage.setItem('splitsync.theme', choice); } catch (_) {}
        applyTheme();
      });
    });
    applyTheme();
  }, { once: true });
})();

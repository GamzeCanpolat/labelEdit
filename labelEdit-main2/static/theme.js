/* Mapin görünümü: sayfa yüklenirken temayı uygula; seçim sayfalar arasında kalır. */
(() => {
  'use strict';
  const storageKey = 'mapin-theme';
  const root = document.documentElement;
  const valid = value => value === 'light' || value === 'dark';
  let theme = 'dark';
  try {
    const stored = localStorage.getItem(storageKey);
    if (valid(stored)) theme = stored;
  } catch (_) { /* Özel gezinmede tema bu sayfada çalışmaya devam eder. */ }

  function apply(value, persist = false) {
    if (!valid(value)) return;
    theme = value;
    root.dataset.theme = value;
    root.style.colorScheme = value;
    document.querySelectorAll('[data-theme-choice]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.themeChoice === value));
    });
    if (persist) {
      try { localStorage.setItem(storageKey, value); } catch (_) { /* Görünüm yine değişir. */ }
    }
  }
  apply(theme);
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-theme-choice]').forEach(button => {
      button.addEventListener('click', () => apply(button.dataset.themeChoice, true));
    });
    apply(theme);
  });
  window.addEventListener('storage', event => {
    if (event.key === storageKey && valid(event.newValue)) apply(event.newValue);
  });
})();

(function () {
  'use strict';
  const key = 'chimera-theme';
  const root = document.documentElement;
  const themes = ['light', 'dark', 'system'];
  const get = () => root.dataset.theme;

  function apply(value) {
    const theme = themes.includes(value) ? value : 'system';
    root.dataset.theme = theme;
    document.dispatchEvent(new CustomEvent('chimera:theme', { detail: theme }));
  }

  let stored;
  try { stored = localStorage.getItem(key); } catch {
    // A blocked storage area still permits changing the theme on this page.
  }
  apply(stored);

  window.ChimeraTheme = {
    get,
    set(theme) {
      if (!themes.includes(theme)) return;
      apply(theme);
      try { localStorage.setItem(key, theme); } catch {
        // Keep the applied theme for the current page.
      }
    }
  };

  window.addEventListener('storage', (event) => {
    if (event.storageArea === localStorage && (event.key === key || event.key === null)) {
      apply(event.newValue);
    }
  });
})();

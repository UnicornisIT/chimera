// Оболочка интерфейса: боковая панель, тема, подтверждения, автоотправка форм.
// По мотивам modern-ui.js из Manticore. Без inline-скриптов (CSP script-src 'self').
(function () {
  'use strict';
  const body = document.body;
  const sidebarToggle = document.querySelector('[data-sidebar-toggle]');
  const mobile = () => window.matchMedia('(max-width: 900px)').matches;

  if (sidebarToggle) {
    const sync = () => {
      const expanded = mobile()
        ? body.classList.contains('sidebar-open')
        : !body.classList.contains('sidebar-collapsed');
      const label = expanded ? 'Свернуть боковую панель' : 'Развернуть боковую панель';
      sidebarToggle.setAttribute('aria-expanded', String(expanded));
      sidebarToggle.setAttribute('aria-label', label);
      sidebarToggle.title = label;
    };
    let collapsed = false;
    try { collapsed = sessionStorage.getItem('chimera-sidebar') === 'collapsed'; } catch (e) { /* приватный режим */ }
    if (collapsed && !mobile()) body.classList.add('sidebar-collapsed');
    sync();
    sidebarToggle.addEventListener('click', () => {
      if (mobile()) body.classList.toggle('sidebar-open');
      else body.classList.toggle('sidebar-collapsed');
      sync();
      try {
        sessionStorage.setItem('chimera-sidebar', body.classList.contains('sidebar-collapsed') ? 'collapsed' : 'expanded');
      } catch (e) { /* приватный режим */ }
    });
    window.addEventListener('resize', sync, { passive: true });
  }

  const themeToggle = document.querySelector('[data-theme-toggle]');
  const themeLabels = { system: 'системная', light: 'светлая', dark: 'тёмная' };
  const syncTheme = () => {
    const theme = window.ChimeraTheme?.get() || 'system';
    if (themeToggle) themeToggle.title = `Тема: ${themeLabels[theme]}`;
  };
  themeToggle?.addEventListener('click', () => {
    const current = window.ChimeraTheme?.get() || 'system';
    window.ChimeraTheme?.set({ system: 'light', light: 'dark', dark: 'system' }[current]);
    syncTheme();
  });
  syncTheme();

  document.querySelectorAll('.flash-message').forEach((message) => {
    message.setAttribute('role', message.classList.contains('error') ? 'alert' : 'status');
    if (message.classList.contains('error')) return;
    window.setTimeout(() => {
      message.classList.add('is-hiding');
      window.setTimeout(() => message.remove(), 200);
    }, 6000);
  });

  // Формы и кнопки с data-confirm спрашивают подтверждение.
  document.addEventListener('submit', (event) => {
    const form = event.target;
    const text = form.dataset.confirm;
    if (text && !window.confirm(text)) event.preventDefault();
  }, true);

  // Селекты с data-autosubmit отправляют форму при изменении (переключатель организации).
  document.querySelectorAll('select[data-autosubmit]').forEach((select) => {
    select.addEventListener('change', () => select.form?.requestSubmit());
  });

  // Поля, зависящие от выбранной роли (подразделение, пользователь).
  document.querySelectorAll('[data-role-select]').forEach((select) => {
    const form = select.closest('form');
    const sync = () => {
      form.querySelectorAll('[data-for-roles]').forEach((field) => {
        const roles = field.dataset.forRoles.split(' ');
        const visible = roles.includes(select.value);
        field.hidden = !visible;
        field.querySelectorAll('select, input').forEach((input) => { input.disabled = !visible; });
      });
    };
    select.addEventListener('change', sync);
    sync();
  });

  document.querySelectorAll('[data-print]').forEach((button) => {
    button.addEventListener('click', () => window.print());
  });
})();

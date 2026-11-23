// Оболочка интерфейса: боковая панель, тема, подтверждения, автоотправка форм.
(function () {
  'use strict';
  const body = document.body;
  const sidebarToggle = document.querySelector('[data-sidebar-toggle]');
  const mobile = window.matchMedia('(max-width: 900px)');

  if (sidebarToggle) {
    const sidebar = document.querySelector('.main-nav');
    const isMobileOpen = () => body.classList.contains('sidebar-open');
    const sync = () => {
      const expanded = mobile.matches
        ? isMobileOpen()
        : !body.classList.contains('sidebar-collapsed');
      const label = expanded ? 'Свернуть боковую панель' : 'Развернуть боковую панель';
      sidebar.inert = !expanded;
      sidebarToggle.setAttribute('aria-expanded', String(expanded));
      sidebarToggle.setAttribute('aria-label', label);
      sidebarToggle.title = label;
    };
    const setMobileOpen = (open) => {
      body.classList.toggle('sidebar-open', open);
      sync();
      (open ? sidebar.querySelector('.nav-brand') : sidebarToggle).focus();
    };
    let collapsed = false;
    try { collapsed = sessionStorage.getItem('chimera-sidebar') === 'collapsed'; } catch (e) { /* приватный режим */ }
    if (collapsed) body.classList.add('sidebar-collapsed');
    sync();
    sidebarToggle.addEventListener('click', () => {
      if (mobile.matches) {
        setMobileOpen(!isMobileOpen());
        return;
      }
      body.classList.toggle('sidebar-collapsed');
      sync();
      try {
        sessionStorage.setItem('chimera-sidebar', body.classList.contains('sidebar-collapsed') ? 'collapsed' : 'expanded');
      } catch (e) { /* приватный режим */ }
    });
    mobile.addEventListener('change', () => {
      if (isMobileOpen()) {
        setMobileOpen(false);
        return;
      }
      const focusWasInSidebar = sidebar.contains(document.activeElement);
      sync();
      if (mobile.matches && focusWasInSidebar) sidebarToggle.focus();
    });
    // При смене ширины браузер может сбросить фокус раньше события change.
    sidebar.addEventListener('focusout', (event) => {
      if (!event.relatedTarget && mobile.matches && !isMobileOpen()) sidebarToggle.focus();
    });
    document.addEventListener('keydown', (event) => {
      if (!mobile.matches || !isMobileOpen()) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        setMobileOpen(false);
        return;
      }
      if (event.key !== 'Tab') return;

      const items = [...sidebar.querySelectorAll('a[href], summary')].filter((item) => {
        const closed = item.closest('details:not([open])');
        return item.tabIndex >= 0 && (!closed || closed.firstElementChild === item)
          && item.getClientRects().length > 0;
      });
      const index = items.indexOf(document.activeElement);
      const edge = event.shiftKey ? 0 : items.length - 1;
      if (index !== -1 && index !== edge) return;
      event.preventDefault();
      (event.shiftKey ? items.at(-1) : items[0])?.focus();
    });
  }

  const themeToggle = document.querySelector('[data-theme-toggle]');
  const themeChoice = document.querySelector('[data-theme-choice]');
  const themeLabels = { system: 'системная', light: 'светлая', dark: 'тёмная' };
  const theme = window.ChimeraTheme;
  if (theme) {
    const syncTheme = () => {
      const current = theme.get();
      const label = `Тема: ${themeLabels[current]}`;
      if (themeToggle) {
        themeToggle.title = label;
        themeToggle.setAttribute('aria-label', `Изменить тему. ${label}`);
      }
      if (themeChoice) themeChoice.value = current;
    };
    if (themeToggle) {
      themeToggle.hidden = false;
      themeToggle.addEventListener('click', () => {
        theme.set({ system: 'light', light: 'dark', dark: 'system' }[theme.get()]);
      });
    }
    if (themeChoice) {
      themeChoice.closest('.form-field').hidden = false;
      themeChoice.addEventListener('change', () => theme.set(themeChoice.value));
    }
    document.addEventListener('chimera:theme', syncTheme);
    syncTheme();
  }

  // Кнопки доступны после регистрации обработчика подтверждения.
  document.addEventListener('submit', (event) => {
    const form = event.target;
    const text = form.dataset.confirm;
    if (text && !window.confirm(text)) event.preventDefault();
  }, true);
  document.querySelectorAll('[data-confirm] [data-confirm-submit]').forEach((button) => {
    button.disabled = false;
  });

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

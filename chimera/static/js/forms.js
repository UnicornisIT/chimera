(function () {
  'use strict';
  const dialog = document.getElementById('action-confirmation');
  const accept = dialog.querySelector('[data-confirm-accept]');
  const main = document.getElementById('main-content');
  const locked = new Map();
  let confirmation;
  let confirmedForm;
  let submitting = false;
  let uncertain = false;
  let activeRequest;
  let accepted = false;

  function init() {
    if (typeof dialog.showModal === 'function') {
      main.querySelectorAll('[data-confirm-submit]').forEach((button) => {
        button.disabled = false;
        button.setAttribute('aria-haspopup', 'dialog');
      });
    }
    main.querySelector('[data-error-focus]')?.focus();
  }

  function connectionMessage(text, focus = false) {
    const message = document.getElementById('connection-message');
    if (!message) return;
    message.querySelector('[data-connection-text]').textContent = text;
    message.hidden = !text;
    if (focus) message.focus();
  }

  function reportOffline() {
    if (!uncertain) {
      connectionMessage(navigator.onLine ? '' : 'Нет подключения. Проверьте связь с приложением.');
    }
  }

  function progress(text, visible = false) {
    const status = document.getElementById('form-progress');
    if (!status) return;
    status.textContent = text;
    status.classList.toggle('sr-only', !visible);
  }

  function lock(form, button) {
    progress('Выполняется…', !button);
    form.setAttribute('aria-busy', 'true');
    locked.set(form, button);
    if (!button) return;
    button.setAttribute('aria-disabled', 'true');
    button.classList.add('is-submitting');
  }

  function unlock(form) {
    const button = locked.get(form);
    form.removeAttribute('aria-busy');
    progress('');
    if (button) {
      button.disabled = false;
      button.removeAttribute('aria-disabled');
      button.classList.remove('is-submitting');
    }
    locked.delete(form);
  }

  async function send(form, button) {
    const fields = new URLSearchParams(new FormData(form, button || undefined));
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    submitting = true;
    activeRequest = controller;
    accepted = false;
    lock(form, button);
    if (button) button.disabled = true;
    try {
      const result = await fetch(form.action, {
        method: 'POST',
        body: fields,
        headers: { 'X-Chimera-Form': '1' },
        redirect: 'error',
        mode: 'same-origin',
        signal: controller.signal,
      });
      if (result.headers.get('X-Chimera-Form') !== '1' || result.status === 408 || result.status >= 500) {
        throw new Error('Unexpected form response');
      }
      const location = result.headers.get('X-Chimera-Location');
      if (result.status === 204 && location) {
        const destination = new URL(location, window.location.href);
        if (destination.origin !== window.location.origin) throw new Error('Unexpected redirect');
        window.location.assign(destination);
        accepted = true;
        return;
      }
      const content = new DOMParser().parseFromString(await result.text(), 'text/html');
      const updatedMain = content.getElementById('main-content');
      if (!updatedMain) throw new Error('Missing form response');
      unlock(form);
      submitting = false;
      main.replaceChildren(...updatedMain.childNodes);
      document.dispatchEvent(new Event('chimera:content'));
      init();
      if (!main.contains(document.activeElement)) main.focus();
      reportOffline();
    } catch {
      if (!uncertain) {
        markUncertain(controller.signal.aborted
          ? 'Приложение не ответило вовремя.'
          : 'Не удалось получить ответ от приложения.');
      }
    } finally {
      window.clearTimeout(timeout);
      activeRequest = undefined;
    }
  }

  function markUncertain(reason) {
    uncertain = true;
    progress('');
    locked.forEach((button, form) => {
      button?.classList.remove('is-submitting');
      form.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ''; });
    });
    connectionMessage(reason + ' Действие могло выполниться. Проверьте страницу перед повтором.', true);
  }

  dialog.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const cancel = dialog.querySelector('[value="cancel"]');
    const edge = event.shiftKey ? cancel : accept;
    if (document.activeElement === edge) {
      event.preventDefault();
      (event.shiftKey ? accept : cancel).focus();
    }
  });

  dialog.addEventListener('close', () => {
    if (!confirmation) return;
    const { form, button } = confirmation;
    confirmation = undefined;
    button.focus();
    if (dialog.returnValue !== 'accept') return;
    confirmedForm = form;
    try {
      form.requestSubmit(button);
    } finally {
      confirmedForm = undefined;
    }
  });

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (event.defaultPrevented || form.method !== 'post') return;
    const button = event.submitter;
    if (locked.has(form) || submitting || uncertain) {
      event.preventDefault();
      return;
    }
    if (form.dataset.confirm && form !== confirmedForm) {
      event.preventDefault();
      if (!button || button.disabled || dialog.open || typeof dialog.showModal !== 'function') return;
      confirmation = { form, button };
      const label = button.textContent.trim();
      dialog.returnValue = '';
      dialog.querySelector('#confirm-title').textContent = form.dataset.confirmTitle || label + '?';
      dialog.querySelector('#confirm-message').textContent = form.dataset.confirm;
      accept.textContent = label;
      dialog.showModal();
      return;
    }
    if (!navigator.onLine) {
      event.preventDefault();
      connectionMessage('Нет подключения. Проверьте связь и отправьте форму после восстановления.', true);
      return;
    }
    if (typeof window.fetch === 'function' && typeof window.AbortController === 'function') {
      event.preventDefault();
      void send(form, button);
      return;
    }
    // Нативная отправка должна сохранить имя и значение нажатой кнопки.
    lock(form, button);
  });

  document.addEventListener('invalid', (event) => {
    const input = event.target;
    if (input.validity.valueMissing) input.setCustomValidity('Заполните это поле.');
    if (input.validity.typeMismatch && input.type === 'email') {
      input.setCustomValidity('Введите адрес электронной почты.');
    }
  }, true);
  for (const name of ['input', 'change']) {
    document.addEventListener(name, (event) => event.target.setCustomValidity?.(''));
  }

  window.addEventListener('pageshow', (event) => {
    if (!event.persisted || uncertain) return;
    if (submitting && !accepted) {
      markUncertain('Не удалось подтвердить ответ от приложения.');
      activeRequest?.abort();
      return;
    }
    locked.forEach((_, form) => unlock(form));
    submitting = false;
    accepted = false;
    reportOffline();
  });
  window.addEventListener('offline', reportOffline);
  window.addEventListener('online', reportOffline);
  init();
  reportOffline();
})();

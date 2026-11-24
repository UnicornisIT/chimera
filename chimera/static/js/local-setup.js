(() => {
  const form = document.getElementById('local-setup');
  const token = form.elements.namedItem('setup_token');
  if (window.location.hash) {
    token.value = window.location.hash.slice(1);
    window.history.replaceState(null, '', window.location.pathname);
  }
  if (token.value) {
    form.querySelector('button[type="submit"]').disabled = false;
    const access = document.getElementById('setup-access');
    if (access) access.hidden = true;
  }
})();

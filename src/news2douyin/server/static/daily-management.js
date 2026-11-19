(() => {
  const panel = document.querySelector('[data-daily-management]');
  if (!panel || panel.dataset.running !== '1') return;
  // Leave the date input and expanded failure details undisturbed while being read.
  window.setInterval(() => {
    if (document.visibilityState === 'visible' &&
        !document.activeElement?.closest('input,button,select') &&
        !panel.querySelector('details[open]')) window.location.reload();
  }, 10000);
})();

(() => {
  const status = document.querySelector('[data-video-build]');
  if (!status) return;
  const refresh = async () => {
    if (!document.hidden) {
      try {
        const reply = await fetch(status.dataset.videoBuild, {cache: 'no-store'});
        if (reply.ok) {
          const issue = await reply.json();
          if (!['pending', 'building'].includes(issue.build_status)) {
            window.location.reload();
            return;
          }
          const message = status.firstChild;
          if (issue.build_status === 'building' && message && message.textContent.trim() !== '正在生成。') {
            message.textContent = '正在生成。 ';
          }
        }
      } catch {
        // Leave the manual refresh link available if polling cannot reach the server.
      }
    }
    window.setTimeout(refresh, 4000);
  };
  window.setTimeout(refresh, 4000);
})();

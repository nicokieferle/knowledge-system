// UX only: every action remains protected and idempotent on the server.
document.addEventListener('submit', (event) => {
  if (event.target.method.toLowerCase() !== 'post') return;
  for (const button of event.target.querySelectorAll('button')) button.disabled = true;
});
// Back/forward cache must not leave a previously submitted form unusable.
window.addEventListener('pageshow', (event) => { if (event.persisted) window.location.reload(); });

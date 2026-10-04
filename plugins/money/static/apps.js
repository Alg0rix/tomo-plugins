/* Connected Apps: durable jobs, accessible progress and explicit destinations. */
(function () {
  'use strict';
  const root = document.getElementById('connectedApps');
  if (!root) return;
  const base = document.querySelector('[data-base]').dataset.base;
  const errorBox = document.getElementById('appsError');
  let disposed = false;
  let polling = false;
  function error(message) { errorBox.textContent = message; errorBox.hidden = !message; }
  async function request(url, body) {
    const response = await fetch(url, body === undefined ? {cache:'no-store'} : {
      method:'POST', headers:{'Content-Type':'application/json','Accept':'application/json'}, body:JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed. Please retry.');
    return data;
  }
  function values(form) {
    const data = Object.fromEntries(new FormData(form));
    if (form.elements.automatic) data.automatic = form.elements.automatic.checked;
    return data;
  }
  function node(tag, text, cls) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }
  function jobView(job) {
    const el = node('div', undefined, 'm-job-row');
    const p = job.progress;
    const label = job.provider === 'gmail' ? 'emails' : job.provider === 'sheets' ? 'transactions' : 'reminders';
    const title = node('strong', job.provider.toUpperCase() + (job.account ? ' · ' + job.account : ''));
    el.append(title, node('span', job.status, 'm-job-status ' + job.status));
    const meter = node('progress');
    meter.max = Math.max(1, p.total);
    if (p.discovered || job.status === 'succeeded') meter.value = job.status === 'succeeded' ? meter.max : p.done;
    meter.setAttribute('aria-label', 'Processing ' + label);
    el.append(meter, node('p', p.done + ' of ' + p.total + (p.discovered ? '' : '+ found') + ' ' + label + ' processed', 'm-sub'));
    if (job.provider === 'gmail') el.append(node('p', p.candidates + ' queued for review · ' + p.duplicates + ' already seen · ' + (p.skipped || 0) + ' unavailable', 'm-sub'));
    if (job.error) el.append(node('p', job.error, 'm-app-error'));
    if (job.status === 'retry') el.append(node('p', 'Retry at ' + new Date(job.available_at * 1000).toLocaleString(), 'm-sub'));
    if (['queued','running','retry','failed','cancelled'].includes(job.status)) {
      const action = ['failed','cancelled'].includes(job.status) ? 'retry' : 'cancel';
      const button = node('button', action === 'retry' ? 'Retry' : 'Cancel', 'm-btn sm');
      button.type = 'button';
      button.onclick = async () => {
        button.disabled = true;
        try { await request(base + '/apps/jobs/' + job.id + '/' + action, {}); await refresh(); }
        catch (e) { error(e.message); button.disabled = false; }
      };
      el.append(button);
    }
    return el;
  }
  async function refresh() {
    if (disposed || polling || document.hidden) return;
    polling = true;
    try {
      const data = await request(root.dataset.statusUrl);
      const list = document.getElementById('appJobs');
      list.replaceChildren();
      if (!data.jobs.length) list.append(node('p','No background jobs yet.','m-sub'));
      data.jobs.forEach(job => list.append(jobView(job)));
      root.querySelectorAll('[data-provider]').forEach(card => {
        const provider = card.dataset.provider, account = card.dataset.account;
        const job = data.jobs.find(j => j.provider === provider && j.account === account);
        const view = card.querySelector('[data-current-job]');
        if (view) { view.replaceChildren(); if (job) view.append(jobView(job)); }
        const connection = provider === 'gmail' ? data.accounts.find(a => a.email === account) : data.connections[provider];
        const last = card.querySelector('[data-last-sync]');
        if (last && connection) {
          const settings = connection.settings;
          last.textContent = settings.last_success ? 'Last success: ' + new Date(settings.last_success * 1000).toLocaleString() : 'No completed sync yet';
          if (settings.automatic) last.textContent += ' · Next: ' + new Date(settings.next_run * 1000).toLocaleTimeString();
          // A terminal provider failure pauses the persisted schedule.
          if (job && job.status === 'failed' && !settings.automatic) {
            const toggle = card.querySelector('[name="automatic"]');
            if (toggle) toggle.checked = false;
          }
        }
        const run = card.querySelector('[data-run]');
        if (run) run.disabled = Boolean(job && ['queued','running','retry'].includes(job.status)) || (provider !== 'gmail' && !(connection && connection.target));
      });
    } catch (e) { error('Cannot refresh job status: ' + e.message); }
    finally { polling = false; }
  }
  root.querySelectorAll('form[data-app-form]').forEach(form => {
    form.addEventListener('submit', async ev => {
      ev.preventDefault();
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
      const button = ev.submitter || form.querySelector('button[type="submit"],button');
      if (button) button.disabled = true;
      error('');
      try {
        await request(form.action, values(form));
        if (form.hasAttribute('data-reload')) { window.location.reload(); return; }
        if (window.Tomo) Tomo.toast('Preferences saved', 'ok');
        await refresh();
      } catch (e) { error(e.message); }
      finally { if (button) button.disabled = false; }
    });
  });
  root.querySelectorAll('[data-run]').forEach(button => {
    button.onclick = async () => {
      button.disabled = true; error('');
      try {
        const form = button.closest('form');
        await request(base + '/apps/' + button.dataset.run + '/sync', values(form));
        await refresh();
      } catch (e) { error(e.message); button.disabled = false; }
    };
  });
  const timezone = root.querySelector('[data-timezone]');
  if (timezone) timezone.value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const timer = setInterval(refresh, 3000);
  window.addEventListener('pagehide', () => { disposed = true; clearInterval(timer); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  refresh();
})();

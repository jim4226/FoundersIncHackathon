/* The project record. Reads the same /ws stream as the other two views, so it
   updates live while a session is running rather than needing a refresh.

   The design job here is one column: effort. A software commit log has author
   and time; this one has how considered the person was. Everything else is
   arranged to make that column readable at a glance. */

const el = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function effortClass(c) {
  if (c.flagged) return 'ce-flagged';
  if (c.effort === null || c.effort === undefined) return 'ce-unknown';
  if (c.effort < 35) return 'ce-diffuse';
  if (c.effort >= 65) return 'ce-focused';
  return '';
}

function render(project) {
  el('p-name').textContent = project.name;
  el('p-desc').textContent = project.description;

  const stats = project.stats || {};
  el('s-decisions').textContent = stats.totalContributions ?? 0;
  el('s-effort').textContent = stats.averageEffort ?? '—';
  el('s-challenged').textContent = stats.challenged ?? 0;

  // --- decision log, newest first
  const commits = [...(project.contributions || [])].reverse();
  el('commits').innerHTML = commits.length ? commits.map((c) => {
    const effort = (c.effort === null || c.effort === undefined) ? 'n/a' : Math.round(c.effort);
    return `<div class="commit">
      <div class="commit-effort ${effortClass(c)}">
        <span class="ce-num">${effort}</span>
        <span class="ce-cap">${c.flagged ? 'flagged' : c.label}</span>
      </div>
      <div class="commit-body">
        <div class="commit-meta">${esc(c.author)} · ${esc(c.kind)}<span class="tag ${c.status}">${c.status}</span></div>
        <p>${esc(c.body)}</p>
        ${c.agent_response ? `<div class="commit-agent">${esc(c.agent_response)}</div>` : ''}
      </div>
    </div>`;
  }).join('') : '<p style="color:var(--dim);font-size:12.5px">Nothing recorded yet. Decisions made at the bench land here.</p>';

  // --- designs, with measured attention as the bar
  const variants = project.variants || [];
  const maxAttention = Math.max(1, ...variants.map((v) => v.attention_seconds));
  el('versions').innerHTML = variants.map((v) => {
    const status = { promoted: 'approved', redesign: 'changes_requested', archived: 'archived' }[v.status] || 'working';
    return `<div class="ver ${status}">
      <span class="ver-dot"></span>
      <div class="ver-body">
        <div class="ver-top">
          <span class="ver-name">${esc(v.label)}</span>
          <span class="ver-meta">v${v.version} · ${esc(v.status)}</span>
        </div>
        <div class="ver-sum">${esc(v.summary)}</div>
        <div class="attn-bar"><div class="attn-fill" style="width:${(v.attention_seconds / maxAttention) * 100}%"></div></div>
        <span class="attn-cap">${v.attention_seconds.toFixed(0)}s measured attention</span>
      </div>
    </div>`;
  }).join('');

  el('files').innerHTML = (project.artifacts || []).map((a) => `
    <div class="file">
      <span class="file-name">${esc(a.name)}</span>
      <span class="file-kind">${esc(a.kind)} v${a.version}</span>
    </div>`).join('');
}

/* ------------------------------------------------------------- transport */

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'project') render(msg.project);
    // Any event that mutates the record ships the whole project with it.
    if (msg.project) render(msg.project);
    if (msg.type === 'agent_response') refresh();
  };
  ws.onclose = () => setTimeout(connect, 1200);
}

async function refresh() {
  render(await (await fetch('/api/project')).json());
}

el('ask-send').addEventListener('click', async () => {
  const question = el('ask-input').value.trim();
  if (!question) return;
  const button = el('ask-send');
  button.disabled = true;
  button.textContent = 'Reading the project…';
  try {
    const res = await fetch('/api/agent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: question, intent: 'ask' }),
    });
    const data = await res.json();
    const answer = el('answer');
    answer.hidden = false;
    answer.className = 'answer';
    answer.textContent = data.agent.text;
  } finally {
    button.disabled = false;
    button.textContent = 'Ask';
  }
});

el('ask-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) el('ask-send').click();
});

refresh();
connect();

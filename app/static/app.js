/* Blast Radius Zero — console */
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
const fmtMs = (ms) => (ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`);
const fmtTime = (t) => new Date(t * 1000).toLocaleString();

const state = { config: null, runId: null, es: null, kind: 'task' };

/* ---------------- boot ---------------- */
async function boot() {
  wireTabs();
  wireComposer();
  wireLab();
  wireLedger();
  await loadConfig();
  await loadLedger();
}
boot();

async function loadConfig() {
  try {
    const cfg = await (await fetch('/api/config')).json();
    state.config = cfg;
    $('#badge-runtime').textContent = cfg.runtime_error ? 'sandbox: unavailable'
      : `${cfg.runtime}${cfg.gvisor ? ' · gVisor' : ' · runc'}`;
    $('#badge-runtime').className = 'badge ' + (cfg.image_present && !cfg.runtime_error ? 'good' : 'bad');
    $('#badge-model').textContent = cfg.mock ? `model: ${cfg.model} (mock)` : `model: ${cfg.model}`;
    $('#badge-stats').textContent = `${cfg.stats.runs} runs · ${cfg.stats.probes_blocked}/${cfg.stats.probes_total} probes blocked`;
    renderLimits(cfg.limits);
  } catch (e) {
    $('#badge-runtime').textContent = 'control plane offline';
  }
}

function renderLimits(limits) {
  const strip = $('#limits-strip');
  strip.innerHTML = '';
  if (!limits) return;
  const rows = [
    ['network', limits.network],
    ['memory', `${limits.memory_mb} MB`],
    ['cpu', limits.cpus],
    ['pids', limits.pids],
    ['wall', `${limits.timeout_s}s`],
    ['rootfs', limits.rootfs],
    ['user', limits.user],
    ['caps', limits.capabilities],
    ['runtime', limits.runtime + (limits.gvisor ? ' (gVisor)' : ' (runc)')],
  ];
  rows.forEach(([k, v]) => strip.appendChild(el('span', 'limit', `${k} <b>${esc(v)}</b>`)));
}

/* ---------------- tabs ---------------- */
function wireTabs() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => switchView(tab.dataset.view));
  });
}
function switchView(name) {
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  if (name === 'ledger') loadLedger();
}

/* ---------------- console ---------------- */
function wireComposer() {
  document.querySelectorAll('.chip[data-task]').forEach((chip) => {
    chip.addEventListener('click', () => { $('#task').value = chip.dataset.task; $('#task').focus(); });
  });
  $('#run-btn').addEventListener('click', dispatchTask);
  $('#task').addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') dispatchTask();
  });
}

async function dispatchTask() {
  const task = $('#task').value.trim();
  if (task.length < 3) return;
  const btn = $('#run-btn');
  btn.disabled = true; btn.textContent = 'Dispatching…';
  try {
    const res = await fetch('/api/runs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || 'dispatch failed');
    switchView('console');
    startRun(body.run_id, 'task', task);
  } catch (e) {
    alert(`Dispatch failed: ${e.message}`);
  } finally {
    btn.disabled = false; btn.textContent = 'Dispatch to sandbox';
  }
}

function startRun(runId, kind, task) {
  state.runId = runId; state.kind = kind;
  $('#run-panel').classList.remove('hidden');
  $('#steps').innerHTML = '';
  $('#answer-box').classList.add('hidden');
  $('#run-id').textContent = runId;
  $('#run-task').textContent = task || '';
  setRunState(kind === 'probe' ? 'running probes' : 'starting');
  setLive(true);
  attach(runId, kind);
}

function setLive(on, err) {
  const dot = $('#live-dot');
  dot.className = 'live' + (on ? (err ? ' err' : ' on') : '');
  $('#live-label').textContent = on ? 'streaming' : 'idle';
}

function setRunState(text, cls) {
  const n = $('#run-state');
  n.textContent = text;
  n.className = 'run-state ' + (cls || '');
}

function attach(runId, kind) {
  if (state.es) { state.es.close(); state.es = null; }
  const es = new EventSource(`/api/runs/${runId}/events`);
  state.es = es;
  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    handleEvent(data, kind);
  };
  es.onerror = () => { setLive(false, true); es.close(); };
}

function handleEvent(ev, kind) {
  switch (ev.type) {
    case 'run.status':
      setRunState(ev.status, ev.status === 'running' ? 'running' : '');
      break;
    case 'step.started':
      addStepCard(ev);
      break;
    case 'step.log': {
      const pre = document.querySelector(`#step-out-${ev.no}`);
      if (pre) { pre.textContent += ev.line + '\n'; pre.scrollTop = pre.scrollHeight; }
      break;
    }
    case 'step.finished':
      finishStepCard(ev);
      break;
    case 'verify':
      setVerifyPill(ev.sufficient, ev.reason);
      break;
    case 'run.error':
      setRunState('error', 'error');
      break;
    case 'probe.started':
      addProbeCard(ev);
      break;
    case 'probe.finished':
      finishProbeCard(ev);
      break;
    case 'probe.suite.finished':
      showProbeSummary(ev);
      break;
    case 'run.finished':
      finishRun(ev, kind);
      break;
  }
}

function addStepCard(ev) {
  const card = el('div', 'step');
  card.id = `step-${ev.no}`;
  card.innerHTML = `
    <div class="step-head">
      <span class="step-no">STEP ${ev.no}</span>
      <span class="step-title">${esc(ev.title)}</span>
      <span class="step-meta"><span class="pill" id="step-state-${ev.no}">running</span></span>
    </div>
    <div class="step-body">
      ${ev.explain ? `<div class="explain">${esc(ev.explain)}</div>` : ''}
      <details><summary>Program</summary><pre>${esc(ev.code)}</pre></details>
      <details open><summary>Output</summary><pre id="step-out-${ev.no}"></pre><pre class="err hidden" id="step-err-${ev.no}"></pre></details>
      <div class="artifacts" id="step-art-${ev.no}"></div>
    </div>`;
  $('#steps').appendChild(card);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function finishStepCard(ev) {
  const pill = $(`#step-state-${ev.no}`);
  if (pill) {
    const bad = ev.exit_code !== 0 || ev.timed_out || ev.oom;
    pill.textContent = ev.timed_out ? 'timeout' : ev.oom ? 'oom-killed'
      : `exit ${ev.exit_code}`;
    pill.className = 'pill ' + (bad ? 'bad' : 'ok');
  }
  const meta = document.querySelector(`#step-${ev.no} .step-meta`);
  if (meta) {
    meta.appendChild(el('span', 'pill', fmtMs(ev.duration_ms)));
    meta.appendChild(el('span', 'pill', esc(ev.runtime)));
    if (ev.killed_reason && ev.killed_reason.length) {
      meta.appendChild(el('span', 'pill warn', `killed: ${esc(ev.killed_reason.join(','))}`));
    }
  }
  const out = $(`#step-out-${ev.no}`);
  if (out) out.textContent = ev.stdout || '(no stdout)';
  if (ev.stderr) {
    const err = $(`#step-err-${ev.no}`);
    if (err) { err.classList.remove('hidden'); err.textContent = ev.stderr; }
  }
  const arts = $(`#step-art-${ev.no}`);
  if (arts && ev.artifacts) {
    ev.artifacts.forEach((a) => {
      const link = el('a', 'artifact', `${esc(a.name)} · ${(a.bytes / 1024).toFixed(1)} KB`);
      link.href = `/api/runs/${state.runId}/artifacts/step-${ev.no}/${a.name}`;
      arts.appendChild(link);
    });
  }
}

function setVerifyPill(ok, reason) {
  const pill = $('#verify-pill');
  if (!pill) return;
  pill.textContent = ok === true ? 'audited: supported' : ok === false ? 'audited: unsupported' : 'audited: unknown';
  pill.className = 'pill ' + (ok === true ? 'ok' : ok === false ? 'bad' : 'warn');
  pill.title = reason || '';
}

function finishRun(ev, kind) {
  setLive(false);
  if (state.es) { state.es.close(); state.es = null; }
  setRunState(ev.status, ev.status);
  if (kind === 'probe' || ev.containment) {
    if (ev.containment) showProbeSummary(ev.containment);
    return;
  }
  const box = $('#answer-box');
  box.classList.remove('hidden');
  $('#answer-body').textContent = ev.answer || '(no answer)';
  const meta = [`model ${ev.model || '—'}`];
  if (ev.verification) meta.push(`audit: ${ev.verification}`);
  $('#answer-meta').textContent = meta.join(' · ');
  setVerifyPill(ev.verified, ev.verification);
  loadConfig();
}

/* ---------------- containment lab ---------------- */
function wireLab() {
  $('#probe-btn').addEventListener('click', runProbes);
}

async function runProbes() {
  const btn = $('#probe-btn');
  btn.disabled = true; btn.textContent = 'Running probes…';
  $('#probe-grid').innerHTML = '';
  $('#probe-summary').classList.add('hidden');
  try {
    const res = await fetch('/api/containment/runs', { method: 'POST' });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || 'failed');
    state.runId = body.run_id; state.kind = 'probe';
    setLive(true);
    attach(body.run_id, 'probe');
  } catch (e) {
    alert(`Probe suite failed: ${e.message}`);
  } finally {
    btn.disabled = false; btn.textContent = 'Run all 7 probes';
  }
}

function addProbeCard(ev) {
  const card = el('div', 'probe');
  card.id = `probe-${ev.id}`;
  card.innerHTML = `
    <div class="probe-top">
      <h3>${esc(ev.title)}</h3>
      <span class="pill" id="probe-pill-${ev.id}">running</span>
    </div>
    <p class="attack">${esc(ev.attack)}</p>
    <p class="expect">expected: ${esc(ev.expect)}</p>
    <div class="evidence" id="probe-ev-${ev.id}">…</div>
    <details><summary>Raw output</summary><pre id="probe-out-${ev.id}"></pre></details>`;
  $('#probe-grid').appendChild(card);
}

function finishProbeCard(ev) {
  const pill = $(`#probe-pill-${ev.id}`);
  if (pill) {
    pill.textContent = ev.blocked ? 'contained' : 'BREACHED';
    pill.className = 'pill ' + (ev.blocked ? 'ok' : 'bad');
  }
  const card = $(`#probe-${ev.id}`);
  if (card && !ev.blocked) card.classList.add('failed');
  const evBox = $(`#probe-ev-${ev.id}`);
  if (evBox) evBox.textContent = ev.evidence;
  const out = $(`#probe-out-${ev.id}`);
  if (out) out.textContent = `exit=${ev.exit_code} · ${fmtMs(ev.duration_ms)} · killed=${(ev.killed_reason || []).join(',') || 'none'}\n${ev.stdout || ''}\n${ev.stderr || ''}`;
}

function showProbeSummary(sum) {
  const box = $('#probe-summary');
  if (!box) return;
  box.classList.remove('hidden');
  box.classList.toggle('bad', !sum.all_blocked);
  box.textContent = sum.all_blocked
    ? `CONTAINMENT VERIFIED — ${sum.contained}/${sum.total} attacks absorbed by the sandbox boundary.`
    : `ATTENTION — ${sum.contained}/${sum.total} attacks contained; inspect the breached probes below.`;
}

/* ---------------- ledger ---------------- */
function wireLedger() {
  $('#refresh-ledger').addEventListener('click', loadLedger);
}

async function loadLedger() {
  try {
    const runs = await (await fetch('/api/runs?limit=30')).json();
    const body = $('#ledger tbody');
    body.innerHTML = '';
    if (!runs.length) {
      body.innerHTML = '<tr><td colspan="7" class="empty">No runs yet.</td></tr>';
      return;
    }
    runs.forEach((r) => {
      const tr = el('tr');
      tr.innerHTML = `
        <td class="mono">${esc(r.id)}</td>
        <td class="mono">${esc(r.kind)}</td>
        <td class="truncate">${esc(r.task)}</td>
        <td class="mono">${r.step_count}</td>
        <td>${r.verified == null ? '<span class="pill">—</span>'
          : r.verified ? '<span class="pill ok">verified</span>'
          : '<span class="pill warn">unverified</span>'}</td>
        <td><span class="pill ${r.status === 'done' ? 'ok' : r.status === 'error' ? 'bad' : 'warn'}">${esc(r.status)}</span></td>
        <td class="mono">${esc(fmtTime(r.started_at))}</td>`;
      tr.addEventListener('click', () => openRun(r.id));
      body.appendChild(tr);
    });
  } catch (e) { /* ignore */ }
}

async function openRun(runId) {
  const run = await (await fetch(`/api/runs/${runId}`)).json();
  if (run.kind === 'probe') {
    switchView('lab');
    $('#probe-grid').innerHTML = '';
    $('#probe-summary').classList.add('hidden');
    (run.probes || []).forEach((p) => {
      const payload = p.payload || { id: p.probe_id, title: p.title, attack: '', expect: '', blocked: p.blocked, evidence: p.evidence, exit_code: null, duration_ms: 0, killed_reason: [], stdout: '', stderr: '', runtime: '' };
      addProbeCard(payload);
      finishProbeCard(payload);
    });
    if (run.payload) showProbeSummary({ contained: run.payload.contained, total: run.payload.total, all_blocked: run.payload.all_blocked });
    return;
  }
  switchView('console');
  state.runId = run.id;
  $('#run-panel').classList.remove('hidden');
  $('#steps').innerHTML = '';
  $('#answer-box').classList.add('hidden');
  $('#run-id').textContent = run.id;
  $('#run-task').textContent = run.task;
  setRunState(run.status, run.status);
  (run.steps || []).forEach((s) => {
    addStepCard(s);
    finishStepCard(s);
  });
  if (run.answer != null) {
    $('#answer-box').classList.remove('hidden');
    $('#answer-body').textContent = run.answer || '(no answer)';
    $('#answer-meta').textContent = `model ${run.model || '—'}${run.verification ? ' · audit: ' + run.verification : ''}`;
    setVerifyPill(run.verified, run.verification);
  }
}

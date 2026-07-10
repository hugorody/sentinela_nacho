'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (path, opts) => (await fetch(path, opts)).json();

let STATE = { running: false };
let VIEW = 'cameras';
let EV_FILTER = 'all';
let camStreamsMounted = new Set();

function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast ' + kind; t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 3200);
}

function fmtTime(ts) { try { return new Date(ts).toLocaleTimeString('pt-BR'); } catch { return ts; } }
function fmtDateTime(ts) { try { return new Date(ts).toLocaleString('pt-BR'); } catch { return ts; } }

/* ---------- Navegação ---------- */
function setView(v) {
  VIEW = v;
  $$('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === v));
  $$('.view').forEach(s => s.hidden = (s.id !== 'view-' + v));
  $('#viewTitle').textContent = { cameras: 'Câmeras', faces: 'Rostos', events: 'Eventos', network: 'Minha rede' }[v];
  if (v === 'faces') loadFaces();
  if (v === 'events') loadEvents();
  if (v === 'network') loadNetwork();
}
$$('.nav-item').forEach(n => n.onclick = () => setView(n.dataset.view));

/* ---------- Status / motor ---------- */
async function refreshStatus() {
  const s = await api('/api/status');
  STATE = s;
  $('#engineDot').className = 'dot ' + (s.running ? 'on' : 'off');
  $('#engineState').textContent = s.running ? 'Streams ativos' : 'Parado';
  const hw = s.face_enabled ? ('IA: ' + (s.using_gpu ? 'GPU' : 'CPU') + ' · ' + s.known_people + ' pessoa(s)') : 'IA off';
  $('#engineHw').textContent = hw;
  $('#btnToggle').textContent = s.running ? 'Parar streams' : 'Iniciar streams';
  $('#btnToggle').classList.toggle('danger', s.running);
  $('#btnToggle').classList.toggle('primary', !s.running);
  renderCameras(s.cameras);
}

$('#btnToggle').onclick = async () => {
  $('#btnToggle').disabled = true;
  const s = await api(STATE.running ? '/api/stop' : '/api/start', { method: 'POST' });
  $('#btnToggle').disabled = false;
  camStreamsMounted.clear();
  await refreshStatus();
  toast(s.running ? 'Streams iniciados' : 'Streams parados', 'ok');
};

$('#btnDiscover').onclick = async () => {
  const b = $('#btnDiscover'); const old = b.textContent;
  b.disabled = true; b.textContent = 'Procurando…';
  try {
    const r = await api('/api/discover', { method: 'POST' });
    toast(r.added.length ? ('Adicionada(s): ' + r.added.map(c => c.name).join(', ')) : 'Nenhuma câmera nova encontrada', r.added.length ? 'ok' : '');
    await refreshStatus();
  } catch (e) { toast('Falha na descoberta', 'err'); }
  b.disabled = false; b.textContent = old;
};

/* ---------- Câmeras ---------- */
function renderCameras(cams) {
  const grid = $('#camGrid');
  $('#camEmpty').hidden = cams.length > 0;
  // Reconstrói os cards só quando a LISTA muda (evita reiniciar streams MJPEG).
  const sig = cams.map(c => c.id).join('|');
  if (grid.dataset.sig !== sig) {
    grid.dataset.sig = sig;
    grid.innerHTML = '';
    for (const c of cams) {
      const card = document.createElement('div');
      card.className = 'cam-card'; card.dataset.cam = c.id;
      card.innerHTML = `
        <div class="cam-video">
          <img src="/video/${c.id}" alt="${c.name}">
          <div class="cam-live"></div>
        </div>
        <div class="cam-foot">
          <div>
            <div class="cam-name"></div>
            <div class="cam-meta"></div>
          </div>
          <div class="cam-actions">
            <button class="rec-btn" title="Gravar">● REC</button>
            <span class="pill">${c.status}</span>
          </div>
        </div>`;
      const nameEl = card.querySelector('.cam-name');
      nameEl.textContent = c.name;
      nameEl.title = 'Clique para renomear';
      nameEl.onclick = () => editCamName(c.id, nameEl);
      card.querySelector('.cam-video').onclick = () => openLightbox(c.id);
      card.querySelector('.rec-btn').onclick = () => toggleRecord(c.id);
      grid.appendChild(card);
    }
  }
  // Atualiza status/live dinamicamente, sem remontar o vídeo.
  for (const c of cams) {
    const card = grid.querySelector(`[data-cam="${c.id}"]`);
    if (!card) continue;
    const online = STATE.running && c.status === 'online';
    const pill = card.querySelector('.pill');
    pill.className = 'pill ' + c.status; pill.textContent = c.status;
    card.querySelector('.cam-live').innerHTML =
      `<span class="dot ${online ? 'on' : 'off'}"></span>${online ? 'AO VIVO' : (c.status || 'parada')}`;
    card.querySelector('.cam-meta').textContent = `${c.ip || ''} · ${c.faces} rosto(s)`;
    const rec = card.querySelector('.rec-btn');
    // 'record' = pedido de gravacao; 'recording' = escrevendo em disco agora.
    rec.classList.toggle('armed', !!c.record);
    rec.classList.toggle('live', !!c.recording);
    rec.textContent = c.recording ? '● GRAVANDO' : (c.record ? '● …' : '● REC');
  }
}

async function toggleRecord(cid) {
  try {
    const r = await api('/api/record', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: cid }),
    });
    if (r.status) refreshStatus();
  } catch { toast('Falha ao alternar gravação', 'err'); }
}

/* ---------- Câmera ampliada (lightbox) ---------- */
function openLightbox(cid) {
  const cam = (STATE.cameras || []).find(c => c.id === cid);
  $('#lbName').textContent = cam ? cam.name : '';
  $('#lbImg').src = '/video/' + cid + '?lb=' + Date.now();
  $('#lightbox').hidden = false;
}
function closeLightbox() {
  $('#lightbox').hidden = true;
  $('#lbImg').src = '';  // encerra a conexão MJPEG extra
}
$('#lbClose').onclick = closeLightbox;
$('#lightbox').onclick = (e) => { if (e.target.id === 'lightbox') closeLightbox(); };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeLightbox(); });

/* ---------- Renomear câmera ---------- */
function editCamName(cid, el) {
  const cur = el.textContent;
  const input = document.createElement('input');
  input.className = 'cam-name-input';
  input.value = cur;
  el.replaceChildren(input);
  input.focus(); input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    const v = input.value.trim();
    const changed = save && v && v !== cur;
    el.textContent = changed ? v : cur;
    el.onclick = () => editCamName(cid, el);
    if (changed) {
      try {
        await api('/api/rename_camera', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: cid, name: v })
        });
        toast('Câmera renomeada para "' + v + '"', 'ok');
        refreshStatus();
      } catch (e) { toast('Falha ao renomear', 'err'); }
    }
  };
  input.onclick = (e) => e.stopPropagation();
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}

/* ---------- Adicionar câmera ---------- */
$('#btnAdd').onclick = () => { $('#modalAdd').hidden = false; $('#addUrl').focus(); };
$('#addCancel').onclick = () => { $('#modalAdd').hidden = true; };
$('#addConfirm').onclick = async () => {
  const url = $('#addUrl').value.trim();
  if (!url) return;
  const r = await api('/api/add_camera', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, name: $('#addName').value.trim() })
  });
  $('#modalAdd').hidden = true; $('#addUrl').value = ''; $('#addName').value = '';
  toast(r.ok ? 'Câmera adicionada' : (r.error || 'Falha'), r.ok ? 'ok' : 'err');
  refreshStatus();
};

/* ---------- Rostos ---------- */
let FACE_FILTER = null;
async function loadFaces() {
  const d = await api('/api/faces');
  const recs = d.records, people = d.people;
  $('#faceEmpty').hidden = recs.length > 0;

  const chips = $('#peopleChips'); chips.innerHTML = '';
  const mk = (label, key, count) => {
    const c = document.createElement('span');
    c.className = 'chip' + (FACE_FILTER === key ? ' active' : '');
    c.textContent = label + (count != null ? ` (${count})` : '');
    c.onclick = () => { FACE_FILTER = FACE_FILTER === key ? null : key; loadFaces(); };
    return c;
  };
  chips.appendChild(mk('Todos', null, recs.length));
  chips.appendChild(mk('Sem nome', '__none__', recs.filter(r => !r.name).length));
  for (const [n, c] of Object.entries(people)) chips.appendChild(mk(n, n, c));
  renderFaces(recs);
}

function renderFaces(recs) {
  let list = recs;
  if (FACE_FILTER === '__none__') list = recs.filter(r => !r.name);
  else if (FACE_FILTER) list = recs.filter(r => r.name === FACE_FILTER);
  const grid = $('#faceGrid'); grid.innerHTML = '';
  for (const r of list) {
    const card = document.createElement('div');
    card.className = 'face-card' + (r.name ? ' named' : '');
    card.innerHTML = `
      <img loading="lazy" src="/media/${encodeURI(r.file)}" alt="rosto">
      <div class="face-meta"><b>${r.camera || ''}</b><br>${fmtDateTime(r.ts)} · ${r.score ?? ''}</div>
      <div class="face-row">
        <input placeholder="nome…" value="${(r.name || '').replace(/"/g, '&quot;')}">
        <button>OK</button>
      </div>`;
    const input = card.querySelector('input');
    const save = async () => {
      await api('/api/label', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: r.file, name: input.value.trim() }) });
      loadFaces();
    };
    card.querySelector('button').onclick = save;
    input.addEventListener('keydown', e => { if (e.key === 'Enter') save(); });
    grid.appendChild(card);
  }
}

/* ---------- Modo Fotos / Grupos ---------- */
let FACE_MODE = 'fotos';
$$('#view-faces .chip.mode').forEach(c => c.onclick = () => {
  $$('#view-faces .chip.mode').forEach(x => x.classList.remove('active'));
  c.classList.add('active');
  FACE_MODE = c.dataset.mode;
  $('#facesMode').hidden = FACE_MODE !== 'fotos';
  $('#clustersMode').hidden = FACE_MODE !== 'grupos';
  if (FACE_MODE === 'grupos') loadClusters();
});

async function loadClusters() {
  const grid = $('#clusterGrid');
  grid.innerHTML = '<div class="muted">agrupando…</div>';
  let clusters = [];
  try { clusters = (await api('/api/clusters')).clusters || []; }
  catch (e) { grid.innerHTML = ''; toast('Falha ao agrupar', 'err'); return; }
  $('#clusterEmpty').hidden = clusters.length > 0;
  grid.innerHTML = '';
  for (const c of clusters) {
    const card = document.createElement('div');
    card.className = 'cluster-card' + (c.confirmed ? '' : ' unnamed');
    const thumbs = c.thumbs.map(f => `<img loading="lazy" src="/media/${encodeURI(f)}" alt="">`).join('');
    const more = c.size > c.thumbs.length ? `<div class="more">+${c.size - c.thumbs.length}</div>` : '';
    const val = (c.confirmed ? c.name : c.suggested) || '';
    card.innerHTML = `
      <div class="cluster-thumbs">${thumbs}${more}</div>
      <div class="cluster-body">
        <div class="cluster-head">
          <span class="cluster-count">${c.size} rosto(s)</span>
          <span class="tag ${c.confirmed ? 'confirmed' : 'unnamed'}">${c.confirmed ? c.name : (c.suggested ? 'talvez ' + c.suggested : 'sem nome')}</span>
        </div>
        <div class="cluster-cams">${c.cameras.join(' · ')}</div>
        <div class="cluster-row">
          <input class="${!c.confirmed && c.suggested ? 'suggest' : ''}" placeholder="nome do grupo…" value="${val.replace(/"/g, '&quot;')}">
          <button>Nomear</button>
        </div>
      </div>`;
    const input = card.querySelector('input');
    const save = async () => {
      const name = input.value.trim();
      if (!name) return;
      await api('/api/label_bulk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ files: c.files, name })
      });
      toast(`${c.size} rosto(s) nomeado(s) como "${name}"`, 'ok');
      loadClusters();
    };
    card.querySelector('button').onclick = save;
    input.addEventListener('keydown', e => { if (e.key === 'Enter') save(); });
    grid.appendChild(card);
  }
}

$('#btnEnroll').onclick = async () => {
  const b = $('#btnEnroll'); const old = b.textContent;
  b.disabled = true; b.textContent = 'Treinando…';
  try {
    const r = await api('/api/enroll', { method: 'POST' });
    if (r.ok) {
      const sq = (r.skipped_quality || []).length;
      let msg = `Reconhecimento atualizado: ${r.total} amostra(s), ${Object.keys(r.people || {}).length} pessoa(s)`;
      if (sq) msg += ` · ${sq} ignorada(s) por baixa qualidade`;
      toast(msg, 'ok'); refreshStatus();
    } else toast(r.error || 'Falha', 'err');
  } catch (e) { toast('Falha ao treinar', 'err'); }
  b.disabled = false; b.textContent = old;
};

/* ---------- Eventos ---------- */
$$('#view-events .chip').forEach(c => c.onclick = () => {
  $$('#view-events .chip').forEach(x => x.classList.remove('active'));
  c.classList.add('active'); EV_FILTER = c.dataset.filter; loadEvents();
});

async function loadEvents() {
  const q = EV_FILTER === 'known' ? '?known=1' : '';
  const d = await api('/api/events' + q);
  const evs = d.events;
  $('#evEmpty').hidden = evs.length > 0;
  $('#evCount').textContent = evs.length + ' evento(s)';
  const tl = $('#timeline'); tl.innerHTML = '';
  for (const e of evs) {
    const known = e.known;
    const row = document.createElement('div');
    row.className = 'ev ' + (known ? 'known' : 'unknown');
    const who = known ? e.name : 'Desconhecido';
    row.innerHTML = `
      ${e.thumb ? `<img class="ev-thumb" src="/media/${encodeURI(e.thumb)}" alt="">` : `<div class="ev-thumb"></div>`}
      <div class="ev-main">
        <div class="ev-title"><span class="who ${known ? '' : 'unknown'}">${who}</span> na <b>${e.camera}</b></div>
        <div class="ev-sub">${fmtDateTime(e.ts)}${known ? ' · similaridade ' + e.score : ''}</div>
      </div>
      <div class="ev-time">${fmtTime(e.ts)}</div>`;
    tl.appendChild(row);
  }
}

/* ---------- Minha rede ---------- */
const STATE_LABEL = { REACHABLE: 'ativo', STALE: 'ocioso', DELAY: 'ocioso', PROBE: 'ocioso', FAILED: 'sem resposta' };

function netTag(d) {
  if (d.is_gateway) return '<span class="net-badge router">roteador</span>';
  if (d.is_camera) return '<span class="net-badge cam">câmera</span>';
  if (d.is_self) return '<span class="net-badge self">este PC</span>';
  return '';
}

async function loadNetwork() {
  $('#netLoading').hidden = false;
  $('#netEmpty').hidden = true;
  $('#netTable').hidden = true;
  let devs = [];
  try { devs = (await api('/api/network')).devices || []; }
  catch { toast('Falha ao escanear a rede', 'err'); }
  $('#netLoading').hidden = true;
  $('#netCount').textContent = devs.length + ' dispositivo(s) na rede';
  $('#netEmpty').hidden = devs.length > 0;
  $('#netTable').hidden = devs.length === 0;
  const body = $('#netBody'); body.innerHTML = '';
  for (const d of devs) {
    const name = d.hostname || d.vendor || '—';
    const tr = document.createElement('tr');
    if (d.is_camera) tr.className = 'is-cam';
    tr.innerHTML = `
      <td><b>${name}</b> ${netTag(d)}</td>
      <td class="mono">${d.ip}</td>
      <td class="mono muted">${d.mac || '—'}</td>
      <td>${d.vendor || '—'}</td>
      <td><span class="net-state ${d.state === 'REACHABLE' ? 'on' : ''}">${STATE_LABEL[d.state] || d.state.toLowerCase()}</span></td>`;
    body.appendChild(tr);
  }
}
$('#btnNetScan').onclick = loadNetwork;

/* ---------- Loops de atualização ---------- */
async function updateBadge() {
  try {
    const d = await api('/api/events');
    const b = $('#evBadge');
    if (d.events.length) { b.hidden = false; b.textContent = d.events.length; }
  } catch {}
}

refreshStatus();
updateBadge();
setInterval(refreshStatus, 4000);
setInterval(updateBadge, 6000);
setInterval(() => { if (VIEW === 'events') loadEvents(); }, 5000);

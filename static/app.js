'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (path, opts) => (await fetch(path, opts)).json();
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

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
  $('#viewTitle').textContent = { cameras: 'Câmeras', faces: 'Rostos', events: 'Eventos', recordings: 'Gravações', smarthome: 'Smart home', network: 'Minha rede' }[v];
  if (v === 'faces') loadFaces();
  if (v === 'events') loadEvents();
  if (v === 'recordings') loadRecordings();
  if (v === 'smarthome') loadSmartHome();
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
    // O backend responde {ok:true, status:{...}} em caso de sucesso, ou
    // {error:"..."} com HTTP != 2xx (ex.: camera fora de sincronia). Sem
    // este tratamento, um erro do servidor nao dava nenhum retorno na tela.
    if (!r.ok) throw new Error(r.error || 'resposta invalida');
    await refreshStatus();
  } catch (e) { toast('Falha ao alternar gravação' + (e.message ? ': ' + e.message : ''), 'err'); }
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

/* ---------- Gravações ---------- */
function fmtSize(bytes) {
  if (!bytes) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n < 10 && i > 0 ? 1 : 0) + ' ' + u[i];
}

async function loadRecordings() {
  let recs = [];
  try { recs = (await api('/api/recordings')).recordings || []; }
  catch { toast('Falha ao listar gravações', 'err'); }
  $('#recCount').textContent = recs.length + ' gravação(ões)';
  $('#recEmpty').hidden = recs.length > 0;
  const grid = $('#recGrid'); grid.innerHTML = '';
  for (const r of recs) {
    const card = document.createElement('div');
    card.className = 'cam-card';
    const when = r.started ? fmtDateTime(r.started) : fmtDateTime(r.mtime * 1000);
    card.innerHTML = `
      <video class="rec-video" controls preload="metadata" src="/gravacoes/${encodeURIComponent(r.file)}"></video>
      <div class="cam-foot">
        <div>
          <div class="cam-name" style="cursor:default">${esc(r.camera)}</div>
          <div class="cam-meta">${when}</div>
        </div>
        <div class="cam-meta">${fmtSize(r.size)}</div>
      </div>`;
    grid.appendChild(card);
  }
}
$('#btnRecRefresh').onclick = loadRecordings;

/* ---------- Smart home (dispositivos Tuya) ---------- */
let SH_LOADING = false;
let SH_DEVICES = [];  // última lista carregada (usada por "acender/apagar tudo")

const SH_KIND_ICON = { switch: '⏻', light: '💡', sensor: '🚪', hub: '⧉' };
// Nome padrão de uma tecla quando o usuário ainda não a renomeou.
function chDefault(code, total, kind) {
  if (total <= 1) return kind === 'light' ? 'Luz' : 'Ligar';
  const n = code.match(/switch_(\d+)/);
  return n ? 'Tecla ' + n[1] : code;
}
// Nome exibido: o rótulo salvo pelo usuário, ou o padrão.
function chName(d, code, total) {
  return (d.labels && d.labels[code]) || chDefault(code, total, d.kind);
}

function renderSmartHome(devs) {
  SH_DEVICES = devs;
  const controllable = devs.filter(d => d.controllable);
  $('#shCount').textContent = devs.length + ' dispositivo(s) · '
    + controllable.length + ' controlável(is)';
  $('#shEmpty').hidden = devs.length > 0;
  const grid = $('#shGrid'); grid.innerHTML = '';
  for (const d of devs) {
    const card = document.createElement('div');
    card.className = 'sh-card' + (d.controllable ? '' : ' readonly');
    card.dataset.id = d.id;
    card.innerHTML = `
      <div class="sh-head">
        <span class="sh-ic">${SH_KIND_ICON[d.kind] || '•'}</span>
        <div class="sh-title">
          <div class="sh-name" title="Clique para renomear">${esc(d.name || '…' + d.id.slice(-4))}</div>
          <div class="sh-sub muted">${d.via === 'lan' ? 'Wi-Fi local' : 'via Hub/nuvem'}
            · <span class="sh-online">—</span></div>
        </div>
      </div>
      <div class="sh-body"><div class="muted sh-loading">carregando…</div></div>`;
    const nameEl = card.querySelector('.sh-name');
    nameEl.onclick = () => editShName(d, nameEl);
    grid.appendChild(card);
    // Busca o estado de cada dispositivo em paralelo (nao bloqueia a lista).
    loadDeviceState(d);
  }
}

async function loadDeviceState(d) {
  const card = $(`#shGrid .sh-card[data-id="${d.id}"]`);
  if (!card) return;
  let st = {};
  try { st = await api('/api/smarthome/state/' + encodeURIComponent(d.id)); }
  catch { st = { online: false, error: 'falha' }; }
  if (VIEW !== 'smarthome') return;
  const onlineEl = card.querySelector('.sh-online');
  onlineEl.textContent = st.online ? 'online' : 'offline';
  onlineEl.className = 'sh-online ' + (st.online ? 'on' : 'off');
  renderDeviceBody(card, d, st);
}

function renderDeviceBody(card, d, st) {
  const body = card.querySelector('.sh-body');
  if (!d.controllable) {
    body.innerHTML = `<div class="muted">${d.kind === 'sensor' ? 'Somente leitura' : 'Sem controle'}</div>`;
    return;
  }
  if (!st.online) {
    body.innerHTML = `<div class="muted">${esc(st.error || 'sem resposta')}</div>`;
    return;
  }
  const switches = st.switches || {};
  const codes = Object.keys(switches).sort();
  body.innerHTML = '';
  // Um toggle por tecla (interruptores de 1..N seções). O nome da tecla é
  // clicável para renomear, no mesmo estilo do nome das câmeras.
  for (const code of codes) {
    const row = document.createElement('div');
    row.className = 'sh-toggle-row';
    const on = !!switches[code];
    row.innerHTML = `
      <span class="sh-toggle-label sh-ch-name" title="Clique para renomear"></span>
      <button class="sh-toggle ${on ? 'on' : ''}" role="switch" aria-checked="${on}">
        <span class="knob"></span>
      </button>`;
    const nameEl = row.querySelector('.sh-ch-name');
    nameEl.textContent = chName(d, code, codes.length);
    nameEl.onclick = () => editChName(d, code, codes.length, nameEl);
    const btn = row.querySelector('.sh-toggle');
    btn.onclick = () => toggleDevice(d, code, !btn.classList.contains('on'), card);
    body.appendChild(row);
  }
  // Slider de brilho, quando a luz reporta brilho.
  if (d.kind === 'light' && st.brightness != null) {
    const row = document.createElement('div');
    row.className = 'sh-bright-row';
    row.innerHTML = `
      <span class="sh-toggle-label">Brilho</span>
      <input class="sh-bright" type="range" min="1" max="100" value="${st.brightness}">
      <span class="sh-bright-val">${st.brightness}%</span>`;
    const slider = row.querySelector('.sh-bright');
    const val = row.querySelector('.sh-bright-val');
    slider.oninput = () => { val.textContent = slider.value + '%'; };
    slider.onchange = () => setBrightness(d, +slider.value);
    body.appendChild(row);
  }
}

async function toggleDevice(d, code, on, card) {
  const btn = card.querySelector(`.sh-toggle`);
  card.classList.add('busy');
  try {
    const r = await api('/api/smarthome/switch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: d.id, code, on })
    });
    if (!r.ok) throw new Error(r.error || 'falha');
    // Relê o estado real (confirma que o aparelho obedeceu).
    await loadDeviceState(d);
  } catch (e) {
    toast('Falha ao alternar ' + (d.name || 'dispositivo') + (e.message ? ': ' + e.message : ''), 'err');
    await loadDeviceState(d);
  }
  card.classList.remove('busy');
}

async function setBrightness(d, pct) {
  try {
    const r = await api('/api/smarthome/brightness', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: d.id, pct })
    });
    if (!r.ok) throw new Error(r.error || 'falha');
    toast('Brilho de "' + (d.name || 'luz') + '" em ' + pct + '%', 'ok');
  } catch (e) { toast('Falha ao ajustar brilho', 'err'); }
}

// Renomeia uma tecla individual (ex.: "Tecla 1" -> "Pia"), no mesmo padrão
// do nome das câmeras: clica, edita, Enter salva / Esc cancela.
function editChName(d, code, total, el) {
  const cur = (d.labels && d.labels[code]) || '';
  const fallback = chDefault(code, total, d.kind);
  const input = document.createElement('input');
  input.className = 'cam-name-input';
  input.value = cur;
  input.placeholder = fallback;
  el.replaceChildren(input);
  input.focus(); input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    const v = input.value.trim();
    const changed = save && v !== cur;
    el.textContent = (changed ? v : cur) || fallback;
    el.onclick = () => editChName(d, code, total, el);
    if (changed) {
      try {
        await api('/api/smarthome/label', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: d.id, code, name: v })
        });
        d.labels = d.labels || {};
        if (v) d.labels[code] = v; else delete d.labels[code];
        toast(v ? 'Tecla renomeada para "' + v + '"' : 'Nome da tecla removido', 'ok');
      } catch (e) { toast('Falha ao renomear tecla', 'err'); }
    }
  };
  input.onclick = (e) => e.stopPropagation();
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}

function editShName(d, el) {
  const cur = d.name || '';
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
    el.textContent = changed ? v : (cur || '…' + d.id.slice(-4));
    el.onclick = () => editShName(d, el);
    if (changed) {
      try {
        await api('/api/smarthome/rename', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: d.id, name: v })
        });
        d.name = v;
        toast('Dispositivo renomeado para "' + v + '"', 'ok');
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

async function loadSmartHome() {
  if (SH_LOADING) return;
  SH_LOADING = true;
  $('#shLoading').hidden = false;
  $('#shEmpty').hidden = true;
  $('#shNotConfigured').hidden = true;
  let data = { configured: false, devices: [] };
  try { data = await api('/api/smarthome/devices'); }
  catch { toast('Falha ao carregar smart home', 'err'); }
  $('#shLoading').hidden = true;
  SH_LOADING = false;
  if (VIEW !== 'smarthome') return;
  if (!data.configured) {
    $('#shNotConfigured').hidden = false;
    $('#shGrid').innerHTML = '';
    $('#shCount').textContent = 'controle não configurado';
    return;
  }
  renderSmartHome(data.devices || []);
}
$('#btnShScan').onclick = loadSmartHome;

async function setAllLights(on) {
  const btns = [$('#btnShAllOn'), $('#btnShAllOff')];
  btns.forEach(b => b.disabled = true);
  try {
    const r = await api('/api/smarthome/all', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on })
    });
    let msg = (on ? 'Acendendo' : 'Apagando') + ` ${r.switched} tecla(s)`;
    if (r.offline) msg += ` · ${r.offline} offline`;
    if (r.failed) msg += ` · ${r.failed} falha(s)`;
    toast(msg, r.failed ? 'err' : 'ok');
  } catch (e) {
    toast('Falha ao ' + (on ? 'acender' : 'apagar') + ' tudo', 'err');
  }
  btns.forEach(b => b.disabled = false);
  // Reflete o novo estado de cada aparelho nos cards.
  if (VIEW === 'smarthome') $$('#shGrid .sh-card').forEach(card => {
    const id = card.dataset.id;
    const d = SH_DEVICES.find(x => x.id === id);
    if (d && d.controllable) loadDeviceState(d);
  });
}
$('#btnShAllOn').onclick = () => setAllLights(true);
$('#btnShAllOff').onclick = () => setAllLights(false);

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
    const name = esc(d.hostname || d.advert || d.vendor || '—');
    const svc = (d.services || []).length
      ? (d.services || []).map(s => `<span class="svc-tag">${esc(s)}</span>`).join(' ')
      : '<span class="muted">—</span>';
    const tr = document.createElement('tr');
    if (d.is_camera) tr.className = 'is-cam';
    tr.innerHTML = `
      <td><b>${name}</b> ${netTag(d)}</td>
      <td class="mono">${esc(d.ip)}</td>
      <td class="mono muted">${esc(d.mac || '—')}</td>
      <td>${esc(d.vendor || '—')}</td>
      <td class="svc-cell">${svc}</td>
      <td><span class="net-state ${d.state === 'REACHABLE' ? 'on' : ''}">${STATE_LABEL[d.state] || esc(d.state.toLowerCase())}</span></td>`;
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

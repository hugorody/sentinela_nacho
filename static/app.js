'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (path, opts) => (await fetch(path, opts)).json();
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let STATE = { running: false };
let VIEW = 'cameras';
let EV_FILTER = 'all';

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
  $('#viewTitle').textContent = { cameras: 'Câmeras', faces: 'Rostos', events: 'Eventos', recordings: 'Gravações', alarms: 'Alarmes', scenes: 'Cenas', smarthome: 'Smart home', network: 'Minha rede', settings: 'Configurações' }[v];
  if (v === 'faces') loadFaces();
  if (v === 'events') loadEvents();
  if (v === 'recordings') loadRecordings();
  if (v === 'alarms') loadAlarms();
  if (v === 'scenes') loadScenes();
  if (v === 'smarthome') loadSmartHome();
  if (v === 'network') loadNetwork();
  if (v === 'settings') loadSettings();
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
  // refreshStatus() -> renderCameras() (re)conecta os streams conforme o novo
  // estado (cada camera online religa sua <img> automaticamente).
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
// Conecta (ou reconecta) o stream MJPEG de uma <img>. A URL leva um timestamp
// para evitar reuso de conexao/cache. Se a conexao cair (onerror), re-tenta uma
// vez, com um pequeno atraso, enquanto a camera continuar marcada como ativa.
function mountStream(img, cid) {
  img.dataset.streaming = cid;
  const connect = () => { img.src = `/video/${cid}?t=${Date.now()}`; };
  img.onerror = () => {
    if (img.dataset.streaming !== cid) return;  // camera saiu do ar; nao insiste
    clearTimeout(img._retry);
    img._retry = setTimeout(connect, 1500);
  };
  connect();
}

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
          <img alt="${c.name}">
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
    // (Re)conecta o stream MJPEG quando a camera esta online e a <img> ainda
    // nao esta transmitindo. Cada (re)conexao usa uma URL nova (cache-buster)
    // para forcar uma conexao fresca -- senao a imagem pode ficar presa no
    // placeholder de quando o motor estava parado. Fora do ar: solta o stream.
    const img = card.querySelector('.cam-video img');
    if (online) {
      if (img.dataset.streaming !== c.id) mountStream(img, c.id);
    } else if (img.dataset.streaming) {
      img.removeAttribute('src');
      delete img.dataset.streaming;
    }
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
      const so = (r.skipped_outlier || []).length;
      let msg = `Reconhecimento atualizado: ${r.total} amostra(s), ${Object.keys(r.people || {}).length} pessoa(s)`;
      if (sq) msg += ` · ${sq} ignorada(s) por baixa qualidade`;
      if (so) msg += ` · ${so} inconsistente(s)`;
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
  // "Alarmes" filtra no cliente (é um subconjunto dos eventos de desconhecido).
  const evs = EV_FILTER === 'alarm' ? d.events.filter(e => e.alarm) : d.events;
  $('#evEmpty').hidden = evs.length > 0;
  $('#evCount').textContent = evs.length + ' evento(s)';
  const tl = $('#timeline'); tl.innerHTML = '';
  for (const e of evs) {
    const known = e.known;
    const alarm = !!e.alarm;
    const row = document.createElement('div');
    row.className = 'ev ' + (known ? 'known' : 'unknown') + (alarm ? ' alarm' : '');
    const who = known ? e.name : 'Desconhecido';
    const badge = alarm ? '<span class="ev-alarm-badge">🔔 ALARME · e-mail enviado</span>' : '';
    row.innerHTML = `
      ${e.thumb ? `<img class="ev-thumb" src="/media/${encodeURI(e.thumb)}" alt="">` : `<div class="ev-thumb"></div>`}
      <div class="ev-main">
        <div class="ev-title"><span class="who ${known ? '' : 'unknown'}">${who}</span> na <b>${e.camera}</b> ${badge}</div>
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

/* ---------- Alarmes (e-mail: pessoa não identificada e sensores smart) ---------- */
let ALARM_CFG = { email: '', cameras: {}, devices: {} };
let ALARM_SENSOR_LABELS = {};

async function loadAlarms() {
  let data = { config: { email: '', cameras: {}, devices: {} }, cameras: [], sensors: [] };
  try { data = await api('/api/alarms'); }
  catch { toast('Falha ao carregar alarmes', 'err'); }
  if (VIEW !== 'alarms') return;
  ALARM_CFG = data.config || { email: '', cameras: {}, devices: {} };
  ALARM_CFG.devices = ALARM_CFG.devices || {};
  ALARM_SENSOR_LABELS = data.sensor_labels || {};
  $('#alarmEmail').value = ALARM_CFG.email || '';
  renderAlarms(data.cameras || []);
  renderAlarmDevices(data.sensors || []);
}

function renderAlarms(cams) {
  $('#alarmEmpty').hidden = cams.length > 0;
  const grid = $('#alarmGrid'); grid.innerHTML = '';
  for (const cam of cams) {
    const cfg = ALARM_CFG.cameras[cam.name] || { enabled: false, windows: [], recipients: '' };
    const card = document.createElement('div');
    card.className = 'alarm-card' + (cfg.enabled ? ' on' : '');
    card.dataset.cam = cam.name;
    card.innerHTML = `
      <div class="alarm-head">
        <div class="alarm-title">${esc(cam.name)}</div>
        <button class="sh-toggle ${cfg.enabled ? 'on' : ''}" role="switch" aria-checked="${cfg.enabled}">
          <span class="knob"></span>
        </button>
      </div>
      <div class="alarm-body">
        <label class="alarm-lbl">Horários ativos <span class="muted">(vazio = 24h)</span></label>
        <div class="alarm-windows"></div>
        <button class="btn small alarm-add-win">+ Adicionar horário</button>
        <label class="alarm-lbl">Destinatário desta câmera <span class="muted">(opcional)</span></label>
        <input class="alarm-recip" type="text" placeholder="usa o e-mail padrão se vazio" value="${(cfg.recipients || '').replace(/"/g, '&quot;')}">
        <div class="alarm-actions">
          <button class="btn small alarm-test">Enviar teste</button>
        </div>
      </div>`;

    // Toggle ativar/desativar.
    const toggle = card.querySelector('.sh-toggle');
    toggle.onclick = () => {
      const on = !toggle.classList.contains('on');
      saveAlarmCamera(cam.name, { enabled: on });
      toggle.classList.toggle('on', on);
      card.classList.toggle('on', on);
    };

    // Janelas de horário.
    const winWrap = card.querySelector('.alarm-windows');
    const windows = (cfg.windows || []).map(w => ({ ...w }));
    const renderWindows = () => {
      winWrap.innerHTML = '';
      if (!windows.length) {
        winWrap.innerHTML = '<div class="muted alarm-247">Ativo 24 horas</div>';
      }
      windows.forEach((w, i) => {
        const row = document.createElement('div');
        row.className = 'alarm-win';
        row.innerHTML = `
          <input type="time" class="win-start" value="${esc(w.start || '22:00')}">
          <span>até</span>
          <input type="time" class="win-end" value="${esc(w.end || '06:00')}">
          <button class="win-del" title="Remover">✕</button>`;
        row.querySelector('.win-start').onchange = (e) => { windows[i].start = e.target.value; persistWindows(); };
        row.querySelector('.win-end').onchange = (e) => { windows[i].end = e.target.value; persistWindows(); };
        row.querySelector('.win-del').onclick = () => { windows.splice(i, 1); renderWindows(); persistWindows(); };
        winWrap.appendChild(row);
      });
    };
    const persistWindows = () => saveAlarmCamera(cam.name, { windows });
    card.querySelector('.alarm-add-win').onclick = () => {
      windows.push({ start: '22:00', end: '06:00' });
      renderWindows(); persistWindows();
    };
    renderWindows();

    // Destinatário específico (salva ao sair do campo).
    const recip = card.querySelector('.alarm-recip');
    recip.onchange = () => saveAlarmCamera(cam.name, { recipients: recip.value.trim() });

    // Teste.
    card.querySelector('.alarm-test').onclick = async (e) => {
      const b = e.target; b.disabled = true; b.textContent = 'Enviando…';
      try {
        const r = await api('/api/alarms/test', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ camera: cam.name })
        });
        toast(r.ok ? ('E-mail de teste enviado para ' + (r.recipients || []).join(', ')) : (r.error || 'Falha'), r.ok ? 'ok' : 'err');
      } catch (err) { toast('Falha ao enviar teste', 'err'); }
      b.disabled = false; b.textContent = 'Enviar teste';
    };

    grid.appendChild(card);
  }
}

async function saveAlarmCamera(camera, fields) {
  try {
    const r = await api('/api/alarms/camera', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera, ...fields })
    });
    if (r.config) ALARM_CFG = r.config;
  } catch (e) { toast('Falha ao salvar alarme', 'err'); }
}

/* ---------- Alarmes de dispositivos smart (sensores) ---------- */

// Ordem preferida dos estados de sensor (porta primeiro, como nas Cenas).
const SENSOR_CODE_ORDER = ['doorcontact_state', 'pir', 'presence_state',
  'watersensor_state', 'smoke_sensor_status', 'temper_alarm'];

function sensorCodes() {
  return Object.keys(ALARM_SENSOR_LABELS).sort(
    (a, b) => SENSOR_CODE_ORDER.indexOf(a) - SENSOR_CODE_ORDER.indexOf(b));
}

// Texto de gatilho para um code+direção. Ex.: doorcontact_state/on -> "aberta".
function sensorStateText(code, dir) {
  const spec = ALARM_SENSOR_LABELS[code] || [code, 'ligado', 'desligado'];
  if (dir === 'off') return spec[2];
  if (dir === 'any') return 'mudar de estado';
  return spec[1];
}

function sensorName(code) {
  return (ALARM_SENSOR_LABELS[code] || [code])[0];
}

function renderAlarmDevices(sensors) {
  $('#alarmDevEmpty').hidden = sensors.length > 0;
  const grid = $('#alarmDevGrid'); grid.innerHTML = '';
  const codes = sensorCodes();
  for (const sensor of sensors) {
    // Para cada sensor, o alarme é por (device, code). Achamos a config
    // existente (se houver, respeitando qual code já foi escolhido) ou usamos o
    // primeiro estado disponível como padrão.
    const existingKey = Object.keys(ALARM_CFG.devices)
      .find(k => k.startsWith(sensor.id + '|'));
    let code = existingKey ? existingKey.split('|')[1] : (codes[0] || 'doorcontact_state');
    let cfg = ALARM_CFG.devices[sensor.id + '|' + code] ||
      { enabled: false, windows: [], recipients: '', trigger: 'on' };

    const card = document.createElement('div');
    card.className = 'alarm-card' + (cfg.enabled ? ' on' : '');
    card.innerHTML = `
      <div class="alarm-head">
        <div class="alarm-title">${esc(sensor.name)}</div>
        <button class="sh-toggle ${cfg.enabled ? 'on' : ''}" role="switch" aria-checked="${cfg.enabled}">
          <span class="knob"></span>
        </button>
      </div>
      <div class="alarm-body">
        <label class="alarm-lbl">Avisar quando</label>
        <div class="alarm-trigger">
          <select class="alarm-dev-code"></select>
          <select class="alarm-dev-dir"></select>
        </div>
        <label class="alarm-lbl">Horários ativos <span class="muted">(vazio = 24h)</span></label>
        <div class="alarm-windows"></div>
        <button class="btn small alarm-add-win">+ Adicionar horário</button>
        <label class="alarm-lbl">Destinatário deste sensor <span class="muted">(opcional)</span></label>
        <input class="alarm-recip" type="text" placeholder="usa o e-mail padrão se vazio" value="${(cfg.recipients || '').replace(/"/g, '&quot;')}">
        <div class="alarm-actions">
          <button class="btn small alarm-test">Enviar teste</button>
        </div>
      </div>`;

    // Seletor do estado (code) do sensor.
    const codeSel = card.querySelector('.alarm-dev-code');
    codeSel.innerHTML = codes.map(c =>
      `<option value="${esc(c)}"${c === code ? ' selected' : ''}>${esc(sensorName(c))}</option>`).join('');

    const dirSel = card.querySelector('.alarm-dev-dir');
    const fillDirs = () => {
      dirSel.innerHTML = ['on', 'off', 'any'].map(d =>
        `<option value="${d}"${d === (cfg.trigger || 'on') ? ' selected' : ''}>${esc(sensorStateText(code, d))}</option>`).join('');
    };
    fillDirs();

    const winWrap = card.querySelector('.alarm-windows');
    let windows = (cfg.windows || []).map(w => ({ ...w }));
    const save = (fields) => saveAlarmDevice(sensor.id, code, fields);

    // Ao trocar o estado observado, migramos para a config daquele (device,code).
    codeSel.onchange = () => {
      code = codeSel.value;
      cfg = ALARM_CFG.devices[sensor.id + '|' + code] ||
        { enabled: cfg.enabled, windows, recipients: card.querySelector('.alarm-recip').value.trim(), trigger: 'on' };
      windows = (cfg.windows || []).map(w => ({ ...w }));
      fillDirs();
      renderWindows();
      // Persiste o estado atual sob a nova chave (inclui enabled/trigger/janelas).
      save({ enabled: cfg.enabled, trigger: dirSel.value, windows,
        recipients: card.querySelector('.alarm-recip').value.trim() });
    };
    dirSel.onchange = () => save({ trigger: dirSel.value });

    // Toggle ativar/desativar.
    const toggle = card.querySelector('.sh-toggle');
    toggle.onclick = () => {
      const on = !toggle.classList.contains('on');
      cfg.enabled = on;
      save({ enabled: on, trigger: dirSel.value });
      toggle.classList.toggle('on', on);
      card.classList.toggle('on', on);
    };

    // Janelas de horário.
    const renderWindows = () => {
      winWrap.innerHTML = '';
      if (!windows.length) {
        winWrap.innerHTML = '<div class="muted alarm-247">Ativo 24 horas</div>';
      }
      windows.forEach((w, i) => {
        const row = document.createElement('div');
        row.className = 'alarm-win';
        row.innerHTML = `
          <input type="time" class="win-start" value="${esc(w.start || '22:00')}">
          <span>até</span>
          <input type="time" class="win-end" value="${esc(w.end || '06:00')}">
          <button class="win-del" title="Remover">✕</button>`;
        row.querySelector('.win-start').onchange = (e) => { windows[i].start = e.target.value; save({ windows }); };
        row.querySelector('.win-end').onchange = (e) => { windows[i].end = e.target.value; save({ windows }); };
        row.querySelector('.win-del').onclick = () => { windows.splice(i, 1); renderWindows(); save({ windows }); };
        winWrap.appendChild(row);
      });
    };
    card.querySelector('.alarm-add-win').onclick = () => {
      windows.push({ start: '22:00', end: '06:00' });
      renderWindows(); save({ windows });
    };
    renderWindows();

    // Destinatário específico.
    const recip = card.querySelector('.alarm-recip');
    recip.onchange = () => save({ recipients: recip.value.trim() });

    // Teste.
    card.querySelector('.alarm-test').onclick = async (e) => {
      const b = e.target; b.disabled = true; b.textContent = 'Enviando…';
      try {
        const r = await api('/api/alarms/device_test', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device: sensor.id, code, label: sensor.name })
        });
        toast(r.ok ? ('E-mail de teste enviado para ' + (r.recipients || []).join(', ')) : (r.error || 'Falha'), r.ok ? 'ok' : 'err');
      } catch (err) { toast('Falha ao enviar teste', 'err'); }
      b.disabled = false; b.textContent = 'Enviar teste';
    };

    grid.appendChild(card);
  }
}

async function saveAlarmDevice(device, code, fields) {
  try {
    const r = await api('/api/alarms/device', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device, code, ...fields })
    });
    if (r.config) { ALARM_CFG = r.config; ALARM_CFG.devices = ALARM_CFG.devices || {}; }
  } catch (e) { toast('Falha ao salvar alarme', 'err'); }
}

$('#btnAlarmEmail').onclick = async () => {
  try {
    const r = await api('/api/alarms/email', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: $('#alarmEmail').value.trim() })
    });
    if (r.config) ALARM_CFG = r.config;
    toast('E-mail de destino salvo', 'ok');
  } catch (e) { toast('Falha ao salvar e-mail', 'err'); }
};

/* ---------- Cenas (automações) ---------- */
let SCENE_DATA = { scenes: [], cameras: [], devices: [], smart_ready: false };
const WEEKDAYS = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom'];

async function loadScenes() {
  try { SCENE_DATA = await api('/api/scenes'); }
  catch { toast('Falha ao carregar cenas', 'err'); }
  if (VIEW !== 'scenes') return;
  renderScenes();
}

function devName(id) {
  const d = SCENE_DATA.devices.find(x => x.id === id);
  return d ? (d.name || id) : id;
}

function codeLabel(deviceId, code) {
  // Sensores: nome amigável do estado (ex.: doorcontact_state -> "Porta").
  const sl = SCENE_DATA.sensor_labels && SCENE_DATA.sensor_labels[code];
  if (sl) return sl[0];
  // Interruptores/luzes: nome que o usuário deu à tecla (labels), senão "Tecla N".
  const d = SCENE_DATA.devices.find(x => x.id === deviceId);
  const lbl = d && d.labels && d.labels[code];
  if (lbl) return lbl;
  const n = (code || '').match(/switch_(\d+)/);
  return n ? 'Tecla ' + n[1] : code;
}

// Um dispositivo é sensor?
function isSensor(deviceId) {
  const d = SCENE_DATA.devices.find(x => x.id === deviceId);
  return !!(d && d.kind === 'sensor');
}

function triggerText(t) {
  if (t.type === 'schedule') {
    const days = (t.days && t.days.length) ? t.days.map(d => WEEKDAYS[d]).join(', ') : 'todos os dias';
    return `⏰ Às ${esc(t.time || '--:--')} · ${days}`;
  }
  if (t.type === 'device') {
    const sl = SCENE_DATA.sensor_labels && SCENE_DATA.sensor_labels[t.code];
    if (isSensor(t.device) && sl) {
      // Sensor: "🚪 Porta Serviço · quando ficar aberta / fechada / mudar".
      const txt = { on: 'ficar ' + sl[1], off: 'ficar ' + sl[2], any_change: 'mudar' }[t.state] || 'mudar';
      const icon = t.code === 'doorcontact_state' ? '🚪' : '📡';
      return `${icon} ${esc(devName(t.device))} · ${esc(sl[0])} ao ${esc(txt)}`;
    }
    const st = { on: 'ligar', off: 'desligar', any_change: 'mudar' }[t.state] || 'mudar';
    return `🔌 ${esc(devName(t.device))} · ${esc(codeLabel(t.device, t.code || 'switch_1'))} ao ${st}`;
  }
  const cam = (!t.camera || t.camera === '*') ? 'qualquer câmera' : esc(t.camera);
  const who = { unknown: 'não identificada', known: 'reconhecida', any: 'qualquer pessoa' }[t.event] || 'pessoa';
  const person = (t.person || '').trim();
  return `📷 ${cam}: ${person ? esc(person) : who}`;
}

function actionText(a) {
  if (a.type === 'all') return (a.on ? 'Acender' : 'Apagar') + ' todos os dispositivos';
  if (a.type === 'brightness') return `Brilho de ${esc(devName(a.device))} → ${a.pct}%`;
  if (a.type === 'switch') {
    // Mostra a tecla quando o dispositivo tem mais de uma (ajuda a distinguir).
    const multi = deviceCodes(a.device).length > 1;
    const suffix = multi ? ` · ${esc(codeLabel(a.device, a.code || 'switch_1'))}` : '';
    return `${a.on ? 'Ligar' : 'Desligar'} ${esc(devName(a.device))}${suffix}`;
  }
  return 'ação';
}

function renderScenes() {
  const scenes = SCENE_DATA.scenes || [];
  $('#scenesCount').textContent = scenes.length + ' cena(s)';
  $('#scenesEmpty').hidden = scenes.length > 0;
  const grid = $('#scenesGrid'); grid.innerHTML = '';
  for (const s of scenes) {
    const card = document.createElement('div');
    card.className = 'scene-card' + (s.enabled ? '' : ' off');
    const acts = (s.actions || []).map(a => `<li>${actionText(a)}</li>`).join('');
    card.innerHTML = `
      <div class="scene-head">
        <div class="scene-name">${esc(s.name)}</div>
        <button class="sh-toggle ${s.enabled ? 'on' : ''}" role="switch" aria-checked="${s.enabled}" title="Ativar/desativar">
          <span class="knob"></span>
        </button>
      </div>
      <div class="scene-when">${triggerText(s.trigger || {})}</div>
      <ul class="scene-then">${acts || '<li class="muted">sem ações</li>'}</ul>
      <div class="scene-foot">
        <button class="btn small scene-run">▶ Executar</button>
        <button class="btn small scene-edit">Editar</button>
        <button class="btn small scene-del">Excluir</button>
      </div>`;
    card.querySelector('.sh-toggle').onclick = async (e) => {
      const on = !e.currentTarget.classList.contains('on');
      await api('/api/scenes/enable', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: s.id, enabled: on }) });
      loadScenes();
    };
    card.querySelector('.scene-run').onclick = async (e) => {
      const b = e.target; b.disabled = true;
      try {
        const r = await api('/api/scenes/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: s.id }) });
        toast(r.ok ? `Cena executada: ${r.done}/${r.total} ação(ões)` : (r.error || 'Falha'), r.ok ? 'ok' : 'err');
      } catch { toast('Falha ao executar cena', 'err'); }
      b.disabled = false;
    };
    card.querySelector('.scene-edit').onclick = () => openSceneModal(s);
    card.querySelector('.scene-del').onclick = async () => {
      if (!confirm(`Excluir a cena "${s.name}"?`)) return;
      await api('/api/scenes/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: s.id }) });
      loadScenes();
    };
    grid.appendChild(card);
  }
}

/* ----- Editor de cena (modal) ----- */
let EDITING_SCENE = null;   // cena em edição (null = nova)

function openSceneModal(scene) {
  EDITING_SCENE = scene || null;
  $('#sceneModalTitle').textContent = scene ? 'Editar cena' : 'Nova cena';
  $('#sceneName').value = scene ? scene.name : '';

  // Câmeras no seletor de gatilho.
  const camSel = $('#trigCam');
  camSel.innerHTML = '<option value="*">Qualquer câmera</option>'
    + SCENE_DATA.cameras.map(c => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('');

  // Gatilho por dispositivo: lista os OBSERVÁVEIS (inclui sensores, que não
  // são controláveis mas têm estado que serve de gatilho).
  const devSel = $('#trigDev');
  const obsDevs = SCENE_DATA.devices.filter(d => d.observable || d.controllable);
  devSel.innerHTML = obsDevs.length
    ? obsDevs.map(d => `<option value="${esc(d.id)}">${esc(d.name || d.id)}</option>`).join('')
    : '<option value="">(smart home não configurado)</option>';
  devSel.onchange = () => { fillDeviceCodes(devSel.value); syncDevStateLabels(); };

  // Dias da semana (chips).
  const daysWrap = $('#trigDays'); daysWrap.innerHTML = '';
  WEEKDAYS.forEach((d, i) => {
    const chip = document.createElement('span');
    chip.className = 'day-chip'; chip.dataset.day = i; chip.textContent = d;
    chip.onclick = () => chip.classList.toggle('on');
    daysWrap.appendChild(chip);
  });

  // Preenche a partir da cena (ou padrões).
  const t = (scene && scene.trigger) || { type: 'camera', event: 'unknown', camera: '*' };
  $$('input[name="trigType"]').forEach(r => { r.checked = r.value === (t.type || 'camera'); });
  camSel.value = t.camera || '*';
  $('#trigEvent').value = t.event || 'unknown';
  $('#trigPerson').value = t.person || '';
  $('#trigTime').value = t.time || '18:30';
  (t.days || []).forEach(d => { const c = daysWrap.querySelector(`[data-day="${d}"]`); if (c) c.classList.add('on'); });
  // Gatilho de dispositivo: seleciona o device, preenche as teclas e o estado.
  if (t.type === 'device' && t.device && obsDevs.some(d => d.id === t.device)) {
    devSel.value = t.device;
  }
  fillDeviceCodes(devSel.value, t.code);
  $('#trigDevState').value = t.state || 'on';
  syncTriggerUI();

  // Ações.
  $('#sceneActions').innerHTML = '';
  const acts = (scene && scene.actions) || [];
  if (acts.length) acts.forEach(addActionRow); else addActionRow();

  $('#modalScene').hidden = false;
}

function syncTriggerUI() {
  const type = $$('input[name="trigType"]').find(r => r.checked).value;
  $('#trigCamera').hidden = type !== 'camera';
  $('#trigDevice').hidden = type !== 'device';
  $('#trigSchedule').hidden = type !== 'schedule';
  $('#trigPersonWrap').hidden = !(type === 'camera' && $('#trigEvent').value === 'known');
}
$$('input[name="trigType"]').forEach(r => r.onchange = syncTriggerUI);
$('#trigEvent').onchange = syncTriggerUI;

// Teclas/canais de um dispositivo. Interruptores expõem TODAS as teclas
// (switch_1..6), não só as que o usuário nomeou — senão as teclas sem nome
// ficariam inacessíveis. Os labels só dão nome; qualquer código que apareça
// neles (ex.: switch_6) também é incluído. Luzes têm uma tecla só.
function deviceCodes(deviceId) {
  const d = SCENE_DATA.devices.find(x => x.id === deviceId);
  // Sensores: os estados observáveis conhecidos (porta, movimento, bateria…).
  // Sensor de porta expõe doorcontact_state; deixamos ele em primeiro.
  if (d && d.kind === 'sensor') {
    const known = Object.keys(SCENE_DATA.sensor_labels || {});
    const order = ['doorcontact_state', 'pir', 'presence_state',
      'watersensor_state', 'smoke_sensor_status', 'temper_alarm'];
    return known.sort((a, b) => order.indexOf(a) - order.indexOf(b));
  }
  const labelCodes = (d && d.labels) ? Object.keys(d.labels) : [];
  if (d && d.kind === 'light') {
    return [...new Set(['switch_1', ...labelCodes])].sort();
  }
  const base = Array.from({ length: 6 }, (_, i) => 'switch_' + (i + 1));
  return [...new Set([...base, ...labelCodes])].sort();
}

// Monta o HTML de um <select> de teclas para um dispositivo.
function codeOptions(deviceId, selectedCode) {
  return deviceCodes(deviceId).map(c =>
    `<option value="${esc(c)}"${c === selectedCode ? ' selected' : ''}>${esc(codeLabel(deviceId, c))}</option>`).join('');
}

// Preenche o seletor de teclas/estados do GATILHO de dispositivo.
function fillDeviceCodes(deviceId, selectedCode) {
  const sel = $('#trigDevCode');
  sel.innerHTML = codeOptions(deviceId, selectedCode);
  // Ao trocar a tecla/estado de um sensor, os rótulos "ligar/desligar" mudam.
  sel.onchange = syncDevStateLabels;
  syncDevStateLabels();
}

// Ajusta os textos do seletor "Quando" conforme o tipo. Para sensores usa o
// vocabulário do estado (ex.: "abrir/fechar"); para teclas, "ligar/desligar".
function syncDevStateLabels() {
  const devId = $('#trigDev').value;
  const code = $('#trigDevCode').value;
  const stateSel = $('#trigDevState');
  const cur = stateSel.value;
  const sl = SCENE_DATA.sensor_labels && SCENE_DATA.sensor_labels[code];
  const hintEl = $('#trigDevHint');
  if (isSensor(devId) && sl) {
    // sl = [nome, textoTrue, textoFalse]. Ex.: ["Porta","aberta","fechada"].
    stateSel.options[0].textContent = capitalize(sl[1]);   // on  -> estado True
    stateSel.options[1].textContent = capitalize(sl[2]);   // off -> estado False
    stateSel.options[2].textContent = 'Mudar (' + sl[1] + '/' + sl[2] + ')';
    if (hintEl) hintEl.textContent = 'O estado é verificado a cada ~6 segundos; a cena dispara na mudança.';
  } else {
    stateSel.options[0].textContent = 'Ligar';
    stateSel.options[1].textContent = 'Desligar';
    stateSel.options[2].textContent = 'Mudar (liga ou desliga)';
    if (hintEl) hintEl.textContent = 'O estado é verificado a cada ~6 segundos; a cena dispara na mudança.';
  }
  stateSel.value = cur;
}
function capitalize(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

// Uma linha de ação: tipo + campos dependentes.
function addActionRow(action) {
  action = action || { type: 'switch', on: true };
  const wrap = $('#sceneActions');
  const row = document.createElement('div');
  row.className = 'scene-action';
  const smartOk = SCENE_DATA.smart_ready;
  const devOpts = SCENE_DATA.devices.filter(d => d.controllable)
    .map(d => `<option value="${esc(d.id)}">${esc(d.name || d.id)}</option>`).join('');
  row.innerHTML = `
    <select class="act-type">
      <option value="switch">Ligar/desligar dispositivo</option>
      <option value="brightness">Ajustar brilho</option>
      <option value="all">Acender/apagar tudo</option>
    </select>
    <span class="act-fields"></span>
    <button class="act-del" title="Remover">✕</button>`;
  const typeSel = row.querySelector('.act-type');
  typeSel.value = action.type || 'switch';
  const fields = row.querySelector('.act-fields');

  const renderFields = () => {
    const t = typeSel.value;
    if (!smartOk && t !== 'all') {
      fields.innerHTML = '<span class="muted">Smart home não configurado</span>';
      return;
    }
    if (t === 'all') {
      fields.innerHTML = `<select class="act-on"><option value="1">acender</option><option value="0">apagar</option></select>`;
      fields.querySelector('.act-on').value = action.on === false ? '0' : '1';
    } else if (t === 'switch') {
      fields.innerHTML = `<select class="act-dev">${devOpts}</select>
        <select class="act-code"></select>
        <select class="act-on"><option value="1">ligar</option><option value="0">desligar</option></select>`;
      const devEl = fields.querySelector('.act-dev');
      const codeEl = fields.querySelector('.act-code');
      if (action.device) devEl.value = action.device;
      // Preenche as teclas do dispositivo escolhido e refaz ao trocar de device.
      const refreshCodes = (selected) => { codeEl.innerHTML = codeOptions(devEl.value, selected); };
      refreshCodes(action.code || 'switch_1');
      devEl.onchange = () => refreshCodes();
      fields.querySelector('.act-on').value = action.on === false ? '0' : '1';
    } else if (t === 'brightness') {
      fields.innerHTML = `<select class="act-dev">${devOpts}</select>
        <input class="act-pct" type="number" min="1" max="100" value="${action.pct || 70}"> <span class="muted">%</span>`;
      if (action.device) fields.querySelector('.act-dev').value = action.device;
    }
  };
  typeSel.onchange = renderFields;
  renderFields();
  row.querySelector('.act-del').onclick = () => row.remove();
  wrap.appendChild(row);
}
$('#btnAddAction').onclick = () => addActionRow();

function collectScene() {
  const type = $$('input[name="trigType"]').find(r => r.checked).value;
  let trigger;
  if (type === 'schedule') {
    const days = $$('#trigDays .day-chip.on').map(c => +c.dataset.day);
    trigger = { type: 'schedule', time: $('#trigTime').value, days };
  } else if (type === 'device') {
    trigger = {
      type: 'device', device: $('#trigDev').value,
      code: $('#trigDevCode').value || 'switch_1',
      state: $('#trigDevState').value,
    };
  } else {
    trigger = { type: 'camera', camera: $('#trigCam').value, event: $('#trigEvent').value };
    const p = $('#trigPerson').value.trim();
    if (trigger.event === 'known' && p) trigger.person = p;
  }
  const actions = $$('#sceneActions .scene-action').map(row => {
    const t = row.querySelector('.act-type').value;
    if (t === 'all') return { type: 'all', on: row.querySelector('.act-on').value === '1' };
    if (t === 'switch') return { type: 'switch', device: row.querySelector('.act-dev')?.value, code: row.querySelector('.act-code')?.value || 'switch_1', on: row.querySelector('.act-on').value === '1' };
    if (t === 'brightness') return { type: 'brightness', device: row.querySelector('.act-dev')?.value, pct: +row.querySelector('.act-pct').value };
    return null;
  }).filter(Boolean);
  const scene = { name: $('#sceneName').value.trim() || 'Cena', trigger, actions };
  if (EDITING_SCENE) scene.id = EDITING_SCENE.id;
  return scene;
}

$('#sceneCancel').onclick = () => { $('#modalScene').hidden = true; };
$('#sceneSave').onclick = async () => {
  const scene = collectScene();
  try {
    await api('/api/scenes/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scene }) });
    $('#modalScene').hidden = true;
    toast('Cena salva', 'ok');
    loadScenes();
  } catch { toast('Falha ao salvar cena', 'err'); }
};
$('#btnSceneNew').onclick = () => openSceneModal(null);

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
          <div class="sh-sub muted">${{ lan: 'Wi-Fi local', hub: 'Zigbee (Hub, local)', cloud: 'via nuvem' }[d.via] || d.via}
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
  // Sensor: mostra o estado atual (porta aberta/fechada etc.) + bateria.
  if (d.kind === 'sensor') {
    if (!st.online) { body.innerHTML = `<div class="muted">${esc(st.error || 'sem resposta')}</div>`; return; }
    const sw = st.switches || {};
    const rows = [];
    for (const [code, val] of Object.entries(sw)) {
      const sl = SCENE_DATA.sensor_labels && SCENE_DATA.sensor_labels[code];
      if (!sl) continue;
      const on = !!val;  // sl[1] = texto p/ true, sl[2] = texto p/ false
      rows.push(`<div class="sensor-row"><span>${esc(sl[0])}</span>
        <span class="sensor-val ${on ? 'alert' : 'ok'}">${esc(capitalize(on ? sl[1] : sl[2]))}</span></div>`);
    }
    if (st.battery != null) {
      rows.push(`<div class="sensor-row"><span>Bateria</span>
        <span class="sensor-val ${st.battery < 20 ? 'alert' : ''}">${st.battery}%</span></div>`);
    }
    body.innerHTML = rows.length ? rows.join('') : '<div class="muted">Somente leitura</div>';
    return;
  }
  if (!d.controllable) {
    body.innerHTML = `<div class="muted">Sem controle</div>`;
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

// Sincroniza com a nuvem Tuya: importa dispositivos novos da Smart Life.
// A escuta local (~8s) deixa a operação um pouco lenta — mostramos progresso.
async function syncSmartHome() {
  const btns = $$('#btnShSync, #btnShSyncFirst').filter(Boolean);
  btns.forEach(b => { b.disabled = true; b._old = b.textContent; b.textContent = 'Sincronizando…'; });
  try {
    const r = await api('/api/smarthome/sync', { method: 'POST' });
    if (!r.ok) throw new Error(r.error || 'falha');
    let msg = `${r.count} dispositivo(s) sincronizado(s)`;
    if (r.added && r.added.length) msg += ` · novo(s): ${r.added.join(', ')}`;
    if (r.removed && r.removed.length) msg += ` · removido(s): ${r.removed.join(', ')}`;
    toast(msg, 'ok');
    // Recarrega a aba atual que dependa da lista de dispositivos, para que
    // sensores/aparelhos novos apareçam sem precisar de F5.
    if (VIEW === 'smarthome') loadSmartHome();
    else if (VIEW === 'alarms') loadAlarms();
    else if (VIEW === 'scenes') loadScenes();
  } catch (e) {
    toast('Falha ao sincronizar: ' + (e.message || ''), 'err');
  }
  btns.forEach(b => { b.disabled = false; b.textContent = b._old || '⟳ Sincronizar'; });
}
$('#btnShSync') && ($('#btnShSync').onclick = syncSmartHome);
$('#btnShSyncFirst') && ($('#btnShSyncFirst').onclick = syncSmartHome);

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

let NET_LOADING = false;
let NET_EDITING = false;
async function loadNetwork(mode = 'cache') {
  if (NET_LOADING || (mode === 'cache' && NET_EDITING)) return;
  NET_LOADING = true;
  if (mode !== 'cache') $('#netLoading').hidden = false;
  $('#netEmpty').hidden = true;
  let data = { devices: [], events: [] };
  const path = mode === 'full' ? '/api/network/analysis' : (mode === 'quick' ? '/api/network/scan' : '/api/network');
  try { data = await api(path, mode === 'cache' ? undefined : { method: 'POST' }); }
  catch { toast('Falha ao carregar a rede', 'err'); }
  NET_LOADING = false;
  const devs = data.devices || [];
  $('#netLoading').hidden = true;
  const online = devs.filter(d => d.online).length;
  $('#netCount').textContent = `${online} online · ${devs.length} no inventário`;
  $('#netEmpty').hidden = devs.length > 0;
  $('#netTable').hidden = devs.length === 0;
  const body = $('#netBody'); body.innerHTML = '';
  for (const d of devs) {
    const name = esc(d.custom_name || d.hostname || d.advert || d.vendor || '—');
    const svc = (d.services || []).length
      ? (d.services || []).map(s => `<span class="svc-tag">${esc(s)}</span>`).join(' ')
      : '<span class="muted">—</span>';
    const tr = document.createElement('tr');
    tr.className = (d.is_camera ? 'is-cam ' : '') + (!d.online ? 'is-offline' : '');
    tr.innerHTML = `
      <td><b class="net-name" title="Clique para nomear">${name}</b> ${netTag(d)}
        <button class="net-known ${d.known ? 'on' : ''}" title="${d.known ? 'Dispositivo conhecido' : 'Marcar como conhecido'}">✓</button></td>
      <td class="mono">${esc(d.ip)}</td>
      <td class="mono muted">${esc(d.mac || '—')}</td>
      <td>${esc(d.vendor || '—')}</td>
      <td class="svc-cell">${svc}</td>
      <td><span class="net-state ${d.online ? 'on' : ''}">${d.online ? (d.missed_scans ? `confirmando ${d.missed_scans}/3` : 'online') : 'offline'}</span></td>
      <td class="muted">${fmtDateTime(d.last_seen)}</td>`;
    const nameEl = tr.querySelector('.net-name');
    nameEl.onclick = () => editNetName(d.mac, nameEl);
    tr.querySelector('.net-known').onclick = async () => {
      await api('/api/network/device', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mac: d.mac, known: !d.known }) });
      loadNetwork();
    };
    body.appendChild(tr);
  }
  const evLabels = { new: 'Novo dispositivo', online: 'Entrou na rede', offline: 'Saiu da rede' };
  const events = data.events || [];
  $('#netEvents').innerHTML = events.length ? events.map(e => `
    <div class="net-event ${esc(e.event)}"><span class="net-event-dot"></span>
      <b>${evLabels[e.event] || esc(e.event)}</b> · ${esc(e.label || e.ip)}
      <span class="muted">${fmtDateTime(e.ts)}</span></div>`).join('')
    : '<span class="muted">Nenhuma mudança registrada ainda.</span>';
}

// Mesmo comportamento da edição do nome das câmeras: edita dentro da tabela,
// Enter confirma, Escape cancela e sair do campo salva.
function editNetName(mac, el) {
  const cur = el.textContent;
  const input = document.createElement('input');
  input.className = 'cam-name-input net-name-input';
  input.value = cur === '—' ? '' : cur;
  el.replaceChildren(input);
  NET_EDITING = true;
  input.focus(); input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    const v = input.value.trim();
    const changed = save && v && v !== cur;
    el.textContent = changed ? v : cur;
    el.onclick = () => editNetName(mac, el);
    NET_EDITING = false;
    if (changed) {
      try {
        await api('/api/network/device', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mac, name: v })
        });
        toast('Dispositivo renomeado para "' + v + '"', 'ok');
        loadNetwork();
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

$('#btnNetScan').onclick = () => loadNetwork('quick');
$('#btnNetAnalysis').onclick = () => loadNetwork('full');
// Atualiza apenas a exibicao do cache; nao gera ping nem trafego de descoberta.
setInterval(() => { if (VIEW === 'network') loadNetwork('cache'); }, 15000);

/* ---------- Configurações (credenciais do .env) ---------- */
let SETTINGS_FIELDS = [];

async function loadSettings() {
  let data = { fields: [] };
  try { data = await api('/api/settings'); }
  catch { toast('Falha ao carregar configurações', 'err'); }
  if (VIEW !== 'settings') return;
  SETTINGS_FIELDS = data.fields || [];
  renderSettings(SETTINGS_FIELDS);
}

function renderSettings(fields) {
  const form = $('#settingsForm'); form.innerHTML = '';
  // Agrupa os campos por seção (E-mail, Smart home…).
  const groups = {};
  for (const f of fields) (groups[f.group] = groups[f.group] || []).push(f);

  for (const [group, items] of Object.entries(groups)) {
    const sec = document.createElement('div');
    sec.className = 'settings-group';
    sec.innerHTML = `<h3 class="settings-group-title">${esc(group)}</h3>`;
    for (const f of items) {
      const row = document.createElement('div');
      row.className = 'settings-field';
      const tip = `<span class="tip" tabindex="0">ⓘ<span class="tip-box">${esc(f.help)}</span></span>`;
      const status = f.secret && f.set
        ? '<span class="settings-set">✓ salvo</span>' : '';
      let control;
      if (f.type === 'select') {
        const opts = (f.options || []).map(o =>
          `<option value="${esc(o.value)}"${o.value === f.value ? ' selected' : ''}>${esc(o.label)}</option>`).join('');
        control = `<select data-key="${esc(f.key)}">${opts}</select>`;
      } else {
        const type = f.secret ? 'password' : 'text';
        const ph = f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : '';
        control = `<input type="${type}" data-key="${esc(f.key)}" value="${esc(f.value)}"${ph} autocomplete="off">`;
      }
      row.innerHTML = `
        <label class="settings-lbl">${esc(f.label)} ${tip} ${status}</label>
        ${control}`;
      sec.appendChild(row);
    }
    form.appendChild(sec);
  }
}

$('#btnSettingsSave').onclick = async () => {
  const values = {};
  $$('#settingsForm [data-key]').forEach(el => { values[el.dataset.key] = el.value; });
  const b = $('#btnSettingsSave'); b.disabled = true;
  $('#settingsMsg').textContent = '';
  try {
    const r = await api('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values })
    });
    if (!r.ok) throw new Error(r.error || 'falha');
    const n = (r.updated || []).length;
    toast(n ? 'Configurações salvas' : 'Nada alterado', 'ok');
    loadSettings();  // recarrega para remascarar segredos recém-salvos
  } catch (e) { toast('Falha ao salvar configurações', 'err'); }
  b.disabled = false;
};

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

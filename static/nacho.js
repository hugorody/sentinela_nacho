'use strict';

const stage = document.querySelector('.stage');
const button = document.querySelector('#talkButton');
const title = document.querySelector('#statusTitle');
const detail = document.querySelector('#statusDetail');
const teeth = document.querySelector('.cap-teeth');

// A borda serrilhada é construída em código para continuar nítida em qualquer tela.
for (let i = 0; i < 28; i += 1) {
  const tooth = document.createElement('i');
  tooth.style.transform = `rotate(${i * (360 / 28)}deg)`;
  teeth.appendChild(tooth);
}

const labels = {
  idle: ['Pode chamar', 'Toque na tampa para começar'],
  listening: ['Estou ouvindo', 'Fale naturalmente; uma pausa envia a frase'],
  thinking: ['Só um instante', 'Organizando o que ouvi'],
  speaking: ['Nacho está falando', 'Você pode interromper com um toque'],
  error: ['Não consegui ouvir', 'Verifique a permissão do microfone'],
};

let state = 'idle';
let stream = null;
let context = null;
let analyser = null;
let animation = 0;
let peer = null;
let channel = null;
let remoteAudio = null;
let realtime = false;
let rtcDiagnostics = { iceErrors: [] };
let turnRecorder = null;
let turnChunks = [];
let conversationActive = false;
let previousResponseId = '';
let speechDetected = false;
let voiceCandidateAt = 0;
let lastVoiceAt = 0;
let speechStartedAt = 0;
let turnStartedAt = 0;
let finishingTurn = false;
let turnAbort = null;

function setState(next, customDetail = '') {
  state = next;
  stage.dataset.state = next;
  title.textContent = labels[next][0];
  detail.textContent = customDetail || labels[next][1];
  button.setAttribute('aria-pressed', next === 'listening' ? 'true' : 'false');
  button.setAttribute('aria-label', next === 'listening' ? 'Parar de ouvir' : 'Começar interação com Nacho');
}

function animateLevel() {
  if (!analyser || state !== 'listening') return;
  const samples = new Uint8Array(analyser.fftSize);
  analyser.getByteTimeDomainData(samples);
  let energy = 0;
  for (const value of samples) {
    const sample = (value - 128) / 128;
    energy += sample * sample;
  }
  const rms = Math.sqrt(energy / samples.length);
  stage.style.setProperty('--level', Math.min(1, rms * 6).toFixed(3));
  // VAD leve no navegador: só decide quando encerrar a gravação; todo o
  // entendimento continua na OpenAI. Uma pausa após fala envia o turno.
  if (turnRecorder?.state === 'recording' && !finishingTurn) {
    const now = performance.now();
    // Ao reabrir o microfone, ignora o ajuste inicial de ganho e qualquer
    // resíduo da voz do próprio Nacho.
    if (now - turnStartedAt < 650) {
      voiceCandidateAt = 0;
    } else if (rms >= 0.025) {
      if (!voiceCandidateAt) voiceCandidateAt = now;
      if (now - voiceCandidateAt >= 180) {
        if (!speechDetected) speechStartedAt = now;
        speechDetected = true;
        lastVoiceAt = now;
      }
    } else if (!speechDetected) {
      voiceCandidateAt = 0;
    } else if (speechDetected && now - lastVoiceAt >= 1150
               && now - speechStartedAt >= 350
               && now - turnStartedAt >= 900) {
      finishTurnMode();
    }
  }
  animation = requestAnimationFrame(animateLevel);
}

async function openMicrophone() {
  if (!navigator.mediaDevices?.getUserMedia) {
    setState('error', 'Este navegador exige HTTPS para liberar o microfone');
    return null;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    context = new (window.AudioContext || window.webkitAudioContext)();
    analyser = context.createAnalyser();
    analyser.fftSize = 512;
    context.createMediaStreamSource(stream).connect(analyser);
    return stream;
  } catch (error) {
    setState('error', error.name === 'NotAllowedError'
      ? 'Autorize o microfone nas configurações do navegador'
      : 'Não foi possível iniciar o microfone');
    return null;
  }
}

async function stopInteraction(message = '') {
  conversationActive = false;
  previousResponseId = '';
  turnAbort?.abort(); turnAbort = null;
  if (turnRecorder?.state === 'recording') {
    try { turnRecorder.stop(); } catch {}
  }
  cancelAnimationFrame(animation);
  stream?.getTracks().forEach(track => track.stop());
  stream = null;
  analyser = null;
  stage.style.setProperty('--level', '0');
  if (context) await context.close().catch(() => {});
  context = null;
  channel?.close(); channel = null;
  peer?.close(); peer = null;
  if (remoteAudio) { remoteAudio.srcObject = null; remoteAudio.remove(); }
  remoteAudio = null;
  realtime = false;
  turnRecorder = null;
  turnChunks = [];
  finishingTurn = false;
  setState('idle', message || 'Toque na tampa para começar');
}

function closeRealtimeKeepMicrophone() {
  channel?.close(); channel = null;
  const oldPeer = peer; peer = null;
  oldPeer?.close();
  if (remoteAudio) { remoteAudio.srcObject = null; remoteAudio.remove(); }
  remoteAudio = null;
  realtime = false;
}

async function startTurnMode(reason = '', existingMic = null) {
  const mic = existingMic || await openMicrophone();
  if (!mic) return;
  if (!window.MediaRecorder) {
    setState('error', 'Este navegador não oferece gravação de áudio compatível');
    return;
  }
  turnChunks = [];
  speechDetected = false;
  voiceCandidateAt = 0;
  lastVoiceAt = 0;
  speechStartedAt = 0;
  turnStartedAt = performance.now();
  finishingTurn = false;
  const preferred = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus' : '';
  turnRecorder = new MediaRecorder(mic, preferred ? { mimeType: preferred } : undefined);
  turnRecorder.addEventListener('dataavailable', event => {
    if (event.data.size) turnChunks.push(event.data);
  });
  turnRecorder.start(250);
  setState('listening', reason || 'Fale naturalmente; uma pausa envia a frase');
  animateLevel();
}

async function finishTurnMode() {
  if (!turnRecorder || turnRecorder.state !== 'recording' || finishingTurn) return;
  finishingTurn = true;
  const recorder = turnRecorder;
  const stopped = new Promise(resolve => recorder.addEventListener('stop', resolve, { once: true }));
  recorder.stop();
  await stopped;
  cancelAnimationFrame(animation);
  stream?.getTracks().forEach(track => track.stop());
  stream = null; analyser = null;
  if (context) await context.close().catch(() => {});
  context = null;
  stage.style.setProperty('--level', '0');
  turnRecorder = null;
  const blob = new Blob(turnChunks, { type: recorder.mimeType || 'audio/webm' });
  turnChunks = [];
  setState('thinking', 'Entendendo e preparando a resposta');
  try {
    const form = new FormData();
    form.append('audio', blob, 'speech.webm');
    if (previousResponseId) form.append('previous_response_id', previousResponseId);
    turnAbort = new AbortController();
    const response = await fetch('/api/voice/turn', {
      method: 'POST', body: form, signal: turnAbort.signal,
    });
    turnAbort = null;
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.audio) throw new Error(data.error || 'Falha ao processar a fala');
    if (!conversationActive) return;
    previousResponseId = data.response_id || previousResponseId;
    const audio = new Audio('data:audio/mpeg;base64,' + data.audio);
    remoteAudio = audio;
    audio.addEventListener('playing', () => setState('speaking'));
    audio.addEventListener('ended', () => {
      remoteAudio = null;
      if (conversationActive) {
        setState('thinking', 'Pode continuar quando quiser');
        window.setTimeout(() => {
          if (conversationActive) startTurnMode('Pode continuar falando');
        }, 850);
      } else setState('idle');
    });
    audio.addEventListener('error', () => setState('error', 'Não foi possível reproduzir a resposta'));
    await audio.play();
  } catch (error) {
    if (error.name !== 'AbortError') {
      setState('error', error.message || 'Falha no modo de voz compatível');
      window.setTimeout(() => {
        if (conversationActive && !turnRecorder) {
          startTurnMode('Não entendi; pode repetir');
        }
      }, 1200);
    }
  } finally {
    finishingTurn = false;
  }
}

async function runToolCall(item) {
  if (!channel || channel.readyState !== 'open') return;
  setState('thinking', 'Consultando o Sentinela');
  let args = {};
  try { args = JSON.parse(item.arguments || '{}'); } catch { args = {}; }
  let output;
  try {
    const response = await fetch('/api/tools/' + encodeURIComponent(item.name), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(args),
    });
    output = await response.json();
  } catch (error) {
    output = { error: 'Falha ao consultar o Sentinela', detail: error.message };
  }
  channel.send(JSON.stringify({
    type: 'conversation.item.create',
    item: { type: 'function_call_output', call_id: item.call_id, output: JSON.stringify(output) },
  }));
  channel.send(JSON.stringify({ type: 'response.create' }));
}

function handleRealtimeEvent(event) {
  let data;
  try { data = JSON.parse(event.data); } catch { return; }
  const type = data.type || '';
  if (type === 'input_audio_buffer.speech_started') setState('listening', 'Pode falar, estou ouvindo');
  else if (type === 'input_audio_buffer.speech_stopped') setState('thinking');
  else if (type.includes('audio') && (type.includes('delta') || type.includes('started'))) setState('speaking');
  else if (type === 'response.done' || type === 'output_audio_buffer.stopped') setState('listening', 'Pode continuar falando');
  else if (type === 'response.output_item.done' && data.item?.type === 'function_call') runToolCall(data.item);
  else if (type === 'error') {
    console.error('Realtime API:', data);
    setState('error', data.error?.message || 'A sessão de voz encontrou um erro');
  }
}

function waitForIceGathering(pc, timeoutMs = 5000) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise(resolve => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      pc.removeEventListener('icegatheringstatechange', check);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === 'complete') finish(); };
    pc.addEventListener('icegatheringstatechange', check);
    window.setTimeout(finish, timeoutMs);
  });
}

function candidateTypes(sdp = '') {
  return [...new Set([...sdp.matchAll(/ typ ([a-z]+)/g)].map(match => match[1]))];
}

function rtcSummary(pc) {
  const local = candidateTypes(pc?.localDescription?.sdp);
  const remote = candidateTypes(pc?.remoteDescription?.sdp);
  return `ICE ${pc?.iceConnectionState || 'n/a'} · conexão ${pc?.connectionState || 'n/a'} · dados ${channel?.readyState || 'n/a'} · local ${local.join('/') || 'sem candidato'} · remoto ${remote.join('/') || 'sem candidato'}`;
}

async function startRealtime() {
  const mic = await openMicrophone();
  if (!mic) return;
  realtime = true;
  setState('thinking', 'Conectando a conversa segura');
  try {
    let rejectConnectionFailure;
    const connectionFailed = new Promise((_, reject) => { rejectConnectionFailure = reject; });
    peer = new RTCPeerConnection();
    rtcDiagnostics = { iceErrors: [] };
    peer.addEventListener('icecandidateerror', event => {
      rtcDiagnostics.iceErrors.push({ code: event.errorCode, text: event.errorText });
      console.warn('ICE candidate error:', event.errorCode, event.errorText);
    });
    peer.addEventListener('connectionstatechange', () => {
      if (!peer) return;
      if (peer.connectionState === 'connected' && channel?.readyState !== 'open') {
        setState('listening', 'Voz conectada · finalizando recursos do Sentinela');
      } else if (peer.connectionState === 'failed') {
        setState('thinking', 'WebRTC indisponível · preparando modo compatível');
        rejectConnectionFailure(new Error('A rede bloqueou WebRTC'));
      } else if (peer.connectionState === 'disconnected') {
        setState('thinking', 'Tentando recuperar a conexão de voz');
      }
    });
    remoteAudio = document.createElement('audio');
    remoteAudio.autoplay = true;
    remoteAudio.setAttribute('playsinline', '');
    remoteAudio.addEventListener('playing', () => setState('speaking'));
    remoteAudio.addEventListener('pause', () => {
      if (realtime) setState('listening', 'Pode continuar falando');
    });
    remoteAudio.addEventListener('ended', () => {
      if (realtime) setState('listening', 'Pode continuar falando');
    });
    document.body.appendChild(remoteAudio);
    peer.ontrack = event => { remoteAudio.srcObject = event.streams[0]; };
    mic.getTracks().forEach(track => peer.addTrack(track, mic));
    channel = peer.createDataChannel('oai-events');
    channel.addEventListener('message', handleRealtimeEvent);
    const channelOpened = new Promise((resolve, reject) => {
      channel.addEventListener('open', resolve, { once: true });
      channel.addEventListener('error', () => reject(new Error(
        'O canal de voz encontrou um erro')), { once: true });
    });
    setState('thinking', 'Obtendo credencial temporária');
    const tokenResponse = await fetch('/api/realtime/token', { method: 'POST' });
    const token = await tokenResponse.json().catch(() => ({}));
    if (!tokenResponse.ok || !token.value) {
      throw new Error(token.error || 'Não foi possível obter a credencial de voz');
    }
    setState('thinking', 'Preparando o canal de áudio');
    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    await waitForIceGathering(peer);
    setState('thinking', 'Negociando conexão segura');
    const response = await fetch(
      'https://api.openai.com/v1/realtime/calls?model=' + encodeURIComponent(token.model), {
        method: 'POST', headers: {
          'Authorization': 'Bearer ' + token.value,
          'Content-Type': 'application/sdp',
        }, body: peer.localDescription.sdp,
    });
    if (!response.ok) {
      const problem = await response.json().catch(() => ({}));
      throw new Error(problem.error || 'Não foi possível abrir a sessão de voz');
    }
    await peer.setRemoteDescription({ type: 'answer', sdp: await response.text() });
    const dataReady = await Promise.race([
      channelOpened.then(() => true, () => false),
      connectionFailed,
      new Promise(resolve => window.setTimeout(() => resolve(false), 15000)),
    ]);
    const mediaReady = peer.connectionState === 'connected'
      || peer.iceConnectionState === 'connected' || peer.iceConnectionState === 'completed';
    if (!dataReady && !mediaReady) {
      throw new Error('Tempo esgotado ao abrir a conexão de voz · ' + rtcSummary(peer));
    }
    setState('listening', dataReady
      ? 'Pode falar, estou ouvindo'
      : 'Voz conectada · consultas do Sentinela temporariamente indisponíveis');
    animateLevel();
  } catch (error) {
    console.error(error);
    const existingMic = stream;
    closeRealtimeKeepMicrophone();
    await startTurnMode('Modo compatível ativado · fale e toque novamente ao terminar', existingMic);
  }
}

async function startPreview() {
  const mic = await openMicrophone();
  if (!mic) return;
  setState('listening', 'Modo de teste — configure a OpenAI API Key');
  animateLevel();
}

async function startInteraction() {
  try {
    const health = await fetch('/api/health').then(r => r.json());
    if (health.voice_configured && health.voice_transport === 'realtime') await startRealtime();
    else if (health.voice_configured) await startTurnMode(
      'Fale naturalmente; uma pausa envia a frase');
    else await startPreview();
  } catch {
    setState('error', 'O serviço Nacho não está respondendo');
  }
}

button.addEventListener('click', () => {
  if (conversationActive) stopInteraction('Conversa encerrada');
  else if (state !== 'thinking') {
    conversationActive = true;
    previousResponseId = '';
    startInteraction();
  }
});

// Ponto único para a futura conexão Realtime controlar a animação.
window.NachoUI = { setState };

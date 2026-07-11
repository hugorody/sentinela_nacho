#!/usr/bin/env python3
"""
alarms.py - Alarmes de camera: e-mail quando alguem NAO identificado aparece.

Cada camera tem uma config independente em alarms.json:
    {
      "email": "destino@exemplo.com, outro@exemplo.com",
      "cameras": {
        "<camera_name>": {
          "enabled": true,
          "windows": [{"start": "22:00", "end": "06:00"}],  # ativo nesses horarios
          "recipients": "extra@exemplo.com"   # opcional; sobrescreve o global
        }
      },
      "devices": {
        "<device_id>|<code>": {          # ex.: "eb36...ry|doorcontact_state"
          "enabled": true,
          "windows": [{"start": "22:00", "end": "06:00"}],
          "recipients": "extra@exemplo.com",  # opcional
          "trigger": "on"                # "on" (default), "off" ou "any"
        }
      }
    }

- 'windows' vazio = ativo 24h (quando enabled). Janelas podem cruzar a meia-noite
  (start > end, ex.: 22:00->06:00).
- CAMERAS: quando um rosto DESCONHECIDO e visto numa camera ativa (e dentro da
  janela), dispara um e-mail via Resend com um screenshot em anexo. O engine
  empurra o evento chamando alarm.notify_unknown(camera_name, frame, score).
- DISPOSITIVOS: um thread interno le o estado dos sensores smart (porta,
  movimento, vazamento...) por polling e dispara um e-mail na TRANSICAO para o
  estado configurado (ex.: porta ABERTA). Precisa de um Controller (Tuya):
    alarm.attach_controller(controller); alarm.start_device_poll()

Ha um cooldown por camera/dispositivo para nao floodar. O envio de e-mail roda
sempre numa thread separada (nao pode travar nem o loop de IA nem o poll).
"""

import base64
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

import envutil

CONFIG = "alarms.json"
# Remetente: dominio verificado no Resend. Pode ser sobrescrito pelo .env
# (alarm_from=...) sem mexer no codigo.
FROM = "Sentinela <novidades@news.mundodna.com>"


def _now_hm(dt):
    return dt.hour * 60 + dt.minute


def _parse_hm(s):
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def in_window(windows, dt):
    """True se 'dt' cai em alguma janela. Sem janelas = sempre ativo."""
    if not windows:
        return True
    cur = _now_hm(dt)
    for w in windows:
        start = _parse_hm(w.get("start"))
        end = _parse_hm(w.get("end"))
        if start is None or end is None:
            continue
        if start == end:
            return True  # janela de 24h
        if start < end:
            if start <= cur < end:
                return True
        else:  # cruza a meia-noite (ex.: 22:00 -> 06:00)
            if cur >= start or cur < end:
                return True
    return False


class AlarmManager:
    """Config de alarmes por camera + disparo de e-mail com screenshot."""

    def __init__(self, config_path=CONFIG, env_path=".env", cooldown=120.0,
                 poll_interval=6.0):
        self.config_path = config_path
        self.env_path = env_path
        self.cooldown = cooldown           # segundos entre e-mails do mesmo alvo
        self.poll_interval = poll_interval  # segundos entre leituras de sensores
        self._lock = threading.Lock()
        self._last_sent = {}               # camera_name/dev_key -> epoch do ultimo e-mail
        self._cfg = self._load()
        # Controle de dispositivos smart (sensores). O Controller (Tuya) e
        # anexado depois por quem tem acesso a ele (app.py), pois nem sempre esta
        # configurado.
        self.controller = None
        self._dev_state = {}               # (dev_id, code) -> bool (ultima leitura)
        self._stop = threading.Event()
        self._thread = None

    # ---- config ----

    def _load(self):
        p = Path(self.config_path)
        if not p.exists():
            return {"email": "", "cameras": {}, "devices": {}}
        try:
            import json
            data = json.loads(p.read_text())
            data.setdefault("email", "")
            data.setdefault("cameras", {})
            data.setdefault("devices", {})
            return data
        except (OSError, ValueError):
            return {"email": "", "cameras": {}, "devices": {}}

    def _save(self):
        import json
        Path(self.config_path).write_text(
            json.dumps(self._cfg, indent=2, ensure_ascii=False) + "\n")

    def reload(self):
        with self._lock:
            self._cfg = self._load()

    def get_config(self):
        """Config completa (para o frontend montar a tela)."""
        with self._lock:
            import copy
            return copy.deepcopy(self._cfg)

    def set_global_email(self, email):
        with self._lock:
            self._cfg["email"] = (email or "").strip()
            self._save()
        return True

    def set_camera(self, camera_name, enabled=None, windows=None, recipients=None):
        """Atualiza a config de uma camera (so os campos passados)."""
        if not camera_name:
            return False
        with self._lock:
            cam = self._cfg["cameras"].setdefault(camera_name, {
                "enabled": False, "windows": [], "recipients": ""})
            if enabled is not None:
                cam["enabled"] = bool(enabled)
            if windows is not None:
                cam["windows"] = self._clean_windows(windows)
            if recipients is not None:
                cam["recipients"] = (recipients or "").strip()
            self._save()
        return True

    @staticmethod
    def _clean_windows(windows):
        out = []
        for w in windows or []:
            s, e = _parse_hm(w.get("start")), _parse_hm(w.get("end"))
            if s is not None and e is not None:
                out.append({"start": w["start"], "end": w["end"]})
        return out

    # ---- config de dispositivos smart (sensores) ----

    @staticmethod
    def _dev_key(device_id, code):
        """Chave que identifica um (dispositivo, estado) na config/estado."""
        return f"{device_id}|{code}"

    def set_device(self, device_id, code, enabled=None, windows=None,
                   recipients=None, trigger=None):
        """Atualiza a config de alarme de um sensor (so os campos passados)."""
        if not device_id or not code:
            return False
        key = self._dev_key(device_id, code)
        with self._lock:
            dev = self._cfg["devices"].setdefault(key, {
                "enabled": False, "windows": [], "recipients": "",
                "trigger": "on"})
            if enabled is not None:
                dev["enabled"] = bool(enabled)
            if windows is not None:
                dev["windows"] = self._clean_windows(windows)
            if recipients is not None:
                dev["recipients"] = (recipients or "").strip()
            if trigger is not None and trigger in ("on", "off", "any"):
                dev["trigger"] = trigger
            self._save()
        return True

    # ---- decisao ----

    def _cam_cfg(self, camera_name):
        return self._cfg["cameras"].get(camera_name)

    def is_active(self, camera_name, dt=None):
        """True se o alarme desta camera esta ligado E dentro da janela agora."""
        dt = dt or datetime.now()
        with self._lock:
            cam = self._cam_cfg(camera_name)
            if not cam or not cam.get("enabled"):
                return False
            return in_window(cam.get("windows"), dt)

    def _recipients_for(self, camera_name):
        with self._lock:
            cam = self._cam_cfg(camera_name) or {}
        return self._recipients_from(cam)

    def _recipients_from(self, cfg):
        """Destinatarios de uma config (camera ou dispositivo): o especifico se
        houver, senao o e-mail global."""
        specific = (cfg.get("recipients") or "").strip()
        with self._lock:
            base = specific or self._cfg.get("email", "")
        return [e.strip() for e in base.replace(";", ",").split(",") if e.strip()]

    def _device_active(self, dev_cfg, dt=None):
        """True se o alarme de um sensor esta ligado E dentro da janela agora."""
        dt = dt or datetime.now()
        if not dev_cfg or not dev_cfg.get("enabled"):
            return False
        return in_window(dev_cfg.get("windows"), dt)

    # ---- disparo ----

    def notify_unknown(self, camera_name, frame, score=0.0):
        """Se a camera estiver ativa e fora do cooldown, dispara e-mail (async)."""
        if not self.is_active(camera_name):
            return False
        now = time.time()
        with self._lock:
            if now - self._last_sent.get(camera_name, 0.0) < self.cooldown:
                return False
            self._last_sent[camera_name] = now
        recipients = self._recipients_for(camera_name)
        if not recipients:
            return False
        # Copia o frame agora (o loop de IA vai sobrescrever cam["frame"]).
        snapshot = frame.copy() if frame is not None else None
        threading.Thread(
            target=self._send_email,
            args=(camera_name, recipients, snapshot, score),
            daemon=True,
        ).start()
        return True

    def _send_email(self, camera_name, recipients, frame, score):
        when = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        subject = f"[Sentinela] Movimento nao identificado — {camera_name}"
        html = (
            f"<div style='font-family:system-ui,Arial,sans-serif'>"
            f"<h2 style='margin:0 0 8px'>Alarme: pessoa nao identificada</h2>"
            f"<p style='margin:0 0 4px'>Camera: <b>{camera_name}</b></p>"
            f"<p style='margin:0 0 4px'>Quando: {when}</p>"
            f"<p style='margin:12px 0 0;color:#666'>Screenshot em anexo.</p>"
            f"</div>"
        )
        attachment = None
        jpg = self._encode_jpeg(frame)
        if jpg is not None:
            attachment = (f"{_safe(camera_name)}_{datetime.now():%Y%m%d_%H%M%S}.jpg", jpg)
        self._deliver(recipients, subject, html, attachment, label=camera_name)

    def _send_device_email(self, label, recipients, state_text):
        """E-mail de alarme de um sensor smart (ex.: 'Porta de servico: aberta')."""
        when = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        subject = f"[Sentinela] {label}: {state_text}"
        html = (
            f"<div style='font-family:system-ui,Arial,sans-serif'>"
            f"<h2 style='margin:0 0 8px'>Alarme de dispositivo</h2>"
            f"<p style='margin:0 0 4px'><b>{label}</b>: {state_text}</p>"
            f"<p style='margin:0 0 4px'>Quando: {when}</p>"
            f"</div>"
        )
        self._deliver(recipients, subject, html, None, label=label)

    def _deliver(self, recipients, subject, html, attachment, label=""):
        """Envio de baixo nivel via Resend. attachment = (filename, bytes) ou None."""
        try:
            import resend
        except ImportError:
            print("[alarms] pacote 'resend' nao instalado; e-mail nao enviado")
            return
        api_key = envutil.get("resend_api_key", path=self.env_path)
        if not api_key:
            print("[alarms] resend_api_key ausente no .env; e-mail nao enviado")
            return
        resend.api_key = api_key
        sender = envutil.get("alarm_from", FROM, path=self.env_path) or FROM
        params = {"from": sender, "to": recipients, "subject": subject, "html": html}
        if attachment is not None:
            fname, data = attachment
            params["attachments"] = [{"filename": fname, "content": list(data)}]
        try:
            resend.Emails.send(params)
            print(f"[alarms] e-mail de alarme enviado ({label}) -> {recipients}")
        except Exception as exc:
            print(f"[alarms] falha ao enviar e-mail: {exc}")

    @staticmethod
    def _encode_jpeg(frame):
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    def send_test(self, camera_name):
        """Dispara um e-mail de teste ignorando janela/cooldown (config UI)."""
        recipients = self._recipients_for(camera_name)
        if not recipients:
            return {"ok": False, "error": "nenhum destinatario configurado"}
        self._send_email(camera_name, recipients, None, 0.0)
        return {"ok": True, "recipients": recipients}

    def send_device_test(self, device_id, code, label="Dispositivo"):
        """E-mail de teste de um alarme de sensor (ignora janela/cooldown)."""
        key = self._dev_key(device_id, code)
        with self._lock:
            dev = self._cfg["devices"].get(key)
        recipients = self._recipients_from(dev or {})
        if not recipients:
            return {"ok": False, "error": "nenhum destinatario configurado"}
        self._send_device_email(label, recipients, "e-mail de teste")
        return {"ok": True, "recipients": recipients}

    # ---- dispositivos smart: polling de estado e disparo ----

    def attach_controller(self, controller):
        """Liga o Controller (Tuya) usado para ler o estado dos sensores."""
        self.controller = controller

    def start_device_poll(self):
        """Sobe o thread que le os sensores e dispara e-mail nas transicoes."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._device_loop, daemon=True)
        self._thread.start()

    def stop_device_poll(self):
        self._stop.set()

    def _device_loop(self):
        while not self._stop.is_set():
            try:
                self._check_devices()
            except Exception as exc:
                print(f"[alarms] device loop: {exc}")
            self._stop.wait(self.poll_interval)

    def _check_devices(self):
        """Le o estado dos sensores com alarme ativo e dispara nas transicoes."""
        if self.controller is None:
            return
        with self._lock:
            active = {k: dict(v) for k, v in self._cfg["devices"].items()
                      if v.get("enabled")}
        if not active:
            return
        # Um sensor pode ter varios 'codes' com alarme; leia cada dispositivo uma
        # vez so.
        device_ids = {k.split("|", 1)[0] for k in active}
        states = {}
        for dev_id in device_ids:
            try:
                st = self.controller.get_state(dev_id)
            except Exception:
                st = {}
            if st.get("online"):
                states[dev_id] = st.get("switches") or {}

        now = time.time()
        for key, cfg in active.items():
            dev_id, code = key.split("|", 1)
            switches = states.get(dev_id)
            if switches is None or code not in switches:
                continue  # offline ou sem esse estado neste ciclo
            cur = bool(switches[code])
            prev = self._dev_state.get(key)
            self._dev_state[key] = cur
            if prev is None or prev == cur:
                continue  # sem transicao (primeira leitura ou estavel)
            want = cfg.get("trigger", "on")
            if want == "on" and not cur:
                continue
            if want == "off" and cur:
                continue
            if not self._device_active(cfg, datetime.now()):
                continue  # fora da janela de horario
            with self._lock:
                if now - self._last_sent.get(key, 0.0) < self.cooldown:
                    continue
                self._last_sent[key] = now
            recipients = self._recipients_from(cfg)
            if not recipients:
                continue
            label, state_text = self._device_labels(code, cur)
            threading.Thread(
                target=self._send_device_email,
                args=(label, recipients, state_text),
                daemon=True,
            ).start()

    @staticmethod
    def _device_labels(code, value):
        """(nome, texto do estado) amigaveis para um sensor. Ex.: doorcontact_state
        True -> ('Porta', 'aberta'). Cai para o proprio code se desconhecido."""
        try:
            import tuya_control
            spec = tuya_control.SENSOR_LABELS.get(code)
        except Exception:
            spec = None
        if spec:
            name, on_text, off_text = spec
            return name, (on_text if value else off_text)
        return code, ("ligado" if value else "desligado")


def _safe(name):
    import re
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", (name or "cam").strip()).strip("_") or "cam"

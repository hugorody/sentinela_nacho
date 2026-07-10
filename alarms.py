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
      }
    }

- 'windows' vazio = ativo 24h (quando enabled). Janelas podem cruzar a meia-noite
  (start > end, ex.: 22:00->06:00).
- Quando um rosto DESCONHECIDO e visto numa camera ativa (e dentro da janela),
  dispara um e-mail via Resend com um screenshot em anexo. Ha um cooldown por
  camera para nao floodar.

O disparo roda numa thread separada (envio de e-mail nao pode travar o loop de
IA). Uso pelo engine:
    alarm.notify_unknown(camera_name, frame, score)
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

    def __init__(self, config_path=CONFIG, env_path=".env", cooldown=120.0):
        self.config_path = config_path
        self.env_path = env_path
        self.cooldown = cooldown           # segundos entre e-mails da mesma camera
        self._lock = threading.Lock()
        self._last_sent = {}               # camera_name -> epoch do ultimo e-mail
        self._cfg = self._load()

    # ---- config ----

    def _load(self):
        p = Path(self.config_path)
        if not p.exists():
            return {"email": "", "cameras": {}}
        try:
            import json
            data = json.loads(p.read_text())
            data.setdefault("email", "")
            data.setdefault("cameras", {})
            return data
        except (OSError, ValueError):
            return {"email": "", "cameras": {}}

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
            specific = (cam.get("recipients") or "").strip()
            base = specific or self._cfg.get("email", "")
        return [e.strip() for e in base.replace(";", ",").split(",") if e.strip()]

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
        params = {"from": sender, "to": recipients, "subject": subject, "html": html}

        jpg = self._encode_jpeg(frame)
        if jpg is not None:
            params["attachments"] = [{
                "filename": f"{_safe(camera_name)}_{datetime.now():%Y%m%d_%H%M%S}.jpg",
                "content": list(jpg),  # Resend aceita lista de bytes
            }]
        try:
            resend.Emails.send(params)
            print(f"[alarms] e-mail de alarme enviado ({camera_name}) -> {recipients}")
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


def _safe(name):
    import re
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", (name or "cam").strip()).strip("_") or "cam"

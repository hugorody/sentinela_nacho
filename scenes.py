#!/usr/bin/env python3
"""
scenes.py - Cenas (automacoes) do Sentinela.

Uma CENA liga um GATILHO a uma ou mais ACOES:

  Gatilhos (trigger.type):
    * "camera"   - um evento de camera dispara a cena.
                   trigger = {type:"camera", camera:"<nome>|*",
                              event:"known"|"unknown"|"any",
                              person:"<nome>|"" }
    * "schedule" - um horario dispara a cena (todo dia ou em dias da semana).
                   trigger = {type:"schedule", time:"HH:MM",
                              days:[0..6] }  (0=segunda ... 6=domingo; vazio=todos)
    * "device"   - uma tecla de um dispositivo smart home muda de estado.
                   trigger = {type:"device", device:"<id>", code:"switch_1",
                              state:"on"|"off"|"any_change" }
                   Dispara na TRANSICAO (o estado e lido por polling, ~6s).

  Acoes (action.type):
    * "switch"     - liga/desliga uma tecla de um dispositivo.
                     {type:"switch", device:"<id>", code:"switch_1", on:true}
    * "brightness" - ajusta o brilho de uma luz (0..100).
                     {type:"brightness", device:"<id>", pct:70}
    * "all"        - acende/apaga todos os dispositivos.
                     {type:"all", on:true}

As cenas ficam em scenes.json. Um thread interno (start/stop) verifica os
gatilhos de horario a cada ~30s; os gatilhos de camera sao empurrados pelo
engine via on_camera_event(). A execucao das acoes usa o Controller (Tuya).
"""

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

CONFIG = "scenes.json"


def _match_event(trigger, camera, known):
    """True se um evento de camera (camera, known) casa com o gatilho."""
    if trigger.get("type") != "camera":
        return False
    tcam = trigger.get("camera", "*")
    if tcam not in ("*", "", camera):
        return False
    ev = trigger.get("event", "any")
    if ev == "known" and not known:
        return False
    if ev == "unknown" and known:
        return False
    return True


class SceneManager:
    """Guarda as cenas, executa acoes e roda os gatilhos de horario."""

    def __init__(self, controller=None, config_path=CONFIG,
                 camera_cooldown=15.0, poll_interval=6.0):
        self.controller = controller        # tuya_control.Controller (pode ser None)
        self.config_path = config_path
        self.camera_cooldown = camera_cooldown
        self.poll_interval = poll_interval   # segundos entre leituras de estado
        self._lock = threading.Lock()
        self._scenes = self._load()
        self._last_fired = {}                # scene_id -> epoch (cooldown camera)
        self._last_minute = None             # evita disparar 2x no mesmo minuto
        # Ultimo estado conhecido de cada (device, code) -> bool. Serve para
        # detectar TRANSICOES (dispara na mudanca, nao a cada leitura).
        self._dev_state = {}
        self._stop = threading.Event()
        self._thread = None
        self.on_log = None                   # callback opcional p/ registrar no log

    # ---- persistencia ----

    def _load(self):
        p = Path(self.config_path)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text())
            return data.get("scenes", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            return []

    def _save(self):
        Path(self.config_path).write_text(
            json.dumps({"scenes": self._scenes}, indent=2, ensure_ascii=False) + "\n")

    def reload(self):
        with self._lock:
            self._scenes = self._load()

    # ---- CRUD ----

    def list_scenes(self):
        with self._lock:
            import copy
            return copy.deepcopy(self._scenes)

    def get_scene(self, scene_id):
        return next((s for s in self._scenes if s.get("id") == scene_id), None)

    def save_scene(self, scene):
        """Cria (sem id) ou atualiza (com id) uma cena. Retorna a cena salva."""
        with self._lock:
            sid = scene.get("id")
            clean = {
                "id": sid or uuid.uuid4().hex[:12],
                "name": (scene.get("name") or "Cena").strip(),
                "enabled": bool(scene.get("enabled", True)),
                "trigger": scene.get("trigger") or {},
                "actions": scene.get("actions") or [],
            }
            if sid and self.get_scene(sid) is not None:
                for i, s in enumerate(self._scenes):
                    if s.get("id") == sid:
                        self._scenes[i] = clean
                        break
            else:
                self._scenes.append(clean)
            self._save()
            return dict(clean)

    def delete_scene(self, scene_id):
        with self._lock:
            n = len(self._scenes)
            self._scenes = [s for s in self._scenes if s.get("id") != scene_id]
            changed = len(self._scenes) != n
            if changed:
                self._save()
            return changed

    def set_enabled(self, scene_id, enabled):
        with self._lock:
            s = self.get_scene(scene_id)
            if s is None:
                return False
            s["enabled"] = bool(enabled)
            self._save()
            return True

    # ---- execucao de acoes ----

    def run_scene(self, scene_id):
        """Dispara manualmente as acoes de uma cena (ignora gatilho)."""
        s = self.get_scene(scene_id)
        if s is None:
            return {"error": "cena nao encontrada"}
        return self._run_actions(s)

    def _run_actions(self, scene):
        results = []
        for action in scene.get("actions", []):
            results.append(self._run_action(action))
        ok = sum(1 for r in results if r.get("ok"))
        self._log(f"Cena '{scene.get('name')}' executada: "
                  f"{ok}/{len(results)} acao(oes) ok")
        return {"ok": True, "actions": results,
                "done": ok, "total": len(results)}

    def _run_action(self, action):
        if self.controller is None:
            return {"ok": False, "error": "smart home nao configurado"}
        atype = action.get("type")
        try:
            if atype == "switch":
                return self.controller.set_switch(
                    action.get("device"), action.get("code", "switch_1"),
                    bool(action.get("on")))
            if atype == "brightness":
                return self.controller.set_brightness(
                    action.get("device"), int(action.get("pct", 100)))
            if atype == "all":
                return self.controller.set_all(bool(action.get("on")))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": f"acao desconhecida: {atype}"}

    # ---- gatilho de camera (chamado pelo engine) ----

    def on_camera_event(self, camera, name, known):
        """Avalia as cenas de camera para um evento e executa as que casarem."""
        now = time.time()
        fired = []
        with self._lock:
            candidates = [s for s in self._scenes if s.get("enabled")
                          and _match_event(s.get("trigger", {}), camera, known)]
        for s in candidates:
            # 'person' opcional: so dispara para uma pessoa especifica.
            person = (s.get("trigger", {}).get("person") or "").strip()
            if person and person != (name or ""):
                continue
            with self._lock:
                if now - self._last_fired.get(s["id"], 0.0) < self.camera_cooldown:
                    continue
                self._last_fired[s["id"]] = now
            self._run_actions(s)
            fired.append(s["id"])
        return fired

    # ---- thread interno: gatilhos de horario e de dispositivo ----

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        """Verifica horarios (por minuto) e estado dos dispositivos (por poll)."""
        while not self._stop.is_set():
            try:
                self._check_schedules(datetime.now())
                self._check_devices()
            except Exception as exc:
                print(f"[scenes] loop: {exc}")
            self._stop.wait(self.poll_interval)

    def _check_schedules(self, dt):
        minute = dt.strftime("%Y-%m-%d %H:%M")
        if minute == self._last_minute:
            return  # ja avaliamos este minuto
        self._last_minute = minute
        hm = dt.strftime("%H:%M")
        weekday = dt.weekday()  # 0=segunda ... 6=domingo
        with self._lock:
            due = []
            for s in self._scenes:
                if not s.get("enabled"):
                    continue
                t = s.get("trigger", {})
                if t.get("type") != "schedule" or t.get("time") != hm:
                    continue
                days = t.get("days") or []
                if days and weekday not in days:
                    continue
                due.append(s)
        for s in due:
            self._run_actions(s)

    # ---- gatilho de dispositivo (polling de estado) ----

    def _check_devices(self):
        """Le o estado dos dispositivos com cenas e dispara nas transicoes."""
        if self.controller is None:
            return
        # Quais dispositivos precisam ser consultados (tem cena habilitada)?
        with self._lock:
            dev_scenes = [s for s in self._scenes if s.get("enabled")
                          and s.get("trigger", {}).get("type") == "device"]
        if not dev_scenes:
            return
        device_ids = {s["trigger"].get("device") for s in dev_scenes
                      if s["trigger"].get("device")}

        # Le o estado de cada dispositivo uma unica vez.
        states = {}
        for dev_id in device_ids:
            try:
                st = self.controller.get_state(dev_id)
            except Exception:
                st = {}
            if st.get("online"):
                states[dev_id] = st.get("switches") or {}

        for s in dev_scenes:
            t = s["trigger"]
            dev_id, code = t.get("device"), t.get("code", "switch_1")
            switches = states.get(dev_id)
            if switches is None or code not in switches:
                continue  # offline ou sem essa tecla neste ciclo
            cur = bool(switches[code])
            key = (dev_id, code)
            prev = self._dev_state.get(key)
            self._dev_state[key] = cur
            if prev is None or prev == cur:
                continue  # sem transicao (primeira leitura ou estado estavel)
            want = t.get("state", "any_change")
            if want == "on" and not cur:
                continue
            if want == "off" and cur:
                continue
            self._run_actions(s)

    # ---- log ----

    def _log(self, msg):
        if callable(self.on_log):
            try:
                self.on_log(msg)
            except Exception:
                pass
        print(f"[scenes] {msg}")

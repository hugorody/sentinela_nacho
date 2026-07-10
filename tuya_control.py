#!/usr/bin/env python3
"""
tuya_control.py - Controle unificado dos dispositivos Tuya (Smart home).

Cada aparelho e controlado por um de dois caminhos, escolhido automaticamente:

  * LAN  - dispositivos Wi-Fi (Led Painel, Smart Led, Servico entrada): falamos
           direto com o aparelho na rede local usando a local_key. Rapido e sem
           internet.
  * Cloud- dispositivos Zigbee atras do Hub (interruptores, sensor de porta):
           nao tem IP proprio, entao passamos pela Cloud API da Tuya.

A escolha vem de tuya_devices.json (gerado por tuya_setup.py): quem tem 'ip'
e Wi-Fi -> LAN; o resto -> Cloud.

API principal:
    ctrl = Controller()
    ctrl.list_devices()             # [{id, name, kind, channels, ...}, ...]
    ctrl.get_state(dev_id)          # {"switches": {"switch_1": bool, ...},
                                    #  "brightness": int|None, "online": bool}
    ctrl.set_switch(dev_id, code, on)
    ctrl.set_brightness(dev_id, pct)   # 0-100
"""

import concurrent.futures
import json
import threading
from pathlib import Path

import tinytuya

DEVICES = "tuya_devices.json"
LABELS = "tuya_labels.json"
ENV = ".env"

# Categorias Tuya que sabemos apresentar. 'switch' = uma ou mais teclas de
# liga/desliga; 'light' = lampada/fita com brilho; 'sensor' = so leitura.
_CATEGORY_KIND = {
    "kg": "switch",   # interruptor (1..N teclas)
    "cz": "light",    # tomada/regua com cor+brilho (aqui: Led Painel)
    "dd": "light",    # fita de LED
    "dj": "light",    # lampada
    "mcs": "sensor",  # sensor de porta/janela
    "wg2": "hub",     # gateway Zigbee (sem controle proprio)
}

# Estados booleanos de sensores (nao sao teclas: sao so leitura, mas servem de
# gatilho para Cenas). Codes tipicos dos sensores Tuya via nuvem.
_SENSOR_CODES = {"doorcontact_state", "pir", "presence_state",
                 "watersensor_state", "smoke_sensor_status", "temper_alarm"}

# Rotulos amigaveis por code de sensor: (nome, texto p/ estado True/False).
SENSOR_LABELS = {
    "doorcontact_state": ("Porta", "aberta", "fechada"),
    "pir": ("Movimento", "detectado", "sem movimento"),
    "presence_state": ("Presença", "detectada", "ausente"),
    "watersensor_state": ("Vazamento", "detectado", "seco"),
    "smoke_sensor_status": ("Fumaça", "detectada", "normal"),
    "temper_alarm": ("Violação", "acionada", "normal"),
}

# DPs usados no controle LAN de luzes (protocolo local nao usa os 'codes' da
# nuvem, e sim numeros de DP). Padrao dos produtos de iluminacao Tuya.
_DP_LIGHT_SWITCH = "1"      # liga/desliga
_DP_LIGHT_BRIGHT = "22"     # brilho no modo branco (escala 10..1000)
_DP_LIGHT_BRIGHT_ALT = "20"  # algumas fitas usam 20 p/ brilho
_BRIGHT_MIN, _BRIGHT_MAX = 10, 1000


def _load_env(path=ENV):
    env = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


class Controller:
    """Controla os dispositivos Tuya, escolhendo LAN ou Cloud por aparelho."""

    def __init__(self, devices_path=DEVICES, env_path=ENV, labels_path=LABELS):
        self.devices_path = devices_path
        self.labels_path = labels_path
        self.env_path = env_path
        self._lock = threading.Lock()
        self._lan_cache = {}   # dev_id -> tinytuya.Device (conexao reaproveitada)
        self._cloud = None
        self._devices = self._load_devices()
        self._labels = self._load_labels()  # {dev_id: {code: nome_da_tecla}}
        self._env = _load_env(env_path)

    # --- carga / metadados --------------------------------------------------

    def _load_devices(self):
        p = Path(self.devices_path)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return []

    def reload(self):
        with self._lock:
            self._devices = self._load_devices()
            self._labels = self._load_labels()
            self._env = _load_env(self.env_path)
            self._cloud = None       # forca recriar com as credenciais novas
            self._lan_cache.clear()

    # --- rotulos das teclas (nome amigavel por code, dado pelo usuario) ------

    def _load_labels(self):
        p = Path(self.labels_path)
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_labels(self):
        Path(self.labels_path).write_text(
            json.dumps(self._labels, indent=2, ensure_ascii=False) + "\n")

    def label_channel(self, dev_id, code, name):
        """Nomeia (ou limpa, se name vazio) uma tecla/canal de um dispositivo."""
        if self._by_id(dev_id) is None:
            return False
        with self._lock:
            name = (name or "").strip()
            slot = self._labels.setdefault(dev_id, {})
            if name:
                slot[code] = name
            else:
                slot.pop(code, None)
                if not slot:
                    self._labels.pop(dev_id, None)
            self._save_labels()
        return True

    def _by_id(self, dev_id):
        for d in self._devices:
            if d.get("id") == dev_id:
                return d
        return None

    def _kind(self, dev):
        return _CATEGORY_KIND.get(dev.get("category", ""), "switch")

    def _is_lan(self, dev):
        """LAN quando o aparelho tem IP proprio na rede (Wi-Fi direto)."""
        return bool(dev.get("ip")) and self._kind(dev) != "hub"

    # --- conexoes -----------------------------------------------------------

    def _cloud_api(self):
        if self._cloud is None:
            self._cloud = tinytuya.Cloud(
                apiRegion=self._env.get("tuya_api_region", "us"),
                apiKey=self._env.get("tuya_api_key", ""),
                apiSecret=self._env.get("tuya_api_secret", ""),
            )
        return self._cloud

    def _lan_device(self, dev):
        d = self._lan_cache.get(dev["id"])
        if d is None:
            d = tinytuya.Device(
                dev["id"], dev["ip"], dev["key"],
                version=float(dev.get("version", "3.3")),
            )
            d.set_socketTimeout(5)
            d.set_socketPersistent(True)  # mantem a conexao TCP aberta
            self._lan_cache[dev["id"]] = d
        return d

    # --- listagem -----------------------------------------------------------

    def list_devices(self):
        """Metadados de todos os dispositivos (sem consultar estado)."""
        out = []
        for d in self._devices:
            kind = self._kind(d)
            out.append({
                "id": d.get("id"),
                "name": d.get("name", ""),
                "kind": kind,
                "category": d.get("category", ""),
                "via": "lan" if self._is_lan(d) else "cloud",
                # controllable = pode ser ACAO (ligar/desligar). sensor nao.
                "controllable": kind in ("switch", "light"),
                # observable = pode ser GATILHO de cena (estado observavel).
                # Sensores entram aqui mesmo sem serem controlaveis.
                "observable": kind in ("switch", "light", "sensor"),
                "labels": dict(self._labels.get(d.get("id"), {})),
            })
        return out

    # --- leitura de estado --------------------------------------------------

    def get_state(self, dev_id):
        dev = self._by_id(dev_id)
        if dev is None:
            return {"error": "dispositivo desconhecido"}
        with self._lock:
            if self._is_lan(dev):
                return self._lan_state(dev)
            return self._cloud_state(dev)

    def _lan_state(self, dev):
        try:
            st = self._lan_device(dev).status() or {}
        except Exception as exc:
            return {"online": False, "error": str(exc)}
        dps = st.get("dps")
        if not isinstance(dps, dict):
            return {"online": False, "error": st.get("Error", "sem resposta")}
        # No LAN so o DP "1" (liga/desliga principal) e uma tecla de verdade;
        # outros bools sao flags internas (ex.: DP 39 = modo memoria da luz).
        switches = {}
        if isinstance(dps.get(_DP_LIGHT_SWITCH), bool):
            switches["switch_1"] = dps[_DP_LIGHT_SWITCH]
        bright = None
        for dp in (_DP_LIGHT_BRIGHT, _DP_LIGHT_BRIGHT_ALT):
            if isinstance(dps.get(dp), (int, float)):
                bright = self._raw_to_pct(dps[dp])
                break
        return {"online": True, "switches": switches, "brightness": bright}

    def _cloud_state(self, dev):
        try:
            r = self._cloud_api().getstatus(dev["id"])
        except Exception as exc:
            return {"online": False, "error": str(exc)}
        if not isinstance(r, dict) or not r.get("success"):
            return {"online": False, "error": (r or {}).get("msg", "falha na nuvem")}
        # switch_backlight (luz de fundo do interruptor) e switch_inching nao
        # sao teclas de carga; ignoramos para nao virarem botoes falsos.
        _NOT_LOAD = {"switch_backlight", "switch_inching"}
        switches, bright, battery = {}, None, None
        for item in r.get("result", []):
            code, val = item.get("code", ""), item.get("value")
            if code in ("battery_percentage", "battery_state") and isinstance(val, (int, float)):
                battery = int(val)
                continue
            if not isinstance(val, bool):
                if code in ("bright_value", "bright_value_v2") and isinstance(val, (int, float)):
                    bright = self._raw_to_pct(val)
                continue
            if code in _NOT_LOAD:
                continue
            # Teclas de carga (switch*) e estados de sensor (doorcontact_state,
            # pir, presence_state...) sao ambos bools observaveis: viram
            # "switches" para reusar a maquinaria de estado/gatilho das Cenas.
            if code.startswith("switch") or code in _SENSOR_CODES:
                switches[code] = val
        st = {"online": True, "switches": switches, "brightness": bright}
        if battery is not None:
            st["battery"] = battery
        return st

    @staticmethod
    def _raw_to_pct(raw):
        pct = round((raw - _BRIGHT_MIN) / (_BRIGHT_MAX - _BRIGHT_MIN) * 100)
        return max(0, min(100, pct))

    @staticmethod
    def _pct_to_raw(pct):
        pct = max(0, min(100, pct))
        return round(_BRIGHT_MIN + pct / 100 * (_BRIGHT_MAX - _BRIGHT_MIN))

    # --- comandos -----------------------------------------------------------

    def set_switch(self, dev_id, code, on):
        dev = self._by_id(dev_id)
        if dev is None:
            return {"error": "dispositivo desconhecido"}
        with self._lock:
            if self._is_lan(dev):
                dp = _DP_LIGHT_SWITCH if code in ("switch_1", "switch") else code.replace("switch_", "")
                try:
                    self._lan_device(dev).set_value(dp, bool(on))
                    return {"ok": True}
                except Exception as exc:
                    return {"error": str(exc)}
            return self._cloud_command(dev, code, bool(on))

    def set_brightness(self, dev_id, pct):
        dev = self._by_id(dev_id)
        if dev is None:
            return {"error": "dispositivo desconhecido"}
        raw = self._pct_to_raw(int(pct))
        with self._lock:
            if self._is_lan(dev):
                try:
                    self._lan_device(dev).set_value(_DP_LIGHT_BRIGHT, raw)
                    return {"ok": True}
                except Exception as exc:
                    return {"error": str(exc)}
            return self._cloud_command(dev, "bright_value_v2", raw)

    def _cloud_command(self, dev, code, value):
        try:
            r = self._cloud_api().sendcommand(
                dev["id"], {"commands": [{"code": code, "value": value}]})
        except Exception as exc:
            return {"error": str(exc)}
        if isinstance(r, dict) and r.get("success"):
            return {"ok": True}
        return {"error": (r or {}).get("msg", "falha na nuvem")}

    # --- comando em massa (acender/apagar tudo) -----------------------------

    def set_all(self, on):
        """Liga (ou desliga) todas as teclas de todos os aparelhos controlaveis.

        Cada dispositivo e tratado numa thread (descobre as teclas pelo estado
        atual e aciona uma a uma). Os metodos publicos pegam o lock por conta
        propria, entao aqui NAO seguramos o lock (evita serializar/travar).
        Retorna um resumo: quantos dispositivos e teclas tiveram sucesso.
        """
        targets = [d for d in self._devices
                   if self._kind(d) in ("switch", "light")]

        def do_one(dev):
            state = self.get_state(dev["id"])
            if not state.get("online"):
                return (0, 0, 1)  # (ok, falhas, offline)
            codes = list((state.get("switches") or {}).keys()) or ["switch_1"]
            ok = fail = 0
            for code in codes:
                if self.set_switch(dev["id"], code, on).get("ok"):
                    ok += 1
                else:
                    fail += 1
            return (ok, fail, 0)

        ok = fail = offline = 0
        if targets:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(8, len(targets))) as ex:
                for o, f, off in ex.map(do_one, targets):
                    ok += o
                    fail += f
                    offline += off
        return {"ok": True, "on": bool(on), "devices": len(targets),
                "switched": ok, "failed": fail, "offline": offline}

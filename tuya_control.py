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
    "wsdcg": "sensor",  # sensor de temperatura e umidade
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

# Leituras NUMERICAS de sensores (temperatura, umidade). Diferente dos sensores
# booleanos acima, sao valores continuos: viram o campo "readings" do estado
# (nao "switches") e servem de gatilho por LIMITE, nao por transicao.
#
# Mapeamos por code de nuvem e por DP local. 'scale' e a casa decimal implicita
# do Tuya (valor_real = valor_bruto / 10**scale). Este produto (wsdcg) usa
# scale=0 (valores diretos), confirmado pela specification da nuvem; outros
# modelos usam scale=1 (ex.: 205 -> 20.5 C).
# (reading, unidade, scale, rotulo).
_READING_SPEC = {
    "va_temperature": ("temperature", "°C", 0, "Temperatura"),
    "va_humidity":    ("humidity", "%", 0, "Umidade"),
    "temp_current":   ("temperature", "°C", 1, "Temperatura"),
    "humidity_value": ("humidity", "%", 0, "Umidade"),
}
# DP local (via Hub) -> mesmo destino que os codes de nuvem acima. Confirmado
# lendo o sensor: DP 101=umidade, 103=temperatura (102=bateria, tratado a parte).
_READING_DP = {
    "101": ("humidity", "%", 0, "Umidade"),
    "103": ("temperature", "°C", 0, "Temperatura"),
}

# Rotulos amigaveis das leituras numericas (reading -> (nome, unidade)).
READING_LABELS = {
    "temperature": ("Temperatura", "°C"),
    "humidity": ("Umidade", "%"),
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
        self._lock = threading.Lock()      # protege caches/config (rapido)
        self._lan_cache = {}   # dev_id -> tinytuya.Device (conexao reaproveitada)
        self._gw_cache = {}    # gateway_id -> tinytuya.Device (Hub, reaproveitado)
        # Lock por CONEXAO fisica: cada aparelho Wi-Fi tem o seu; todos os
        # sub-dispositivos de um Hub compartilham o lock do Hub (conexao unica).
        # Assim Wi-Fi e Hub operam em paralelo, mas o I/O de uma mesma conexao
        # nao se atropela.
        self._io_locks = {}
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
            self._gw_cache.clear()

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
        with self._lock:
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

    # --- controle local de sub-dispositivos Zigbee (via Hub) ----------------

    def _gateway_dev(self, gateway_id):
        """Metadados do Hub (gateway) de um sub-dispositivo. None se ausente."""
        gw = self._by_id(gateway_id)
        return gw if gw and gw.get("ip") else None

    def _sub_local(self, dev):
        """Conexao LOCAL de um sub-dispositivo Zigbee, atraves do Hub.

        Retorna um tinytuya.Device ligado ao gateway (persistente e cacheado)
        usando o node_id como 'cid'. None se o Hub nao tiver IP conhecido
        (ai o controle cai para a nuvem).
        """
        gw_meta = self._gateway_dev(dev.get("gateway_id"))
        if gw_meta is None or not dev.get("node_id"):
            return None
        with self._lock:
            gw = self._gw_cache.get(gw_meta["id"])
            if gw is None:
                gw = tinytuya.Device(
                    gw_meta["id"], address=gw_meta["ip"], local_key=gw_meta["key"],
                    version=float(gw_meta.get("version", "3.3")), persist=True,
                )
                gw.set_socketTimeout(5)
                self._gw_cache[gw_meta["id"]] = gw
            sub = self._lan_cache.get(dev["id"])
            if sub is None:
                sub = tinytuya.Device(
                    dev_id=gw_meta["id"], cid=dev["node_id"], parent=gw,
                    version=float(gw_meta.get("version", "3.3")),
                )
                self._lan_cache[dev["id"]] = sub
            return sub

    def _is_zigbee(self, dev):
        """Sub-dispositivo Zigbee: tem node_id e um Hub com IP na rede."""
        return bool(dev.get("node_id")) and self._gateway_dev(dev.get("gateway_id")) is not None

    def _io_lock_for(self, dev):
        """Lock da CONEXAO fisica do dispositivo (por Hub, por aparelho LAN, ou
        um lock de nuvem compartilhado). Serializa o I/O sem bloquear conexoes
        independentes."""
        if self._is_zigbee(dev):
            key = "gw:" + dev.get("gateway_id", "")
        elif self._is_lan(dev):
            key = "lan:" + dev["id"]
        else:
            key = "cloud"
        with self._lock:
            lk = self._io_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._io_locks[key] = lk
            return lk

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
                # 'lan' = Wi-Fi direto; 'hub' = Zigbee controlado localmente
                # pelo Hub; 'cloud' = precisa da nuvem.
                "via": ("lan" if self._is_lan(d)
                        else "hub" if self._is_zigbee(d) else "cloud"),
                # controllable = pode ser ACAO (ligar/desligar). sensor nao.
                "controllable": kind in ("switch", "light"),
                # observable = pode ser GATILHO de cena (estado observavel).
                # Sensores entram aqui mesmo sem serem controlaveis.
                "observable": kind in ("switch", "light", "sensor"),
                # readings = leituras numericas que o sensor oferece (temperatura,
                # umidade). Vazio para quem so tem estados booleanos.
                "readings": self._reading_names(d),
                "labels": dict(self._labels.get(d.get("id"), {})),
            })
        return out

    @staticmethod
    def _reading_names(dev):
        """Leituras numericas que um dispositivo oferece, pela categoria."""
        if dev.get("category") == "wsdcg":
            return ["temperature", "humidity"]
        return []

    # --- leitura de estado --------------------------------------------------

    def get_state(self, dev_id):
        dev = self._by_id(dev_id)
        if dev is None:
            return {"error": "dispositivo desconhecido"}
        # I/O sob o lock da CONEXAO (Hub/LAN/nuvem), nao um lock global: conexoes
        # independentes rodam em paralelo (essencial p/ acender/apagar tudo).
        with self._io_lock_for(dev):
            if self._is_lan(dev):
                return self._lan_state(dev)
            if self._is_zigbee(dev):
                st = self._hub_state(dev)
                if st.get("online"):
                    return st
            return self._cloud_state(dev)

    def _hub_state(self, dev):
        """Le o estado de um sub-dispositivo Zigbee LOCALMENTE, via Hub.

        Sub-dispositivos reportam DPs numericos ('1'..'6' = teclas). Mapeamos
        para switch_N; sensores (kind sensor) usam o DP conhecido do estado.
        """
        sub = self._sub_local(dev)
        if sub is None:
            return {"online": False}
        try:
            st = sub.status() or {}
        except Exception as exc:
            return {"online": False, "error": str(exc)}
        dps = st.get("dps")
        if not isinstance(dps, dict):
            return {"online": False, "error": st.get("Error", "sem resposta")}
        switches, readings, battery = {}, {}, None
        kind = self._kind(dev)
        for dp, val in dps.items():
            if isinstance(val, bool):
                if kind == "sensor":
                    # Sensor de porta local: DP '1' = doorcontact_state.
                    if dp == "1":
                        switches["doorcontact_state"] = val
                else:
                    switches[f"switch_{dp}"] = val
            elif kind == "sensor" and isinstance(val, (int, float)):
                spec = _READING_DP.get(dp)
                if spec:
                    name, unit, scale, _ = spec
                    readings[name] = round(val / (10 ** scale), 1 if scale else 0)
                elif dp in ("2", "3", "4", "102"):
                    # Bateria: sensor de porta reporta no DP 3/4; o de
                    # temperatura/umidade no DP 102.
                    battery = int(val)
        out = {"online": True, "switches": switches, "brightness": None}
        if readings:
            out["readings"] = readings
        if battery is not None:
            out["battery"] = battery
        return out

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
        switches, readings, bright, battery = {}, {}, None, None
        for item in r.get("result", []):
            code, val = item.get("code", ""), item.get("value")
            if code in ("battery_percentage", "battery_state", "va_battery") \
                    and isinstance(val, (int, float)):
                battery = int(val)
                continue
            if not isinstance(val, bool):
                if code in ("bright_value", "bright_value_v2") and isinstance(val, (int, float)):
                    bright = self._raw_to_pct(val)
                elif code in _READING_SPEC and isinstance(val, (int, float)):
                    name, unit, scale, _ = _READING_SPEC[code]
                    readings[name] = round(val / (10 ** scale), 1 if scale else 0)
                continue
            if code in _NOT_LOAD:
                continue
            # Teclas de carga (switch*) e estados de sensor (doorcontact_state,
            # pir, presence_state...) sao ambos bools observaveis: viram
            # "switches" para reusar a maquinaria de estado/gatilho das Cenas.
            if code.startswith("switch") or code in _SENSOR_CODES:
                switches[code] = val
        st = {"online": True, "switches": switches, "brightness": bright}
        if readings:
            st["readings"] = readings
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
        # code "switch_N" -> DP numerico "N" (protocolo local).
        dp = _DP_LIGHT_SWITCH if code in ("switch_1", "switch") else code.replace("switch_", "")
        with self._io_lock_for(dev):
            if self._is_lan(dev):
                try:
                    self._lan_device(dev).set_value(dp, bool(on))
                    return {"ok": True}
                except Exception as exc:
                    return {"error": str(exc)}
            # Zigbee: tenta pelo Hub local; se falhar, cai para a nuvem.
            if self._is_zigbee(dev):
                r = self._hub_command(dev, dp, bool(on))
                if r.get("ok"):
                    return r
            return self._cloud_command(dev, code, bool(on))

    def set_brightness(self, dev_id, pct):
        dev = self._by_id(dev_id)
        if dev is None:
            return {"error": "dispositivo desconhecido"}
        raw = self._pct_to_raw(int(pct))
        with self._io_lock_for(dev):
            if self._is_lan(dev):
                try:
                    self._lan_device(dev).set_value(_DP_LIGHT_BRIGHT, raw)
                    return {"ok": True}
                except Exception as exc:
                    return {"error": str(exc)}
            if self._is_zigbee(dev):
                r = self._hub_command(dev, _DP_LIGHT_BRIGHT, raw)
                if r.get("ok"):
                    return r
            return self._cloud_command(dev, "bright_value_v2", raw)

    def _hub_command(self, dev, dp, value, nowait=False):
        """Envia um comando a um sub-dispositivo Zigbee LOCALMENTE, via Hub.

        nowait=True dispara sem esperar a confirmacao (~10ms vs ~180ms). Usado
        no acender/apagar tudo, onde o estado e relido depois; e evita o timeout
        longo ao mandar para uma tecla que o interruptor nao possui.
        """
        sub = self._sub_local(dev)
        if sub is None:
            return {"ok": False, "error": "hub indisponivel"}
        try:
            r = sub.set_value(dp, value, nowait=nowait)
            if not nowait and isinstance(r, dict) and r.get("Error"):
                return {"ok": False, "error": r.get("Error")}
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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

    def _switch_codes(self, dev):
        """Teclas de carga de um dispositivo.

        Luz = switch_1. Interruptor Zigbee (Hub) = switch_1..6: teclas extras
        sao inofensivas la (disparadas com nowait, o Hub ignora as inexistentes).
        Interruptor Wi-Fi (LAN direto) = so as teclas REAIS, lidas do estado --
        mandar teclas inexistentes no protocolo LAN trava ~5s cada esperando
        resposta.
        """
        kind = self._kind(dev)
        if kind == "light":
            return ["switch_1"]
        if self._is_lan(dev):
            st = self._lan_state(dev)
            codes = sorted((st.get("switches") or {}).keys())
            return codes or ["switch_1"]
        codes = ["switch_%d" % i for i in range(1, 7)]
        labels = self._labels.get(dev.get("id"), {})
        for c in labels:
            if c.startswith("switch") and c not in codes:
                codes.append(c)
        return codes

    def set_all(self, on):
        """Liga (ou desliga) todas as teclas de todos os aparelhos controlaveis.

        Estrategia por tipo de conexao:
          * Zigbee (Hub): uma unica passagem, disparando todas as teclas com
            nowait=True (~10ms cada). Reusa a conexao do Hub; nao espera
            confirmacao (a UI rele o estado depois). Evita o gargalo de abrir
            conexao e esperar resposta por tecla.
          * Wi-Fi: em paralelo (conexoes independentes), caminho normal.
        As teclas vem de _switch_codes (sem ler o estado antes).
        """
        targets = [d for d in self._devices
                   if self._kind(d) in ("switch", "light")]
        zigbee = [d for d in targets if self._is_zigbee(d)]
        others = [d for d in targets if not self._is_zigbee(d)]

        ok = fail = offline = 0

        # --- Zigbee: rajada nowait numa passagem so (por Hub) ---
        for dev in zigbee:
            with self._io_lock_for(dev):
                sub = self._sub_local(dev)
                if sub is None:
                    offline += 1
                    continue
                acted = False
                for code in self._switch_codes(dev):
                    dp = _DP_LIGHT_SWITCH if code == "switch_1" else code.replace("switch_", "")
                    try:
                        sub.set_value(dp, bool(on), nowait=True)
                        ok += 1
                        acted = True
                    except Exception:
                        fail += 1
                if not acted:
                    offline += 1

        # --- Wi-Fi / nuvem: em paralelo (conexoes independentes) ---
        def do_one(dev):
            o = f = 0
            for code in self._switch_codes(dev):
                r = self.set_switch(dev["id"], code, on)
                if r.get("ok"):
                    o += 1
                elif r.get("error"):
                    f += 1
            return (o, f, 1 if o == 0 else 0)

        if others:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(8, len(others))) as ex:
                for o, f, off in ex.map(do_one, others):
                    ok += o
                    fail += f
                    offline += off

        return {"ok": True, "on": bool(on), "devices": len(targets),
                "switched": ok, "failed": fail, "offline": offline}

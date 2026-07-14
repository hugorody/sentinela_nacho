#!/usr/bin/env python3
"""Ferramentas permitidas que o Nacho pode usar no Sentinela."""

import json
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import envutil


TOOL_DEFINITIONS = [
    {"type": "function", "name": "get_system_status",
     "description": "Consulta se o Sentinela e as câmeras estão funcionando.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "get_camera_presence",
     "description": "Consulta quantas pessoas e quais identidades confirmadas aparecem em uma câmera.",
     "parameters": {"type": "object", "properties": {
         "camera": {"type": "string", "description": "Nome ou id da câmera; vazio lista todas."}},
         "additionalProperties": False}},
    {"type": "function", "name": "get_recent_camera_events",
     "description": "Retorna os eventos recentes de pessoas vistas pelas câmeras.",
     "parameters": {"type": "object", "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
         "additionalProperties": False}},
    {"type": "function", "name": "list_smart_devices",
     "description": "Lista luzes, interruptores e sensores conhecidos pelo Sentinela.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "get_smart_device_state",
     "description": (
         "Consulta o estado de um dispositivo: se uma luz está acesa, se uma "
         "porta/sensor está aberto, ou a temperatura e a umidade de um sensor "
         "ambiente (campo 'readings', ex.: {\"temperature\": 20, \"humidity\": 50}) "
         "e o nível de bateria."),
     "parameters": {"type": "object", "properties": {
         "device": {"type": "string", "description": "Nome ou id do dispositivo."}},
         "required": ["device"], "additionalProperties": False}},
    {"type": "function", "name": "set_smart_device_power",
     "description": (
         "Liga ou desliga uma luz ou interruptor conhecido pelo Sentinela. "
         "Use o nome específico do dispositivo ou da tecla e confirme o resultado retornado."),
     "parameters": {"type": "object", "properties": {
         "device": {"type": "string", "description": "Nome ou id do dispositivo ou da tecla."},
         "on": {"type": "boolean", "description": "true para ligar; false para desligar."}},
         "required": ["device", "on"], "additionalProperties": False}},
    {"type": "function", "name": "list_samsung_tvs",
     "description": "Lista somente as TVs disponíveis na conta SmartThings.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "get_samsung_tv_state",
     "description": "Consulta o estado e as capacidades de uma TV no SmartThings.",
     "parameters": {"type": "object", "properties": {
         "tv": {"type": "string", "description": "Nome da TV."}},
         "required": ["tv"], "additionalProperties": False}},
    {"type": "function", "name": "control_samsung_tv",
     "description": "Controla energia, volume ou mudo de uma TV Samsung via SmartThings.",
     "parameters": {"type": "object", "properties": {
         "tv": {"type": "string", "description": "Nome da TV."},
         "action": {"type": "string", "enum": ["on", "off", "mute", "unmute",
                    "volume_up", "volume_down", "set_volume"]},
         "value": {"type": "integer", "minimum": 0, "maximum": 100,
                   "description": "Volume desejado; usado apenas em set_volume."}},
         "required": ["tv", "action"], "additionalProperties": False}},
    {"type": "function", "name": "get_recent_network_events",
     "description": "Consulta dispositivos que entraram ou saíram recentemente da rede interna.",
     "parameters": {"type": "object", "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
         "additionalProperties": False}},
]


class SentinelaClient:
    def __init__(self, base_url=None, timeout=12):
        self.base = (base_url or envutil.get(
            "sentinela_url", "http://127.0.0.1:8001")).rstrip("/")
        self.timeout = timeout

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=self.timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            return {"error": "Sentinela indisponível", "detail": str(exc)}

    def _post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                detail = {"error": f"Sentinela respondeu HTTP {exc.code}"}
            return detail
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            return {"error": "Sentinela indisponível", "detail": str(exc)}

    @staticmethod
    def _normalized(value):
        text = unicodedata.normalize("NFKD", str(value or "")).casefold()
        return " ".join("".join(c if c.isalnum() else " " for c in text
                                if not unicodedata.combining(c)).split())

    def _resolve_smart_device(self, query):
        data = self._get("/api/smarthome/devices")
        devices = data.get("devices") or []
        wanted = self._normalized(query)
        if not wanted:
            return None, None, {"error": "Informe o nome do dispositivo"}

        generic = {"a", "as", "da", "das", "de", "do", "dos", "lampada",
                   "lampadas", "luz", "luzes", "interruptor"}
        simplified = " ".join(word for word in wanted.split() if word not in generic)
        candidates = []
        for device in devices:
            labels = device.get("labels") or {}
            fields = [(device.get("id"), None), (device.get("name"), None)]
            fields.extend((label, code) for code, label in labels.items())
            for value, code in fields:
                normalized = self._normalized(value)
                if normalized:
                    candidates.append((device, code or "switch_1", normalized))

        match_groups = [
            [(device, code) for device, code, value in candidates if value == wanted],
            [(device, code) for device, code, value in candidates
             if simplified and value == simplified],
            [(device, code) for device, code, value in candidates if wanted in value],
            [(device, code) for device, code, value in candidates
             if simplified and simplified in value],
        ]
        matches = next((group for group in match_groups if group), [])
        matches = list({(device.get("id"), code): (device, code)
                        for device, code in matches}.values())

        if not matches:
            return None, None, {"error": "Dispositivo não encontrado", "query": query}
        if len(matches) > 1:
            return None, None, {
                "error": "Nome de dispositivo ambíguo",
                "query": query,
                "matches": [device.get("name") for device, _ in matches],
            }
        device, code = matches[0]
        if not device.get("controllable"):
            return None, None, {"error": "Dispositivo não pode ser controlado",
                                "device": device.get("name")}
        return device, code, None

    def execute(self, name, args):
        args = args if isinstance(args, dict) else {}
        if name == "get_system_status":
            return self._get("/api/status")
        if name == "get_camera_presence":
            data = self._get("/api/nacho/cameras")
            wanted = str(args.get("camera") or "").casefold()
            if wanted and "cameras" in data:
                matches = [c for c in data["cameras"] if wanted in
                           (c.get("name", "") + " " + c.get("id", "")).casefold()]
                return {"cameras": matches, "count": len(matches)}
            return data
        if name == "get_recent_camera_events":
            limit = max(1, min(int(args.get("limit", 10)), 20))
            data = self._get("/api/events")
            return {"events": (data.get("events") or [])[:limit]}
        if name == "list_smart_devices":
            return self._get("/api/smarthome/devices")
        if name == "get_smart_device_state":
            devices = self._get("/api/smarthome/devices").get("devices") or []
            wanted = str(args.get("device") or "").casefold()
            dev = next((d for d in devices if wanted in
                        (d.get("name", "") + " " + d.get("id", "")).casefold()), None)
            if not dev:
                return {"error": "Dispositivo não encontrado", "query": args.get("device")}
            state = self._get("/api/smarthome/state/" + urllib.parse.quote(dev["id"], safe=""))
            return {"device": dev, "state": state}
        if name == "set_smart_device_power":
            device, code, error = self._resolve_smart_device(args.get("device"))
            if error:
                return error
            on = args.get("on")
            if not isinstance(on, bool):
                return {"error": "O estado deve ser verdadeiro ou falso"}
            result = self._post("/api/smarthome/switch", {
                "id": device["id"], "code": code, "on": on,
            })
            return {"ok": bool(result.get("ok")), "device": device.get("name"),
                    "code": code, "on": on, "result": result}
        if name == "list_samsung_tvs":
            return self._get("/api/smartthings/tvs")
        if name == "get_samsung_tv_state":
            return self._get("/api/smartthings/tv/state/" +
                             urllib.parse.quote(str(args.get("tv") or ""), safe=""))
        if name == "control_samsung_tv":
            payload = {"tv": args.get("tv"), "action": args.get("action")}
            if "value" in args:
                payload["value"] = args["value"]
            return self._post("/api/smartthings/tv/command", payload)
        if name == "get_recent_network_events":
            limit = max(1, min(int(args.get("limit", 10)), 20))
            data = self._get("/api/network")
            return {"events": (data.get("events") or [])[:limit]}
        return {"error": "Ferramenta não autorizada", "tool": name}

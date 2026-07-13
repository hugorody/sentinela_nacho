#!/usr/bin/env python3
"""Ferramentas somente-leitura que o Nacho pode usar no Sentinela."""

import json
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
     "description": "Consulta se uma luz está acesa ou uma porta/sensor está aberto.",
     "parameters": {"type": "object", "properties": {
         "device": {"type": "string", "description": "Nome ou id do dispositivo."}},
         "required": ["device"], "additionalProperties": False}},
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
        if name == "get_recent_network_events":
            limit = max(1, min(int(args.get("limit", 10)), 20))
            data = self._get("/api/network")
            return {"events": (data.get("events") or [])[:limit]}
        return {"error": "Ferramenta não autorizada", "tool": name}

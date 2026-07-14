#!/usr/bin/env python3
"""Cliente mínimo da API SmartThings, restrito a televisores."""

import json
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import envutil


API = "https://api.smartthings.com/v1"
TV_CAPABILITIES = {"tvChannel", "mediaInputSource", "audioVolume", "audioMute"}


class SmartThingsError(RuntimeError):
    pass


class Controller:
    def __init__(self, token=None, timeout=15):
        self.token = token
        self.timeout = timeout

    def _token(self):
        token = self.token or envutil.get("smartthings_token")
        if not token:
            raise SmartThingsError("Token SmartThings não configurado")
        return token

    def _request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            API + path, data=body, method=method,
            headers={"Authorization": "Bearer " + self._token(),
                     "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            if exc.code == 401:
                raise SmartThingsError("Token SmartThings inválido ou expirado") from exc
            raise SmartThingsError(f"SmartThings HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SmartThingsError("SmartThings indisponível") from exc

    @staticmethod
    def _capabilities(device):
        return {cap.get("id") for component in device.get("components") or []
                for cap in component.get("capabilities") or []}

    @classmethod
    def _is_tv(cls, device):
        caps = cls._capabilities(device)
        categories = {cat.get("name", "").casefold()
                      for component in device.get("components") or []
                      for cat in component.get("categories") or []}
        return bool(caps & TV_CAPABILITIES or categories & {"television", "tv"})

    def list_tvs(self):
        items = self._request("GET", "/devices").get("items") or []
        return [{"id": item.get("deviceId"), "name": item.get("label") or item.get("name"),
                 "manufacturer": item.get("manufacturerName", ""),
                 "capabilities": sorted(self._capabilities(item))}
                for item in items if self._is_tv(item)]

    @staticmethod
    def _normalize(value):
        text = unicodedata.normalize("NFKD", str(value or "")).casefold()
        return "".join(c for c in text if not unicodedata.combining(c)).strip()

    def resolve(self, query):
        wanted = self._normalize(query)
        tvs = self.list_tvs()
        exact = [tv for tv in tvs if self._normalize(tv["name"]) == wanted]
        matches = exact or [tv for tv in tvs if wanted in self._normalize(tv["name"])]
        if not matches:
            raise SmartThingsError("TV não encontrada")
        if len(matches) > 1:
            raise SmartThingsError("Nome de TV ambíguo: " + ", ".join(tv["name"] for tv in matches))
        return matches[0]

    def status(self, query):
        tv = self.resolve(query)
        status = self._request("GET", "/devices/" + urllib.parse.quote(tv["id"], safe="") + "/status")
        return {"tv": tv, "status": status}

    def command(self, query, action, value=None):
        tv = self.resolve(query)
        commands = {
            "on": ("switch", "on", []), "off": ("switch", "off", []),
            "mute": ("audioMute", "mute", []), "unmute": ("audioMute", "unmute", []),
            "volume_up": ("audioVolume", "volumeUp", []),
            "volume_down": ("audioVolume", "volumeDown", []),
            "set_volume": ("audioVolume", "setVolume", [max(0, min(100, int(value)))]),
        }
        if action not in commands:
            raise SmartThingsError("Comando de TV não autorizado")
        capability, command, arguments = commands[action]
        if capability not in tv["capabilities"]:
            raise SmartThingsError(f"A TV não oferece o controle {capability}")
        result = self._request("POST", "/devices/" + urllib.parse.quote(tv["id"], safe="") +
                               "/commands", {"commands": [{"component": "main",
                               "capability": capability, "command": command,
                               "arguments": arguments}]})
        accepted = all(item.get("status") == "ACCEPTED" for item in result.get("results") or [])
        return {"ok": accepted, "tv": tv["name"], "action": action, "result": result}

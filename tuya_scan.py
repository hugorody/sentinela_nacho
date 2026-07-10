#!/usr/bin/env python3
"""
tuya_scan.py - Descoberta local de dispositivos smart home Tuya ("Smart home").

Dispositivos Tuya (Novadigital, Smart Life etc.) anunciam presenca a cada ~5s
por broadcast UDP: porta 6666 (protocolo 3.1, JSON puro) e 6667 (3.3+, AES-ECB
com uma chave universal publica). Escutar passivamente ja revela IP, ID do
dispositivo e versao do protocolo - sem nuvem, sem credenciais.

Os nomes dados pelo usuario ficam em smarthome.json (fora do git), no formato
{"devices": [{id, name, ip, version, product_key, last_seen}, ...]}.

Uso como biblioteca:
    import tuya_scan
    devs = tuya_scan.scan_and_merge()   # escuta ~8s e funde com os salvos

Uso direto (teste):
    python3 tuya_scan.py
"""

import json
import select
import socket
import time
from datetime import datetime
from hashlib import md5
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CONFIG = "smarthome.json"
PORTS = (6666, 6667)

# Chave dos broadcasts 3.3+ (porta 6667): md5("yGAdlopoPVldABfn"). E publica e
# identica em todos os dispositivos; protege so o anuncio, nao o controle.
_BCAST_KEY = md5(b"yGAdlopoPVldABfn").digest()


def _decode_packet(data, port):
    """Extrai o dict JSON de um pacote de broadcast Tuya. None se invalido."""
    payload = data[20:-8]  # remove cabecalho (prefixo+seq+cmd+len) e crc+sufixo
    if port == 6667:
        try:
            dec = Cipher(algorithms.AES(_BCAST_KEY), modes.ECB()).decryptor()
            payload = dec.update(payload) + dec.finalize()
            payload = payload[: -payload[-1]]  # padding PKCS#7
        except Exception:
            return None
    try:
        info = json.loads(payload)
    except Exception:
        return None
    return info if isinstance(info, dict) else None


def listen(duration=8.0):
    """Escuta broadcasts por 'duration's -> {device_id: {ip, version, ...}}.

    Como cada dispositivo anuncia a cada ~5s, 8s pega todos os que estao
    ligados. Best-effort: porta ja em uso e apenas ignorada.
    """
    socks = []
    for port in PORTS:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", port))
            socks.append(s)
        except OSError:
            continue

    found = {}
    deadline = time.time() + duration
    while time.time() < deadline and socks:
        ready, _, _ = select.select(socks, [], [], 0.5)
        for s in ready:
            try:
                data, addr = s.recvfrom(4096)
            except OSError:
                continue
            info = _decode_packet(data, s.getsockname()[1])
            if not info or "gwId" not in info:
                continue
            found[info["gwId"]] = {
                "ip": info.get("ip") or addr[0],
                "version": str(info.get("version", "")),
                "product_key": info.get("productKey", ""),
            }
    for s in socks:
        s.close()
    return found


# --- Persistencia (nomes dados pelo usuario sobrevivem entre scans) ---------

def load_devices(path=CONFIG):
    p = Path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("devices", [])
    except (OSError, ValueError):
        return []


def save_devices(devices, path=CONFIG):
    persist = [{k: v for k, v in d.items() if k != "online"} for d in devices]
    Path(path).write_text(
        json.dumps({"devices": persist}, indent=2, ensure_ascii=False) + "\n")


def scan_and_merge(path=CONFIG, duration=8.0):
    """Escuta a rede e funde com os dispositivos salvos (preserva os nomes).

    Retorna a lista completa; cada item ganha 'online' (visto neste scan).
    """
    devices = load_devices(path)
    by_id = {d["id"]: d for d in devices}
    seen = listen(duration)
    now = datetime.now().isoformat(timespec="seconds")
    for did, info in seen.items():
        d = by_id.get(did)
        if d is None:
            d = {"id": did, "name": ""}
            by_id[did] = d
            devices.append(d)
        d.update(ip=info["ip"], version=info["version"],
                 product_key=info["product_key"], last_seen=now)
    save_devices(devices, path)
    for d in devices:
        d["online"] = d["id"] in seen
    return devices


def rename(did, name, path=CONFIG):
    devices = load_devices(path)
    for d in devices:
        if d["id"] == did:
            d["name"] = (name or "").strip()
            save_devices(devices, path)
            return True
    return False


def main():
    t = time.time()
    devs = scan_and_merge()
    print(f"=== {len(devs)} dispositivo(s) em {time.time()-t:.1f}s ===")
    for d in devs:
        estado = "ligado" if d.get("online") else "sem resposta"
        nome = d.get("name") or "-"
        print(f"  {d.get('ip', '?'):15} {d['id']}  v{d.get('version', '?')}  "
              f"{nome}  [{estado}]")


if __name__ == "__main__":
    main()

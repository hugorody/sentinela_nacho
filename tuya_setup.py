#!/usr/bin/env python3
"""
tuya_setup.py - Puxa da nuvem Tuya a lista de dispositivos + local_keys.

Le as credenciais do projeto Cloud (platform.tuya.com) do .env, consulta a
Cloud API e grava tuya_devices.json com {id, name, ip?, key, version} de cada
aparelho. Depois disso o controle (ligar/desligar/brilho) roda 100% local.

    python3 tuya_setup.py
"""

import json
from pathlib import Path

import tinytuya

import tuya_scan

ENV = ".env"
OUT = "tuya_devices.json"


def load_env(path=ENV):
    env = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def main():
    env = load_env()
    api_region = env.get("tuya_api_region", "us")
    api_key = env.get("tuya_api_key", "")
    api_secret = env.get("tuya_api_secret", "")
    if not api_key or not api_secret:
        raise SystemExit("[!] Preencha tuya_api_key e tuya_api_secret no .env")

    cloud = tinytuya.Cloud(apiRegion=api_region, apiKey=api_key,
                           apiSecret=api_secret)
    if isinstance(cloud.token, dict) and "Error" in cloud.token:
        raise SystemExit(f"[!] Erro de credencial/regiao: {cloud.token}")

    devices = cloud.getdevices(verbose=False)
    if not isinstance(devices, list):
        raise SystemExit(f"[!] Resposta inesperada da API: {devices}")

    # A nuvem nao da o IP local nem a versao real do protocolo (costuma vir 3.3
    # como default). Escuta os broadcasts locais para preencher isso nos que
    # estao ligados; o controle local precisa da versao certa (ex.: 3.4).
    print("[i] escutando a rede local (~8s) para IP/versao dos aparelhos...")
    local = tuya_scan.listen(duration=8.0)

    out = []
    for d in devices:
        did = d.get("id")
        seen = local.get(did, {})
        out.append({
            "id": did,
            "name": d.get("name", ""),
            "key": d.get("key", ""),          # local_key: essencial p/ controle local
            "ip": seen.get("ip") or d.get("ip", ""),
            "version": seen.get("version") or str(d.get("version", "3.3")),
            "category": d.get("category", ""),
            "product_name": d.get("product_name", ""),
        })

    Path(OUT).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"[i] {len(out)} dispositivo(s) salvos em {OUT}:")
    for d in out:
        got_key = "com chave" if d["key"] else "SEM CHAVE"
        loc = f"{d['ip']} v{d['version']}" if d["ip"] else "offline"
        print(f"  {d['name'] or '(sem nome)':28} {d['id']}  "
              f"[{d['category']}]  {got_key}  {loc}")


if __name__ == "__main__":
    main()

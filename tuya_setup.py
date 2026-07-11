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

import envutil
import tuya_scan

ENV = ".env"
OUT = "tuya_devices.json"

# Mantido por compatibilidade (era usado por outros modulos); delega ao envutil.
load_env = envutil.load_env


class SyncError(Exception):
    """Falha esperada ao sincronizar (credencial ausente, erro da nuvem...)."""


def _load_existing(path=OUT):
    p = Path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return []


def sync_devices(env_path=ENV, out_path=OUT, listen_seconds=8.0):
    """Puxa a lista de dispositivos + local_keys da nuvem Tuya e grava out_path.

    Enriquece com IP/versao real via broadcast local. Retorna um resumo:
        {devices:[...], count, added:[nomes...], removed:[nomes...]}
    Lanca SyncError em falhas esperadas (sem credencial, erro de API).
    """
    env = envutil.load_env(env_path)
    api_region = env.get("tuya_api_region", "us")
    api_key = env.get("tuya_api_key", "")
    api_secret = env.get("tuya_api_secret", "")
    if not api_key or not api_secret:
        raise SyncError("Credenciais Tuya ausentes. Preencha em Configurações.")

    cloud = tinytuya.Cloud(apiRegion=api_region, apiKey=api_key,
                           apiSecret=api_secret)
    if isinstance(cloud.token, dict) and "Error" in cloud.token:
        raise SyncError(f"Erro de credencial/regiao Tuya: {cloud.token}")

    devices = cloud.getdevices(verbose=False)
    if not isinstance(devices, list):
        raise SyncError(f"Resposta inesperada da API Tuya: {devices}")

    # A nuvem nao da o IP local nem a versao real do protocolo (costuma vir 3.3
    # como default). Escuta os broadcasts locais para preencher isso nos que
    # estao ligados; o controle local precisa da versao certa (ex.: 3.4).
    local = tuya_scan.listen(duration=listen_seconds) if listen_seconds else {}

    # Nomes antigos para calcular o que entrou/saiu (feedback ao usuario).
    before = {d.get("id"): d.get("name", "") for d in _load_existing(out_path)}

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
            # Zigbee/sub-dispositivo: controlado LOCALMENTE atraves do Hub
            # (gateway_id) usando o node_id como 'cid'. Vazio = Wi-Fi direto.
            "node_id": d.get("node_id", ""),
            "gateway_id": d.get("gateway_id", ""),
        })

    Path(out_path).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")

    now_ids = {d["id"] for d in out}
    added = [d["name"] or d["id"] for d in out if d["id"] not in before]
    removed = [name or did for did, name in before.items() if did not in now_ids]
    return {"devices": out, "count": len(out), "added": added, "removed": removed}


def main():
    try:
        res = sync_devices()
    except SyncError as exc:
        raise SystemExit(f"[!] {exc}")
    print(f"[i] {res['count']} dispositivo(s) salvos em {OUT}:")
    for d in res["devices"]:
        got_key = "com chave" if d["key"] else "SEM CHAVE"
        loc = f"{d['ip']} v{d['version']}" if d["ip"] else "offline"
        print(f"  {d['name'] or '(sem nome)':28} {d['id']}  "
              f"[{d['category']}]  {got_key}  {loc}")
    if res["added"]:
        print(f"[i] novos: {', '.join(res['added'])}")
    if res["removed"]:
        print(f"[i] removidos: {', '.join(res['removed'])}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""envutil.py - Leitura simples de um arquivo .env (KEY = valor por linha).

Sem dependencias. Ignora linhas em branco e comentarios; corta espacos ao
redor do '=' e aspas ao redor do valor.
"""

from pathlib import Path

_ENV = ".env"


def load_env(path=_ENV):
    env = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        env[k.strip()] = v
    return env


def get(key, default="", path=_ENV):
    return load_env(path).get(key, default)


def save_env(updates, path=_ENV):
    """Grava as chaves de 'updates' no .env preservando comentarios e ordem.

    Chaves ja presentes sao atualizadas na propria linha (mantendo o estilo
    'chave = valor'); chaves novas sao acrescentadas ao final. Comentarios e
    linhas em branco ficam intactos. Um valor None remove a chave.
    """
    p = Path(path)
    lines = p.read_text().splitlines() if p.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            val = remaining.pop(key)
            if val is None:
                continue  # remove a chave
            out.append(f"{key} = {val}")
        else:
            out.append(line)
    # Chaves novas que ainda nao existiam no arquivo.
    for key, val in remaining.items():
        if val is not None:
            out.append(f"{key} = {val}")
    p.write_text("\n".join(out) + "\n")

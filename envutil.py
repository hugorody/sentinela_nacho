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

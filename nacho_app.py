#!/usr/bin/env python3
"""Interface independente do Nacho, o assistente por voz do Sentinela.

Esta primeira etapa entrega a interface e o ciclo visual de áudio. A conexão
com a OpenAI e as ferramentas do Sentinela entram atrás desta aplicação, sem
expor credenciais ao navegador.
"""

import argparse
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

import envutil
from nacho_tools import SentinelaClient, TOOL_DEFINITIONS
import nacho_turn


app = Flask(__name__)
SENTINELA = SentinelaClient()


@app.after_request
def disable_nacho_cache(response):
    """Evita frontend antigo durante o desenvolvimento do protocolo de voz."""
    if request.path == "/" or request.path.startswith("/static/nacho"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.before_request
def require_household_access():
    """Proteção opcional sem acrescentar controles à interface minimalista."""
    pin = envutil.get("nacho_pin")
    if not pin:
        return None
    auth = request.authorization
    if auth and hmac.compare_digest(auth.password or "", pin):
        return None
    return Response("Autenticação necessária", 401, {
        "WWW-Authenticate": 'Basic realm="Nacho", charset="UTF-8"'})


@app.get("/")
def index():
    return render_template("nacho.html")


@app.get("/api/health")
def health():
    voice_ready = bool(envutil.get("openai_api_key"))
    transport = envutil.get("nacho_voice_transport") or "http"
    sentinela = SENTINELA.execute("get_system_status", {})
    return jsonify({
        "ok": True,
        "service": "nacho",
        "voice_configured": voice_ready,
        "voice_connected": False,
        "voice_transport": transport,
        "sentinela_connected": "error" not in sentinela,
        "access_protected": bool(envutil.get("nacho_pin")),
    })


@app.post("/api/tools/<name>")
def execute_tool(name):
    """Executa apenas ferramentas presentes na allowlist somente-leitura."""
    allowed = {tool["name"] for tool in TOOL_DEFINITIONS}
    if name not in allowed:
        return jsonify({"error": "Ferramenta não autorizada"}), 404
    args = request.get_json(force=True, silent=True) or {}
    return jsonify(SENTINELA.execute(name, args))


@app.post("/api/voice/turn")
def voice_turn():
    """Fallback HTTP: transcreve, raciocina/consulta e sintetiza uma resposta."""
    if not envutil.get("openai_api_key"):
        return jsonify({"error": "OpenAI API key não configurada"}), 503
    upload = request.files.get("audio")
    if upload is None:
        return jsonify({"error": "Áudio ausente"}), 400
    audio = upload.read(8 * 1024 * 1024 + 1)
    if not audio or len(audio) > 8 * 1024 * 1024:
        return jsonify({"error": "Áudio vazio ou maior que 8 MB"}), 400
    try:
        previous = (request.form.get("previous_response_id") or "").strip() or None
        if previous and (len(previous) > 200 or not previous.startswith("resp_")):
            return jsonify({"error": "Contexto de conversa inválido"}), 400
        return jsonify(nacho_turn.run_turn(
            audio, upload.filename, SENTINELA, previous_response_id=previous))
    except nacho_turn.OpenAIError as exc:
        print(f"[nacho] fallback por turnos: {exc}")
        return jsonify({"error": str(exc)}), 502


def _multipart(fields):
    """Monta multipart/form-data sem adicionar uma dependencia HTTP."""
    boundary = "----nacho-" + secrets.token_hex(12)
    chunks = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode() if isinstance(value, str) else value,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _session_config():
    return {
        "type": "realtime",
        "model": envutil.get("nacho_realtime_model") or "gpt-realtime-2.1",
        "instructions": (
            "Você é Nacho, assistente residencial do sistema Sentinela. "
            "Fale em português do Brasil, de forma natural, breve e acolhedora. "
            "Nunca afirme ter consultado câmeras, sensores ou luzes sem usar uma "
            "ferramenta do Sentinela. Você pode ligar e desligar luzes e controlar TVs "
            "Samsung quando o usuário pedir; só confirme a ação quando a ferramenta "
            "retornar sucesso. Se uma "
            "ferramenta ainda não estiver disponível, explique isso claramente."
        ),
        "audio": {"output": {"voice": envutil.get("nacho_voice") or "marin"}},
        "tools": TOOL_DEFINITIONS,
        "tool_choice": "auto",
    }


@app.post("/api/realtime/token")
def realtime_token():
    """Cria uma credencial efêmera; a chave permanente nunca sai do backend."""
    api_key = envutil.get("openai_api_key")
    if not api_key:
        return jsonify({"error": "OpenAI API key não configurada"}), 503
    safety_id = hashlib.sha256(
        ("nacho:" + (request.remote_addr or "local")).encode()).hexdigest()
    upstream = urllib.request.Request(
        "https://api.openai.com/v1/realtime/client_secrets",
        data=json.dumps({"session": _session_config()}, ensure_ascii=False).encode(),
        method="POST", headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "OpenAI-Safety-Identifier": safety_id,
        })
    try:
        with urllib.request.urlopen(upstream, timeout=20) as result:
            data = json.loads(result.read().decode("utf-8"))
            return jsonify({"value": data.get("value"),
                            "expires_at": data.get("expires_at"),
                            "model": _session_config()["model"]})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        print(f"[nacho] client secret HTTP {exc.code}: {detail}")
        return jsonify({"error": "A OpenAI recusou a credencial de voz",
                        "upstream_status": exc.code}), 502
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[nacho] falha ao conectar à OpenAI: {exc}")
        return jsonify({"error": "Não foi possível conectar à OpenAI"}), 502


@app.post("/api/realtime/session")
def realtime_session():
    """Troca o SDP do navegador pelo SDP da OpenAI sem expor a API key."""
    api_key = envutil.get("openai_api_key")
    if not api_key:
        return jsonify({"error": "OpenAI API key não configurada"}), 503
    if not request.content_type or "application/sdp" not in request.content_type:
        return jsonify({"error": "Content-Type deve ser application/sdp"}), 415
    offer = request.get_data(cache=False, as_text=True)
    if not offer or len(offer) > 100_000:
        return jsonify({"error": "SDP inválido"}), 400

    session = _session_config()
    body, content_type = _multipart({"sdp": offer, "session": json.dumps(session)})
    safety_id = hashlib.sha256(
        ("nacho:" + (request.remote_addr or "local")).encode()).hexdigest()
    upstream = urllib.request.Request(
        "https://api.openai.com/v1/realtime/calls", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
            "OpenAI-Safety-Identifier": safety_id,
        },
    )
    try:
        with urllib.request.urlopen(upstream, timeout=25) as result:
            answer = result.read()
            return Response(answer, status=result.status, content_type="application/sdp")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        print(f"[nacho] OpenAI Realtime HTTP {exc.code}: {detail}")
        return jsonify({"error": "A OpenAI recusou a sessão de voz",
                        "upstream_status": exc.code}), 502
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[nacho] falha ao conectar à OpenAI: {exc}")
        return jsonify({"error": "Não foi possível conectar à OpenAI"}), 502


def main():
    parser = argparse.ArgumentParser(description="Nacho — assistente por voz")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--https", action="store_true",
                        help="serve HTTPS usando os certificados locais")
    parser.add_argument("--cert", default="certs/nacho-server.crt")
    parser.add_argument("--key", default="certs/nacho-server.key")
    args = parser.parse_args()
    ssl_context = None
    scheme = "http"
    if args.https:
        if not Path(args.cert).is_file() or not Path(args.key).is_file():
            parser.error("certificado ausente; execute python3 https_setup.py primeiro")
        ssl_context = (args.cert, args.key)
        scheme = "https"
    print(f"[i] Nacho em {scheme}://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True,
            debug=args.reload, use_reloader=args.reload, ssl_context=ssl_context)


if __name__ == "__main__":
    main()

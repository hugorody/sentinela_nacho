#!/usr/bin/env python3
"""Fallback de voz por turnos HTTP para redes sem WebRTC."""

import base64
import json
import secrets
import urllib.error
import urllib.request

import envutil
from nacho_tools import SentinelaClient, TOOL_DEFINITIONS


API = "https://api.openai.com/v1"


class OpenAIError(RuntimeError):
    pass


def _headers(content_type):
    return {"Authorization": "Bearer " + envutil.get("openai_api_key"),
            "Content-Type": content_type}


def _request(path, body, content_type, timeout=60):
    req = urllib.request.Request(API + path, data=body, method="POST",
                                 headers=_headers(content_type))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read(), res.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1200]
        raise OpenAIError(f"OpenAI HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OpenAIError("Não foi possível conectar à OpenAI") from exc


def _multipart_audio(audio, filename="speech.webm"):
    boundary = "----nacho-turn-" + secrets.token_hex(12)
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n"
        "gpt-4o-mini-transcribe\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n"
        "pt\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: audio/webm\r\n\r\n".encode(),
        audio, b"\r\n", f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def transcribe(audio, filename):
    body, content_type = _multipart_audio(audio, filename or "speech.webm")
    raw, _ = _request("/audio/transcriptions", body, content_type)
    return json.loads(raw).get("text", "").strip()


def _answer_text(response):
    texts = []
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text" and part.get("text"):
                    texts.append(part["text"])
    return "\n".join(texts).strip()


def answer(question, sentinela=None, previous_response_id=None):
    sentinela = sentinela or SentinelaClient()
    payload = {
        "model": envutil.get("nacho_text_model") or "gpt-5.6-sol",
        "instructions": (
            "Você é Nacho, assistente residencial do Sentinela. Responda em "
            "português do Brasil, de forma breve e natural para ser falada. "
            "Use as ferramentas para qualquer afirmação sobre câmeras, sensores, "
            "luzes ou rede. Nunca invente um estado da casa."
        ),
        "input": question, "tools": TOOL_DEFINITIONS,
    }
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    raw, _ = _request("/responses", json.dumps(payload, ensure_ascii=False).encode(),
                      "application/json", timeout=90)
    response = json.loads(raw)
    for _ in range(3):
        calls = [item for item in response.get("output", [])
                 if item.get("type") == "function_call"]
        if not calls:
            return {"text": _answer_text(response) or "Não consegui formular uma resposta.",
                    "response_id": response.get("id")}
        outputs = []
        for call in calls:
            try:
                args = json.loads(call.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = sentinela.execute(call.get("name"), args)
            outputs.append({"type": "function_call_output", "call_id": call["call_id"],
                            "output": json.dumps(result, ensure_ascii=False)})
        followup = {"model": payload["model"], "previous_response_id": response["id"],
                    "input": outputs, "tools": TOOL_DEFINITIONS}
        raw, _ = _request("/responses", json.dumps(followup, ensure_ascii=False).encode(),
                          "application/json", timeout=90)
        response = json.loads(raw)
    return {"text": "A consulta exigiu etapas demais. Tente formular de outra maneira.",
            "response_id": response.get("id")}


def synthesize(text):
    payload = {"model": "gpt-4o-mini-tts", "voice": envutil.get("nacho_voice") or "marin",
               "input": text, "instructions": "Fale em português brasileiro, com tom acolhedor."}
    audio, _ = _request("/audio/speech", json.dumps(payload, ensure_ascii=False).encode(),
                        "application/json", timeout=60)
    return base64.b64encode(audio).decode("ascii")


def run_turn(audio, filename, sentinela=None, previous_response_id=None):
    try:
        transcript = transcribe(audio, filename)
    except OpenAIError as exc:
        raise OpenAIError("Falha na transcrição do áudio") from exc
    if not transcript:
        raise OpenAIError("Nenhuma fala foi identificada")
    try:
        result = answer(transcript, sentinela, previous_response_id)
    except OpenAIError as exc:
        raise OpenAIError("Falha ao gerar a resposta") from exc
    reply = result["text"]
    try:
        audio_reply = synthesize(reply)
    except OpenAIError as exc:
        raise OpenAIError("Falha ao gerar a voz da resposta") from exc
    return {"transcript": transcript, "reply": reply,
            "response_id": result.get("response_id"), "audio": audio_reply}

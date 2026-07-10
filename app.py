#!/usr/bin/env python3
"""
app.py - Dashboard web unificado (Flask) do produto de cameras.

Reune tudo em um so lugar: iniciar/parar streams ao vivo (MJPEG no navegador),
descobrir cameras na rede, painel de rostos (nomear + treinar reconhecimento) e
log de eventos ("Hugo na Cam X as Y").

Executar:
    python3 app.py                      # http://localhost:5000
    python3 app.py --host 0.0.0.0 --port 5000
"""

import argparse
import time
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, render_template,
                   request, send_file)

import engine as engine_mod
import face_panel
import face_recog
import netscan

FACE_LOG = "./historico_faces"
CONFIG = "cameras.json"

ENGINE = engine_mod.Engine(config_path=CONFIG, face_log=FACE_LOG)
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


# --- Controle do motor / cameras ------------------------------------------

@app.route("/api/status")
def api_status():
    return jsonify(ENGINE.status())


@app.post("/api/start")
def api_start():
    ENGINE.start()
    return jsonify(ENGINE.status())


@app.post("/api/stop")
def api_stop():
    ENGINE.stop()
    return jsonify(ENGINE.status())


@app.post("/api/discover")
def api_discover():
    added = ENGINE.discover_and_add(prompt=False)
    return jsonify({
        "added": [{"id": c["id"], "name": c["name"]} for c in added],
        "status": ENGINE.status(),
    })


@app.post("/api/rename_camera")
def api_rename_camera():
    data = request.get_json(force=True, silent=True) or {}
    ok = ENGINE.rename_camera(data.get("id"), data.get("name"))
    return jsonify({"ok": ok, "status": ENGINE.status()})


@app.post("/api/add_camera")
def api_add_camera():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url obrigatoria"}), 400
    cam = ENGINE.add_manual(url, data.get("name"))
    return jsonify({"ok": bool(cam), "status": ENGINE.status()})


# --- Gravacao --------------------------------------------------------------

@app.post("/api/record")
def api_record():
    """Liga/desliga a gravacao de uma camera. Sem 'on', alterna o estado."""
    data = request.get_json(force=True, silent=True) or {}
    cid = data.get("id")
    if ENGINE.get_camera(cid) is None:
        return jsonify({"error": "camera nao encontrada"}), 404
    if "on" in data:
        ENGINE.set_recording(cid, bool(data["on"]))
    else:
        ENGINE.toggle_recording(cid)
    return jsonify({"ok": True, "status": ENGINE.status()})


# --- Minha rede (dispositivos conectados) ---------------------------------

@app.route("/api/network")
def api_network():
    """Lista os dispositivos conectados na rede local."""
    try:
        devices = netscan.scan_network(config_path=CONFIG)
    except Exception as exc:
        return jsonify({"error": str(exc), "devices": []}), 500
    return jsonify({"devices": devices, "count": len(devices)})


# --- Video ao vivo (MJPEG) ------------------------------------------------

@app.route("/video/<cid>")
def video(cid):
    if ENGINE.get_camera(cid) is None:
        abort(404)

    def gen():
        while True:
            jpg = ENGINE.annotated_jpeg(cid)
            if jpg is None:
                time.sleep(0.2)
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            time.sleep(1 / 12.0)

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


# --- Painel de rostos ------------------------------------------------------

@app.route("/api/faces")
def api_faces():
    recs = face_panel.load_history(FACE_LOG)
    labels = face_panel.load_labels(FACE_LOG)
    people = {}
    for r in recs:
        n = labels.get(r.get("file"))
        if n:
            r["name"] = n
            people[n] = people.get(n, 0) + 1
    people = dict(sorted(people.items(), key=lambda kv: (-kv[1], kv[0])))
    return jsonify({"records": recs, "people": people})


@app.post("/api/label")
def api_label():
    data = request.get_json(force=True, silent=True) or {}
    file = data.get("file")
    name = (data.get("name") or "").strip()
    if not file:
        return jsonify({"error": "file obrigatorio"}), 400
    labels = face_panel.load_labels(FACE_LOG)
    if name:
        labels[file] = name
    else:
        labels.pop(file, None)
    face_panel.save_labels(FACE_LOG, labels)
    return jsonify({"ok": True, "name": name})


@app.route("/api/clusters")
def api_clusters():
    try:
        clusters = ENGINE.cluster_faces()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"clusters": clusters})


@app.post("/api/label_bulk")
def api_label_bulk():
    data = request.get_json(force=True, silent=True) or {}
    files = data.get("files") or []
    name = (data.get("name") or "").strip()
    if not files:
        return jsonify({"error": "files obrigatorio"}), 400
    labels = face_panel.load_labels(FACE_LOG)
    for f in files:
        if name:
            labels[f] = name
        else:
            labels.pop(f, None)
    face_panel.save_labels(FACE_LOG, labels)
    return jsonify({"ok": True, "count": len(files), "name": name})


@app.post("/api/enroll")
def api_enroll():
    try:
        res = face_recog.enroll_from_labels(FACE_LOG)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if ENGINE.known is not None:
        ENGINE.known.reload_if_changed()
    return jsonify({"ok": True, **res})


# --- Eventos ---------------------------------------------------------------

@app.route("/api/events")
def api_events():
    known_only = request.args.get("known") == "1"
    name = request.args.get("name") or None
    evs = ENGINE.events.recent(limit=300, known_only=known_only, name=name)
    return jsonify({"events": evs})


# --- Midia (recortes e miniaturas) ----------------------------------------

@app.route("/media/<path:rel>")
def media(rel):
    base = Path(FACE_LOG).resolve()
    target = (base / rel).resolve()
    if base not in target.parents and target != base:
        abort(403)
    if not target.is_file():
        abort(404)
    return send_file(str(target))


def main():
    ap = argparse.ArgumentParser(description="Dashboard web de cameras")
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 para acessar de outros dispositivos")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--start", action="store_true",
                    help="ja inicia os streams ao subir")
    args = ap.parse_args()

    if args.start:
        ENGINE.start()

    print(f"[i] Dashboard em http://{args.host}:{args.port}")
    # threaded=True e essencial: cada stream MJPEG segura uma conexao.
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()

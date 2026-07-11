#!/usr/bin/env python3
"""
app.py - Dashboard web unificado (Flask) do produto de cameras.

Reune tudo em um so lugar: iniciar/parar streams ao vivo (MJPEG no navegador),
descobrir cameras na rede, painel de rostos (nomear + treinar reconhecimento) e
log de eventos ("Hugo na Cam X as Y").

Executar:
    python3 app.py                      # http://localhost:8001
    python3 app.py --host 0.0.0.0 --port 8001
"""

import argparse
import re
import time
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, render_template,
                   request, send_file)

import engine as engine_mod
import envutil
import face_panel
import face_recog
import netscan
import scenes as scenes_mod
import tuya_control
import tuya_scan
import tuya_setup

FACE_LOG = "./historico_faces"
CONFIG = "cameras.json"
SMARTHOME = "smarthome.json"

ENGINE = engine_mod.Engine(config_path=CONFIG, face_log=FACE_LOG)
# Controlador Tuya carregado sob demanda: so existe se tuya_devices.json estiver
# presente (gerado por tuya_setup.py). Sem ele, a aba mostra so o scan local.
TUYA = tuya_control.Controller() if Path(tuya_control.DEVICES).exists() else None

# Cenas (automacoes): usam o Controller para agir e recebem eventos de camera
# do engine. O scheduler de horario roda num thread proprio.
SCENES = scenes_mod.SceneManager(controller=TUYA)
ENGINE.on_camera_event = SCENES.on_camera_event
SCENES.start()

# Alarmes de dispositivos smart: o mesmo AlarmManager das cameras tambem le os
# sensores (porta, movimento...) por polling e manda e-mail nas transicoes.
ENGINE.alarms.attach_controller(TUYA)
ENGINE.alarms.start_device_poll()

app = Flask(__name__)


# --- Configuracoes (credenciais do .env editaveis pela interface) ----------
# Cada campo declara: rotulo, se e segredo (mascarado na leitura) e uma ajuda
# (tooltip) explicando como o usuario obtem aquela chave.
SETTINGS_FIELDS = [
    {
        "key": "resend_api_key", "label": "Resend API Key", "secret": True,
        "group": "E-mail (alarmes)",
        "help": "Chave para enviar os e-mails de alarme. Crie uma conta em "
                "resend.com, va em API Keys > Create API Key e copie o valor "
                "(comeca com 're_'). O dominio do remetente precisa estar "
                "verificado em resend.com/domains.",
    },
    {
        "key": "alarm_from", "label": "Remetente dos e-mails", "secret": False,
        "group": "E-mail (alarmes)",
        "placeholder": "Sentinela <novidades@news.mundodna.com>",
        "help": "Endereco 'De' dos e-mails de alarme, no formato "
                "'Nome <email@seudominio.com>'. O dominio precisa estar "
                "verificado no Resend. Deixe em branco para usar o padrao.",
    },
    {
        "key": "tuya_api_region", "label": "Tuya · Data center", "secret": False,
        "group": "Smart home (Tuya)", "type": "select",
        "options": [
            {"value": "us", "label": "Western America (us)"},
            {"value": "eu", "label": "Central Europe (eu)"},
            {"value": "cn", "label": "China (cn)"},
            {"value": "in", "label": "India (in)"},
        ],
        "help": "Data center onde seu projeto Tuya foi criado. Para contas do "
                "Brasil no app Smart Life, normalmente e 'Western America'. "
                "Precisa ser o mesmo escolhido em platform.tuya.com.",
    },
    {
        "key": "tuya_api_key", "label": "Tuya · Access ID", "secret": False,
        "group": "Smart home (Tuya)",
        "help": "Access ID (Client ID) do seu projeto na Tuya. Em "
                "platform.tuya.com abra Cloud > Development > seu projeto > "
                "aba Overview. Crie o projeto e vincule sua conta do app Smart "
                "Life em Devices > Link App Account.",
    },
    {
        "key": "tuya_api_secret", "label": "Tuya · Access Secret", "secret": True,
        "group": "Smart home (Tuya)",
        "help": "Access Secret (Client Secret) do seu projeto Tuya, ao lado do "
                "Access ID na aba Overview de platform.tuya.com. Mantenha em "
                "segredo.",
    },
]

_SECRET_MASK = "••••••••"


def _mask(value):
    """Mostra so os ultimos caracteres de um segredo (o resto vira bullets)."""
    if not value:
        return ""
    tail = value[-4:] if len(value) > 8 else ""
    return _SECRET_MASK + tail


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


@app.route("/api/recordings")
def api_recordings():
    """Lista os segmentos .mp4 ja gravados, do mais recente para o mais antigo."""
    rec_dir = Path(ENGINE.rec_dir)
    items = []
    if rec_dir.is_dir():
        for f in rec_dir.glob("*.mp4"):
            if not f.is_file():
                continue
            st = f.stat()
            # Nome do arquivo: "{camera}_{YYYYMMDD_HHMMSS}.mp4".
            stem = f.stem
            camera, ts = stem, None
            m = re.match(r"^(.*)_(\d{8}_\d{6})$", stem)
            if m:
                camera = m.group(1)
                try:
                    ts = datetime.strptime(m.group(2), "%Y%m%d_%H%M%S").isoformat()
                except ValueError:
                    ts = None
            items.append({
                "file": f.name,
                "camera": camera,
                "started": ts,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"recordings": items})


@app.route("/gravacoes/<path:name>")
def recording_file(name):
    """Serve um segmento .mp4 gravado (com protecao contra path traversal)."""
    base = Path(ENGINE.rec_dir).resolve()
    target = (base / name).resolve()
    if base not in target.parents:
        abort(403)
    if target.suffix.lower() != ".mp4" or not target.is_file():
        abort(404)
    # conditional=True habilita Range requests (seek no player de video).
    return send_file(str(target), conditional=True)


# --- Minha rede (dispositivos conectados) ---------------------------------

@app.route("/api/network")
def api_network():
    """Lista os dispositivos conectados na rede local."""
    try:
        devices = netscan.scan_network(config_path=CONFIG)
    except Exception as exc:
        return jsonify({"error": str(exc), "devices": []}), 500
    return jsonify({"devices": devices, "count": len(devices)})


# --- Smart home (dispositivos Tuya) ----------------------------------------

@app.route("/api/smarthome")
def api_smarthome():
    """Lista os dispositivos smart home ja conhecidos (sem escutar a rede)."""
    return jsonify({"devices": tuya_scan.load_devices(SMARTHOME)})


@app.post("/api/smarthome/scan")
def api_smarthome_scan():
    """Escuta os broadcasts Tuya por ~8s e atualiza/retorna a lista."""
    try:
        devices = tuya_scan.scan_and_merge(SMARTHOME)
    except Exception as exc:
        return jsonify({"error": str(exc), "devices": []}), 500
    return jsonify({"devices": devices})


@app.post("/api/smarthome/rename")
def api_smarthome_rename():
    data = request.get_json(force=True, silent=True) or {}
    ok = tuya_scan.rename(data.get("id"), data.get("name"), SMARTHOME)
    return jsonify({"ok": ok})


# --- Smart home: controle (liga/desliga/brilho via Tuya) -------------------

@app.route("/api/smarthome/devices")
def api_smarthome_devices():
    """Dispositivos controlaveis (Tuya Cloud + local). Vazio se nao configurado."""
    if TUYA is None:
        return jsonify({"configured": False, "devices": []})
    return jsonify({"configured": True, "devices": TUYA.list_devices()})


@app.post("/api/smarthome/sync")
def api_smarthome_sync():
    """Puxa da nuvem Tuya a lista atual de dispositivos (inclui os novos da
    Smart Life) e recarrega o controlador. Requer credenciais em Configuracoes."""
    global TUYA
    try:
        res = tuya_setup.sync_devices()
    except tuya_setup.SyncError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    # Primeira sincronizacao: cria o controlador. Depois: recarrega.
    if TUYA is None:
        TUYA = tuya_control.Controller()
        SCENES.controller = TUYA
        ENGINE.alarms.attach_controller(TUYA)
        ENGINE.alarms.start_device_poll()
    else:
        TUYA.reload()
    return jsonify({
        "ok": True, "count": res["count"],
        "added": res["added"], "removed": res["removed"],
        "devices": TUYA.list_devices(),
    })


@app.route("/api/smarthome/state/<dev_id>")
def api_smarthome_state(dev_id):
    if TUYA is None:
        return jsonify({"error": "smart home nao configurado"}), 400
    return jsonify(TUYA.get_state(dev_id))


@app.post("/api/smarthome/switch")
def api_smarthome_switch():
    if TUYA is None:
        return jsonify({"error": "smart home nao configurado"}), 400
    data = request.get_json(force=True, silent=True) or {}
    res = TUYA.set_switch(data.get("id"), data.get("code", "switch_1"),
                          bool(data.get("on")))
    return jsonify(res), (200 if res.get("ok") else 502)


@app.post("/api/smarthome/brightness")
def api_smarthome_brightness():
    if TUYA is None:
        return jsonify({"error": "smart home nao configurado"}), 400
    data = request.get_json(force=True, silent=True) or {}
    res = TUYA.set_brightness(data.get("id"), data.get("pct", 100))
    return jsonify(res), (200 if res.get("ok") else 502)


@app.post("/api/smarthome/all")
def api_smarthome_all():
    """Acende ('on':true) ou apaga todas as luzes/interruptores de uma vez."""
    if TUYA is None:
        return jsonify({"error": "smart home nao configurado"}), 400
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(TUYA.set_all(bool(data.get("on"))))


@app.post("/api/smarthome/label")
def api_smarthome_label():
    """Renomeia uma tecla/canal de um dispositivo (ex.: 'switch_1' -> 'Pia')."""
    if TUYA is None:
        return jsonify({"error": "smart home nao configurado"}), 400
    data = request.get_json(force=True, silent=True) or {}
    ok = TUYA.label_channel(data.get("id"), data.get("code"), data.get("name"))
    return jsonify({"ok": ok})


# --- Cam alarmes (e-mail quando aparece alguem nao identificado) -----------

@app.route("/api/alarms")
def api_alarms():
    """Config de alarmes + cameras e sensores smart (para casar cada um com sua
    config)."""
    cfg = ENGINE.alarms.get_config()
    cams = [{"id": c["id"], "name": c["name"]} for c in ENGINE.cameras]
    # Sensores smart: dispositivos cujo kind e 'sensor' expoem estados (porta,
    # movimento...) que servem de gatilho de alarme.
    sensors = []
    if TUYA is not None:
        for d in TUYA.list_devices():
            if d.get("kind") == "sensor":
                sensors.append({"id": d["id"], "name": d.get("name") or d["id"]})
    return jsonify({
        "config": cfg,
        "cameras": cams,
        "sensors": sensors,
        "sensor_labels": tuya_control.SENSOR_LABELS,
        "smart_ready": TUYA is not None,
    })


@app.post("/api/alarms/email")
def api_alarms_email():
    """Define o e-mail global (destino padrao de todas as cameras)."""
    data = request.get_json(force=True, silent=True) or {}
    ENGINE.alarms.set_global_email(data.get("email"))
    return jsonify({"ok": True, "config": ENGINE.alarms.get_config()})


@app.post("/api/alarms/camera")
def api_alarms_camera():
    """Atualiza a config de alarme de uma camera (enabled/windows/recipients)."""
    data = request.get_json(force=True, silent=True) or {}
    ok = ENGINE.alarms.set_camera(
        data.get("camera"),
        enabled=data.get("enabled"),
        windows=data.get("windows"),
        recipients=data.get("recipients"),
    )
    return jsonify({"ok": ok, "config": ENGINE.alarms.get_config()})


@app.post("/api/alarms/test")
def api_alarms_test():
    """Envia um e-mail de teste para os destinatarios da camera indicada."""
    data = request.get_json(force=True, silent=True) or {}
    res = ENGINE.alarms.send_test(data.get("camera"))
    return jsonify(res), (200 if res.get("ok") else 400)


@app.post("/api/alarms/device")
def api_alarms_device():
    """Atualiza o alarme de um sensor smart (enabled/windows/recipients/trigger).

    Identificado por (device + code), ex.: porta -> doorcontact_state."""
    data = request.get_json(force=True, silent=True) or {}
    ok = ENGINE.alarms.set_device(
        data.get("device"),
        data.get("code"),
        enabled=data.get("enabled"),
        windows=data.get("windows"),
        recipients=data.get("recipients"),
        trigger=data.get("trigger"),
    )
    return jsonify({"ok": ok, "config": ENGINE.alarms.get_config()})


@app.post("/api/alarms/device_test")
def api_alarms_device_test():
    """Envia um e-mail de teste para os destinatarios de um alarme de sensor."""
    data = request.get_json(force=True, silent=True) or {}
    res = ENGINE.alarms.send_device_test(
        data.get("device"), data.get("code"), data.get("label") or "Dispositivo")
    return jsonify(res), (200 if res.get("ok") else 400)


# --- Cenas (automacoes) ----------------------------------------------------

@app.route("/api/scenes")
def api_scenes():
    """Cenas + o que ha para montar gatilhos/acoes (cameras e dispositivos)."""
    cams = [{"id": c["id"], "name": c["name"]} for c in ENGINE.cameras]
    devices = TUYA.list_devices() if TUYA is not None else []
    return jsonify({
        "scenes": SCENES.list_scenes(),
        "cameras": cams,
        "devices": devices,
        "smart_ready": TUYA is not None,
        # Rotulos dos estados de sensor (code -> [nome, textoLigado, textoDesligado]).
        "sensor_labels": tuya_control.SENSOR_LABELS,
    })


@app.post("/api/scenes/save")
def api_scenes_save():
    data = request.get_json(force=True, silent=True) or {}
    scene = SCENES.save_scene(data.get("scene") or {})
    return jsonify({"ok": True, "scene": scene})


@app.post("/api/scenes/delete")
def api_scenes_delete():
    data = request.get_json(force=True, silent=True) or {}
    ok = SCENES.delete_scene(data.get("id"))
    return jsonify({"ok": ok})


@app.post("/api/scenes/enable")
def api_scenes_enable():
    data = request.get_json(force=True, silent=True) or {}
    ok = SCENES.set_enabled(data.get("id"), bool(data.get("enabled")))
    return jsonify({"ok": ok})


@app.post("/api/scenes/run")
def api_scenes_run():
    """Executa manualmente as acoes de uma cena (para testar)."""
    data = request.get_json(force=True, silent=True) or {}
    res = SCENES.run_scene(data.get("id"))
    return jsonify(res), (200 if res.get("ok") else 400)


# --- Configuracoes ---------------------------------------------------------

@app.route("/api/settings")
def api_settings():
    """Campos de configuracao + valores atuais (segredos vem mascarados)."""
    env = envutil.load_env()
    fields = []
    for f in SETTINGS_FIELDS:
        raw = env.get(f["key"], "")
        item = {k: f[k] for k in f if k != "help"}
        item["help"] = f["help"]
        item["value"] = _mask(raw) if f.get("secret") else raw
        item["set"] = bool(raw)          # ja tem valor salvo?
        fields.append(item)
    return jsonify({"fields": fields})


@app.post("/api/settings")
def api_settings_save():
    """Salva no .env. Para segredos, so grava se o valor mudou (nao a mascara)."""
    data = request.get_json(force=True, silent=True) or {}
    values = data.get("values") or {}
    by_key = {f["key"]: f for f in SETTINGS_FIELDS}
    updates = {}
    for key, val in values.items():
        f = by_key.get(key)
        if f is None:
            continue
        val = (val or "").strip()
        # Campo secreto que voltou mascarado (usuario nao mexeu): mantem o atual.
        if f.get("secret") and (val == "" or val.startswith(_SECRET_MASK)):
            continue
        updates[key] = val
    if updates:
        envutil.save_env(updates)
        # Se mudou algo do Tuya, recarrega o controlador para usar as novas
        # credenciais na proxima chamada a nuvem.
        if TUYA is not None and any(k.startswith("tuya_") for k in updates):
            TUYA.reload()
    return jsonify({"ok": True, "updated": sorted(updates.keys())})


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
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--start", action="store_true",
                    help="ja inicia os streams ao subir")
    ap.add_argument("--reload", action="store_true",
                    help="reinicia sozinho ao salvar codigo (desenvolvimento)")
    args = ap.parse_args()

    if args.start:
        ENGINE.start()

    print(f"[i] Dashboard em http://{args.host}:{args.port}"
          + ("  (auto-reload ON)" if args.reload else ""))
    # threaded=True e essencial: cada stream MJPEG segura uma conexao.
    # Com --reload, o Flask reinicia ao salvar qualquer .py; e observamos
    # tambem os templates/estaticos, pra editar HTML/JS/CSS refletir na hora
    # (basta recarregar a pagina). use_reloader liga o watcher; debug fica
    # ligado junto para dar stack traces uteis durante o desenvolvimento.
    extra_files = None
    if args.reload:
        here = Path(__file__).parent
        extra_files = [str(p) for p in [
            *here.glob("templates/*.html"),
            *here.glob("static/*.js"),
            *here.glob("static/*.css"),
        ]]
    app.run(host=args.host, port=args.port, threaded=True,
            debug=args.reload, use_reloader=args.reload,
            extra_files=extra_files)


if __name__ == "__main__":
    main()

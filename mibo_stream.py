#!/usr/bin/env python3
"""
mibo_stream.py - Dashboard RTSP para uma ou multiplas cameras Intelbras Mibo.

Recursos:
- Exibe uma grade (dashboard) com multiplos streams RTSP.
- Reconexao automatica individual por camera em caso de queda.
- Grava segmentos .mp4 por camera (opcional).
- Captura snapshot do dashboard (opcional).

Entrada das cameras:
- --url repetido na linha de comando (recomendado)
- MIBO_URLS no ambiente (separado por virgula/; ou quebra de linha)
- modo legado de uma camera: --host/--password (ou MIBO_HOST/MIBO_PASS)

Exemplos:
    python mibo_stream.py --url "rtsp://admin:senha@192.168.1.10:554/cam/realmonitor?channel=1&subtype=0" --url "rtsp://admin:senha@192.168.1.11:554/cam/realmonitor?channel=1&subtype=0"
    python mibo_stream.py --record
"""

import argparse
import math
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import cv2
import numpy as np

import discover
import face_recog

# Força RTSP sobre TCP. Definido ANTES de instanciar VideoCapture.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",  # timeout de socket = 5 s
)


def build_url(host, user, password, port, sub):
    """Monta a URL RTSP. subtype=0 principal (Full HD), subtype=1 substream."""
    subtype = 1 if sub else 0
    return (
        f"rtsp://{user}:{password}@{host}:{port}"
        f"/cam/realmonitor?channel=1&subtype={subtype}&unicast=true&proto=Onvif"
    )


def normalize_rtsp_url(raw):
    """Normaliza URL RTSP e aceita texto colado no formato markdown."""
    value = (raw or "").strip()
    if not value:
        return ""

    # Aceita formato: rtsp://[user:pass@host/path](http://user:pass@host/path)
    md = re.match(r"^rtsp://\[(.+?)\]\(http://.+\)$", value)
    if md:
        return f"rtsp://{md.group(1)}"

    # Se vier em http(s), troca para rtsp mantendo o restante.
    value = re.sub(r"^https?://", "rtsp://", value, flags=re.IGNORECASE)
    return value


def parse_env_urls(raw):
    """Converte MIBO_URLS em lista de URLs (aceita , ; e quebra de linha)."""
    if not raw:
        return []
    parts = re.split(r"[;,\n]", raw)
    urls = [normalize_rtsp_url(p) for p in parts if p.strip()]
    return [u for u in urls if u]


def mask_rtsp_url(url):
    """Mascara senha da URL para log seguro."""
    try:
        parsed = urlsplit(url)
        if "@" not in parsed.netloc:
            return url
        creds, host = parsed.netloc.rsplit("@", 1)
        if ":" in creds:
            user, _ = creds.split(":", 1)
            safe_netloc = f"{user}:***@{host}"
        else:
            safe_netloc = parsed.netloc
        return urlunsplit((parsed.scheme, safe_netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return re.sub(r":([^:@/]+)@", r":***@", url, count=1)


def open_capture(url):
    """Abre o stream com backend FFMPEG. Retorna VideoCapture ou None."""
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # menor latência: descarta buffer
    if not cap.isOpened():
        cap.release()
        return None
    return cap


def safe_name(name):
    """Converte nome de camera para slug seguro em arquivo."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip())
    return cleaned.strip("_") or "cam"


def new_writer(cap, outdir, prefix, fps_fallback=15.0):
    """Cria um VideoWriter .mp4 com timestamp no nome."""
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 1 or fps > 60:
        fps = fps_fallback
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = outdir / f"{safe_name(prefix)}_{ts}.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    return writer, path


def build_tile(frame, name, status, recording, width, height,
               faces=None, face_size=None):
    """Gera o tile individual da camera com overlay (inclui caixas de rosto)."""
    if frame is None:
        tile = np.zeros((height, width, 3), dtype=np.uint8)
        cv2.putText(tile, "SEM VIDEO", (20, height // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 165, 255), 2)
    else:
        tile = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)

    # Desenha caixas de rosto reescaladas para o tamanho do tile.
    n_faces = 0
    if faces and face_size:
        sx = width / float(face_size[0])
        sy = height / float(face_size[1])
        for f in faces:
            x, y, bw, bh = f["box"]
            p1 = (int(x * sx), int(y * sy))
            p2 = (int((x + bw) * sx), int((y + bh) * sy))
            # Verde = pessoa identificada; laranja = detectado mas nao reconhecido.
            has_id = "name" in f
            name = f.get("name")
            color = (0, 255, 0) if name else ((0, 165, 255) if has_id else (0, 255, 0))
            cv2.rectangle(tile, p1, p2, color, 2)
            if has_id:
                label = name if name else "desconhecido"
                if name:
                    label = f"{name} {f.get('match', 0):.2f}"
            else:
                label = f"{f['score']:.2f}"
            ly = max(14, p1[1] - 5)
            cv2.putText(tile, label, (p1[0], ly),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        n_faces = len(faces)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rec_tag = "REC" if recording else "LIVE"
    face_tag = f" | {n_faces} rosto(s)" if (faces is not None) else ""
    line1 = f"{name} | {status} | {rec_tag}{face_tag}"
    cv2.rectangle(tile, (0, 0), (width, 52), (0, 0, 0), -1)
    cv2.putText(tile, line1, (12, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (0, 255, 0) if status == "ONLINE" else (0, 165, 255), 2)
    cv2.putText(tile, stamp, (12, 43), cv2.FONT_HERSHEY_SIMPLEX, 0.48,
                (220, 220, 220), 1)
    return tile


def build_dashboard(tiles, cols, tile_width, tile_height):
    """Monta o mosaico final em grade a partir dos tiles."""
    total = len(tiles)
    rows = int(math.ceil(total / cols))
    black = np.zeros((tile_height, tile_width, 3), dtype=np.uint8)
    padded = tiles[:]
    while len(padded) < rows * cols:
        padded.append(black.copy())

    lines = []
    for r in range(rows):
        row_tiles = padded[r * cols:(r + 1) * cols]
        lines.append(np.hstack(row_tiles))
    return np.vstack(lines)


def main():
    ap = argparse.ArgumentParser(description="Dashboard RTSP Intelbras Mibo")
    ap.add_argument("--url", action="append", default=[],
                    help="URL RTSP. Pode repetir o parametro para varias cameras")
    ap.add_argument("--name", action="append", default=[],
                    help="Nome amigavel da camera (na mesma ordem do --url)")
    ap.add_argument("--grid-cols", type=int, default=0,
                    help="quantidade de colunas da grade (padrao automatico)")
    ap.add_argument("--tile-width", type=int, default=640,
                    help="largura de cada tile do dashboard")
    ap.add_argument("--tile-height", type=int, default=360,
                    help="altura de cada tile do dashboard")

    # Modo legado (camera unica).
    ap.add_argument("--host", default=os.getenv("MIBO_HOST"))
    ap.add_argument("--user", default=os.getenv("MIBO_USER", "admin"))
    ap.add_argument("--password", default=os.getenv("MIBO_PASS"))
    ap.add_argument("--port", type=int, default=554)
    ap.add_argument("--sub", action="store_true", help="usa substream (baixa res)")
    ap.add_argument("--record", action="store_true", help="grava desde o início")
    ap.add_argument("--headless", action="store_true", help="sem janela")
    ap.add_argument("--outdir", default="./gravacoes")
    ap.add_argument("--segment", type=int, default=600,
                    help="duração de cada arquivo em segundos (default 600)")

    # Autodescoberta de cameras na rede.
    ap.add_argument("--auto", action="store_true",
                    help="descobre cameras na rede ao iniciar e adiciona ao dashboard")
    ap.add_argument("--config", default=str(discover.DEFAULT_CONFIG_PATH),
                    help="arquivo cameras.json para carregar/salvar")
    ap.add_argument("--rescan", type=int, default=0,
                    help="re-descobre a rede a cada N segundos em segundo plano (0=off)")
    ap.add_argument("--no-prompt", action="store_true",
                    help="na descoberta, nao perguntar senha (so credenciais padrao)")
    ap.add_argument("--no-scan", action="store_true",
                    help="na descoberta, nao varrer sub-rede (so WS-Discovery)")

    # Reconhecimento facial / historico.
    ap.add_argument("--face", action="store_true",
                    help="ativa deteccao de rostos ao vivo e registro de historico")
    ap.add_argument("--face-score", type=float, default=0.6,
                    help="confianca minima para considerar um rosto (0-1)")
    ap.add_argument("--face-interval", type=float, default=0.7,
                    help="intervalo em segundos entre deteccoes por camera")
    ap.add_argument("--face-cooldown", type=float, default=10.0,
                    help="segundos minimos entre registros de historico por camera")
    ap.add_argument("--face-log", default="./historico_faces",
                    help="pasta onde salvar recortes e historico.jsonl")
    ap.add_argument("--face-det-width", type=int, default=640,
                    help="largura para deteccao (menor = mais rapido)")
    ap.add_argument("--face-save-full", action="store_true",
                    help="salvar tambem o quadro inteiro anotado no historico")
    ap.add_argument("--face-identify", action="store_true",
                    help="identifica pessoas nomeadas (mostra o nome ao vivo)")
    ap.add_argument("--face-threshold", type=float,
                    default=face_recog.SFACE_COSINE_THRESHOLD,
                    help="similaridade minima para reconhecer (0-1, padrao 0.363)")
    ap.add_argument("--face-known", default=None,
                    help="known_faces.json (padrao: <face-log>/known_faces.json)")
    ap.add_argument("--alert-unknown", action="store_true",
                    help="alerta no terminal quando aparece alguem nao cadastrado")
    args = ap.parse_args()

    # Fonte 1: cameras salvas no config (persistidas de descobertas anteriores).
    saved = discover.load_config(args.config)

    # Fonte 2: URLs manuais (--url / MIBO_URLS).
    manual_urls = [normalize_rtsp_url(u) for u in args.url if u]
    manual_urls.extend(parse_env_urls(os.getenv("MIBO_URLS")))
    manual = []
    for idx, url in enumerate(manual_urls):
        name = args.name[idx].strip() if idx < len(args.name) and args.name[idx].strip() else None
        manual.append({"url": url, "name": name})

    # Fonte 3: autodescoberta na rede (opcional).
    discovered = []
    if args.auto:
        print("[i] Autodescoberta iniciada...")
        discovered = discover.discover_cameras(
            prompt=not args.no_prompt,
            do_subnet_scan=not args.no_scan,
        )
        print(f"[i] Descoberta concluida: {len(discovered)} camera(s).")

    # Une tudo evitando duplicatas por IP/URL; salva o resultado no config.
    combined = discover.merge_cameras(saved, discovered)
    combined = discover.merge_cameras(combined, manual)

    # Modo legado (--host/--password) se nada foi encontrado.
    if not combined:
        if args.host and args.password:
            legacy = build_url(args.host, args.user, args.password, args.port, args.sub)
            combined = [{"url": legacy, "name": "Cam legado"}]
        else:
            sys.exit(
                "Erro: nenhuma camera. Use --auto para descobrir na rede, "
                "informe --url/MIBO_URLS, ou defina MIBO_HOST e MIBO_PASS."
            )

    if args.auto or (discovered and args.config):
        try:
            discover.save_config(combined, args.config)
            print(f"[i] Config salvo em {args.config} ({len(combined)} camera(s)).")
        except OSError as exc:
            print(f"[!] Nao consegui salvar config: {exc}")

    urls = [c["url"] for c in combined]
    names = []
    for idx, cam in enumerate(combined):
        nm = cam.get("name") or (f"Cam {cam['ip']}" if cam.get("ip") else f"Cam {idx + 1}")
        names.append(nm)

    print("[i] Cameras configuradas:")
    for idx, url in enumerate(urls, start=1):
        print(f"    - {names[idx - 1]}: {mask_rtsp_url(url)}")

    outdir = Path(args.outdir)
    recording = args.record
    running = {"on": True}

    def stop(*_):
        running["on"] = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def make_cam(name, url):
        return {
            "name": name,
            "url": url,
            "cap": None,
            "last_frame": None,
            "status": "OFFLINE",
            "backoff": 1.0,
            "next_retry": 0.0,
            "writer": None,
            "seg_start": 0.0,
            "faces": [],            # ultimas caixas de rosto detectadas
            "face_size": None,      # (w, h) do quadro em que foram detectadas
            "next_face": 0.0,       # proximo instante de deteccao
        }

    cams = [make_cam(names[idx], url) for idx, url in enumerate(urls)]
    cams_lock = threading.Lock()

    # Rescan periodico em segundo plano: adiciona cameras novas sem travar a UI.
    def rescan_worker():
        while running["on"]:
            for _ in range(args.rescan):  # dorme em passos de 1s p/ sair rapido
                if not running["on"]:
                    return
                time.sleep(1)
            try:
                found = discover.discover_cameras(
                    prompt=False,  # em background nunca pergunta senha
                    do_subnet_scan=not args.no_scan,
                    on_progress=lambda m: None,
                )
            except Exception as exc:  # descoberta nunca deve derrubar o dashboard
                print(f"[!] Rescan falhou: {exc}")
                continue
            with cams_lock:
                known = {c["url"] for c in cams}
                known_ips = {u.split("@")[-1].split(":")[0] for u in known}
                added = []
                for cam in found:
                    if cam["url"] in known or cam.get("ip") in known_ips:
                        continue
                    cams.append(make_cam(cam.get("name") or f"Cam {cam['ip']}", cam["url"]))
                    added.append(cam)
                if added:
                    persist = discover.merge_cameras(
                        discover.load_config(args.config),
                        added,
                    )
                    try:
                        discover.save_config(persist, args.config)
                    except OSError:
                        pass
                    for cam in added:
                        print(f"[+] Rescan adicionou: {cam['name']} ({cam['url'] and mask_rtsp_url(cam['url'])})")

    if args.rescan and args.rescan > 0:
        threading.Thread(target=rescan_worker, daemon=True).start()
        print(f"[i] Rescan automatico a cada {args.rescan}s ativado.")

    # Reconhecimento facial: um worker unico processa todas as cameras em baixa
    # frequencia, sem travar o video. Detector nao e thread-safe -> 1 so thread.
    if args.face_identify:
        args.face = True  # identificacao exige a deteccao ligada

    face_logger = None
    face_recognizer = None
    known_faces = None
    alert_last = {}
    if args.face:
        try:
            face_detector = face_recog.FaceDetector(
                score_threshold=args.face_score, det_width=args.face_det_width)
            face_logger = face_recog.HistoryLogger(
                outdir=args.face_log, cooldown=args.face_cooldown,
                save_full=args.face_save_full)
            gpu = "GPU CUDA" if face_detector.using_gpu else "CPU"
            print(f"[i] Reconhecimento facial ATIVO ({gpu}). Historico em {args.face_log}")
            if args.face_identify:
                face_recognizer = face_recog.FaceRecognizer()
                known_path = args.face_known or str(Path(args.face_log) / "known_faces.json")
                known_faces = face_recog.KnownFaces(known_path)
                print(f"[i] Identificacao ATIVA: {len(known_faces.names)} amostra(s) "
                      f"de {len(set(known_faces.names))} pessoa(s) em {known_path}")
        except Exception as exc:
            print(f"[!] Nao foi possivel iniciar reconhecimento facial: {exc}")
            args.face = False

    def face_worker():
        while running["on"]:
            now = time.time()
            for cam in list(cams):
                if cam["cap"] is None or now < cam["next_face"]:
                    continue
                frame = cam["last_frame"]
                if frame is None:
                    continue
                cam["next_face"] = now + args.face_interval
                try:
                    faces = face_detector.detect(frame)
                except Exception:
                    faces = []

                # Identificacao: nomeia cada rosto reconhecido.
                if faces and known_faces is not None:
                    known_faces.reload_if_changed()  # pega novos cadastros do painel
                    for f in faces:
                        try:
                            emb = face_recognizer.embed(frame, f["row"])
                            name, sim = known_faces.identify(emb, args.face_threshold)
                        except Exception:
                            name, sim = None, 0.0
                        f["name"] = name
                        f["match"] = sim
                        # Log positivo quando identifica (cooldown por pessoa+camera).
                        if name:
                            key = (cam["name"], name)
                            if now - alert_last.get(key, 0.0) >= args.face_cooldown:
                                alert_last[key] = now
                                print(f"[ID] {cam['name']}: {name} reconhecido "
                                      f"(sim {sim:.2f})")
                    # Alerta de desconhecido (com cooldown por camera).
                    if args.alert_unknown and any(f.get("name") is None for f in faces):
                        if now - alert_last.get(cam["name"], 0.0) >= args.face_cooldown:
                            alert_last[cam["name"]] = now
                            print(f"[ALERTA] Pessoa NAO cadastrada em {cam['name']} "
                                  f"({datetime.now():%H:%M:%S})")

                cam["faces"] = faces
                cam["face_size"] = (frame.shape[1], frame.shape[0])
                if faces and face_logger is not None:
                    n = face_logger.maybe_log(cam["name"], frame, faces, now=now)
                    if n:
                        print(f"[FACE] {cam['name']}: {n} rosto(s) registrado(s).")
            time.sleep(0.05)

    if args.face:
        threading.Thread(target=face_worker, daemon=True).start()

    cols = args.grid_cols if args.grid_cols and args.grid_cols > 0 else int(math.ceil(math.sqrt(len(cams))))
    tile_w = max(160, args.tile_width)
    tile_h = max(120, args.tile_height)
    dashboard = None

    while running["on"]:
        now = time.time()
        for cam in cams:
            if cam["cap"] is None and now >= cam["next_retry"]:
                cap = open_capture(cam["url"])
                if cap is None:
                    cam["status"] = f"RETRY {cam['backoff']:.0f}s"
                    cam["next_retry"] = now + cam["backoff"]
                    cam["backoff"] = min(cam["backoff"] * 2, 30)
                    continue
                cam["cap"] = cap
                cam["status"] = "ONLINE"
                cam["backoff"] = 1.0
                cam["next_retry"] = 0.0
                print(f"[i] Conectado: {cam['name']}")

            if cam["cap"] is None:
                continue

            ok, frame = cam["cap"].read()
            if not ok:
                cam["cap"].release()
                cam["cap"] = None
                cam["status"] = "DESCONECTADA"
                cam["next_retry"] = time.time() + cam["backoff"]
                cam["backoff"] = min(cam["backoff"] * 2, 30)
                if cam["writer"] is not None:
                    cam["writer"].release()
                    cam["writer"] = None
                print(f"[!] Stream caiu: {cam['name']}. Reconectando...")
                continue

            cam["last_frame"] = frame

            # Rotacao de gravacao por camera.
            if recording:
                if cam["writer"] is None:
                    cam["writer"], seg_path = new_writer(cam["cap"], outdir, cam["name"])
                    cam["seg_start"] = time.time()
                    print(f"[REC] {cam['name']}: {seg_path}")
                elif time.time() - cam["seg_start"] >= args.segment:
                    cam["writer"].release()
                    cam["writer"], seg_path = new_writer(cam["cap"], outdir, cam["name"])
                    cam["seg_start"] = time.time()
                    print(f"[REC] {cam['name']}: {seg_path}")
                cam["writer"].write(frame)
            elif cam["writer"] is not None:
                cam["writer"].release()
                cam["writer"] = None

        if not args.headless:
            tiles = [
                build_tile(cam["last_frame"], cam["name"], cam["status"], recording,
                           tile_w, tile_h,
                           faces=cam["faces"] if args.face else None,
                           face_size=cam["face_size"])
                for cam in cams
            ]
            dashboard = build_dashboard(tiles, cols=cols, tile_width=tile_w, tile_height=tile_h)
            cv2.imshow("Mibo Dashboard", dashboard)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                running["on"] = False
            elif key == ord("s") and dashboard is not None:
                outdir.mkdir(parents=True, exist_ok=True)
                snap = outdir / f"dashboard_{datetime.now():%Y%m%d_%H%M%S}.jpg"
                cv2.imwrite(str(snap), dashboard)
                print(f"[SNAP] {snap}")
            elif key == ord("r"):
                recording = not recording
                print(f"[i] Gravacao {'ON' if recording else 'OFF'}")

        # Evita loop quente quando todas as cameras estao offline.
        time.sleep(0.01)

    for cam in cams:
        if cam["cap"] is not None:
            cam["cap"].release()
        if cam["writer"] is not None:
            cam["writer"].release()

    if not args.headless:
        cv2.destroyAllWindows()
    print("[i] Encerrado.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
engine.py - Motor de cameras + IA facial, desacoplado de qualquer interface.

Gerencia a captura RTSP (uma thread por camera, reconexao automatica), roda a
deteccao/identificacao facial em segundo plano, grava historico de recortes e um
log de EVENTOS ("Hugo na Cam X as Y"). Expoe quadros anotados em JPEG para
streaming MJPEG no navegador. E consumido pelo app Flask (app.py).
"""

import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

import discover
import face_recog

# RTSP sobre TCP, definido antes de qualquer VideoCapture.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",
)


def safe_name(name):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (name or "").strip())
    return cleaned.strip("_") or "cam"


def mask_rtsp_url(url):
    return re.sub(r"://([^:/@]+):[^@/]+@", r"://\1:***@", url or "", count=1)


_FFMPEG = shutil.which("ffmpeg")


class FfmpegWriter:
    """Grava frames BGR em .mp4 H.264 (avc1) via ffmpeg, tocavel no navegador.

    O VideoWriter do OpenCV neste build so encoda MPEG-4 Part 2 (mp4v), que os
    navegadores nao reproduzem. Encaminhamos os quadros crus para o ffmpeg do
    sistema (libx264) pelo stdin. Mesma interface do VideoWriter: write/release.
    """

    def __init__(self, path, width, height, fps):
        self.path = str(path)
        self._ok = False
        cmd = [
            _FFMPEG, "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-r", f"{fps:g}",
            "-i", "-",
            "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            self.path,
        ]
        try:
            self._p = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._ok = True
        except OSError:
            self._p = None

    def isOpened(self):
        return self._ok and self._p is not None

    def write(self, frame):
        if not self._ok or self._p is None or self._p.stdin is None:
            return
        try:
            self._p.stdin.write(frame.tobytes())
        except (BrokenPipeError, ValueError, OSError):
            self._ok = False

    def release(self):
        if self._p is None:
            return
        try:
            if self._p.stdin:
                self._p.stdin.close()
            self._p.wait(timeout=10)
        except Exception:
            self._p.kill()
        finally:
            self._p = None
            self._ok = False


def new_writer(width, height, fps, outdir, prefix):
    """Cria um writer .mp4 (H.264) com timestamp no nome. Retorna (writer, path).

    Usa ffmpeg (libx264) quando disponivel, gerando video H.264 que toca no
    navegador. Sem ffmpeg, cai no VideoWriter mp4v do OpenCV (nao toca no
    navegador, mas ainda grava para players de desktop/VLC).
    """
    if not fps or fps <= 1 or fps > 60:
        fps = 15.0
    width = width or 1920
    height = height or 1080
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = outdir / f"{safe_name(prefix)}_{ts}.mp4"
    if _FFMPEG:
        w = FfmpegWriter(path, width, height, fps)
        if w.isOpened():
            return w, path
        w.release()
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    return cv2.VideoWriter(str(path), fourcc, fps, (width, height)), path


# --- Log de eventos --------------------------------------------------------

class EventLogger:
    """Registra eventos de reconhecimento em eventos.jsonl com miniatura."""

    def __init__(self, outdir, cooldown=20.0):
        self.dir = Path(outdir)
        self.jsonl = self.dir / "eventos.jsonl"
        self.cooldown = cooldown
        self._last = {}
        self._lock = threading.Lock()

    def log(self, camera, name, score, frame, box):
        key = (camera, name or "__desconhecido__")
        now = time.time()
        with self._lock:
            if now - self._last.get(key, 0.0) < self.cooldown:
                return None
            self._last[key] = now

        dt = datetime.now()
        day = self.dir / "eventos" / dt.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        slug = safe_name(name or "desconhecido")
        fname = f"{safe_name(camera)}_{dt.strftime('%H%M%S_%f')[:-3]}_{slug}.jpg"
        thumb_rel = f"eventos/{dt.strftime('%Y-%m-%d')}/{fname}"
        try:
            x, y, w, h = box
            mx, my = int(w * 0.3), int(h * 0.3)
            H, W = frame.shape[:2]
            crop = frame[max(0, y - my):min(H, y + h + my),
                         max(0, x - mx):min(W, x + w + mx)]
            if crop.size:
                cv2.imwrite(str(self.dir / thumb_rel), crop)
        except Exception:
            thumb_rel = None

        rec = {
            "ts": dt.isoformat(timespec="seconds"),
            "camera": camera,
            "name": name,
            "known": bool(name),
            "score": round(float(score), 3),
            "thumb": thumb_rel,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.jsonl, "a", encoding="utf-8") as fh:
            fh.write(_json_line(rec))
        return rec

    def recent(self, limit=200, known_only=False, name=None):
        if not self.jsonl.exists():
            return []
        out = []
        for line in reversed(self.jsonl.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                r = _json_loads(line)
            except Exception:
                continue
            if known_only and not r.get("known"):
                continue
            if name and r.get("name") != name:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out


def _json_line(obj):
    import json
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _json_loads(s):
    import json
    return json.loads(s)


# --- Camera (thread de captura) -------------------------------------------

class CameraWorker(threading.Thread):
    """Captura RTSP com reconexao automatica; mantem o ultimo quadro em memoria."""

    def __init__(self, cam, rec_dir="./gravacoes", seg_seconds=600):
        super().__init__(daemon=True)
        self.cam = cam          # dict compartilhado com o Engine
        self.rec_dir = rec_dir
        self.seg_seconds = seg_seconds
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        cam = self.cam
        backoff = 1.0
        cap = None
        writer = None
        seg_start = 0.0

        def close_writer():
            nonlocal writer
            if writer is not None:
                writer.release()
                writer = None
                cam["recording"] = False

        while not self._stop.is_set():
            if cap is None:
                cap = cv2.VideoCapture(cam["url"], cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not cap.isOpened():
                    cap.release()
                    cap = None
                    cam["status"] = f"reconectando ({backoff:.0f}s)"
                    if self._stop.wait(backoff):
                        break
                    backoff = min(backoff * 2, 30)
                    continue
                cam["status"] = "online"
                backoff = 1.0

            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = None
                cam["status"] = "offline"
                cam["frame"] = None
                close_writer()  # segmento incompleto e finalizado ao reconectar
                continue
            cam["frame"] = frame

            # Gravacao continua em segmentos .mp4 (liga/desliga via cam["record"]).
            if cam.get("record"):
                now = time.time()
                if writer is None or now - seg_start >= self.seg_seconds:
                    close_writer()
                    h, w = frame.shape[:2]
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    writer, _ = new_writer(w, h, fps, self.rec_dir, cam["name"])
                    seg_start = now
                    cam["recording"] = True
                writer.write(frame)
            else:
                close_writer()

        close_writer()
        if cap is not None:
            cap.release()
        cam["status"] = "parada"
        cam["frame"] = None


# --- Motor -----------------------------------------------------------------

class Engine:
    """Orquestra cameras, IA facial, historico e eventos. Thread-safe."""

    def __init__(self, config_path="cameras.json", face_log="./historico_faces",
                 face_interval=0.7, face_threshold=None, det_width=960,
                 rec_dir="./gravacoes", seg_seconds=600, record_all=False):
        self.config_path = config_path
        self.face_log = face_log
        self.face_interval = face_interval
        self.face_threshold = (face_threshold if face_threshold is not None
                               else face_recog.SFACE_COSINE_THRESHOLD)
        self.det_width = det_width
        self.rec_dir = rec_dir
        self.seg_seconds = seg_seconds
        self.record_all = record_all

        self.cameras = []          # lista de dicts
        self._lock = threading.Lock()
        self.running = False
        self._face_thread = None

        # Componentes de IA (carregados no start()).
        self.detector = None
        self.recognizer = None
        self.known = None
        self.history = None
        self.events = EventLogger(face_log)
        self.face_enabled = False
        self.using_gpu = False

        self._load_cameras_from_config()

    # ---- Config / cameras ----

    def _load_cameras_from_config(self):
        saved = discover.load_config(self.config_path)
        with self._lock:
            for c in saved:
                self._add_cam_dict(c)

    def _add_cam_dict(self, c):
        url = c.get("url")
        if not url or any(x["url"] == url for x in self.cameras):
            return None
        ip = c.get("ip") or _host_of(url)
        cid = self._unique_id(safe_name(ip or c.get("name") or f"cam{len(self.cameras)}"))
        cam = {
            "id": cid,
            "name": c.get("name") or (f"Cam {ip}" if ip else cid),
            "url": url,
            "ip": ip,
            "status": "parada",
            "frame": None,
            "faces": [],
            "face_size": None,
            "next_face": 0.0,
            "record": self.record_all,   # grava desde o inicio se ligado global
            "recording": False,          # esta escrevendo em disco agora?
        }
        self.cameras.append(cam)
        return cam

    def _unique_id(self, base):
        ids = {c["id"] for c in self.cameras}
        if base not in ids:
            return base
        i = 2
        while f"{base}-{i}" in ids:
            i += 1
        return f"{base}-{i}"

    def get_camera(self, cid):
        return next((c for c in self.cameras if c["id"] == cid), None)

    # ---- Ciclo de vida ----

    def start(self):
        with self._lock:
            if self.running:
                return
            self.running = True
            self._init_face()
            for cam in self.cameras:
                cam["worker"] = CameraWorker(cam, self.rec_dir, self.seg_seconds)
                cam["worker"].start()
            if self.face_enabled:
                self._face_thread = threading.Thread(target=self._face_loop, daemon=True)
                self._face_thread.start()

    def stop(self):
        with self._lock:
            self.running = False
            for cam in self.cameras:
                w = cam.get("worker")
                if w:
                    w.stop()
            for cam in self.cameras:
                cam["frame"] = None
                cam["faces"] = []
                cam["status"] = "parada"

    def _init_face(self):
        try:
            self.detector = face_recog.FaceDetector(det_width=self.det_width)
            self.history = face_recog.HistoryLogger(outdir=self.face_log, cooldown=10.0)
            self.using_gpu = self.detector.using_gpu
            try:
                self.recognizer = face_recog.FaceRecognizer()
                known_path = str(Path(self.face_log) / "known_faces.json")
                self.known = face_recog.KnownFaces(known_path)
            except Exception:
                self.recognizer = None
                self.known = None
            self.face_enabled = True
        except Exception as exc:
            print(f"[engine] IA facial indisponivel: {exc}")
            self.face_enabled = False

    # ---- Loop de IA ----

    def _face_loop(self):
        while self.running:
            if self.known is not None:
                self.known.reload_if_changed()
            for cam in list(self.cameras):
                if not self.running:
                    break
                frame = cam.get("frame")
                now = time.time()
                if frame is None or now < cam["next_face"]:
                    continue
                cam["next_face"] = now + self.face_interval
                try:
                    faces = self.detector.detect(frame)
                except Exception:
                    faces = []
                for f in faces:
                    # So identifica rostos de qualidade (evita embeddings-lixo).
                    f["good"] = face_recog.good_quality(f["box"], f.get("score", 1.0))
                    if (self.recognizer is not None and self.known is not None
                            and f["good"]):
                        try:
                            emb = self.recognizer.embed(frame, f["row"])
                            name, sim = self.known.identify(emb, self.face_threshold)
                        except Exception:
                            name, sim = None, 0.0
                        f["name"] = name
                        f["match"] = sim
                cam["faces"] = faces
                cam["face_size"] = (frame.shape[1], frame.shape[0])

                if faces and self.history is not None:
                    self.history.maybe_log(cam["name"], frame, faces, now=now)
                for f in faces:
                    if not f.get("good"):
                        continue  # ignora rostos de baixa qualidade nos eventos
                    name = f.get("name")
                    if name:
                        self.events.log(cam["name"], name, f.get("match", 0), frame, f["box"])
                    elif "name" in f:  # identificacao ligada, mas desconhecido
                        self.events.log(cam["name"], None, f.get("score", 0), frame, f["box"])
            time.sleep(0.03)

    # ---- Saida de video ----

    def annotated_jpeg(self, cid, quality=75):
        cam = self.get_camera(cid)
        if cam is None:
            return None
        frame = cam.get("frame")
        if frame is None:
            frame = _placeholder(cam["name"], cam.get("status", "sem sinal"))
        else:
            frame = _draw_faces(frame.copy(), cam.get("faces"), cam.get("face_size"))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None

    # ---- Descoberta / adicao ----

    def discover_and_add(self, prompt=False):
        found = discover.discover_cameras(prompt=prompt, on_progress=lambda m: None)
        added = []
        with self._lock:
            for c in found:
                cam = self._add_cam_dict(c)
                if cam is not None:
                    added.append(cam)
                    if self.running:
                        cam["worker"] = CameraWorker(cam, self.rec_dir, self.seg_seconds)
                        cam["worker"].start()
        if added:
            self._persist()
        return added

    def cluster_faces(self, threshold=None):
        """Agrupa os rostos capturados por semelhanca (reusa modelos carregados)."""
        if self.detector is None:
            self.detector = face_recog.FaceDetector(det_width=self.det_width)
        if self.recognizer is None:
            try:
                self.recognizer = face_recog.FaceRecognizer()
            except Exception:
                return []
        th = threshold if threshold is not None else face_recog.CLUSTER_THRESHOLD
        return face_recog.cluster_faces(self.face_log, recognizer=self.recognizer,
                                        threshold=th)

    def rename_camera(self, cid, name):
        name = (name or "").strip()
        if not name:
            return False
        with self._lock:
            cam = self.get_camera(cid)
            if cam is None:
                return False
            cam["name"] = name
        self._persist()
        return True

    def add_manual(self, url, name=None):
        url = discover.normalize_rtsp_url(url)
        with self._lock:
            cam = self._add_cam_dict({"url": url, "name": name})
            if cam and self.running:
                cam["worker"] = CameraWorker(cam, self.rec_dir, self.seg_seconds)
                cam["worker"].start()
        if cam:
            self._persist()
        return cam

    def _persist(self):
        payload = [{"url": c["url"], "name": c["name"], "ip": c["ip"]}
                   for c in self.cameras]
        try:
            discover.save_config(payload, self.config_path)
        except OSError:
            pass

    # ---- Gravacao ----

    def set_recording(self, cid, on):
        """Liga/desliga a gravacao de uma camera. O worker aplica no proximo quadro."""
        with self._lock:
            cam = self.get_camera(cid)
            if cam is None:
                return False
            cam["record"] = bool(on)
        return True

    def toggle_recording(self, cid):
        cam = self.get_camera(cid)
        if cam is None:
            return None
        new = not cam.get("record")
        self.set_recording(cid, new)
        return new

    # ---- Status ----

    def status(self):
        people = len(set(self.known.names)) if self.known and self.known.names else 0
        return {
            "running": self.running,
            "face_enabled": self.face_enabled,
            "using_gpu": self.using_gpu,
            "known_people": people,
            "cameras": [{
                "id": c["id"], "name": c["name"], "ip": c.get("ip"),
                "status": c.get("status", "parada"),
                "url": mask_rtsp_url(c["url"]),
                "faces": len(c.get("faces") or []),
                "record": bool(c.get("record")),
                "recording": bool(c.get("recording")),
            } for c in self.cameras],
        }


def _host_of(url):
    m = re.search(r"@([^:/]+)", url or "")
    return m.group(1) if m else None


def _draw_faces(frame, faces, face_size):
    if not faces or not face_size:
        return frame
    H, W = frame.shape[:2]
    sx = W / float(face_size[0])
    sy = H / float(face_size[1])
    for f in faces:
        x, y, bw, bh = f["box"]
        p1 = (int(x * sx), int(y * sy))
        p2 = (int((x + bw) * sx), int((y + bh) * sy))
        # Rosto de baixa qualidade: caixa cinza fina, sem rotulo.
        if "good" in f and not f["good"]:
            cv2.rectangle(frame, p1, p2, (120, 120, 120), 1)
            continue
        has_id = "name" in f
        name = f.get("name")
        color = (80, 220, 100) if name else ((40, 150, 255) if has_id else (80, 220, 100))
        cv2.rectangle(frame, p1, p2, color, 2)
        if has_id:
            label = f"{name} {f.get('match', 0):.2f}" if name else "desconhecido"
        else:
            label = f"{f.get('score', 0):.2f}"
        ytxt = max(16, p1[1] - 6)
        cv2.rectangle(frame, (p1[0], ytxt - 16), (p1[0] + 12 + 9 * len(label), ytxt + 4),
                      (0, 0, 0), -1)
        cv2.putText(frame, label, (p1[0] + 4, ytxt), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, color, 1, cv2.LINE_AA)
    return frame


_PLACEHOLDER_CACHE = {}


def _placeholder(name, status):
    import numpy as np
    key = (name, status)
    img = np.zeros((360, 640, 3), dtype=np.uint8)
    img[:] = (28, 32, 39)
    cv2.putText(img, name, (24, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 2)
    cv2.putText(img, status, (24, 205), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (120, 160, 255), 1)
    return img

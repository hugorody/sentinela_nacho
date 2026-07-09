#!/usr/bin/env python3
"""
face_recog.py - Deteccao de rostos ao vivo e registro de historico.

Usa os modelos ONNX embutidos no OpenCV 5 (YuNet p/ deteccao, SFace p/ embedding),
sem dependencias extras. Roda em CPU e usa a GPU NVIDIA automaticamente quando o
driver CUDA estiver instalado (cv2.cuda disponivel).

Objetivo principal: REGISTRAR HISTORICO - a cada rosto detectado, salva um recorte
com timestamp e uma linha em historico.jsonl.

Uso como biblioteca:
    from face_recog import FaceDetector, HistoryLogger
    det = FaceDetector("models/face_detection_yunet_2023mar.onnx")
    faces = det.detect(frame)          # [{"box": (x,y,w,h), "score": 0.9}, ...]
    logger = HistoryLogger("./historico_faces", cooldown=10)
    logger.maybe_log("Cam 1", frame, faces)
"""

import collections
import json
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

DEFAULT_YUNET = str(Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx")
DEFAULT_SFACE = str(Path(__file__).with_name("models") / "face_recognition_sface_2021dec.onnx")
# Limiar de similaridade de cosseno recomendado pelo OpenCV para o SFace.
SFACE_COSINE_THRESHOLD = 0.363

# Qualidade minima para USAR um rosto em reconhecimento (cadastro/identificacao).
# Rostos menores/borrados geram embeddings ruins e destroem o agrupamento.
MIN_FACE_PX = 90        # menor lado da caixa do rosto, em pixels
MIN_DET_SCORE = 0.65    # confianca minima da deteccao


def good_quality(box, score, min_px=MIN_FACE_PX, min_score=MIN_DET_SCORE):
    """Rosto grande e nitido o bastante para reconhecimento confiavel?"""
    return min(box[2], box[3]) >= min_px and score >= min_score


def cuda_available():
    """True se o OpenCV enxerga uma GPU CUDA utilizavel."""
    try:
        return cv2.cuda.getCudaEnabledDeviceCount() > 0
    except Exception:
        return False


def _backend_target():
    """Escolhe backend/target: CUDA se disponivel, senao CPU. GPU-ready."""
    if cuda_available():
        return cv2.dnn.DNN_BACKEND_CUDA, cv2.dnn.DNN_TARGET_CUDA
    return cv2.dnn.DNN_BACKEND_DEFAULT, cv2.dnn.DNN_TARGET_CPU


class FaceDetector:
    """Detector YuNet. Redimensiona o quadro para acelerar e reescala as caixas."""

    def __init__(self, model_path=DEFAULT_YUNET, score_threshold=0.6,
                 nms_threshold=0.3, top_k=5000, det_width=640):
        if not Path(model_path).exists():
            raise FileNotFoundError(
                f"Modelo YuNet nao encontrado: {model_path}. "
                "Baixe de opencv_zoo/models/face_detection_yunet."
            )
        backend, target = _backend_target()
        self.det_width = det_width
        self.score_threshold = score_threshold
        # input_size inicial provisorio; ajustado por quadro em detect().
        self._det = cv2.FaceDetectorYN.create(
            model_path, "", (det_width, det_width),
            score_threshold, nms_threshold, top_k, backend, target,
        )
        self.using_gpu = cuda_available()

    def detect(self, frame):
        """Detecta rostos. Retorna lista de {box:(x,y,w,h), score, landmarks}.

        As caixas estao nas coordenadas do quadro ORIGINAL recebido.
        """
        if frame is None:
            return []
        h, w = frame.shape[:2]
        # Redimensiona para acelerar (mantendo proporcao).
        scale = 1.0
        proc = frame
        if w > self.det_width:
            scale = self.det_width / float(w)
            proc = cv2.resize(frame, (self.det_width, int(round(h * scale))),
                              interpolation=cv2.INTER_AREA)
        ph, pw = proc.shape[:2]
        self._det.setInputSize((pw, ph))
        _, faces = self._det.detect(proc)
        results = []
        if faces is None:
            return results
        inv = 1.0 / scale
        for f in faces:
            # Reescala caixa + 5 landmarks para as coordenadas do quadro original
            # (os landmarks sao necessarios para alinhar o rosto no SFace).
            row = np.array(f, dtype=np.float32).copy()
            row[0:14] *= inv
            x, y, bw, bh = row[0], row[1], row[2], row[3]
            results.append({
                "box": (int(x), int(y), int(bw), int(bh)),
                "score": float(f[-1]),
                "row": row,   # linha YuNet (15 val) em coords originais
            })
        return results


class HistoryLogger:
    """Salva recortes de rosto + linha em JSONL, com cooldown por camera.

    O cooldown evita gravar o mesmo rosto dezenas de vezes por segundo: apos
    registrar em uma camera, espera `cooldown` segundos antes de registrar de novo.
    """

    def __init__(self, outdir="./historico_faces", cooldown=10.0,
                 min_size=MIN_FACE_PX, min_score=MIN_DET_SCORE, save_full=False):
        self.outdir = Path(outdir)
        self.cooldown = cooldown
        self.min_size = min_size          # so guarda rostos grandes o bastante
        self.min_score = min_score        # e nitidos o bastante (nomeaveis)
        self.save_full = save_full        # tambem salva o quadro inteiro anotado
        self._last = {}                   # cam_name -> timestamp do ultimo registro
        self.jsonl = self.outdir / "historico.jsonl"

    def maybe_log(self, cam_name, frame, faces, now=None):
        """Registra os rostos se o cooldown da camera ja passou.

        Retorna a quantidade de rostos salvos (0 se em cooldown ou sem rosto).
        """
        if not faces or frame is None:
            return 0
        now = now if now is not None else time.time()
        if now - self._last.get(cam_name, 0.0) < self.cooldown:
            return 0

        # So guarda rostos com qualidade suficiente para nomear/reconhecer.
        valid = [f for f in faces
                 if good_quality(f["box"], f.get("score", 1.0), self.min_size, self.min_score)]
        if not valid:
            return 0

        stamp_dt = datetime.now()
        day_dir = self.outdir / stamp_dt.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        ts = stamp_dt.strftime("%H%M%S_%f")[:-3]
        from re import sub
        slug = sub(r"[^a-zA-Z0-9_-]+", "_", cam_name).strip("_") or "cam"

        h, w = frame.shape[:2]
        saved = 0
        records = []
        for i, f in enumerate(valid):
            x, y, bw, bh = f["box"]
            # Margem ao redor do rosto para um recorte mais util.
            mx, my = int(bw * 0.25), int(bh * 0.25)
            x0, y0 = max(0, x - mx), max(0, y - my)
            x1, y1 = min(w, x + bw + mx), min(h, y + bh + my)
            crop = frame[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            fname = f"{slug}_{ts}_{i}.jpg"
            cv2.imwrite(str(day_dir / fname), crop)
            records.append({
                "ts": stamp_dt.isoformat(timespec="seconds"),
                "camera": cam_name,
                "file": str((day_dir / fname).relative_to(self.outdir)),
                "score": round(f["score"], 3),
                "box": [x, y, bw, bh],
            })
            saved += 1

        if self.save_full and saved:
            full = frame.copy()
            for f in valid:
                x, y, bw, bh = f["box"]
                cv2.rectangle(full, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
            cv2.imwrite(str(day_dir / f"{slug}_{ts}_full.jpg"), full)

        if records:
            self.outdir.mkdir(parents=True, exist_ok=True)
            with open(self.jsonl, "a", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            self._last[cam_name] = now
        return saved


# --- Reconhecimento (identidade) ------------------------------------------

class FaceRecognizer:
    """Extrai embeddings faciais (SFace) e compara por similaridade de cosseno."""

    def __init__(self, model_path=DEFAULT_SFACE):
        if not Path(model_path).exists():
            raise FileNotFoundError(
                f"Modelo SFace nao encontrado: {model_path}. "
                "Baixe de opencv_zoo/models/face_recognition_sface."
            )
        backend, target = _backend_target()
        self._rec = cv2.FaceRecognizerSF.create(model_path, "", backend, target)
        self.using_gpu = cuda_available()

    def embed(self, frame, row):
        """Alinha o rosto (via landmarks da linha YuNet) e retorna o embedding."""
        row = np.asarray(row, dtype=np.float32).reshape(1, -1)
        aligned = self._rec.alignCrop(frame, row)
        feat = self._rec.feature(aligned)
        return np.asarray(feat, dtype=np.float32).flatten().copy()


def cosine_sim(a, b):
    """Similaridade de cosseno entre um vetor a e um vetor OU matriz b (linhas)."""
    a = np.asarray(a, dtype=np.float32).flatten()
    b = np.asarray(b, dtype=np.float32)
    na = np.linalg.norm(a) + 1e-8
    if b.ndim == 1:
        return float(np.dot(a, b) / (na * (np.linalg.norm(b) + 1e-8)))
    nb = np.linalg.norm(b, axis=1) + 1e-8
    return (b @ a) / (nb * na)


class KnownFaces:
    """Banco de rostos conhecidos (nome -> embeddings). Recarrega se o arquivo mudar."""

    def __init__(self, path):
        self.path = Path(path)
        self.names = []
        self.embeddings = None      # np.array (N, D)
        self._mtime = 0.0
        self.load()

    def load(self):
        if not self.path.exists():
            self.names, self.embeddings = [], None
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self.names, self.embeddings = [], None
            return
        faces = data.get("faces", [])
        self.names = [f["name"] for f in faces]
        self.embeddings = (np.array([f["embedding"] for f in faces], dtype=np.float32)
                           if faces else None)
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = 0.0

    def reload_if_changed(self):
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if m != self._mtime:
            self.load()

    def identify(self, embedding, threshold=SFACE_COSINE_THRESHOLD):
        """Retorna (nome, similaridade) do melhor match, ou (None, sim) se abaixo."""
        if self.embeddings is None or len(self.names) == 0:
            return (None, 0.0)
        sims = cosine_sim(embedding, self.embeddings)
        idx = int(np.argmax(sims))
        best = float(sims[idx])
        if best >= threshold:
            return (self.names[idx], best)
        return (None, best)


def enroll_from_labels(outdir, known_path=None, detector=None, recognizer=None,
                       min_px=MIN_FACE_PX, min_score=MIN_DET_SCORE):
    """Constroi known_faces.json a partir dos rotulos do painel (labels.json).

    So treina com rostos de boa qualidade (tamanho/score originais do historico),
    pois rostos minusculos geram embeddings ruins e pioram o agrupamento.
    Retorna dict {"people", "total", "path", "skipped_quality", "skipped"}.
    """
    outdir = Path(outdir)
    known_path = Path(known_path) if known_path else (outdir / "known_faces.json")
    labels_path = outdir / "labels.json"
    if not labels_path.exists():
        return {"people": {}, "total": 0, "path": str(known_path)}
    labels = json.loads(labels_path.read_text(encoding="utf-8"))

    # Qualidade original de cada recorte (box/score do momento da captura).
    hist = {}
    hpath = outdir / "historico.jsonl"
    if hpath.exists():
        for line in hpath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                hist[r["file"]] = r
            except (json.JSONDecodeError, KeyError):
                continue

    # Limiar baixo p/ reencontrar rostos em recortes pequenos/menos nitidos.
    detector = detector or FaceDetector(score_threshold=0.3)
    recognizer = recognizer or FaceRecognizer()

    faces = []
    people = {}
    skipped = []
    skipped_quality = []
    for file, name in labels.items():
        name = (name or "").strip()
        if not name:
            continue
        # Filtro de qualidade pelo tamanho/score originais, quando disponiveis.
        h = hist.get(file)
        if h and not good_quality(h["box"], h.get("score", 1.0), min_px, min_score):
            skipped_quality.append(file)
            continue
        img = cv2.imread(str(outdir / file))
        if img is None:
            skipped.append(file)
            continue
        dets = detector.detect(img)
        if not dets:
            skipped.append(file)
            continue
        # Usa o maior rosto do recorte.
        d = max(dets, key=lambda r: r["box"][2] * r["box"][3])
        try:
            emb = recognizer.embed(img, d["row"])
        except Exception:
            skipped.append(file)
            continue
        faces.append({"name": name, "file": file, "embedding": emb.tolist()})
        people[name] = people.get(name, 0) + 1

    known_path.parent.mkdir(parents=True, exist_ok=True)
    known_path.write_text(json.dumps({"faces": faces}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return {"people": people, "total": len(faces), "path": str(known_path),
            "skipped_quality": skipped_quality, "skipped": skipped}


# --- Agrupamento automatico (clustering) ----------------------------------

# Limiar de agrupamento: acima do limiar de match (0.363) e do maior valor
# entre-pessoas observado (~0.41), para nao fundir pessoas diferentes.
CLUSTER_THRESHOLD = 0.42


def _crop_embedding(outdir, rec_row, detector, recognizer):
    img = cv2.imread(str(Path(outdir) / rec_row["file"]))
    if img is None:
        return None
    dets = detector.detect(img)
    if not dets:
        return None
    d = max(dets, key=lambda r: r["box"][2] * r["box"][3])
    try:
        return recognizer.embed(img, d["row"])
    except Exception:
        return None


def cluster_faces(outdir, detector=None, recognizer=None, threshold=CLUSTER_THRESHOLD,
                  min_px=MIN_FACE_PX, min_score=MIN_DET_SCORE):
    """Agrupa os rostos capturados por semelhanca (clustering guloso por centroide).

    Retorna lista de grupos ordenados por tamanho, cada um:
    {id, size, suggested, files, thumbs, cameras}.
    """
    outdir = Path(outdir)
    hpath = outdir / "historico.jsonl"
    if not hpath.exists():
        return []
    labels = {}
    lpath = outdir / "labels.json"
    if lpath.exists():
        try:
            labels = json.loads(lpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            labels = {}

    detector = detector or FaceDetector(score_threshold=0.3)
    recognizer = recognizer or FaceRecognizer()

    # Coleta embeddings dos rostos de boa qualidade (evita rostos-lixo).
    items = []
    seen = set()
    for line in hpath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        f = r.get("file")
        if not f or f in seen:
            continue
        if not good_quality(r["box"], r.get("score", 1.0), min_px, min_score):
            continue
        emb = _crop_embedding(outdir, r, detector, recognizer)
        if emb is None:
            continue
        seen.add(f)
        items.append({
            "file": f, "emb": emb, "camera": r.get("camera"), "ts": r.get("ts"),
            "name": labels.get(f),
            "quality": r.get("score", 0) * min(r["box"][2], r["box"][3]),
        })

    # Melhores primeiro: sementes de cluster mais confiaveis.
    items.sort(key=lambda x: x["quality"], reverse=True)

    # Rostos ja nomeados: agrupa pelo NOME (confia no humano), e guarda o
    # centroide de cada pessoa para sugerir nomes aos grupos sem nome.
    named = collections.defaultdict(list)
    unnamed = []
    for it in items:
        (named[it["name"]] if it["name"] else unnamed).append(it)
    named_centroids = {
        name: np.mean([x["emb"] for x in members], axis=0)
        for name, members in named.items()
    }

    # Clustering guloso por centroide apenas dos rostos SEM nome.
    clusters = []
    for it in unnamed:
        best, best_sim = None, -1.0
        for c in clusters:
            sim = float(cosine_sim(it["emb"], c["sum"] / c["n"]))
            if sim > best_sim:
                best_sim, best = sim, c
        if best is not None and best_sim >= threshold:
            best["sum"] += it["emb"]
            best["n"] += 1
            best["items"].append(it)
        else:
            clusters.append({"sum": it["emb"].astype(np.float32).copy(), "n": 1, "items": [it]})

    def group(members, gid, confirmed, name, suggested):
        members = sorted(members, key=lambda x: x["quality"], reverse=True)
        return {
            "id": gid, "size": len(members), "confirmed": confirmed,
            "name": name, "suggested": suggested,
            "files": [m["file"] for m in members],
            "thumbs": [m["file"] for m in members[:4]],
            "cameras": sorted({m["camera"] for m in members if m["camera"]}),
        }

    out = []
    gid = 0
    # Grupos sem nome primeiro (sao os que precisam de acao), com sugestao.
    for c in clusters:
        centroid = c["sum"] / c["n"]
        best_name, best_sim = "", threshold
        for name, cen in named_centroids.items():
            sim = float(cosine_sim(centroid, cen))
            if sim >= best_sim:
                best_sim, best_name = sim, name
        out.append(group(c["items"], gid, False, "", best_name))
        gid += 1
    out.sort(key=lambda g: g["size"], reverse=True)

    # Depois, os grupos ja confirmados (nomeados).
    confirmed = [group(members, gid + i, True, name, name)
                 for i, (name, members) in enumerate(
                     sorted(named.items(), key=lambda kv: -len(kv[1])))]
    return out + confirmed


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Ferramentas de reconhecimento facial")
    sub = ap.add_subparsers(dest="cmd")
    en = sub.add_parser("enroll", help="treina o reconhecimento a partir dos nomes do painel")
    en.add_argument("--log", default="./historico_faces")
    en.add_argument("--known", default=None)
    args = ap.parse_args()

    if args.cmd == "enroll":
        res = enroll_from_labels(args.log, args.known)
        print(f"[i] Enrollment concluido: {res['total']} rosto(s) de "
              f"{len(res['people'])} pessoa(s).")
        for name, n in sorted(res["people"].items()):
            print(f"    - {name}: {n} amostra(s)")
        print(f"[i] Salvo em {res['path']}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
face_panel.py - Painel web para revisar rostos capturados e atribuir nomes.

Le o historico gravado por face_recog.HistoryLogger (historico_faces/) e serve
uma galeria no navegador. Cada rosto pode receber um nome; os nomes ficam em
historico_faces/labels.json (mapa arquivo -> nome). Agrupa por pessoa.

Sem dependencias externas (usa http.server da biblioteca padrao).

Uso:
    python3 face_panel.py                       # http://localhost:8080
    python3 face_panel.py --log ./historico_faces --port 8080 --host 0.0.0.0
"""

import argparse
import json
import mimetypes
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

_LOCK = threading.Lock()


def load_history(outdir):
    """Le historico.jsonl -> lista de registros (mais novos primeiro)."""
    path = Path(outdir) / "historico.jsonl"
    records = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return records


def load_labels(outdir):
    path = Path(outdir) / "labels.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_labels(outdir, labels):
    path = Path(outdir) / "labels.json"
    path.write_text(json.dumps(labels, indent=2, ensure_ascii=False), encoding="utf-8")


INDEX_HTML = """<!DOCTYPE html>
<html lang="pt-br"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel de Rostos</title>
<style>
  :root { --bg:#0f1216; --card:#1a1f27; --fg:#e7edf3; --mut:#8a97a6; --acc:#22c55e; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:16px 20px; border-bottom:1px solid #232a34; position:sticky; top:0; background:var(--bg); z-index:5; }
  h1 { margin:0 0 4px; font-size:18px; }
  .sub { color:var(--mut); font-size:13px; }
  .people { display:flex; gap:8px; flex-wrap:wrap; padding:12px 20px; }
  .chip { background:var(--card); border:1px solid #2a323d; padding:6px 12px; border-radius:20px; cursor:pointer; font-size:13px; }
  .chip.active { border-color:var(--acc); color:var(--acc); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:14px; padding:16px 20px; }
  .card { background:var(--card); border:1px solid #232a34; border-radius:10px; overflow:hidden; }
  .card img { width:100%; height:180px; object-fit:cover; background:#000; display:block; }
  .meta { padding:8px 10px; font-size:12px; color:var(--mut); }
  .meta b { color:var(--fg); }
  .row { display:flex; gap:6px; padding:0 10px 10px; }
  .row input { flex:1; background:#0f1319; border:1px solid #2a323d; color:var(--fg); border-radius:6px; padding:7px 8px; font-size:13px; }
  .row button { background:var(--acc); border:0; color:#04120a; font-weight:600; border-radius:6px; padding:0 12px; cursor:pointer; }
  .named { border-color:var(--acc); }
  .empty { text-align:center; color:var(--mut); padding:60px 20px; }
</style></head>
<body>
<header style="display:flex;align-items:center;justify-content:space-between;gap:12px">
  <div>
    <h1>Painel de Rostos</h1>
    <div class="sub" id="sub">carregando...</div>
  </div>
  <button id="enroll" style="background:var(--acc);border:0;color:#04120a;font-weight:600;border-radius:8px;padding:10px 16px;cursor:pointer">
    Atualizar reconhecimento
  </button>
</header>
<div class="people" id="people"></div>
<div class="grid" id="grid"></div>
<div class="empty" id="empty" style="display:none">Nenhum rosto capturado ainda. Rode o dashboard com <code>--face</code>.</div>
<script>
let FILTER = null;
let RECS = [], PEOPLE = {};
async function api(path, opts){ const r = await fetch(path, opts); return r.json(); }
function fmtTs(ts){ try { return new Date(ts).toLocaleString('pt-BR'); } catch(e){ return ts; } }

async function load(){
  const data = await api('/api/faces');
  RECS = data.records; PEOPLE = data.people;
  document.getElementById('sub').textContent =
    RECS.length + ' captura(s) | ' + Object.keys(PEOPLE).length + ' pessoa(s) nomeada(s)';
  renderChips();
  render();
}

function renderChips(){
  const pdiv = document.getElementById('people');
  pdiv.innerHTML = '';
  const mkChip = (label, key, count) => {
    const c = document.createElement('span');
    c.className = 'chip' + (FILTER===key ? ' active':'');
    c.textContent = label + (count!=null ? ' ('+count+')' : '');
    c.onclick = () => { FILTER = (FILTER===key? null : key); renderChips(); render(); };
    return c;
  };
  pdiv.appendChild(mkChip('Todos', null, RECS.length));
  pdiv.appendChild(mkChip('Sem nome', '__none__', RECS.filter(r=>!r.name).length));
  for(const [name,count] of Object.entries(PEOPLE)) pdiv.appendChild(mkChip(name, name, count));
}

function render(){
  const recs = RECS;
  const grid = document.getElementById('grid');
  const empty = document.getElementById('empty');
  let list = recs;
  if(FILTER==='__none__') list = recs.filter(r=>!r.name);
  else if(FILTER) list = recs.filter(r=>r.name===FILTER);
  grid.innerHTML='';
  empty.style.display = list.length? 'none':'block';
  for(const r of list){
    const card = document.createElement('div');
    card.className = 'card' + (r.name? ' named':'');
    card.innerHTML = `
      <img loading="lazy" src="/crop/${encodeURI(r.file)}" alt="rosto">
      <div class="meta"><b>${r.camera||''}</b><br>${fmtTs(r.ts)} · score ${r.score??''}</div>
      <div class="row">
        <input placeholder="nome..." value="${(r.name||'').replace(/"/g,'&quot;')}">
        <button>OK</button>
      </div>`;
    const input = card.querySelector('input');
    const save = async () => {
      await api('/api/label', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({file:r.file, name:input.value.trim()})});
      load();
    };
    card.querySelector('button').onclick = save;
    input.addEventListener('keydown', e=>{ if(e.key==='Enter') save(); });
    grid.appendChild(card);
  }
}
document.getElementById('enroll').onclick = async (e) => {
  const btn = e.target; const old = btn.textContent;
  btn.textContent = 'Treinando...'; btn.disabled = true;
  try {
    const r = await api('/api/enroll', {method:'POST'});
    if(r.ok) alert('Reconhecimento atualizado: ' + r.total + ' amostra(s) de ' +
      Object.keys(r.people||{}).length + ' pessoa(s).\\nO dashboard passa a identificar automaticamente.');
    else alert('Erro: ' + (r.error||'desconhecido'));
  } catch(err){ alert('Falha: ' + err); }
  btn.textContent = old; btn.disabled = false;
};

load();
setInterval(load, 15000);  // atualiza a galeria periodicamente
</script>
</body></html>
"""


def make_handler(outdir):
    outdir = Path(outdir).resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # silencia log de requisicao

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/" or route == "/index.html":
                return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            if route == "/api/faces":
                return self._api_faces()
            if route.startswith("/crop/"):
                return self._serve_crop(route[len("/crop/"):])
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/label":
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return self._send(400, {"error": "json invalido"})
                return self._api_label(data)
            if parsed.path == "/api/enroll":
                return self._api_enroll()
            return self._send(404, {"error": "not found"})

        def _api_enroll(self):
            """Treina o reconhecimento (known_faces.json) a partir dos nomes salvos."""
            try:
                import face_recog
            except Exception as exc:
                return self._send(500, {"error": f"face_recog indisponivel: {exc}"})
            try:
                with _LOCK:
                    res = face_recog.enroll_from_labels(outdir)
                return self._send(200, {"ok": True, **res})
            except Exception as exc:
                return self._send(500, {"error": str(exc)})

        def _api_faces(self):
            with _LOCK:
                records = load_history(outdir)
                labels = load_labels(outdir)
            people = {}
            for r in records:
                name = labels.get(r.get("file"))
                if name:
                    r["name"] = name
                    people[name] = people.get(name, 0) + 1
            people = dict(sorted(people.items(), key=lambda kv: (-kv[1], kv[0])))
            self._send(200, {"records": records, "people": people})

        def _api_label(self, data):
            file = data.get("file")
            name = (data.get("name") or "").strip()
            if not file:
                return self._send(400, {"error": "file obrigatorio"})
            with _LOCK:
                labels = load_labels(outdir)
                if name:
                    labels[file] = name
                else:
                    labels.pop(file, None)  # nome vazio = remover rotulo
                save_labels(outdir, labels)
            self._send(200, {"ok": True, "file": file, "name": name})

        def _serve_crop(self, rel):
            rel = unquote(rel)
            target = (outdir / rel).resolve()
            # Protecao contra path traversal: precisa estar dentro de outdir.
            if outdir not in target.parents and target != outdir:
                return self._send(403, {"error": "proibido"})
            if not target.is_file():
                return self._send(404, {"error": "sem imagem"})
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Painel web de rostos capturados")
    ap.add_argument("--log", default="./historico_faces",
                    help="pasta do historico (mesma do --face-log do dashboard)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="use 0.0.0.0 para acessar de outros dispositivos")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    outdir = Path(args.log)
    outdir.mkdir(parents=True, exist_ok=True)
    handler = make_handler(outdir)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    shown = args.host if args.host != "0.0.0.0" else "<ip-da-maquina>"
    print(f"[i] Painel de rostos em http://{shown}:{args.port}  (pasta: {outdir.resolve()})")
    print("[i] Ctrl+C para encerrar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] Encerrado.")
        server.shutdown()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
discover.py - Autodescoberta de cameras IP na rede local.

Estrategia (sem dependencias externas alem de opencv):
  1. ONVIF WS-Discovery (multicast UDP 239.255.255.250:3702) -> IPs de cameras.
  2. Varredura da sub-rede local na porta RTSP 554 (fallback / complemento).
  3. Para cada IP, tenta montar a URL RTSP a partir de templates de fabricante
     e valida capturando 1 frame. Testa uma lista de credenciais padrao e,
     opcionalmente, pergunta a senha ao instalador (modo hibrido).

Uso como biblioteca:
    from discover import discover_cameras, load_config, save_config
    cams = discover_cameras()  # lista de dicts {name, url, ip, vendor}

Uso direto (teste):
    python3 discover.py            # escaneia e imprime o que achou
    python3 discover.py --no-prompt
"""

import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
from base64 import b64encode
from pathlib import Path
from urllib.parse import quote, urlsplit

# --- Configuracao de descoberta -------------------------------------------

# Credenciais padrao de fabrica testadas automaticamente (modo hibrido).
# (usuario, senha) - a senha vazia cobre cameras "abertas".
DEFAULT_CREDS = [
    ("admin", "admin"),
    ("admin", ""),
    ("admin", "123456"),
    ("admin", "12345"),
    ("admin", "1234"),
    ("admin", "888888"),
    ("admin", "9999"),
    ("root", "root"),
    ("root", "admin"),
    ("user", "user"),
]

# Templates de caminho RTSP por fabricante. {ch} = canal, {st} = subtype/stream.
# A ordem importa: o primeiro que entregar frame vence.
RTSP_TEMPLATES = [
    # Intelbras / Dahua
    ("intelbras/dahua", "/cam/realmonitor?channel={ch}&subtype={st}"),
    # Hikvision
    ("hikvision", "/Streaming/Channels/{ch}0{st_hik}"),
    # Axis
    ("axis", "/axis-media/media.amp"),
    # Generico ONVIF (muitos firmwares)
    ("onvif", "/onvif1"),
    ("onvif", "/onvif2"),
    ("generico", "/live/ch0{st}"),
    ("generico", "/11"),
    ("generico", "/live"),
    ("generico", "/stream1"),
    ("generico", "/h264"),
    ("generico", "/"),
]

RTSP_PORT = 554
WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)

DEFAULT_CONFIG_PATH = Path(__file__).with_name("cameras.json")


# --- WS-Discovery (ONVIF) --------------------------------------------------

def _ws_probe_message():
    """Mensagem SOAP de Probe padrao do WS-Discovery para dispositivos ONVIF."""
    # MessageID unico e obrigatorio; usamos o relogio para variar.
    msg_id = f"urn:uuid:{int(time.time()*1000):032x}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        '<e:Header>'
        f'<w:MessageID>{msg_id}</w:MessageID>'
        '<w:To e:mustUnderstand="true">'
        'urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action e:mustUnderstand="true">'
        'http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
        '</e:Header>'
        '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>'
        '</d:Probe></e:Body></e:Envelope>'
    ).encode("utf-8")


def ws_discovery(timeout=3.0):
    """Envia Probe multicast e coleta IPs de dispositivos ONVIF que responderem.

    Retorna dict {ip: {"xaddrs": [...], "scopes": "..."}}.
    """
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        try:
            sock.bind(("", 0))
        except OSError:
            pass

        msg = _ws_probe_message()
        for _ in range(2):  # 2 envios: pacotes UDP podem se perder
            try:
                sock.sendto(msg, WS_DISCOVERY_ADDR)
            except OSError:
                break

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "ignore")
            ip = addr[0]
            xaddrs = re.findall(r"http://[^\s<]+", text)
            scopes_match = re.search(r"<[^>]*Scopes[^>]*>(.*?)</", text, re.S)
            # Prefere o IP anunciado no XAddr; senao usa o IP de origem.
            entry = found.setdefault(ip, {"xaddrs": [], "scopes": ""})
            entry["xaddrs"] = xaddrs
            if scopes_match:
                entry["scopes"] = scopes_match.group(1).strip()
            for x in xaddrs:
                host = urlsplit(x).hostname
                if host and host not in found:
                    found[host] = {"xaddrs": [x], "scopes": entry["scopes"]}
    finally:
        sock.close()
    return found


def vendor_from_scopes(scopes):
    """Extrai um palpite de fabricante dos scopes ONVIF."""
    if not scopes:
        return ""
    m = re.search(r"/name/([^\s]+)", scopes) or re.search(r"/hardware/([^\s]+)", scopes)
    return m.group(1).replace("%20", " ") if m else ""


# --- Varredura de sub-rede (fallback) -------------------------------------

def local_ipv4_networks():
    """Descobre as redes IPv4 locais (por interface) para varrer /24."""
    nets = []
    try:
        # Truque: conecta um socket UDP para descobrir o IP de saida.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        nets.append(ipaddress.ip_network(f"{local_ip}/24", strict=False))
    except OSError:
        pass
    return nets


def _port_open(ip, port, timeout=0.6):
    try:
        with socket.create_connection((str(ip), port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_subnet_rtsp(networks=None, port=RTSP_PORT, workers=64):
    """Escaneia a(s) sub-rede(s) /24 procurando hosts com a porta RTSP aberta."""
    networks = networks or local_ipv4_networks()
    hosts = []
    for net in networks:
        if net.num_addresses > 1024:  # seguranca: evita varrer redes enormes
            continue
        hosts.extend(str(h) for h in net.hosts())
    open_hosts = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_port_open, h, port): h for h in hosts}
        for fut in concurrent.futures.as_completed(futs):
            if fut.result():
                open_hosts.append(futs[fut])
    return sorted(open_hosts, key=lambda x: tuple(int(p) for p in x.split(".")))


# --- Resolucao/validacao de URL RTSP --------------------------------------

def _build_rtsp(ip, user, password, path, port=RTSP_PORT):
    cred = ""
    if user:
        cred = quote(user, safe="") + (":" + quote(password, safe="") if password else "") + "@"
    return f"rtsp://{cred}{ip}:{port}{path}"


def normalize_rtsp_url(url):
    """Limpa a URL informada manualmente e garante o esquema rtsp://.

    Nao reescreve URLs ja validas (so tira espacos e prefixo ausente); serve
    para o campo "adicionar camera" aceitar coisas como "192.168.0.10:554/..."
    ou uma URL com espacos coladas do teclado.
    """
    url = (url or "").strip()
    if not url:
        return url
    if "://" not in url:
        url = "rtsp://" + url
    return url


# --- Sondagem RTSP DESCRIBE (leve, le codigo de status) -------------------
# Evita o bloqueio por tentativas: 1 requisicao por senha para descobrir a
# credencial certa (401 = errada, 2xx/4xx-de-path = autenticou), depois busca
# o caminho valido. Muito mais barato que abrir a stream com opencv.

def _parse_www_authenticate(header):
    """Extrai esquema (Digest/Basic) e parametros do header WWW-Authenticate."""
    scheme = "Basic"
    params = {}
    if header.lower().startswith("digest"):
        scheme = "Digest"
    for k, v in re.findall(r'(\w+)="?([^",]+)"?', header):
        params[k.lower()] = v
    return scheme, params


def _digest_header(user, password, method, uri, params):
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"',
             f'uri="{uri}"', f'response="{resp}"']
    if "opaque" in params:
        parts.append(f'opaque="{params["opaque"]}"')
    return "Digest " + ", ".join(parts)


def rtsp_describe(ip, user, password, path, port=RTSP_PORT, timeout=4.0):
    """Faz um DESCRIBE autenticado. Retorna o codigo de status RTSP (int) ou -1.

    200 = ok (cred + caminho validos); 401 = credencial invalida;
    404/451/455 = autenticou mas caminho/errado; 403 = bloqueado/proibido.
    """
    uri = f"rtsp://{ip}:{port}{path}"

    def request(auth_header, cseq):
        lines = [f"DESCRIBE {uri} RTSP/1.0",
                 f"CSeq: {cseq}",
                 "User-Agent: cam-discover",
                 "Accept: application/sdp"]
        if auth_header:
            lines.append(f"Authorization: {auth_header}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request(None, 1))
            data = sock.recv(4096).decode("latin-1", "ignore")
            status = _status_code(data)
            if status != 401:
                return status
            # Precisa autenticar.
            m = re.search(r"WWW-Authenticate:\s*(.+)", data, re.I)
            if not m:
                return 401
            scheme, params = _parse_www_authenticate(m.group(1).strip())
            if scheme == "Digest":
                auth = _digest_header(user, password, "DESCRIBE", uri, params)
            else:
                token = b64encode(f"{user}:{password}".encode()).decode()
                auth = f"Basic {token}"
            sock.sendall(request(auth, 2))
            data2 = sock.recv(4096).decode("latin-1", "ignore")
            return _status_code(data2)
    except OSError:
        return -1


def _status_code(response):
    m = re.match(r"RTSP/1\.0\s+(\d{3})", response)
    return int(m.group(1)) if m else -1


def _validate_rtsp(url, open_timeout=6.0):
    """Tenta abrir a URL e capturar 1 frame. Retorna (ok, w, h)."""
    # Import tardio: discover pode ser usado sem opencv para so listar IPs.
    import cv2
    os.environ.setdefault(
        "OPENCV_FFMPEG_CAPTURE_OPTIONS",
        "rtsp_transport;tcp|stimeout;5000000",
    )
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return (False, 0, 0)
        deadline = time.time() + open_timeout
        while time.time() < deadline:
            ok, frame = cap.read()
            if ok and frame is not None:
                return (True, frame.shape[1], frame.shape[0])
        return (False, 0, 0)
    finally:
        cap.release()


def _expand_path(template, channel=1, subtype=0):
    return template.format(
        ch=channel, st=subtype,
        st_hik=(subtype + 1),  # Hikvision: 01=principal, 02=sub
    )


def _ordered_templates(templates, vendor_hint):
    ordered = list(templates)
    if vendor_hint:
        vh = vendor_hint.lower()
        ordered.sort(key=lambda t: 0 if t[0].split("/")[0] in vh or vh in t[0] else 1)
    return ordered


# Codigos que indicam "autenticou, mas o caminho pode nao ser este".
_AUTH_OK_CODES = {200, 400, 404, 451, 453, 455, 457, 500}


def _find_working_credential(ip, creds, ordered, channel, subtype):
    """Descobre qual credencial autentica no IP com 1 DESCRIBE por senha.

    Retorna (user, password) ou None. Para no primeiro 403 (bloqueio).
    """
    probe_path = _expand_path(ordered[0][1], channel, subtype)
    for user, password in creds:
        code = rtsp_describe(ip, user, password, probe_path)
        if code == 403:
            return None  # camera bloqueou o IP; nao insistir
        if code == 200 or code in _AUTH_OK_CODES:
            return (user, password)
        # 401 = senha errada -> proxima; -1 = sem resposta -> proxima
    return None


def _find_working_path(ip, user, password, ordered, channel, subtype):
    """Com a credencial certa, acha o caminho RTSP que responde 200."""
    for vendor, template in ordered:
        path = _expand_path(template, channel, subtype)
        code = rtsp_describe(ip, user, password, path)
        if code == 200:
            return vendor, path
        if code == 403:
            break
    return None, None


def resolve_camera(ip, creds, prompt=True, channel=1, subtype=0,
                   templates=RTSP_TEMPLATES, vendor_hint="", confirm_frame=True):
    """Descobre uma URL RTSP funcional para o IP usando sondagem RTSP leve.

    1) acha a credencial que autentica (1 DESCRIBE por senha);
    2) acha o caminho valido (200);
    3) confirma capturando 1 frame com opencv.
    Se as senhas padrao falharem e prompt=True, pergunta ao instalador.
    Retorna dict {ip, url, vendor, width, height, user} ou None.
    """
    ordered = _ordered_templates(templates, vendor_hint)

    def resolve_with(cred_list, is_default):
        cred = _find_working_credential(ip, cred_list, ordered, channel, subtype)
        if not cred:
            return None
        user, password = cred
        vendor, path = _find_working_path(ip, user, password, ordered, channel, subtype)
        if not path:
            return None
        url = _build_rtsp(ip, user, password, path)
        w = h = 0
        if confirm_frame:
            ok, w, h = _validate_rtsp(url)
            if not ok:
                return None
        return {"ip": ip, "url": url, "vendor": vendor,
                "width": w, "height": h, "user": user}

    result = resolve_with(creds, is_default=True)
    if result:
        return result

    if prompt and sys.stdin and sys.stdin.isatty():
        print(f"  [?] {ip}: senhas padrao falharam.")
        try:
            import getpass
            user = input(f"      Usuario para {ip} [admin] (enter=pular): ").strip()
            if user == "":
                return None  # pular esta camera
            password = getpass.getpass(f"      Senha para {ip}: ")
            result = resolve_with([(user, password)], is_default=False)
            if result:
                return result
            print(f"  [x] {ip}: credenciais nao autenticaram.")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
    return None


# --- Orquestrador ----------------------------------------------------------

def discover_cameras(prompt=True, ws_timeout=3.0, do_subnet_scan=True,
                     channel=1, subtype=0, creds=None, on_progress=None):
    """Descobre cameras na rede e retorna lista de dicts prontos p/ o dashboard.

    Cada item: {name, url, ip, vendor, width, height}.
    on_progress(msg): callback opcional para log de progresso.
    """
    def log(msg):
        if on_progress:
            on_progress(msg)
        else:
            print(msg)

    creds = creds or DEFAULT_CREDS

    log("[i] WS-Discovery (ONVIF)...")
    onvif_hosts = ws_discovery(timeout=ws_timeout)
    log(f"[i] ONVIF respondeu: {len(onvif_hosts)} dispositivo(s).")

    candidates = {}  # ip -> vendor_hint
    for ip, info in onvif_hosts.items():
        candidates[ip] = vendor_from_scopes(info.get("scopes", ""))

    if do_subnet_scan:
        log("[i] Varrendo sub-rede na porta 554...")
        for ip in scan_subnet_rtsp():
            candidates.setdefault(ip, "")
        log(f"[i] Total de candidatos: {len(candidates)}.")

    cameras = []
    for idx, (ip, vendor_hint) in enumerate(sorted(candidates.items()), start=1):
        log(f"[i] ({idx}/{len(candidates)}) Validando {ip}...")
        cam = resolve_camera(ip, creds, prompt=prompt, channel=channel,
                             subtype=subtype, vendor_hint=vendor_hint)
        if cam:
            cam["name"] = f"Cam {ip}"
            cameras.append(cam)
            log(f"    [+] {ip} OK ({cam['vendor']}, {cam['width']}x{cam['height']})")
        else:
            log(f"    [-] {ip} sem stream valido.")
    return cameras


# --- Persistencia ----------------------------------------------------------

def load_config(path=DEFAULT_CONFIG_PATH):
    """Carrega cameras salvas de cameras.json. Retorna lista (vazia se nao existe)."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("cameras", []) if isinstance(data, dict) else data
    except (json.JSONDecodeError, OSError):
        return []


def save_config(cameras, path=DEFAULT_CONFIG_PATH):
    """Grava a lista de cameras em cameras.json (merge por URL feito pelo chamador)."""
    path = Path(path)
    payload = {"cameras": cameras}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def merge_cameras(existing, discovered):
    """Une listas evitando duplicatas por IP (mantem a config existente)."""
    by_ip = {c.get("ip") or c.get("url"): c for c in existing}
    for cam in discovered:
        key = cam.get("ip") or cam.get("url")
        if key not in by_ip:
            by_ip[key] = cam
    return list(by_ip.values())


def main():
    ap = argparse.ArgumentParser(description="Autodescoberta de cameras IP")
    ap.add_argument("--no-prompt", action="store_true",
                    help="nao perguntar senha; so credenciais padrao")
    ap.add_argument("--no-scan", action="store_true",
                    help="nao varrer sub-rede (so WS-Discovery)")
    ap.add_argument("--ws-timeout", type=float, default=3.0)
    ap.add_argument("--save", action="store_true",
                    help="salvar/mesclar resultado em cameras.json")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = ap.parse_args()

    cams = discover_cameras(
        prompt=not args.no_prompt,
        ws_timeout=args.ws_timeout,
        do_subnet_scan=not args.no_scan,
    )

    print("\n=== Cameras encontradas ===")
    if not cams:
        print("(nenhuma)")
    for c in cams:
        print(f"  {c['name']}  [{c['vendor']} {c['width']}x{c['height']}]  {c['url']}")

    if args.save and cams:
        existing = load_config(args.config)
        merged = merge_cameras(existing, cams)
        p = save_config(merged, args.config)
        print(f"\n[i] Salvo em {p} ({len(merged)} camera(s)).")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
netscan.py - Descoberta de dispositivos na rede local ("Minha rede").

Sem dependencias externas. Estrategia (nao precisa de root):
  1. Ping sweep concorrente na /24 local para popular a tabela ARP do SO.
  2. Le a tabela ARP (ip neigh / /proc/net/arp) -> IP + MAC + estado.
  3. Resolve o nome (reverse DNS) e o fabricante pelo prefixo do MAC (OUI).

Uso como biblioteca:
    from netscan import scan_network
    devs = scan_network()   # [{ip, mac, vendor, hostname, state, ...}, ...]
"""

import concurrent.futures
import ipaddress
import re
import socket
import subprocess
import time
from pathlib import Path

import discover

# Prefixos OUI (primeiros 3 bytes do MAC) -> fabricante. Lista curta e pratica,
# focada no que costuma aparecer numa rede domestica/pequena. Um MAC nao listado
# fica sem fabricante (o campo vem vazio), sem quebrar nada.
OUI_VENDORS = {
    "80:85:44": "Intelbras", "80:8a:bd": "Intelbras", "3c:e0:64": "Intelbras",
    "e8:45:8b": "Intelbras",
    "e4:5f:01": "Raspberry Pi", "dc:a6:32": "Raspberry Pi", "b8:27:eb": "Raspberry Pi",
    "fc:3c:d7": "Apple", "3c:06:30": "Apple", "a4:83:e7": "Apple",
    "00:be:43": "Amazon", "38:a5:c9": "Amazon",
    "0c:dc:91": "Samsung", "e8:1c:ba": "Samsung",
    "d8:3a:dd": "Google", "1c:f2:9a": "Google",
    "00:17:88": "Philips Hue", "ec:fa:bc": "Tuya/Smart",
    "50:c7:bf": "TP-Link", "b0:be:76": "TP-Link", "c4:e9:0a": "D-Link",
}


def vendor_from_mac(mac):
    """Palpite de fabricante pelo prefixo OUI do MAC. '' se desconhecido."""
    if not mac:
        return ""
    return OUI_VENDORS.get(mac.lower()[:8], "")


def _ping(ip, timeout=1):
    """Um ping ICMP (best-effort) so para popular a tabela ARP."""
    try:
        subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), str(ip)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _ping_sweep(network, workers=128):
    hosts = [str(h) for h in network.hosts()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_ping, hosts))


# Estados ARP do melhor (dispositivo respondendo agora) ao pior.
_STATE_RANK = {"REACHABLE": 0, "STALE": 1, "DELAY": 2, "PROBE": 3,
               "PERMANENT": 1, "NOARP": 4, "INCOMPLETE": 8, "FAILED": 9}


def _read_arp():
    """Le a tabela ARP -> {ip: (mac, state)}. Tenta `ip neigh`, cai p/ /proc."""
    table = {}

    def consider(ip, mac, state):
        if not mac or mac in ("00:00:00:00:00:00", "<incomplete>"):
            return
        mac = mac.lower()
        prev = table.get(ip)
        if prev is None or _STATE_RANK.get(state, 5) < _STATE_RANK.get(prev[1], 5):
            table[ip] = (mac, state)

    try:
        out = subprocess.run(["ip", "neigh"], capture_output=True, text=True,
                             timeout=5).stdout
        for line in out.splitlines():
            m = re.match(r"(\d+\.\d+\.\d+\.\d+)\s+dev\s+\S+\s+lladdr\s+"
                         r"([0-9a-fA-F:]+)\s+(\w+)", line)
            if m:
                consider(m.group(1), m.group(2), m.group(3))
    except (OSError, subprocess.SubprocessError):
        pass

    if not table:  # fallback: tabela ARP do /proc (formato tabular)
        arp = Path("/proc/net/arp")
        if arp.exists():
            for line in arp.read_text().splitlines()[1:]:
                cols = line.split()
                if len(cols) >= 4 and cols[3] != "00:00:00:00:00:00":
                    consider(cols[0], cols[3], "STALE")
    return table


# --- Portas conhecidas (parte A): perfil de servicos por TCP connect ---------
# So portas comuns em rede domestica; TCP connect nao precisa de root.
KNOWN_PORTS = {
    22: "SSH", 23: "Telnet", 53: "DNS", 80: "HTTP", 443: "HTTPS",
    139: "SMB", 445: "SMB", 554: "RTSP", 631: "Impressora (IPP)",
    1883: "MQTT", 1900: "UPnP", 3389: "RDP", 5000: "HTTP-alt",
    5353: "mDNS", 8000: "HTTP-alt", 8080: "HTTP-alt", 8443: "HTTPS-alt",
    8554: "RTSP-alt", 9000: "HTTP-alt", 32400: "Plex", 62078: "iPhone-sync",
}


def _probe_port(ip, port, timeout=0.4):
    """True se a porta TCP aceita conexao (servico ouvindo)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def _scan_ports(ip, ports=None, timeout=0.4, workers=24):
    """Retorna a lista ordenada de servicos abertos num IP (ex.: ['HTTP','RTSP'])."""
    ports = ports or list(KNOWN_PORTS)
    open_ports = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_probe_port, ip, p, timeout): p for p in ports}
        for fut in concurrent.futures.as_completed(futs):
            if fut.result():
                open_ports.append(futs[fut])
    # Nomes unicos, preservando a ordem numerica das portas.
    seen, services = set(), []
    for p in sorted(open_ports):
        name = KNOWN_PORTS.get(p, str(p))
        if name not in seen:
            seen.add(name)
            services.append(name)
    return services


def _reverse_dns(ip, timeout=0.5):
    socket.setdefaulttimeout(timeout)
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        return ""
    finally:
        socket.setdefaulttimeout(None)


# --- Anuncios mDNS / SSDP (parte B): nomes e servicos que o aparelho divulga --
# Servicos mDNS comuns -> rotulo amigavel.
_MDNS_SERVICES = {
    "_googlecast": "Chromecast", "_airplay": "AirPlay", "_raop": "AirPlay",
    "_spotify-connect": "Spotify", "_printer": "Impressora", "_ipp": "Impressora",
    "_http": "Web", "_https": "Web", "_ssh": "SSH", "_smb": "SMB",
    "_hap": "HomeKit", "_homekit": "HomeKit", "_rtsp": "RTSP",
    "_amzn-wplay": "Amazon", "_daap": "iTunes", "_workstation": "PC",
}


def _parse_mdns_name(data):
    """Extrai rotulos de servico legiveis de um pacote mDNS cru (heuristico)."""
    try:
        text = data.decode("latin-1", "ignore")
    except Exception:
        return set()
    found = set()
    for key, label in _MDNS_SERVICES.items():
        if key in text:
            found.add(label)
    return found


def _sniff_advertisements(duration=2.0):
    """Escuta passiva de mDNS(5353)/SSDP(1900) -> {ip: {"services": set, "name": str}}.

    Envia um M-SEARCH SSDP (multicast, nao mira dispositivo nenhum em especifico)
    e coleta as respostas + anuncios espontaneos por 'duration' segundos. Best-
    effort: se um socket nao puder ser aberto, apenas ignora aquela fonte.
    """
    info = {}

    def note(ip, service=None, name=None):
        rec = info.setdefault(ip, {"services": set(), "name": ""})
        if service:
            rec["services"].add(service)
        if name and not rec["name"]:
            rec["name"] = name

    socks = []
    # SSDP: manda um M-SEARCH e escuta as respostas unicast.
    try:
        ssdp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        ssdp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ssdp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        ssdp.settimeout(0.5)
        msearch = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 1\r\n"
            "ST: ssdp:all\r\n\r\n"
        ).encode()
        ssdp.sendto(msearch, ("239.255.255.250", 1900))
        socks.append(("ssdp", ssdp))
    except OSError:
        pass

    # mDNS: junta-se ao grupo multicast 224.0.0.251:5353 e escuta anuncios.
    try:
        mdns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        mdns.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            mdns.bind(("", 5353))
            mreq = socket.inet_aton("224.0.0.251") + socket.inet_aton("0.0.0.0")
            mdns.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            mdns.bind(("", 0))  # sem 5353 (em uso): ainda pega respostas unicast
        mdns.settimeout(0.5)
        socks.append(("mdns", mdns))
    except OSError:
        pass

    deadline = time.time() + duration
    while time.time() < deadline and socks:
        for kind, sk in socks:
            try:
                data, addr = sk.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            ip = addr[0]
            if kind == "ssdp":
                text = data.decode("latin-1", "ignore")
                m = re.search(r"SERVER:\s*(.+)", text, re.I)
                note(ip, service="UPnP", name=(m.group(1).strip() if m else None))
            else:
                for svc in _parse_mdns_name(data):
                    note(ip, service=svc)
    for _, sk in socks:
        try:
            sk.close()
        except OSError:
            pass
    return info


def _known_camera_ips(config_path="cameras.json"):
    """IPs (e hosts) das cameras ja configuradas, para marcar na lista."""
    ips = set()
    for cam in discover.load_config(config_path):
        if cam.get("ip"):
            ips.add(cam["ip"])
        host = discover._host_of(cam["url"]) if hasattr(discover, "_host_of") else None
        m = re.search(r"@([^:/]+)", cam.get("url", ""))
        if m:
            ips.add(m.group(1))
    return ips


def scan_network(config_path="cameras.json", do_ping=True, resolve_names=True,
                 networks=None, scan_ports=True, sniff_adverts=True):
    """Descobre dispositivos na(s) rede(s) local(is).

    Retorna lista de dicts ordenada por IP:
        {ip, mac, vendor, hostname, state, is_camera, is_gateway, is_self,
         services, advert}
    'services' = servicos/portas abertas (parte A); 'advert' = nome amigavel
    que o aparelho anuncia via mDNS/SSDP (parte B).
    """
    networks = networks or discover.local_ipv4_networks()
    if do_ping:
        for net in networks:
            if net.num_addresses <= 1024:  # evita varrer redes enormes
                _ping_sweep(net)

    # Escuta anuncios (mDNS/SSDP) enquanto ainda nem lemos a ARP: passivo e rapido.
    adverts = _sniff_advertisements() if sniff_adverts else {}

    table = _read_arp()

    # IPs locais desta maquina e o gateway, para rotular.
    self_ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        self_ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass

    cam_ips = _known_camera_ips(config_path)
    net_objs = [ipaddress.ip_network(n, strict=False) for n in networks]

    def in_scope(ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in net_objs)

    in_scope_ips = [ip for ip in table if not net_objs or in_scope(ip)]

    # Parte A: escaneia as portas conhecidas de cada dispositivo, em paralelo.
    port_map = {}
    if scan_ports and in_scope_ips:
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
            futs = {ex.submit(_scan_ports, ip): ip for ip in in_scope_ips}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    port_map[futs[fut]] = fut.result()
                except Exception:
                    port_map[futs[fut]] = []

    devices = []
    for ip in in_scope_ips:
        mac, state = table[ip]
        host = _reverse_dns(ip) if resolve_names else ""
        adv = adverts.get(ip, {})
        # Junta servicos das portas (A) com os anunciados via mDNS/SSDP (B).
        services = list(dict.fromkeys(
            list(port_map.get(ip, [])) + sorted(adv.get("services", set()))
        ))
        devices.append({
            "ip": ip,
            "mac": mac,
            "vendor": vendor_from_mac(mac),
            "hostname": "" if host == "_gateway" else host,
            "state": state,
            "is_camera": ip in cam_ips,
            "is_gateway": host == "_gateway",
            "is_self": ip in self_ips,
            "services": services,
            "advert": adv.get("name", ""),
        })

    # Inclui esta maquina mesmo que nao apareca na ARP (nao pingamos a nos mesmos).
    for sip in self_ips:
        if in_scope(sip) and not any(d["ip"] == sip for d in devices):
            devices.append({
                "ip": sip, "mac": "", "vendor": "", "hostname": socket.gethostname(),
                "state": "REACHABLE", "is_camera": False, "is_gateway": False,
                "is_self": True, "services": [], "advert": "",
            })

    devices.sort(key=lambda d: tuple(int(p) for p in d["ip"].split(".")))
    return devices


def main():
    t = time.time()
    devs = scan_network()
    print(f"=== {len(devs)} dispositivo(s) em {time.time()-t:.1f}s ===")
    for d in devs:
        tags = []
        if d["is_gateway"]:
            tags.append("roteador")
        if d["is_camera"]:
            tags.append("camera")
        if d["is_self"]:
            tags.append("este pc")
        tag = f" [{', '.join(tags)}]" if tags else ""
        label = d["hostname"] or d["advert"] or d["vendor"] or "-"
        svc = f"  {{{', '.join(d['services'])}}}" if d.get("services") else ""
        print(f"  {d['ip']:15} {d['mac'] or '--':17} {label}{tag}{svc}")


if __name__ == "__main__":
    main()

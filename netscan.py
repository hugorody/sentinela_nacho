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


def _reverse_dns(ip, timeout=0.5):
    socket.setdefaulttimeout(timeout)
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        return ""
    finally:
        socket.setdefaulttimeout(None)


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
                 networks=None):
    """Descobre dispositivos na(s) rede(s) local(is).

    Retorna lista de dicts ordenada por IP:
        {ip, mac, vendor, hostname, state, is_camera, is_gateway, is_self}
    """
    networks = networks or discover.local_ipv4_networks()
    if do_ping:
        for net in networks:
            if net.num_addresses <= 1024:  # evita varrer redes enormes
                _ping_sweep(net)

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

    devices = []
    for ip, (mac, state) in table.items():
        if net_objs and not in_scope(ip):
            continue
        host = _reverse_dns(ip) if resolve_names else ""
        devices.append({
            "ip": ip,
            "mac": mac,
            "vendor": vendor_from_mac(mac),
            "hostname": "" if host == "_gateway" else host,
            "state": state,
            "is_camera": ip in cam_ips,
            "is_gateway": host == "_gateway",
            "is_self": ip in self_ips,
        })

    # Inclui esta maquina mesmo que nao apareca na ARP (nao pingamos a nos mesmos).
    for sip in self_ips:
        if in_scope(sip) and not any(d["ip"] == sip for d in devices):
            devices.append({
                "ip": sip, "mac": "", "vendor": "", "hostname": socket.gethostname(),
                "state": "REACHABLE", "is_camera": False, "is_gateway": False,
                "is_self": True,
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
        label = d["hostname"] or d["vendor"] or "-"
        print(f"  {d['ip']:15} {d['mac'] or '--':17} {label}{tag}")


if __name__ == "__main__":
    main()

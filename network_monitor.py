#!/usr/bin/env python3
"""Inventario leve e historico de presenca da rede local.

O monitor executa apenas ping/ARP no ciclo automatico. Descoberta de servicos,
mDNS e SSDP fica reservada para a analise completa solicitada pelo usuario.
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import netscan


class NetworkMonitor:
    def __init__(self, db_path="network_inventory.db", config_path="cameras.json",
                 interval=300, offline_after=3):
        self.db_path = Path(db_path)
        self.config_path = config_path
        self.interval = max(30, int(interval))
        self.offline_after = max(1, int(offline_after))
        self._stop = threading.Event()
        self._scan_lock = threading.Lock()
        self._thread = None
        self._init_db()

    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    mac TEXT PRIMARY KEY,
                    ip TEXT NOT NULL DEFAULT '',
                    custom_name TEXT NOT NULL DEFAULT '',
                    hostname TEXT NOT NULL DEFAULT '',
                    vendor TEXT NOT NULL DEFAULT '',
                    advert TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    services TEXT NOT NULL DEFAULT '[]',
                    is_camera INTEGER NOT NULL DEFAULT 0,
                    is_gateway INTEGER NOT NULL DEFAULT 0,
                    is_self INTEGER NOT NULL DEFAULT 0,
                    known INTEGER NOT NULL DEFAULT 0,
                    online INTEGER NOT NULL DEFAULT 1,
                    missed_scans INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS network_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    mac TEXT NOT NULL,
                    ip TEXT NOT NULL DEFAULT '',
                    event TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_network_events_ts
                    ON network_events(ts DESC);
            """)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="network-monitor")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        # Faz a primeira leitura logo ao subir; depois trabalha em baixa frequencia.
        while not self._stop.is_set():
            try:
                self.scan(full=False)
            except Exception as exc:
                print(f"[rede] falha na varredura automatica: {exc}")
            if self._stop.wait(self.interval):
                break

    @staticmethod
    def _identity(device):
        # A propria maquina pode nao aparecer na ARP e, portanto, nao ter MAC.
        return (device.get("mac") or f"ip:{device.get('ip', '')}").lower()

    @staticmethod
    def _label(device):
        return (device.get("custom_name") or device.get("hostname") or
                device.get("advert") or device.get("vendor") or
                device.get("ip") or device.get("mac"))

    def scan(self, full=False):
        """Atualiza o inventario. Chamadas concorrentes compartilham um scan."""
        with self._scan_lock:
            found = netscan.scan_network(
                config_path=self.config_path,
                resolve_names=bool(full),
                scan_ports=bool(full),
                sniff_adverts=bool(full),
            )
            now = datetime.now().isoformat(timespec="seconds")
            seen = set()
            with self._connect() as db:
                had_inventory = db.execute("SELECT 1 FROM devices LIMIT 1").fetchone() is not None
                for dev in found:
                    mac = self._identity(dev)
                    if not mac:
                        continue
                    seen.add(mac)
                    old = db.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
                    services = json.dumps(dev.get("services") or [], ensure_ascii=False)
                    # O scan rapido nao apaga detalhes obtidos numa analise completa.
                    hostname = dev.get("hostname") or (old["hostname"] if old else "")
                    vendor = dev.get("vendor") or (old["vendor"] if old else "")
                    advert = dev.get("advert") or (old["advert"] if old else "")
                    if not full and old:
                        services = old["services"]
                    if old:
                        was_online = bool(old["online"])
                        db.execute("""UPDATE devices SET ip=?, hostname=?, vendor=?, advert=?,
                            state=?, services=?, is_camera=?, is_gateway=?, is_self=?, online=1,
                            missed_scans=0, last_seen=?, updated_at=? WHERE mac=?""",
                            (dev.get("ip", ""), hostname, vendor, advert,
                             dev.get("state", ""), services, int(bool(dev.get("is_camera"))),
                             int(bool(dev.get("is_gateway"))), int(bool(dev.get("is_self"))),
                             now, now, mac))
                        if not was_online:
                            self._event(db, now, mac, dev.get("ip", ""), "online",
                                        self._label({**dict(old), **dev}))
                    else:
                        db.execute("""INSERT INTO devices
                            (mac, ip, hostname, vendor, advert, state, services, is_camera,
                             is_gateway, is_self, first_seen, last_seen, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (mac, dev.get("ip", ""), hostname, vendor, advert,
                             dev.get("state", ""), services, int(bool(dev.get("is_camera"))),
                             int(bool(dev.get("is_gateway"))), int(bool(dev.get("is_self"))),
                             now, now, now))
                        # A primeira execucao cria a base sem gerar uma tempestade de alertas.
                        if had_inventory:
                            self._event(db, now, mac, dev.get("ip", ""), "new",
                                        self._label(dev))

                rows = db.execute("SELECT * FROM devices WHERE online=1").fetchall()
                for row in rows:
                    if row["mac"] in seen:
                        continue
                    misses = row["missed_scans"] + 1
                    online = misses < self.offline_after
                    db.execute("UPDATE devices SET missed_scans=?, online=?, updated_at=? WHERE mac=?",
                               (misses, int(online), now, row["mac"]))
                    if not online:
                        self._event(db, now, row["mac"], row["ip"], "offline",
                                    self._label(dict(row)))
            return self.snapshot()

    @staticmethod
    def _event(db, ts, mac, ip, event, label):
        db.execute("INSERT INTO network_events(ts,mac,ip,event,label) VALUES(?,?,?,?,?)",
                   (ts, mac, ip, event, label or ""))

    def snapshot(self):
        with self._connect() as db:
            rows = db.execute("""SELECT * FROM devices
                ORDER BY online DESC, is_gateway DESC, ip""").fetchall()
        devices = []
        for row in rows:
            d = dict(row)
            try:
                d["services"] = json.loads(d["services"])
            except (TypeError, ValueError):
                d["services"] = []
            for key in ("is_camera", "is_gateway", "is_self", "known", "online"):
                d[key] = bool(d[key])
            devices.append(d)
        return devices

    def events(self, limit=50):
        limit = max(1, min(int(limit), 200))
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM network_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def update_device(self, mac, name=None, known=None):
        fields, values = [], []
        if name is not None:
            fields.append("custom_name=?")
            values.append(str(name).strip()[:100])
        if known is not None:
            fields.append("known=?")
            values.append(int(bool(known)))
        if not mac or not fields:
            return False
        values.append(mac.lower())
        with self._connect() as db:
            cur = db.execute(f"UPDATE devices SET {', '.join(fields)} WHERE mac=?", values)
            return cur.rowcount > 0


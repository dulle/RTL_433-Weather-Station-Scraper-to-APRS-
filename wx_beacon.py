#!/usr/bin/env python3
"""
wx_beacon.py — RTL-SDR Weather Station Logger & APRS Beacon Writer
====================================================================
Reads live JSON output from rtl_433, logs every raw packet to a rolling
log file, and periodically writes an APRS-formatted weather string to a
flat file that can be picked up by Direwolf, YAAC, aprx, or any APRS
client that supports file injection / beacon scripts.

Usage:
    python3 wx_beacon.py                          # uses wx_station.json
    python3 wx_beacon.py --config my_config.json
    python3 wx_beacon.py --help

Requires:
    pip install rich          (optional, for coloured console output)
    rtl_433 in PATH
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Optional rich console
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    RICH = True
    _console = Console()
except ImportError:
    RICH = False
    _console = None

CONFIG_PATH_DEFAULT = Path("wx_station.json")

# ---------------------------------------------------------------------------
# APRS-IS TCP sender
# ---------------------------------------------------------------------------

class APRSISSender:
    """
    Persistent TCP connection to an APRS-IS Tier-2 server.
    Handles login, periodic keepalive comments, and auto-reconnect.
    """

    def __init__(self, cfg: dict, logger: logging.Logger) -> None:
        self.host      = cfg["host"]
        self.port      = int(cfg["port"])
        self.callsign  = cfg["callsign"]
        self.passcode  = str(cfg.get("passcode", "-1"))
        self.srv_filter = cfg.get("filter", "")
        self.retry_s   = int(cfg.get("retry_interval_seconds", 30))
        self.keepalive = int(cfg.get("keepalive_seconds", 60))
        self.log       = logger
        self._sock: Optional[socket.socket] = None
        self._lock      = threading.Lock()
        self._connected = False
        self._stop      = threading.Event()
        self._last_tx   = 0.0

    def _connect(self) -> bool:
        try:
            s = socket.create_connection((self.host, self.port), timeout=15)
            s.settimeout(30)
            banner = s.recv(512).decode("ascii", errors="replace").strip()
            self.log.debug(f"APRS-IS banner: {banner}")
            login = (f"user {self.callsign} pass {self.passcode} vers wx_beacon 1.0"
                     + (f" filter {self.srv_filter}" if self.srv_filter else ""))
            s.sendall((login + "\r\n").encode())
            resp = s.recv(512).decode("ascii", errors="replace").strip()
            self.log.debug(f"APRS-IS login response: {resp}")
            if "unverified" in resp.lower() and self.passcode != "-1":
                self.log.warning("APRS-IS: logged in UNVERIFIED — check passcode")
            self._sock      = s
            self._connected = True
            self.log.info(f"APRS-IS connected → {self.host}:{self.port} [{self.callsign}]")
            return True
        except Exception as e:
            self.log.warning(f"APRS-IS connect failed: {e}")
            self._connected = False
            return False

    def _disconnect(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock      = None
        self._connected = False

    def start(self) -> None:
        t = threading.Thread(target=self._ka_loop, daemon=True, name="aprsis-ka")
        t.start()

    def _ka_loop(self) -> None:
        while not self._stop.is_set():
            if not self._connected:
                self._connect()
                if not self._connected:
                    self._stop.wait(self.retry_s)
                    continue
            if time.monotonic() - self._last_tx > self.keepalive:
                self._raw_send("# keepalive\r\n")
            self._stop.wait(10)

    def _raw_send(self, data: str) -> bool:
        with self._lock:
            if not self._connected or self._sock is None:
                return False
            try:
                self._sock.sendall(data.encode("ascii", errors="replace"))
                self._last_tx = time.monotonic()
                return True
            except Exception as e:
                self.log.warning(f"APRS-IS send error: {e} — will reconnect")
                self._disconnect()
                return False

    def send_packet(self, packet: str) -> bool:
        if not self._connected:
            self.log.warning("APRS-IS not connected — packet dropped")
            return False
        ok = self._raw_send(packet.rstrip() + "\r\n")
        if ok:
            self.log.info(f"APRS-IS TX → {packet}")
        return ok

    def stop(self) -> None:
        self._stop.set()
        self._disconnect()

    @property
    def connected(self) -> bool:
        return self._connected


# ---------------------------------------------------------------------------
# Network KISS / AX.25 RF sender
# ---------------------------------------------------------------------------

class KISSFrame:
    """Minimal AX.25 UI framer with KISS byte-stuffing."""

    FEND  = 0xC0
    FESC  = 0xDB
    TFEND = 0xDC
    TFESC = 0xDD

    @staticmethod
    def _encode_addr(callsign: str, ssid: int, last: bool) -> bytes:
        call = callsign.upper().split("-")[0].ljust(6)[:6]
        enc  = bytes([ord(c) << 1 for c in call])
        ssid_byte = ((ssid & 0x0F) << 1) | (0x01 if last else 0x00)
        return enc + bytes([ssid_byte])

    @staticmethod
    def _parse_call(full: str) -> tuple[str, int]:
        parts = full.upper().split("-")
        return parts[0], int(parts[1]) if len(parts) > 1 else 0

    @classmethod
    def build_ax25(cls, src: str, dst: str, via: str, info: str) -> bytes:
        """Build a raw AX.25 UI frame."""
        src_c, src_s = cls._parse_call(src)
        dst_c, dst_s = cls._parse_call(dst)
        digi_parts   = [p.strip() for p in via.split(",") if p.strip()]
        digi_addrs: list[bytes] = []
        for i, d in enumerate(digi_parts):
            dc, ds = cls._parse_call(d)
            digi_addrs.append(cls._encode_addr(dc, ds, last=(i == len(digi_parts) - 1)))
        if digi_addrs:
            address = (cls._encode_addr(dst_c, dst_s, last=False)
                       + cls._encode_addr(src_c, src_s, last=False)
                       + b"".join(digi_addrs))
        else:
            address = (cls._encode_addr(dst_c, dst_s, last=False)
                       + cls._encode_addr(src_c, src_s, last=True))
        return address + bytes([0x03, 0xF0]) + info.encode("ascii", errors="replace")

    @classmethod
    def kiss_wrap(cls, frame: bytes, tnc_port: int = 0) -> bytes:
        cmd     = (tnc_port & 0x0F) << 4
        escaped: list[int] = []
        for b in frame:
            if b == cls.FEND:
                escaped += [cls.FESC, cls.TFEND]
            elif b == cls.FESC:
                escaped += [cls.FESC, cls.TFESC]
            else:
                escaped.append(b)
        return bytes([cls.FEND, cmd]) + bytes(escaped) + bytes([cls.FEND])


class NetworkKISSSender:
    """
    Sends AX.25 APRS frames via a KISS-over-TCP endpoint.
    Compatible with Direwolf, soundmodem, and hardware TNCs
    that expose a KISS TCP interface.
    """

    def __init__(self, cfg: dict, logger: logging.Logger) -> None:
        kiss          = cfg["kiss"]
        self.host     = kiss["host"]
        self.port     = int(kiss["port"])
        self.tnc_port = int(kiss.get("tnc_port", 0))
        self.callsign = cfg["callsign"]
        self.digi_path= cfg["digi_path"]
        self.log      = logger
        self._sock: Optional[socket.socket] = None
        self._lock      = threading.Lock()
        self._connected = False
        self._stop      = threading.Event()

    def start(self) -> None:
        t = threading.Thread(target=self._conn_loop, daemon=True, name="kiss-conn")
        t.start()

    def _conn_loop(self) -> None:
        while not self._stop.is_set():
            if not self._connected:
                self._connect()
                if not self._connected:
                    self._stop.wait(15)
            else:
                self._stop.wait(30)

    def _connect(self) -> bool:
        try:
            s = socket.create_connection((self.host, self.port), timeout=10)
            s.settimeout(5)
            self._sock      = s
            self._connected = True
            self.log.info(
                f"Network KISS connected → {self.host}:{self.port} "
                f"(TNC port {self.tnc_port})"
            )
            return True
        except Exception as e:
            self.log.warning(f"Network KISS connect failed ({self.host}:{self.port}): {e}")
            self._connected = False
            return False

    def _disconnect(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock      = None
        self._connected = False

    def send_packet(self, aprs_packet: str) -> bool:
        if not self._connected:
            self.log.warning("Network KISS not connected — RF packet dropped")
            return False
        try:
            info  = aprs_packet.split(":", 1)[1]
        except IndexError:
            info  = aprs_packet
        ax25  = KISSFrame.build_ax25(
            src  = self.callsign,
            dst  = "APRS",
            via  = self.digi_path,
            info = info,
        )
        frame = KISSFrame.kiss_wrap(ax25, tnc_port=self.tnc_port)
        with self._lock:
            try:
                if self._sock is None:
                    return False
                self._sock.sendall(frame)
                self.log.info(
                    f"RF TX [KISS {self.host}:{self.port} "
                    f"path={self.digi_path}] → {aprs_packet}"
                )
                return True
            except Exception as e:
                self.log.warning(f"RF TX error: {e} — reconnecting")
                self._disconnect()
                return False

    def stop(self) -> None:
        self._stop.set()
        self._disconnect()

    @property
    def connected(self) -> bool:
        return self._connected

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(level_name: str = "INFO") -> logging.Logger:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logger = logging.getLogger("wx_beacon")
    logger.setLevel(level)

    if RICH:
        handler: logging.Handler = RichHandler(
            console=_console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
        )
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")
        )
    logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Run  python3 wx_config.py  to create one."
        )
    with path.open() as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# APRS coordinate conversion helpers
# ---------------------------------------------------------------------------

def _dd_to_aprs_lat(dd: float) -> str:
    """Convert decimal-degree latitude to APRS ddmm.mm[NS]."""
    hemi = "N" if dd >= 0 else "S"
    dd = abs(dd)
    deg = int(dd)
    mins = (dd - deg) * 60
    return f"{deg:02d}{mins:05.2f}{hemi}"


def _dd_to_aprs_lon(dd: float) -> str:
    """Convert decimal-degree longitude to APRS dddmm.mm[EW]."""
    hemi = "E" if dd >= 0 else "W"
    dd = abs(dd)
    deg = int(dd)
    mins = (dd - deg) * 60
    return f"{deg:03d}{mins:05.2f}{hemi}"


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def _mps_to_mph(mps: float) -> float:
    return mps * 2.23694


def _kmh_to_mph(kmh: float) -> float:
    return kmh * 0.621371


def _hpa_to_tenths_mbar(hpa: float) -> int:
    """APRS pressure is in tenths of millibars (== tenths of hPa)."""
    return int(round(hpa * 10))


def _mm_to_hundredths_inch(mm: float) -> int:
    """APRS rain fields are hundredths of an inch."""
    return int(round(mm / 25.4 * 100))


# ---------------------------------------------------------------------------
# APRS packet builder
# ---------------------------------------------------------------------------

class APRSBuilder:
    """
    Constructs APRS WX packets from aggregated sensor data.

    APRS WX format (position + weather):
        CALL>APRS,TCPIP*:@DDHHMMz/LLLLL.LLN\\LLLLL.LLE_CCC/SSSgGGGtTTTrRRRpPPPPPbBBBBBhHHcomment

    Fields used:
        _ = wind direction (degrees, 3 digits)
        / = wind speed sustained (mph, 3 digits)
        g = wind gust (mph, 3 digits)
        t = temperature (°F, 3 digits, can be negative with leading -)
        r = rainfall last 60 min (hundredths inch)
        p = rainfall last 24 hr (hundredths inch)
        P = rainfall since midnight (hundredths inch)
        b = barometric pressure (tenths of millibar)
        h = humidity (% 00–99, 00 = 100%)
    """

    def __init__(self, callsign: str, lat: float, lon: float, comment: str):
        self.callsign = callsign
        self.lat = lat
        self.lon = lon
        self.comment = comment

    def build(self, wx: dict, path: str = "TCPIP*") -> str:
        now = datetime.now(timezone.utc)
        ts = now.strftime("%d%H%Mz")

        lat_s = _dd_to_aprs_lat(self.lat)
        lon_s = _dd_to_aprs_lon(self.lon)

        # Wind direction
        wind_dir = wx.get("wind_dir_deg")
        wd = f"{int(wind_dir):03d}" if wind_dir is not None else "..."

        # Wind speed (sustained, mph)
        wspd = wx.get("wind_speed_mph")
        ws = f"{int(wspd):03d}" if wspd is not None else "..."

        # Wind gust (mph)
        wgust = wx.get("wind_gust_mph")
        wg = f"{int(wgust):03d}" if wgust is not None else "..."

        # Temperature (°F)
        temp_f = wx.get("temperature_F")
        if temp_f is not None:
            tf = f"{int(temp_f):03d}" if temp_f >= 0 else f"{int(temp_f):04d}"
        else:
            tf = "..."

        # Rain last 60 min
        r60 = wx.get("rain_60min_mm")
        rain_hr = f"{_mm_to_hundredths_inch(r60):03d}" if r60 is not None else "..."

        # Rain last 24 hr
        r24 = wx.get("rain_24h_mm")
        rain_24 = f"{_mm_to_hundredths_inch(r24):03d}" if r24 is not None else "..."

        # Rain since midnight
        r0 = wx.get("rain_midnight_mm")
        rain_mn = f"{_mm_to_hundredths_inch(r0):03d}" if r0 is not None else "..."

        # Pressure (tenths mbar)
        press = wx.get("pressure_hpa")
        baro = f"{_hpa_to_tenths_mbar(press):05d}" if press is not None else "....."

        # Humidity (00 = 100 %)
        hum = wx.get("humidity_pct")
        if hum is not None:
            h_val = 0 if hum >= 100 else int(hum)
            hum_s = f"{h_val:02d}"
        else:
            hum_s = ".."

        position = f"{lat_s}/{lon_s}"
        wx_fields = (
            f"_{wd}/{ws}"
            f"g{wg}"
            f"t{tf}"
            f"r{rain_hr}"
            f"p{rain_24}"
            f"P{rain_mn}"
            f"b{baro}"
            f"h{hum_s}"
        )
        header = f"{self.callsign}>APRS,{path}:@{ts}"
        packet = f"{header}{position}_{wx_fields}{self.comment}"
        return packet


# ---------------------------------------------------------------------------
# Weather data aggregator
# ---------------------------------------------------------------------------

class WXAggregator:
    """
    Aggregates multiple sensor readings (temperature, humidity, wind, rain,
    pressure) from one or more rtl_433 devices into a single wx dict.
    """

    _FIELD_MAP = {
        "temperature_C":        ("temperature_F",          "C_to_F"),
        "temperature_F":        ("temperature_F",          "direct"),
        "temp_C":               ("temperature_F",          "C_to_F"),
        "temp_F":               ("temperature_F",          "direct"),
        "humidity":             ("humidity_pct",            "direct"),
        "wind_dir_deg":         ("wind_dir_deg",            "direct"),
        "wind_avg_m_s":         ("wind_speed_mph",          "mps_to_mph"),
        "wind_avg_km_h":        ("wind_speed_mph",          "kmh_to_mph"),
        "wind_avg_mi_h":        ("wind_speed_mph",          "direct"),
        "wind_speed_m_s":       ("wind_speed_mph",          "mps_to_mph"),
        "wind_speed_km_h":      ("wind_speed_mph",          "kmh_to_mph"),
        "wind_max_m_s":         ("wind_gust_mph",           "mps_to_mph"),
        "wind_max_km_h":        ("wind_gust_mph",           "kmh_to_mph"),
        "gust_speed_m_s":       ("wind_gust_mph",           "mps_to_mph"),
        "rain_mm":              ("rain_60min_mm",            "direct"),
        "rain_rate_mm_h":       ("rain_60min_mm",            "direct"),
        "rain_total_mm":        ("rain_24h_mm",              "direct"),
        "rain_24h_mm":          ("rain_24h_mm",              "direct"),
        "pressure_hPa":         ("pressure_hpa",             "direct"),
        "pressure_kPa":         ("pressure_hpa",             "kpa_to_hpa"),
    }

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._lock = threading.Lock()

    def ingest(self, pkt: dict) -> None:
        with self._lock:
            for src_key, (dst_key, conv) in self._FIELD_MAP.items():
                if src_key in pkt and pkt[src_key] is not None:
                    val = float(pkt[src_key])
                    if conv == "C_to_F":
                        val = _c_to_f(val)
                    elif conv == "mps_to_mph":
                        val = _mps_to_mph(val)
                    elif conv == "kmh_to_mph":
                        val = _kmh_to_mph(val)
                    elif conv == "kpa_to_hpa":
                        val = val * 10
                    self._data[dst_key] = val

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    def has_data(self) -> bool:
        with self._lock:
            return bool(self._data)


# ---------------------------------------------------------------------------
# Raw packet logger
# ---------------------------------------------------------------------------

class RawLogger:
    """Appends every received JSON packet (plus metadata) to a log file."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, pkt: dict) -> None:
        entry = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "packet": pkt,
        }
        line = json.dumps(entry)
        with self._lock:
            with self.path.open("a") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------------------
# APRS file writer
# ---------------------------------------------------------------------------

class APRSWriter:
    """Writes the latest APRS packet to a flat file (overwrite each time)."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, packet: str) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(packet + "\n")
        tmp.replace(self.path)   # atomic rename


# ---------------------------------------------------------------------------
# Device filter
# ---------------------------------------------------------------------------

def _matches_device(pkt: dict, devices: list[dict]) -> bool:
    """Return True if pkt comes from one of the configured devices (or no filter set)."""
    if not devices:
        return True
    model = pkt.get("model", "")
    dev_id = pkt.get("id", pkt.get("channel", None))
    for d in devices:
        if d.get("model") and d["model"] != model:
            continue
        if d.get("id") is not None and str(d["id"]) != str(dev_id):
            if d.get("channel") is not None and str(d.get("channel")) != str(pkt.get("channel", "")):
                continue
        return True
    return False


# ---------------------------------------------------------------------------
# Live status panel (rich)
# ---------------------------------------------------------------------------

def _build_status_table(wx: dict, last_aprs: str, pkt_count: int,
                        last_rx: Optional[str],
                        routing: dict,
                        aprsis_ok: Optional[bool],
                        kiss_ok: Optional[bool],
                        first_packet_received: bool) -> Table:
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("Field", style="bold cyan", width=22)
    t.add_column("Value", style="white")

    def row(k, v):
        t.add_row(k, str(v) if v is not None else "[dim]—[/dim]")

    if not first_packet_received:
        t.add_row(
            "[bold yellow]Status[/bold yellow]",
            "[yellow blink]⏳ Waiting for first packet from rtl_433 …[/yellow blink]"
        )
        t.add_row("", "")

    row("Temperature", f"{wx.get('temperature_F', '—'):.1f} °F" if 'temperature_F' in wx else "—")
    row("Humidity", f"{wx.get('humidity_pct', '—'):.0f} %" if 'humidity_pct' in wx else "—")
    row("Wind dir", f"{wx.get('wind_dir_deg', '—'):.0f}°" if 'wind_dir_deg' in wx else "—")
    row("Wind speed", f"{wx.get('wind_speed_mph', '—'):.1f} mph" if 'wind_speed_mph' in wx else "—")
    row("Wind gust", f"{wx.get('wind_gust_mph', '—'):.1f} mph" if 'wind_gust_mph' in wx else "—")
    row("Pressure", f"{wx.get('pressure_hpa', '—'):.1f} hPa" if 'pressure_hpa' in wx else "—")
    row("Rain (60 min)", f"{wx.get('rain_60min_mm', '—'):.1f} mm" if 'rain_60min_mm' in wx else "—")
    t.add_row("", "")
    row("Packets received", str(pkt_count))
    row("Last packet at", last_rx or "—")
    t.add_row("", "")

    mode = routing.get("mode", "file_only")
    if mode == "file_only":
        t.add_row("[bold yellow]Routing[/bold yellow]", "[dim]File only[/dim]")
    else:
        if aprsis_ok is not None:
            s = "[green]✓ connected[/green]" if aprsis_ok else "[red]✗ offline[/red]"
            t.add_row("[bold yellow]APRS-IS[/bold yellow]", s)
        if kiss_ok is not None:
            s = "[green]✓ connected[/green]" if kiss_ok else "[red]✗ offline[/red]"
            t.add_row("[bold yellow]RF / KISS[/bold yellow]", s)

    t.add_row("", "")
    t.add_row("[bold yellow]Last APRS[/bold yellow]", f"[dim]{last_aprs or '—'}[/dim]")
    return t


# ---------------------------------------------------------------------------
# Main rtl_433 reader loop
# ---------------------------------------------------------------------------

class WXBeacon:
    def __init__(self, config: dict, logger: logging.Logger) -> None:
        self.cfg = config
        self.log = logger
        self.devices: list[dict] = config.get("devices", [])
        self.freq_mhz: float = config["frequency_mhz"]
        self.sdr: dict = config["sdr"]
        self.aprs_cfg: dict = config["aprs"]
        self.log_cfg: dict = config["logging"]
        self.rtl433_bin: str = config.get("rtl433_bin", "rtl_433")
        self.beacon_interval: int = self.aprs_cfg.get("beacon_interval_seconds", 300)
        self.routing: dict = config.get("routing", {"mode": "file_only",
                                                     "aprsis": None, "rf": None})

        self.aggregator = WXAggregator()
        self.raw_logger = RawLogger(self.log_cfg["raw_log"])
        self.aprs_writer = APRSWriter(self.log_cfg["aprs_file"])
        self.aprs_builder = APRSBuilder(
            callsign=self.aprs_cfg["callsign"],
            lat=self.aprs_cfg["latitude"],
            lon=self.aprs_cfg["longitude"],
            comment=self.aprs_cfg.get("comment", ""),
        )

        # Instantiate active senders based on routing mode
        mode = self.routing.get("mode", "file_only")
        self._aprsis: Optional[APRSISSender]      = None
        self._kiss:   Optional[NetworkKISSSender] = None

        if mode in ("aprsis_only", "both") and self.routing.get("aprsis"):
            self._aprsis = APRSISSender(self.routing["aprsis"], self.log)
        if mode in ("rf_only", "both") and self.routing.get("rf"):
            self._kiss = NetworkKISSSender(self.routing["rf"], self.log)

        self._stop_event = threading.Event()
        self._first_packet_event = threading.Event()   # set on first valid RX
        self._pkt_count = 0
        self._last_rx: Optional[str] = None
        self._last_aprs: str = ""
        self._last_beacon_time: float = 0.0

    # ------------------------------------------------------------------
    def _build_rtl433_cmd(self) -> list[str]:
        cmd = [
            self.rtl433_bin,
            "-f", str(int(self.freq_mhz * 1e6)),
            "-s", str(self.sdr["sample_rate"]),
            "-F", "json",
            "-d", str(self.sdr["device_index"]),
        ]
        if self.sdr.get("gain", "auto") != "auto":
            cmd += ["-g", str(self.sdr["gain"])]
        if self.sdr.get("ppm", 0) != 0:
            cmd += ["-p", str(self.sdr["ppm"])]
        return cmd

    # ------------------------------------------------------------------
    def _dispatch_beacon(self) -> None:
        """Build APRS packet and deliver via all configured channels."""
        if not self.aggregator.has_data():
            self.log.warning("Beacon interval reached but no wx data yet; skipping.")
            return

        wx   = self.aggregator.snapshot()
        mode = self.routing.get("mode", "file_only")

        # APRS-IS uses TCPIP* path
        if self._aprsis:
            pkt = self.aprs_builder.build(wx, path="TCPIP*")
            self._aprsis.send_packet(pkt)
            self.aprs_writer.write(pkt)
            self._last_aprs = pkt

        # RF uses the configured digi path
        if self._kiss:
            rf_path = self.routing["rf"]["digi_path"]
            pkt     = self.aprs_builder.build(wx, path=rf_path)
            self._kiss.send_packet(pkt)
            if not self._aprsis:          # write file only if APRS-IS didn't already
                self.aprs_writer.write(pkt)
                self._last_aprs = pkt

        # File-only mode — just write the packet
        if mode == "file_only":
            pkt = self.aprs_builder.build(wx)
            self.aprs_writer.write(pkt)
            self._last_aprs = pkt
            self.log.info(f"APRS beacon written → {self.log_cfg['aprs_file']}")
            self.log.debug(f"Packet: {pkt}")

        self._last_beacon_time = time.monotonic()

    # ------------------------------------------------------------------
    def _beacon_loop(self) -> None:
        """
        Background thread: fires _dispatch_beacon on the configured interval.

        Deliberately waits until the first valid packet has been received from
        rtl_433 before starting the interval countdown.  This ensures the very
        first beacon contains real sensor data rather than firing immediately at
        startup with empty fields.
        """
        self.log.info("Beacon scheduler: waiting for first packet from rtl_433 …")

        # Block here until _process_line signals that a valid packet arrived,
        # or until a stop is requested.
        while not self._stop_event.is_set():
            if self._first_packet_event.wait(timeout=5):
                break   # first packet received — proceed to interval loop

        if self._stop_event.is_set():
            return

        # Arm the interval timer from the moment the first packet arrived,
        # so the full beacon_interval elapses before the first transmission.
        self._last_beacon_time = time.monotonic()
        self.log.info(
            f"First packet received — beacon timer armed, "
            f"first TX in {self.beacon_interval}s"
        )

        while not self._stop_event.is_set():
            if time.monotonic() - self._last_beacon_time >= self.beacon_interval:
                self._dispatch_beacon()
            self._stop_event.wait(timeout=5)

    # ------------------------------------------------------------------
    def _process_line(self, line: str) -> None:
        line = line.strip()
        if not line or not line.startswith("{"):
            return
        try:
            pkt = json.loads(line)
        except json.JSONDecodeError as e:
            self.log.debug(f"JSON parse error: {e}  raw={line!r}")
            return

        if not _matches_device(pkt, self.devices):
            self.log.debug(f"Filtered out: model={pkt.get('model')} id={pkt.get('id')}")
            return

        self._pkt_count += 1
        self._last_rx = datetime.now().strftime("%H:%M:%S")
        self.raw_logger.log(pkt)
        self.aggregator.ingest(pkt)

        # Signal the beacon scheduler that real data has arrived.
        # set() is idempotent — safe to call on every subsequent packet.
        self._first_packet_event.set()

        model = pkt.get("model", "unknown")
        dev_id = pkt.get("id", "?")
        temp_f = self.aggregator.snapshot().get("temperature_F")
        hum    = pkt.get("humidity")
        self.log.info(
            f"RX [{model} id={dev_id}]  "
            + (f"temp={temp_f:.1f}°F  " if temp_f is not None else "")
            + (f"hum={hum}%  " if hum is not None else "")
            + f"(total pkts: {self._pkt_count})"
        )

    # ------------------------------------------------------------------
    def run(self) -> None:
        cmd = self._build_rtl433_cmd()
        self.log.info(f"Starting rtl_433: {' '.join(cmd)}")
        self.log.info(f"Raw log  → {self.log_cfg['raw_log']}")
        self.log.info(f"APRS out → {self.log_cfg['aprs_file']}")
        self.log.info(f"Routing  → {self.routing.get('mode','file_only').upper()}  "
                      f"| interval={self.beacon_interval}s")

        # Start network senders
        if self._aprsis:
            self._aprsis.start()
        if self._kiss:
            self._kiss.start()

        # Start beacon thread
        beacon_thread = threading.Thread(target=self._beacon_loop, daemon=True)
        beacon_thread.start()

        # Signal handler
        def _sig(sig, frame):
            self.log.info("Shutdown signal received …")
            self._stop_event.set()

        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        if RICH:
            self._run_rich(cmd)
        else:
            self._run_plain(cmd)

        beacon_thread.join(timeout=5)
        if self._aprsis:
            self._aprsis.stop()
        if self._kiss:
            self._kiss.stop()
        self.log.info("wx_beacon stopped.")

    # ------------------------------------------------------------------
    def _run_plain(self, cmd: list[str]) -> None:
        while not self._stop_event.is_set():
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )
                while not self._stop_event.is_set():
                    line = proc.stdout.readline()
                    if not line:
                        break
                    self._process_line(line)
                proc.terminate()
                proc.wait(timeout=5)
            except FileNotFoundError:
                self.log.critical(f"Cannot find rtl_433 at: {cmd[0]}")
                self._stop_event.set()
            except Exception as e:
                self.log.error(f"rtl_433 error: {e}; restarting in 5s …")
                if not self._stop_event.wait(5):
                    continue

    # ------------------------------------------------------------------
    def _run_rich(self, cmd: list[str]) -> None:
        from rich.live import Live

        proc: Optional[subprocess.Popen] = None

        def _start_proc():
            return subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )

        try:
            proc = _start_proc()
        except FileNotFoundError:
            self.log.critical(f"Cannot find rtl_433: {cmd[0]}")
            return

        with Live(
            console=_console,
            refresh_per_second=2,
            screen=False,
        ) as live:
            while not self._stop_event.is_set():
                if proc.poll() is not None:
                    self.log.warning("rtl_433 exited unexpectedly; restarting …")
                    if not self._stop_event.wait(3):
                        proc = _start_proc()
                    continue

                line = proc.stdout.readline()
                if line:
                    self._process_line(line)

                wx = self.aggregator.snapshot()
                panel = Panel(
                    _build_status_table(
                        wx, self._last_aprs, self._pkt_count, self._last_rx,
                        self.routing,
                        self._aprsis.connected if self._aprsis else None,
                        self._kiss.connected   if self._kiss   else None,
                        self._first_packet_event.is_set(),
                    ),
                    title=f"[bold cyan]WX Beacon — {self.aprs_cfg['callsign']}[/bold cyan]",
                    subtitle=f"[dim]{self.freq_mhz} MHz  |  gain={self.sdr['gain']}  |  {self.sdr['sample_rate']//1000} kSps  |  {self.routing.get('mode','file_only').upper()}[/dim]",
                    border_style="cyan",
                )
                live.update(panel)

        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="RTL-SDR Weather Station Logger & APRS Beacon Writer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 wx_beacon.py
  python3 wx_beacon.py --config /etc/wx/station.json
  python3 wx_beacon.py --once          # write one APRS packet and exit
        """,
    )
    p.add_argument("--config", type=Path, default=CONFIG_PATH_DEFAULT,
                   help="Path to JSON config file (default: wx_station.json)")
    p.add_argument("--once", action="store_true",
                   help="Run for one beacon interval, write packet, then exit")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Bootstrap logger at INFO until we read config
    logger = setup_logging("INFO")

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.critical(str(e))
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.critical(f"Config parse error: {e}")
        sys.exit(1)

    # Re-init logger at configured level
    logger = setup_logging(config.get("logging", {}).get("log_level", "INFO"))

    if args.once:
        beacon = WXBeacon(config, logger)
        interval = beacon.beacon_interval
        logger.info(f"--once mode: collecting for {interval}s then exiting")
        cmd = beacon._build_rtl433_cmd()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line:
                beacon._process_line(line)
        proc.terminate()
        proc.wait(timeout=5)
        if beacon.aggregator.has_data():
            wx = beacon.aggregator.snapshot()
            packet = beacon.aprs_builder.build(wx)
            beacon.aprs_writer.write(packet)
            logger.info(f"APRS packet: {packet}")
        else:
            logger.warning("No wx data collected.")
        return

    beacon = WXBeacon(config, logger)
    beacon.run()


if __name__ == "__main__":
    main()

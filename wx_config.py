#!/usr/bin/env python3
"""
wx_config.py — Interactive RTL-SDR Weather Station Configurator
Scans for rtl_433-compatible weather stations, lets you pick devices,
tune SDR parameters, and writes a config file for wx_beacon.py.

Requirements:
    pip install rich pyserial
    apt/brew install rtl-433  (or build from source)
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Optional: rich for pretty terminal UI
# ---------------------------------------------------------------------------
try:
    from rich import print as rprint
    from rich.columns import Columns
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn
    from rich.prompt import Confirm, IntPrompt, Prompt
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
    RICH = True
    console = Console()
except ImportError:
    RICH = False
    console = None

CONFIG_PATH = Path("wx_station.json")

# ---------------------------------------------------------------------------
# ISM Band definitions  (freq_mhz, label, typical protocols)
# ---------------------------------------------------------------------------
ISM_BANDS = {
    "1": {
        "label": "433 MHz  (EU/Asia primary, US secondary)",
        "freq_mhz": 433.92,
        "protocols": "Acurite, Oregon Scientific, Bresser, LaCrosse, …",
    },
    "2": {
        "label": "315 MHz  (North America primary)",
        "freq_mhz": 315.00,
        "protocols": "Acurite-5n1, Ambient Weather WS-2902, …",
    },
    "3": {
        "label": "868 MHz  (EU primary, IoT)",
        "freq_mhz": 868.30,
        "protocols": "Bresser 5-in-1, Hideki, …",
    },
    "4": {
        "label": "915 MHz  (North America ISM / FCC Part 15)",
        "freq_mhz": 915.00,
        "protocols": "Davis Instruments, Ambient WS-2902C, …",
    },
    "5": {
        "label": "Custom frequency",
        "freq_mhz": None,
        "protocols": "User-specified",
    },
}

# RTL-Blog v4 native sample rates (Hz) that avoid aliasing artefacts
RTL_V4_SAMPLE_RATES = [
    250_000,
    1_000_000,
    1_024_000,
    1_536_000,
    2_048_000,
    2_400_000,   # native maximum without drop-outs on most v4 sticks
    3_200_000,
]

# Sensible gain presets (dB).  "auto" = hardware AGC.
RTL_GAIN_PRESETS = ["auto", "0", "10", "20", "30", "40", "49.6"]

# ---------------------------------------------------------------------------
# Packet routing catalogs
# ---------------------------------------------------------------------------

# Four top-level delivery modes
ROUTE_MODES = {
    "1": {
        "label":       "File only  (log APRS packet to disk, no network TX)",
        "description": "Writes wx_aprs.txt each beacon cycle. Safe default; "
                       "use with Direwolf file-beacon or any external injector.",
    },
    "2": {
        "label":       "APRS-IS only  (internet Tier-2 gateway)",
        "description": "TCP connection to an APRS-IS server. No transmitter needed; "
                       "packets appear globally on aprs.fi within seconds.",
    },
    "3": {
        "label":       "RF only  (network KISS → TNC → VHF radio)",
        "description": "AX.25 KISS frames injected into a local TNC or Direwolf "
                       "instance for over-the-air transmission.",
    },
    "4": {
        "label":       "APRS-IS + RF simultaneously",
        "description": "Internet delivery AND RF transmission on every beacon cycle. "
                       "Maximum coverage and redundancy.",
    },
}

# APRS-IS Tier-2 server presets
APRSIS_SERVERS = {
    "1": {"label": "rotate.aprs2.net:14580  (global round-robin — recommended)",
          "host": "rotate.aprs2.net",  "port": 14580},
    "2": {"label": "noam.aprs2.net:14580    (North America)",
          "host": "noam.aprs2.net",    "port": 14580},
    "3": {"label": "euro.aprs2.net:14580    (Europe)",
          "host": "euro.aprs2.net",    "port": 14580},
    "4": {"label": "asia.aprs2.net:14580    (Asia-Pacific)",
          "host": "asia.aprs2.net",    "port": 14580},
    "5": {"label": "Custom server / port",
          "host": None,                "port": None},
}

# Network KISS endpoint presets (KISS-over-TCP)
KISS_ENDPOINTS = {
    "1": {"label": "Direwolf  localhost:8001  (KISS TCP — default)",
          "host": "localhost", "port": 8001},
    "2": {"label": "Direwolf  localhost:8000  (AGW TCP port)",
          "host": "localhost", "port": 8000},
    "3": {"label": "soundmodem  localhost:8010",
          "host": "localhost", "port": 8010},
    "4": {"label": "Remote / hardware TNC with KISS-over-TCP (custom host:port)",
          "host": None,        "port": None},
    "5": {"label": "Custom host:port",
          "host": None,        "port": None},
}

# ---------------------------------------------------------------------------
# RF digipeater path presets — based on APRS best practices
#
# References:
#   APRS Working Group  "New Paradigm" path guidelines (WB4APR, 2004 onwards)
#   APRS101.PDF §6 — Path Aliases
#   aprs.org/fix14439.html — recommended paths by station type
# ---------------------------------------------------------------------------
DIGI_PATH_PRESETS = {
    "1": {
        "label":       "WIDE1-1,WIDE2-1  — Standard home / fixed WX station",
        "path":        "WIDE1-1,WIDE2-1",
        "hops":        2,
        "best_for":    "Fixed home station, rural or suburban coverage area",
        "notes":       "Most widely used path. Hits fill-in digis (WIDE1-1) then "
                       "wide-area digis (WIDE2-1). Correct for a permanent WX beacon.",
    },
    "2": {
        "label":       "WIDE2-1  — Single wide-area hop (good digi coverage area)",
        "path":        "WIDE2-1",
        "hops":        1,
        "best_for":    "Urban / metro areas with dense digi coverage",
        "notes":       "Skips fill-in digis. Use when a single high-site digi provides "
                       "full coverage. Reduces RF congestion in busy areas.",
    },
    "3": {
        "label":       "WIDE1-1  — Fill-in digi only (no wide-area repeat)",
        "path":        "WIDE1-1",
        "hops":        1,
        "best_for":    "Very local monitoring; igate is within one hop",
        "notes":       "Minimal RF footprint. Packet is picked up by a local fill-in "
                       "digi or a nearby igate, but does not propagate further.",
    },
    "4": {
        "label":       "WIDE2-2  — Two wide-area hops (extended rural coverage)",
        "path":        "WIDE2-2",
        "hops":        2,
        "best_for":    "Rural / remote areas with sparse digi infrastructure",
        "notes":       "Two wide-area hops with no fill-in requirement. Better than "
                       "WIDE1-1,WIDE2-1 when fill-in digis are absent. Use sparingly "
                       "in metro areas to avoid channel flooding.",
    },
    "5": {
        "label":       "NOGATE  — RF only, block igate upload to APRS-IS",
        "path":        "NOGATE",
        "hops":        0,
        "best_for":    "RF-only beacon when also running APRS-IS path (mode 4)",
        "notes":       "Tells igates NOT to forward this packet to the internet. "
                       "Use in 'Both' mode to prevent duplicate entries on aprs.fi "
                       "when APRS-IS already delivers the packet.",
    },
    "6": {
        "label":       "RFONLY  — Digi repeats but igates must not upload",
        "path":        "RFONLY",
        "hops":        0,
        "best_for":    "Local RF awareness only; internet blackout desired",
        "notes":       "Similar to NOGATE. Digipeaters repeat the frame for local "
                       "stations but igates suppress upload. Useful for private nets.",
    },
    "7": {
        "label":       "Custom path  — Enter manually",
        "path":        None,
        "hops":        None,
        "best_for":    "Special circumstances",
        "notes":       "Enter any valid AX.25 via string (comma-separated).",
    },
}

# Regional APRS VHF channel frequencies
APRS_RF_FREQS = {
    "1": {"label": "144.390 MHz  — North America (standard)", "freq_mhz": 144.390},
    "2": {"label": "144.800 MHz  — Europe / most of world",   "freq_mhz": 144.800},
    "3": {"label": "144.575 MHz  — Japan",                    "freq_mhz": 144.575},
    "4": {"label": "144.660 MHz  — Australia",                "freq_mhz": 144.660},
    "5": {"label": "Custom frequency",                        "freq_mhz": None},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print(msg: str, style: str = "") -> None:
    if RICH:
        console.print(msg, style=style if style else None)
    else:
        print(msg)


def _rule(title: str = "") -> None:
    if RICH:
        console.print(Rule(title, style="cyan"))
    else:
        print(f"\n{'─'*60}  {title}")


def _banner() -> None:
    banner = r"""
 ██╗    ██╗██╗  ██╗     ██████╗ ██████╗ ███╗   ██╗███████╗██╗ ██████╗
 ██║    ██║╚██╗██╔╝    ██╔════╝██╔═══██╗████╗  ██║██╔════╝██║██╔════╝
 ██║ █╗ ██║ ╚███╔╝     ██║     ██║   ██║██╔██╗ ██║█████╗  ██║██║  ███╗
 ██║███╗██║ ██╔██╗     ██║     ██║   ██║██║╚██╗██║██╔══╝  ██║██║   ██║
 ╚███╔███╔╝██╔╝ ██╗    ╚██████╗╚██████╔╝██║ ╚████║██║     ██║╚██████╔╝
  ╚══╝╚══╝ ╚═╝  ╚═╝     ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝╚═╝     ╚═╝ ╚═════╝
    """
    if RICH:
        console.print(Panel(Text(banner, style="bold cyan"), subtitle="[dim]RTL-SDR Weather Station Configurator[/dim]", border_style="cyan"))
    else:
        print(banner)
        print("  RTL-SDR Weather Station Configurator")
        print("=" * 60)


def _check_rtl433() -> str:
    """Return path to rtl_433 binary or raise."""
    for candidate in ("rtl_433", "/usr/local/bin/rtl_433", "/usr/bin/rtl_433"):
        try:
            result = subprocess.run(
                [candidate, "--version"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 or "rtl_433" in (result.stdout + result.stderr).lower():
                return candidate
        except FileNotFoundError:
            continue
        except Exception:
            continue
    _print("[bold red]ERROR:[/bold red] rtl_433 not found in PATH.", "bold red")
    _print("Install it:  https://github.com/merbanan/rtl_433", "yellow")
    sys.exit(1)


# ---------------------------------------------------------------------------
# ISM band selection
# ---------------------------------------------------------------------------

def select_ism_band() -> float:
    _rule("ISM Band Selection")
    if RICH:
        t = Table(show_header=True, header_style="bold cyan", box=None)
        t.add_column("#", style="bold yellow", width=4)
        t.add_column("Band", style="white")
        t.add_column("Typical Protocols", style="dim")
        for key, val in ISM_BANDS.items():
            t.add_row(key, val["label"], val["protocols"])
        console.print(t)
    else:
        for key, val in ISM_BANDS.items():
            print(f"  [{key}] {val['label']}")
            print(f"       Protocols: {val['protocols']}")

    while True:
        choice = (Prompt.ask("\nSelect ISM band", default="1") if RICH
                  else input("Select ISM band [1]: ").strip() or "1")
        if choice in ISM_BANDS:
            band = ISM_BANDS[choice]
            if band["freq_mhz"] is None:
                raw = (Prompt.ask("Enter frequency in MHz") if RICH
                       else input("Enter frequency in MHz: ").strip())
                try:
                    freq = float(raw)
                    _print(f"  → Custom frequency: [bold]{freq} MHz[/bold]")
                    return freq
                except ValueError:
                    _print("Invalid frequency. Try again.", "red")
                    continue
            _print(f"  → Selected: [bold]{band['label']}[/bold]")
            return band["freq_mhz"]
        _print("Invalid selection.", "red")


# ---------------------------------------------------------------------------
# SDR hardware parameters
# ---------------------------------------------------------------------------

def select_sdr_params() -> dict:
    _rule("RTL-SDR / RTL-Blog v4 Parameters")

    # --- Device index ---
    if RICH:
        dev_idx = IntPrompt.ask("RTL-SDR device index (0 = first stick)", default=0)
    else:
        try:
            dev_idx = int(input("RTL-SDR device index [0]: ").strip() or "0")
        except ValueError:
            dev_idx = 0

    # --- Sample rate ---
    _print("\n[bold]Available native sample rates (RTL-Blog v4):[/bold]")
    for i, sr in enumerate(RTL_V4_SAMPLE_RATES):
        marker = "  ← recommended" if sr == 250_000 else ""
        _print(f"  [{i}]  {sr:>10,} Hz  ({sr/1e6:.3f} MHz){marker}")

    while True:
        raw = (Prompt.ask("Select sample rate index or type custom Hz", default="0") if RICH
               else input("Sample rate index or custom Hz [0]: ").strip() or "0")
        try:
            idx = int(raw)
            if 0 <= idx < len(RTL_V4_SAMPLE_RATES):
                sample_rate = RTL_V4_SAMPLE_RATES[idx]
                break
            # treat as raw Hz
            sample_rate = idx
            break
        except ValueError:
            _print("Enter a valid number.", "red")

    _print(f"  → Sample rate: [bold]{sample_rate:,} Hz[/bold]")

    # --- Gain ---
    _print("\n[bold]Gain presets:[/bold]")
    for i, g in enumerate(RTL_GAIN_PRESETS):
        marker = "  ← hardware AGC" if g == "auto" else ""
        _print(f"  [{i}]  {g} dB{marker}")

    while True:
        raw = (Prompt.ask("Select gain index or type value (dB)", default="0") if RICH
               else input("Gain preset index [0]: ").strip() or "0")
        try:
            idx = int(raw)
            if 0 <= idx < len(RTL_GAIN_PRESETS):
                gain = RTL_GAIN_PRESETS[idx]
                break
            gain = str(idx)
            break
        except ValueError:
            _print("Enter a valid number.", "red")

    _print(f"  → Gain: [bold]{gain}[/bold]")

    # --- PPM correction ---
    if RICH:
        ppm = IntPrompt.ask("Frequency correction in PPM (0 = none)", default=0)
    else:
        try:
            ppm = int(input("Frequency correction PPM [0]: ").strip() or "0")
        except ValueError:
            ppm = 0

    return {
        "device_index": dev_idx,
        "sample_rate": sample_rate,
        "gain": gain,
        "ppm": ppm,
    }


# ---------------------------------------------------------------------------
# Routing prompt helpers  (thin wrappers so we can call from configure_routing)
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    if RICH:
        return Prompt.ask(prompt, default=default)
    shown = f" [{default}]" if default else ""
    val = input(f"{prompt}{shown}: ").strip()
    return val if val else default


def _ask_int(prompt: str, default: int) -> int:
    if RICH:
        return IntPrompt.ask(prompt, default=default)
    try:
        return int(input(f"{prompt} [{default}]: ").strip() or str(default))
    except ValueError:
        return default


def _confirm(prompt: str, default: bool = True) -> bool:
    if RICH:
        return Confirm.ask(prompt, default=default)
    yn = "Y/n" if default else "y/N"
    val = input(f"{prompt} [{yn}]: ").strip().lower()
    return val.startswith("y") if val else default


# ---------------------------------------------------------------------------
# Packet routing configuration
# ---------------------------------------------------------------------------

def configure_routing() -> dict:
    """
    Interactively configure how APRS weather packets are delivered.

    Returns a routing dict stored under config["routing"]:
      {
        "mode": "file_only" | "aprsis_only" | "rf_only" | "both",
        "aprsis": { ... } | None,
        "rf":     { ... } | None,
      }
    """
    _rule("APRS Packet Routing")
    _print(
        "Choose how decoded APRS weather packets are delivered.\n"
        "\n"
        "  [bold cyan]File only[/bold cyan]   — Write wx_aprs.txt each cycle. No network or radio TX.\n"
        "  [bold cyan]APRS-IS[/bold cyan]     — TCP to a Tier-2 internet server (no transmitter needed).\n"
        "  [bold cyan]RF[/bold cyan]          — AX.25 KISS frames → network KISS TNC → VHF radio.\n"
        "  [bold cyan]Both[/bold cyan]        — APRS-IS + RF on every beacon cycle.\n"
    )

    if RICH:
        t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        t.add_column("#",           style="bold yellow", width=4)
        t.add_column("Mode",        style="white",       min_width=40)
        t.add_column("Description", style="dim")
        for k, v in ROUTE_MODES.items():
            t.add_row(k, v["label"], v["description"])
        console.print(t)
    else:
        for k, v in ROUTE_MODES.items():
            print(f"\n  [{k}] {v['label']}")
            print(f"       {v['description']}")

    while True:
        choice = _ask("\nSelect routing mode", "1")
        if choice in ROUTE_MODES:
            _print(f"  → [bold]{ROUTE_MODES[choice]['label']}[/bold]")
            break
        _print("Invalid choice.", "red")

    mode_map = {"1": "file_only", "2": "aprsis_only", "3": "rf_only", "4": "both"}
    mode     = mode_map[choice]
    result: dict = {"mode": mode, "aprsis": None, "rf": None}

    # ── APRS-IS block ────────────────────────────────────────────────────────
    if mode in ("aprsis_only", "both"):
        _rule("APRS-IS  (Internet Gateway) Configuration")

        # Server
        _print("\n[bold cyan]APRS-IS server[/bold cyan]")
        if RICH:
            t = Table(show_header=True, header_style="bold cyan", box=None)
            t.add_column("#",      style="bold yellow", width=4)
            t.add_column("Server", style="white")
            for k, v in APRSIS_SERVERS.items():
                t.add_row(k, v["label"])
            console.print(t)
        else:
            for k, v in APRSIS_SERVERS.items():
                print(f"  [{k}] {v['label']}")

        while True:
            sc = _ask("Select server", "1")
            if sc in APRSIS_SERVERS:
                srv = dict(APRSIS_SERVERS[sc])
                if srv["host"] is None:
                    srv["host"] = _ask("Hostname", "rotate.aprs2.net")
                    srv["port"] = _ask_int("Port", 14580)
                _print(f"  → [bold]{srv['host']}:{srv['port']}[/bold]")
                break
            _print("Invalid choice.", "red")

        # Credentials
        _print("\n[bold cyan]Login credentials[/bold cyan]")
        _print("[dim]  Passcode generator: https://apps.magicbug.co.uk/passcode/[/dim]")
        _print("[dim]  Use -1 for receive-only (packets still upload if passcode is valid)[/dim]")
        callsign = _ask("APRS-IS callsign (e.g. W1AW-13)", "N0CALL-13").upper()
        passcode = _ask("APRS-IS passcode  (-1 = receive-only)", "-1")

        # Server-side filter
        _print("\n[bold cyan]Server-side receive filter[/bold cyan]")
        _print("[dim]  Controls what the server streams back to you, not what you upload.[/dim]")
        _print("[dim]  Format: r/lat/lon/km  — only receive packets within radius.[/dim]")
        filt_lat = _ask("Station latitude",  "38.00")
        filt_lon = _ask("Station longitude", "-84.50")
        srv_filter = _ask("Filter string", f"r/{filt_lat}/{filt_lon}/100")

        result["aprsis"] = {
            "host":                   srv["host"],
            "port":                   srv["port"],
            "callsign":               callsign,
            "passcode":               passcode,
            "filter":                 srv_filter,
            "retry_interval_seconds": 30,
            "keepalive_seconds":      60,
        }

    # ── RF / network KISS block ───────────────────────────────────────────────
    if mode in ("rf_only", "both"):
        _rule("RF Routing — Network KISS Configuration")
        _print(
            "wx_beacon.py connects to a KISS-over-TCP endpoint, builds a proper\n"
            "AX.25 UI frame, and injects it into the TNC transmit queue.\n"
            "\n"
            "Compatible backends: [bold]Direwolf[/bold], [bold]soundmodem[/bold], "
            "hardware TNCs with KISS-over-TCP\n"
            "(e.g. Kenwood TM-D710A, Yaesu FTM-400XD, Argent Data T3-Mini).\n"
        )

        # TX callsign
        tx_call = _ask("TX callsign for RF (e.g. W1AW-13)", "N0CALL-13").upper()

        # KISS endpoint
        _print("\n[bold cyan]Network KISS endpoint[/bold cyan]")
        if RICH:
            t = Table(show_header=True, header_style="bold cyan", box=None)
            t.add_column("#",         style="bold yellow", width=4)
            t.add_column("Endpoint",  style="white")
            for k, v in KISS_ENDPOINTS.items():
                t.add_row(k, v["label"])
            console.print(t)
        else:
            for k, v in KISS_ENDPOINTS.items():
                print(f"  [{k}] {v['label']}")

        while True:
            kc = _ask("Select KISS endpoint", "1")
            if kc in KISS_ENDPOINTS:
                ep = dict(KISS_ENDPOINTS[kc])
                if ep["host"] is None:
                    ep["host"] = _ask("KISS host", "localhost")
                    ep["port"] = _ask_int("KISS port", 8001)
                _print(f"  → [bold]{ep['host']}:{ep['port']}[/bold]")
                break
            _print("Invalid choice.", "red")

        tnc_port_num = _ask_int(
            "KISS TNC port number (0 = first/only radio port)", 0
        )

        # APRS RF channel
        _print("\n[bold cyan]APRS RF channel[/bold cyan]")
        _print("[dim]  Informational — the TNC / radio controls the actual frequency.[/dim]")
        if RICH:
            t = Table(show_header=True, header_style="bold cyan", box=None)
            t.add_column("#",       style="bold yellow", width=4)
            t.add_column("Channel", style="white")
            for k, v in APRS_RF_FREQS.items():
                t.add_row(k, v["label"])
            console.print(t)
        else:
            for k, v in APRS_RF_FREQS.items():
                print(f"  [{k}] {v['label']}")

        while True:
            fc = _ask("Select channel", "1")
            if fc in APRS_RF_FREQS:
                ch = dict(APRS_RF_FREQS[fc])
                if ch["freq_mhz"] is None:
                    ch["freq_mhz"] = float(_ask("APRS frequency MHz", "144.390"))
                _print(f"  → [bold]{ch['freq_mhz']} MHz[/bold]")
                break
            _print("Invalid choice.", "red")

        # Digipeater path — with full best-practice guidance
        _print("\n[bold cyan]Digipeater path[/bold cyan]")
        _print(
            "[dim]  APRS path determines how many times and by which digipeaters your[/dim]\n"
            "[dim]  packet is repeated before reaching an igate or other stations.[/dim]\n"
            "[dim]  Follow the APRS 'New Paradigm' guidelines (WB4APR / ARRL):[/dim]\n"
            "[dim]    • Use the minimum path needed to reach an igate.[/dim]\n"
            "[dim]    • Longer paths increase QRM; never use WIDE3-3 or higher.[/dim]\n"
            "[dim]    • Fixed WX stations: WIDE1-1,WIDE2-1 is the standard choice.[/dim]\n"
        )

        if RICH:
            t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
            t.add_column("#",        style="bold yellow", width=4)
            t.add_column("Path",     style="bold white",  min_width=28)
            t.add_column("Hops",     style="cyan",        justify="center", width=6)
            t.add_column("Best for", style="green",       min_width=30)
            t.add_column("Notes",    style="dim")
            for k, v in DIGI_PATH_PRESETS.items():
                hops_s = str(v["hops"]) if v["hops"] is not None else "—"
                t.add_row(k, v["label"].split("—")[0].strip(),
                          hops_s, v["best_for"], v["notes"])
            console.print(t)
        else:
            for k, v in DIGI_PATH_PRESETS.items():
                hops_s = str(v["hops"]) if v["hops"] is not None else "—"
                print(f"\n  [{k}] {v['label']}")
                print(f"       Hops    : {hops_s}")
                print(f"       Best for: {v['best_for']}")
                print(f"       Notes   : {v['notes']}")

        while True:
            dc = _ask("\nSelect digipeater path", "1")
            if dc in DIGI_PATH_PRESETS:
                digi = dict(DIGI_PATH_PRESETS[dc])
                if digi["path"] is None:
                    digi["path"] = _ask("Enter custom path (e.g. WIDE1-1,WIDE2-1)",
                                        "WIDE1-1,WIDE2-1")
                _print(f"  → [bold]{digi['path']}[/bold]  — {digi['best_for']}")
                break
            _print("Invalid choice.", "red")

        # Recommend NOGATE when running Both mode
        if mode == "both" and digi["path"] not in ("NOGATE", "RFONLY"):
            _print(
                "\n[yellow]Tip:[/yellow] You selected 'Both' (APRS-IS + RF). "
                "Consider using path [bold]NOGATE[/bold] for the RF leg\n"
                "  so igates do not re-upload the packet and create duplicates on aprs.fi.\n"
                "  Your APRS-IS connection already handles internet delivery.\n"
            )
            if _confirm("Switch RF path to NOGATE?", default=True):
                digi = dict(DIGI_PATH_PRESETS["5"])   # NOGATE entry
                _print("  → RF path changed to [bold]NOGATE[/bold]")

        # TX power (informational)
        _print("\n[bold cyan]Transmitter power (informational)[/bold cyan]")
        try:
            tx_w = float(_ask("TX power in Watts", "5"))
        except ValueError:
            tx_w = 5.0
        import math as _math
        tx_dbm = round(10 * _math.log10(tx_w * 1000), 1)
        _print(f"  → {tx_w} W  ({tx_dbm} dBm)")

        result["rf"] = {
            "callsign":       tx_call,
            "aprs_freq_mhz":  ch["freq_mhz"],
            "digi_path":      digi["path"],
            "digi_hops":      digi["hops"],
            "kiss": {
                "host":     ep["host"],
                "port":     ep["port"],
                "tnc_port": tnc_port_num,
            },
            "tx_power_watts": tx_w,
            "tx_power_dbm":   tx_dbm,
        }

    return result


# ---------------------------------------------------------------------------
# Station scan
# ---------------------------------------------------------------------------

def scan_for_stations(rtl433_bin: str, freq_mhz: float, sdr: dict, scan_seconds: int) -> list[dict]:
    """Run rtl_433 and collect JSON-decoded packets. Returns list of seen devices."""
    _rule("Scanning for Weather Stations")
    _print(f"  Frequency : [bold]{freq_mhz} MHz[/bold]")
    _print(f"  Duration  : [bold]{scan_seconds} seconds[/bold]")
    _print(f"  Sample rate: [bold]{sdr['sample_rate']:,} Hz[/bold]   Gain: [bold]{sdr['gain']}[/bold]\n")

    cmd = [
        rtl433_bin,
        "-f", f"{int(freq_mhz * 1e6)}",
        "-s", str(sdr["sample_rate"]),
        "-F", "json",
        "-T", str(scan_seconds),
        "-d", str(sdr["device_index"]),
    ]
    if sdr["gain"] != "auto":
        cmd += ["-g", sdr["gain"]]
    if sdr["ppm"] != 0:
        cmd += ["-p", str(sdr["ppm"])]

    _print(f"[dim]Running: {' '.join(cmd)}[/dim]\n")

    seen: dict[str, dict] = {}  # key = "model|id"
    packets: list[dict] = []

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        if RICH:
            with Progress(
                SpinnerColumn(),
                "[progress.description]{task.description}",
                TimeElapsedColumn(),
                console=console,
                transient=True,
            ) as progress:
                task = progress.add_task(
                    f"[cyan]Listening on {freq_mhz} MHz …", total=None
                )
                start = time.time()
                while time.time() - start < scan_seconds:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    _process_line(line, seen, packets)
                    progress.update(task, description=f"[cyan]Listening … found {len(seen)} device(s)")
        else:
            start = time.time()
            while time.time() - start < scan_seconds:
                line = proc.stdout.readline()
                if not line:
                    break
                _process_line(line, seen, packets)
                print(f"\r  Scanning … {int(time.time()-start)}s / {scan_seconds}s  |  {len(seen)} device(s) found   ", end="", flush=True)
            print()

    except KeyboardInterrupt:
        _print("\n[yellow]Scan interrupted by user.[/yellow]")
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    return list(seen.values())


def _process_line(line: str, seen: dict, packets: list) -> None:
    line = line.strip()
    if not line or not line.startswith("{"):
        return
    try:
        pkt = json.loads(line)
        packets.append(pkt)
        model = pkt.get("model", "Unknown")
        dev_id = pkt.get("id", pkt.get("channel", "?"))
        key = f"{model}|{dev_id}"
        if key not in seen:
            seen[key] = {
                "model": model,
                "id": dev_id,
                "channel": pkt.get("channel", "—"),
                "last_packet": pkt,
                "count": 1,
            }
        else:
            seen[key]["count"] += 1
            seen[key]["last_packet"] = pkt
    except json.JSONDecodeError:
        pass


# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------

def select_devices(found: list[dict]) -> list[dict]:
    _rule("Detected Weather Stations")

    if not found:
        _print("[yellow]No devices detected during scan.[/yellow]")
        _print("Tips: move antenna outdoors, increase scan time, check frequency.\n")
        if RICH:
            proceed = Confirm.ask("Continue and configure manually?", default=False)
        else:
            proceed = input("Continue and configure manually? [y/N]: ").strip().lower() == "y"
        if not proceed:
            sys.exit(0)
        return []

    if RICH:
        t = Table(show_header=True, header_style="bold cyan")
        t.add_column("#", style="bold yellow", width=4)
        t.add_column("Model", style="white")
        t.add_column("ID / Channel", style="cyan")
        t.add_column("Packets rx", style="green")
        t.add_column("Last Temp", style="magenta")
        t.add_column("Last Humidity", style="blue")
        for i, d in enumerate(found):
            lp = d["last_packet"]
            temp = _fmt_temp(lp)
            hum = f"{lp['humidity']}%" if "humidity" in lp else "—"
            t.add_row(
                str(i),
                d["model"],
                f"{d['id']} / {d['channel']}",
                str(d["count"]),
                temp,
                hum,
            )
        console.print(t)
    else:
        print(f"\n{'#':>3}  {'Model':<30} {'ID/Ch':<10} {'Pkts':>5}  {'Temp':>8}  {'Hum':>6}")
        print("-" * 70)
        for i, d in enumerate(found):
            lp = d["last_packet"]
            temp = _fmt_temp(lp)
            hum = f"{lp['humidity']}%" if "humidity" in lp else "—"
            print(f"{i:>3}  {d['model']:<30} {str(d['id']):<10} {d['count']:>5}  {temp:>8}  {hum:>6}")

    _print('\nEnter device numbers to use, comma-separated (e.g. [bold]0,2[/bold]), or [bold]all[/bold]:')
    while True:
        raw = (Prompt.ask("Selection", default="all") if RICH
               else input("Selection [all]: ").strip() or "all")
        if raw.strip().lower() == "all":
            return found
        try:
            indices = [int(x.strip()) for x in raw.split(",")]
            selected = [found[i] for i in indices if 0 <= i < len(found)]
            if selected:
                return selected
        except (ValueError, IndexError):
            pass
        _print("Invalid selection. Try again.", "red")


def _fmt_temp(pkt: dict) -> str:
    for k in ("temperature_C", "temperature_F", "temp_C", "temp_F"):
        if k in pkt:
            unit = "°C" if k.endswith("_C") else "°F"
            return f"{pkt[k]}{unit}"
    return "—"


# ---------------------------------------------------------------------------
# APRS / callsign config
# ---------------------------------------------------------------------------

def configure_aprs() -> dict:
    _rule("APRS Weather Beacon Configuration")
    _print("This configures the APRS-formatted output used by wx_beacon.py.\n")

    if RICH:
        callsign = Prompt.ask("Your callsign (e.g. W1AW-13)", default="N0CALL-13")
        latitude  = Prompt.ask("Latitude  (decimal, N positive)", default="38.0000")
        longitude = Prompt.ask("Longitude (decimal, E positive)", default="-84.5000")
        comment   = Prompt.ask("APRS comment string", default="RTL-SDR WX Beacon")
        interval  = IntPrompt.ask("Beacon interval (seconds)", default=300)
    else:
        callsign  = input("Callsign [N0CALL-13]: ").strip() or "N0CALL-13"
        latitude  = input("Latitude [38.0000]: ").strip() or "38.0000"
        longitude = input("Longitude [-84.5000]: ").strip() or "-84.5000"
        comment   = input("APRS comment [RTL-SDR WX Beacon]: ").strip() or "RTL-SDR WX Beacon"
        try:
            interval = int(input("Beacon interval seconds [300]: ").strip() or "300")
        except ValueError:
            interval = 300

    return {
        "callsign": callsign.upper(),
        "latitude": float(latitude),
        "longitude": float(longitude),
        "comment": comment,
        "beacon_interval_seconds": interval,
    }


# ---------------------------------------------------------------------------
# Logging / output config
# ---------------------------------------------------------------------------

def configure_logging() -> dict:
    _rule("Logging & Output Paths")

    defaults = {
        "raw_log": "wx_raw.log",
        "aprs_file": "wx_aprs.txt",
        "log_level": "INFO",
    }

    if RICH:
        raw_log   = Prompt.ask("Raw data log file path", default=defaults["raw_log"])
        aprs_file = Prompt.ask("APRS output file path",  default=defaults["aprs_file"])
        lvl       = Prompt.ask("Log level", choices=["DEBUG", "INFO", "WARNING"], default="INFO")
    else:
        raw_log   = input(f"Raw log path [{defaults['raw_log']}]: ").strip() or defaults["raw_log"]
        aprs_file = input(f"APRS file path [{defaults['aprs_file']}]: ").strip() or defaults["aprs_file"]
        lvl       = input("Log level [INFO]: ").strip().upper() or "INFO"

    return {
        "raw_log": raw_log,
        "aprs_file": aprs_file,
        "log_level": lvl,
    }


# ---------------------------------------------------------------------------
# Save / preview config
# ---------------------------------------------------------------------------

def save_config(config: dict) -> None:
    _rule("Configuration Summary")

    pretty = json.dumps(config, indent=2)
    if RICH:
        from rich.syntax import Syntax
        console.print(Syntax(pretty, "json", theme="monokai", line_numbers=False))
    else:
        print(pretty)

    _print(f"\n[bold green]Saving to:[/bold green] [cyan]{CONFIG_PATH}[/cyan]")
    CONFIG_PATH.write_text(pretty)
    _print("[bold green]✓ Configuration saved.[/bold green]\n")
    _print(f"  Run the beacon with:  [bold]python3 wx_beacon.py[/bold]")
    _print(f"  (or)                  [bold]python3 wx_beacon.py --config {CONFIG_PATH}[/bold]\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _banner()

    rtl433_bin = _check_rtl433()
    _print(f"  rtl_433 found at: [dim]{rtl433_bin}[/dim]\n")

    # 1. ISM band
    freq_mhz = select_ism_band()

    # 2. SDR hardware params
    sdr_params = select_sdr_params()

    # 3. Packet routing
    routing = configure_routing()

    # 4. Scan duration
    _rule("Scan Duration")
    if RICH:
        scan_secs = IntPrompt.ask(
            "How many seconds should the scan run?", default=30
        )
    else:
        try:
            scan_secs = int(input("Scan duration in seconds [30]: ").strip() or "30")
        except ValueError:
            scan_secs = 30

    # 5. Scan
    found = scan_for_stations(rtl433_bin, freq_mhz, sdr_params, scan_secs)

    # 6. Select devices
    selected = select_devices(found)

    # 7. APRS config
    aprs_cfg = configure_aprs()

    # 8. Logging config
    log_cfg = configure_logging()

    # 9. Assemble and save
    config = {
        "rtl433_bin": rtl433_bin,
        "frequency_mhz": freq_mhz,
        "sdr": sdr_params,
        "routing": routing,
        "devices": [
            {"model": d["model"], "id": d["id"], "channel": d["channel"]}
            for d in selected
        ],
        "aprs": aprs_cfg,
        "logging": log_cfg,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }

    save_config(config)


if __name__ == "__main__":
    main()

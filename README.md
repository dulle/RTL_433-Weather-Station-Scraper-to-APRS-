****Summary**
**

This script is a complete RTL-SDR weather station logger + APRS beacon transmitter. 
It listens to live JSON output from rtl_433, aggregates weather data from one or more sensors, 
and periodically generates a standard APRS weather packet that can be sent via multiple routes:

- File injection (for Direwolf, YAAC, aprx, etc.)
- APRS-IS (internet)
- RF via KISS/TCP (hardware/software TNCs)

****Core Purpose**
**
- Continuously decode 433 MHz weather sensors with an RTL-SDR.
- Log every raw packet.
- Build and transmit a clean APRS weather beacon every N seconds (default 300 s).
- Provide a nice live dashboard (_if rich is installed_).

****Main Features**
**
- Multi-route support: file-only, APRS-IS only, RF/KISS only, or both.
- Persistent connections with auto-reconnect for APRS-IS and KISS.
- Smart aggregation – merges readings from multiple sensors/devices into one coherent weather report.
- Full APRS WX format including position, wind (dir/speed/gust), temp (°F), humidity, rain (1 h / 24 h / midnight), pressure, and custom comment.
- Device filtering – only accept configured sensor models/IDs.
- Atomic file output for safe beacon injection.
- Rich live UI – real-time status table with temperature, wind, pressure, packet count, connection status, etc.
- Robust error handling – restarts rtl_433 if it dies, reconnects network links, graceful shutdown on SIGINT/SIGTERM.
- One-shot mode (--once) for testing or cron-style use.

**Architecture**
**
- **WXBeacon** - Main orchestrator, runs rtl_433, manages threads
- **WXAggregator** - Converts rtl_433 fields → standardized weather dict (handles °C/°F, m/s, km/h, etc.)
- **APRSBuilder** - Builds the exact APRS position+weather string
- **APRSISSender** - TCP connection to APRS-IS Tier-2 server (login, keepalive, reconnect)
- **NetworkKISSSender + KISSFrame** - KISS-over-TCP AX.25 UI framing for RF transmission
- **RawLogger** - JSON rolling log of every received packet
- **APRSWritter** - Atomic write of latest beacon to disk
- **_build_status_table** - Rich live dashboard

**Usage**

- python3 wx_beacon.py                        # normal run (uses wx_station.json)
- python3 wx_beacon.py --config my.json      # custom config
- python3 wx_beacon.py --once                # collect one interval then exit

___________________________________________________________________

**RUNNING IT AS A SERVICE**

There are a few ways, depending on your OS. What are you running on? 
User: Linux (Raspberry Pi / Ubuntu / Debian)

**systemd** is the right tool — it's built into every modern Raspberry Pi OS, Ubuntu, and Debian. Here's how to set it up:

**1. Find your paths first**

which python3        # e.g. /usr/bin/python3
which rtl_433        # e.g. /usr/local/bin/rtl_433
pwd                  # run this from your project folder to get the full path

**2. Create the service file**

sudo nano /etc/systemd/system/wx-beacon.service

**Paste this** — edit the paths to match your setup:

[Unit]
Description=RTL-SDR Weather Station APRS Beacon
#- Wait for network before starting (needed for APRS-IS mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/wx-beacon
ExecStart=/usr/bin/python3 /home/pi/wx-beacon/wx_beacon.py --config /home/pi/wx-beacon/wx_station.json

#- Restart automatically if it crashes
Restart=always
RestartSec=10

#- Give rtl_433 time to release the USB device on stop
TimeoutStopSec=15

#- Log stderr/stdout to the journal (read with: journalctl -u wx-beacon)
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wx-beacon

[Install]
WantedBy=multi-user.target


User=pi — change to your actual username (whoami to check). Never run as root unless you have to.
WorkingDirectory — the folder containing wx_beacon.py and wx_station.json.

**3. Enable and start it**

#- Reload systemd so it sees the new file
sudo systemctl daemon-reload

#- Enable it — this makes it auto-start on every boot
sudo systemctl enable wx-beacon

#- Start it right now without rebooting
sudo systemctl start wx-beacon

#- Check it's running
sudo systemctl status wx-beacon

**4. Useful commands**

#- Live log output (Ctrl+C to exit)
journalctl -u wx-beacon -f

#- Last 100 lines of log
journalctl -u wx-beacon -n 100

#- Stop the service
sudo systemctl stop wx-beacon

#- Restart after making changes to the script
sudo systemctl restart wx-beacon

#- Disable auto-start (but don't delete the file)
sudo systemctl disable wx-beacon

**5. RTL-SDR USB permissions (common issue**)

sudo usermod -aG plugdev pi
#- Then reboot, or run:
sudo udevadm control --reload-rules && sudo udevadm trigger

**6. Verify it survives a reboot**

sudo reboot

#- After it comes back up:
sudo systemctl status wx-beacon

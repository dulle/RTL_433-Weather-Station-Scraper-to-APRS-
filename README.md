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

****Architecture (key classes)
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
python3 wx_beacon.py                        # normal run (uses wx_station.json)
python3 wx_beacon.py --config my.json      # custom config
python3 wx_beacon.py --once                # collect one interval then exit


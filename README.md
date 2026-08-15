# Radio Dispatcher Console

A web-based dispatcher console for HamVoIP / AllStar repeater nodes running DVSwitch. Provides real-time monitoring of DMR and analog activity, talkgroup tuning, network switching, Allstar node control, Trunk Recorder call playback, and RTL-SDR scanner integration — all from a browser on your local network.

---

## Features

- **Live TX monitor** — callsign, DMR ID, talkgroup, and network displayed in real time via SSE push
- **DMR talkgroup tuning** — tune BrandMeister or TGIF talkgroups with name lookup and favorite management
- **Network switching** — one-click toggle between BrandMeister and TGIF via DVSwitch scripts
- **Allstar / IAX2 control** — connects to your Asterisk node as an IAX2 client; link/unlink remote nodes, send DTMF commands, monitor connected nodes
- **Allstar audio** — streams decoded ulaw audio from the IAX2 session to the browser
- **Service management** — restart STFU, Analog Bridge, or MMDVM Bridge directly from the UI (API-key protected)
- **Last heard log** — recent activity merged from MMDVM Bridge and Analog Bridge logs; timestamps shown in browser local time
- **Live log tail** — view the last N lines of any service log in the browser, including the watchdog log
- **DMR and Allstar favorites** — save and recall talkgroups (per network) and Allstar nodes, persisted across restarts
- **Node connect/disconnect toasts** — centered popup alerts for Allstar node link/unlink events with configurable duration and optional audio chimes
- **DMR talkgroup change toasts** — popup alert when the active talkgroup changes
- **Trunk Recorder integration** — receives call uploads from TR, plays audio in-browser with a scrollable call log, auto-play mode, per-system display labels
- **RTL-SDR scanner integration** — live relay of an rtl-airband-scanner instance with full channel management, audio playback, and synchronized display (see below)
- **Audio controls overlay** — accessible via the 🎧 button in the title bar; per-source volume, LP/HP filters, and squelch tail

---

## RTL-SDR Scanner Integration

The dispatcher connects to a separate [rtl-airband-scanner](https://github.com/mostlychris/rtl-scanner) instance over its WebSocket control API and HTTP audio stream, relaying everything to the browser.

### Scanner bar

The SDR bar shows the current scanner state at a glance:

| Element | Meaning |
|---|---|
| Pulse dot | Green when squelch is open (signal active) |
| Frequency | Active channel frequency in MHz |
| Label | Channel label; shows `🔒` suffix when held |
| **SCANNING** | No active signal / scanner is sweeping |
| dB badge | Signal level relative to threshold |
| HOLD badge | Displayed when scanner is locked to a frequency |

### Scanner bar buttons

| Button | Action |
|---|---|
| ⏭ Next | Force immediate advance to next channel |
| ⊘ Skip | Toggle current frequency in/out of scan rotation (highlights orange when skipped) |
| 🔒 Hold | Lock scanner to current frequency; click again to release |
| ⊞ Channels | Open channel management modal |

### Channel management modal

- **Add** — add a new channel with frequency, label, bank, mode, channel width, CTCSS/PL tone, sub-audio HPF, squelch RMS, and gain
- **Edit** — inline edit any channel field
- **Skip toggle** (⊘/▶) — include or exclude a channel from the scan rotation
- **Hold toggle** (🔒/🔓) — lock or release hold on a specific channel
- **Delete** — remove a channel
- **Scan banks** — enable/disable scan banks; bank toggle buttons appear automatically when more than one bank is defined

### Audio

SDR audio is streamed as WAV via the dispatcher's HTTP proxy and played through the Web Audio API with:

- **Volume** slider
- **LP Cut** — low-pass filter cutoff (500 Hz – 8 kHz, default 3 kHz)
- **HP Cut** — high-pass filter cutoff (0 – 600 Hz, default off)

Display activation is automatically synchronized to audio playback using the scanner's `audio_stats` stream — no manual lag adjustment needed. The display shows the active channel when you hear it, and clears when the audio tail drains.

### Configuration

```python
# Base URL of the rtl-airband-scanner app (no trailing slash)
SDR_SCANNER_URL = 'http://192.168.1.100:8080'
```

The dispatcher connects to `SDR_SCANNER_URL/ws` for control events and proxies `SDR_SCANNER_URL/stream` for audio.

---

## Requirements

- Python 3.10+
- HamVoIP / Asterisk node with DVSwitch (MMDVM Bridge, Analog Bridge, STFU)
- IAX2 peer configured in `/etc/asterisk/iax.conf` (for Allstar section)
- `websocket-client` Python package (for SDR scanner relay)

```bash
pip install flask flask-sock websocket-client
```

---

## Installation

### Quick install (systemd service)

```bash
git clone https://github.com/mostlychris/dispatcher
cd dispatcher
cp config.example.py config.py
# Edit config.py — see Configuration below
bash install.sh
```

`install.sh` writes a systemd unit file, grants the required `sudo systemctl restart` permissions via `/etc/sudoers.d/dispatcher`, enables the service at boot, and starts it immediately.

### Manual run

```bash
python app.py
```

UI available at `http://<host>:9090` (or whatever `LISTEN_PORT` is set to in `config.py`).

---

## Configuration

Copy `config.example.py` to `config.py` and fill in your values. `config.py` is gitignored and never committed.

```python
# Audio monitor WebSocket URL (leave empty to disable)
AUDIO_WS_URL = ''

# Allstar / IAX2 — must match your iax.conf peer
ALLSTAR_HOST   = '127.0.0.1'
ALLSTAR_PORT   = 4569
ALLSTAR_USER   = 'iaxrpt'
ALLSTAR_SECRET = 'your_secret'
ALLSTAR_NODE   = '12345'

# API key — required by the browser for service-restart endpoints
API_KEY = 'generate_a_random_string'

# Flask listen port
LISTEN_PORT = 9090

# DVSwitch script paths
DVSWITCH_SCRIPT       = '/opt/MMDVM_Bridge/dvswitch.sh'
CONNECT_TGIF_SCRIPT   = '/opt/MMDVM_Bridge/connectTGIF.sh'
CONNECT_BM_SCRIPT     = '/opt/MMDVM_Bridge/connectBM.sh'

# systemd service names
STFU_SERVICE          = 'stfu.service'
ANALOG_BRIDGE_SERVICE = 'analog_bridge.service'
MMDVM_SERVICE         = 'mmdvm_bridge.service'

# DVSwitchPlayer WebSocket port (browser-side audio player)
DVSWITCHPLAYER_PORT = 8080

# Trunk Recorder
TR_API_KEY  = 'CHANGE_ME'
TR_AUDIO_DIR = '/var/lib/dispatcher/tr-audio'
TR_MAX_CALLS = 5000
TR_SYSTEMS = {
    # 'mysystem': 'My County P25',
}

# RTL-SDR scanner (no trailing slash)
SDR_SCANNER_URL = 'http://192.168.1.100:8080'
```

Generate a random `API_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_hex(16))"
```

### Allstar IAX2 peer (`/etc/asterisk/iax.conf`)

Add a peer for the dispatcher to connect as:

```ini
[iaxrpt]
type=user
context=iax-client
auth=md5
secret=your_secret
host=dynamic
disallow=all
allow=ulaw
```

---

## Trunk Recorder integration

The dispatcher accepts call uploads directly from Trunk Recorder using the same multipart HTTP format as rdio-scanner, so no custom scripts are needed — just point TR's built-in uploader at the dispatcher.

### Trunk Recorder config (`config.json`)

```json
"uploaders": [
  {
    "url": "http://<dispatcher-ip>:9090",
    "key": "CHANGE_ME"
  }
]
```

`CHANGE_ME` must match `TR_API_KEY` in `config.py`. The dispatcher receives calls at `POST /api/call-upload` (the same path rdio-scanner uses).

### Adding multiple systems

Each call upload includes a `short_name` field from Trunk Recorder. The dispatcher registers systems automatically on first upload. You can give them friendly display names in `config.py`:

```python
TR_SYSTEMS = {
    'countyp25': 'County P25',
    'fire':      'Fire Dispatch',
}
```

### What happens on each call

1. Audio file is saved to `TR_AUDIO_DIR`
2. Call metadata is added to the in-memory ring buffer (newest first, max `TR_MAX_CALLS`)
3. An SSE event is pushed to all connected browsers
4. The Trunk status bar flashes and updates with system + talkgroup
5. If **Auto-play** is enabled, audio plays immediately in the browser

### Audio storage

Audio files are stored in `TR_AUDIO_DIR` (default `/var/lib/dispatcher/tr-audio`). When the ring buffer reaches `TR_MAX_CALLS`, the oldest call's audio file is deleted automatically.

---

## Watchdog

`watchdog.py` is an optional cron script that monitors the three DVSwitch services and restarts any that have died. It writes a structured log to `/var/log/dispatcher-watchdog.log`, which is visible in the app's log viewer under the **Watchdog** tab.

Install the cron job on the dispatcher host:

```bash
# Run as root (needs systemctl restart privileges)
*/2 * * * * /usr/bin/python3 /opt/MMDVM_Bridge/dispatcher/watchdog.py
```

The watchdog only acts on services that `systemctl is-active` reports as inactive — it does **not** restart services just because there is no active radio traffic (USRP silence is normal and expected).

> **Note:** If your logrotate configs use `systemctl reload` for STFU, Analog Bridge, or MMDVM Bridge, change them to `systemctl restart`. These services exit cleanly on `SIGINT` (exit code 0), which bypasses `Restart=on-failure` in systemd and leaves them dead until manually restarted.

---

## Architecture

The entire application is a single file (`app.py`) — a Flask server with the complete HTML/CSS/JS frontend embedded as a string and rendered by the `/` route.

### UI layout

| Section | Status bar shows | Controls |
|---|---|---|
| DMR | TX pulse, mode badge, connection state, active TG | Network switch, TG tune, favorites |
| Allstar | RX dot, connection badge, local node, linked node | Connect/disconnect, PTT, node linking, DTMF |
| Trunk Recorder | Call pulse, system, talkgroup, duration | Auto-play toggle, volume, call log |
| SDR Scanner | Signal pulse, frequency, label, dB, HOLD badge | Next, Skip, Hold, Channels modal |

### Backend layers

| Component | Description |
|---|---|
| `usrp_listener()` | Daemon thread; binds UDP 31002, receives USRP PTT packets from Analog Bridge |
| `tg_refresh_loop()` | Daemon thread; reloads TG name lists from `/tmp` every 5 minutes |
| `AllstarManager` | Wraps `IAX2Client`; handles connect/disconnect, fans PCM audio to WebSocket listeners |
| `IAX2Client` (`iax2.py`) | Self-contained IAX2 client — NEW → CALLTOKEN → AUTHREQ/AUTHREP (MD5) → ACCEPT |
| `_sdr_relay_loop()` | Daemon thread; maintains WebSocket connection to scanner, relays events to SSE clients, mirrors scanner state |
| SSE stream (`/api/stream`) | Fan-out queue per browser client; pushes real-time events for all sources |

### Data files

| File | Purpose |
|---|---|
| `favorites.json` | Per-network favorite talkgroups (persisted) |
| `as_favorites.json` | Saved Allstar node favorites (persisted) |
| `last_state.json` | Last tuned TG, network, and time (survives restarts) |
| `tg_names_cache.json` | Snapshot of TG names from last successful load |

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Single-page UI |
| GET | `/api/stream` | SSE stream (all real-time events) |
| GET | `/api/status` | Mode, active TG, service states, connection state |
| GET | `/api/lastheard` | 20 most recent heard entries |
| GET | `/api/log/<key>` | Tail log; keys: `mmdvm`, `analog`, `stfu`, `watchdog`; `?lines=N` (max 500) |
| GET | `/api/favorites` | DMR favorites dict |
| POST | `/api/favorites` | Add DMR favorite `{tg, network}` |
| DELETE | `/api/favorites/<network>/<tg>` | Remove a DMR favorite |
| GET | `/api/as_favorites` | Allstar node favorites list |
| POST | `/api/as_favorites` | Add Allstar favorite `{node, label}` |
| DELETE | `/api/as_favorites/<node>` | Remove an Allstar favorite |
| GET | `/api/tune_history` | In-memory tune history (cleared on restart) |
| POST | `/api/tgif` | Switch to TGIF network |
| POST | `/api/bm` | Switch to BrandMeister network |
| POST | `/api/tune` | Tune talkgroup `{tg}` |
| POST | `/api/restart` | Restart STFU service *(requires X-Api-Key)* |
| POST | `/api/restart_ab` | Restart Analog Bridge *(requires X-Api-Key)* |
| POST | `/api/restart_mmdvm` | Restart MMDVM Bridge *(requires X-Api-Key)* |
| GET | `/api/allstar/status` | Allstar connection state and active RX flag |
| GET | `/api/allstar/nodes` | List currently linked nodes |
| POST | `/api/allstar/connect` | Connect to configured Allstar node |
| POST | `/api/allstar/disconnect` | Disconnect IAX2 session |
| POST | `/api/allstar/link` | Link remote node `{node, mode}` (`monitor`/`transceive`) |
| POST | `/api/allstar/unlink` | Unlink remote node `{node}` |
| POST | `/api/allstar/command` | Send DTMF command string `{cmd}` |
| GET | `/api/debug/abinfo` | Dump raw ABInfo JSON |
| WS | `/ws/allstar-audio` | Raw 16-bit PCM at 8 kHz (ulaw decoded) |
| POST | `/api/call-upload` | Trunk Recorder call upload (multipart: `key`, `audio`, `call` JSON) |
| GET | `/api/tr/calls` | Recent TR call list (newest first, max `TR_MAX_CALLS`) |
| GET | `/api/tr/systems` | Registered TR systems with display labels |
| GET | `/api/tr/audio/<filename>` | Serve a stored TR audio file |
| GET | `/api/sdr/state` | Current SDR scanner state snapshot |
| GET | `/api/sdr/stream` | Proxied WAV audio stream from scanner |
| POST | `/api/sdr/skip` | Toggle frequency in/out of scan rotation `{freq}` |
| POST | `/api/sdr/resume` | Force immediate scan advance (next channel) |
| POST | `/api/sdr/hold` | Toggle hold on a frequency `{freq}` |
| PUT | `/api/sdr/channel` | Add or edit a channel |
| DELETE | `/api/sdr/channel/<freq>` | Remove a channel |
| POST | `/api/sdr/bank` | Enable/disable a scan bank `{bank, enabled}` |

Endpoints marked *requires X-Api-Key* must include the header `X-Api-Key: <your API_KEY>`.

---

## Removing the Service

```bash
bash uninstall.sh
```

# Radio Dispatcher Console

A web-based dispatcher console for HamVoIP / AllStar repeater nodes running DVSwitch. Provides real-time monitoring of DMR and analog activity, talkgroup tuning, network switching, and Allstar node control — all from a browser on your local network.

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
- **Floating audio controls** — accessible at any screen size via an overlay button; controls volume, HP/LP filters, squelch tail, and Allstar volume

---

## Requirements

- Python 3.10+
- HamVoIP / Asterisk node with DVSwitch (MMDVM Bridge, Analog Bridge, STFU)
- IAX2 peer configured in `/etc/asterisk/iax.conf` (for Allstar section)

```bash
pip install flask flask-sock
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

Both the DMR and Allstar sections are **status bars with icon-button modals** rather than collapsible panels:

| Section | Status bar shows | ⚙ Controls modal | ★ Quick Tune modal |
|---|---|---|---|
| DMR | TX pulse, mode badge, connection state, active TG | Network switch, TG tune, favorites management | Scrollable favorites grid + recent tune history |
| Allstar | RX dot, connection badge, local node, linked node | Connect/disconnect/audio, PTT/mic, node linking, command, connected nodes, alert config | Saved node favorites with add/remove and quick-connect |

### Backend layers

| Component | Description |
|---|---|
| `usrp_listener()` | Daemon thread; binds UDP 31002, receives USRP PTT packets from Analog Bridge, pushes `tx_start`/`tx_end` SSE events |
| `tg_refresh_loop()` | Daemon thread; reloads TG name lists from `/tmp` every 5 minutes |
| `AllstarManager` | Wraps `IAX2Client`; handles connect/disconnect, fans PCM audio to WebSocket listeners, parses `L` TEXT frames for connected-node list |
| `IAX2Client` (`iax2.py`) | Self-contained IAX2 client — NEW → CALLTOKEN → AUTHREQ/AUTHREP (MD5) → ACCEPT; sends DTMF as TEXT frames; decodes ulaw PCM |
| SSE stream (`/api/stream`) | Fan-out queue per browser client; pushes real-time TX events |

### Mode detection

`current_mode` is updated by `get_status()` based on which service is running:
- `stfu.service` active → **BrandMeister**
- `mmdvm_bridge.service` active → **TGIF**

### Data files

| File | Purpose |
|---|---|
| `favorites.json` | Per-network favorite talkgroups (persisted) |
| `as_favorites.json` | Saved Allstar node favorites (persisted) |
| `last_state.json` | Last tuned TG, network, and time (survives restarts) |
| `tg_names_cache.json` | Snapshot of TG names from last successful load |

### Runtime paths

| Path | Used for |
|---|---|
| `/tmp/ABInfo_31001.json` | Active mode detection |
| `/tmp/TGList_BM.txt` | BrandMeister TG name list |
| `/tmp/TGList_TGIF.txt` | TGIF TG name list |
| `/tmp/TGIF_node_list.txt` | TGIF node list |
| `/var/lib/mmdvm/DMRIds.dat` | DMR ID → callsign lookup |
| `/var/log/mmdvm/MMDVM_Bridge-{date}.log` | Last-heard and TX detection |
| `/var/log/dvswitch/Analog_Bridge-{date}.log` | Last-heard |
| `/var/log/dvswitch/STFU.log` | Last-heard (BM) |
| `/var/log/dispatcher-watchdog.log` | Watchdog restart events |

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Single-page UI |
| GET | `/api/stream` | SSE stream (`tx_start` / `tx_end` events) |
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

Endpoints marked *requires X-Api-Key* must include the header `X-Api-Key: <your API_KEY>`.

---

## Removing the Service

```bash
bash uninstall.sh
```

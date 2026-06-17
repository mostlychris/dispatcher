# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Application

```bash
python app.py
```

Web UI available at `http://<host>:9090`. The app expects to run on the Linux radio host — file paths, systemctl commands, and socket ports are hardcoded for that environment.

## Configuration

Copy `config.example.py` to `config.py` (gitignored) and set `AUDIO_WS_URL` to the WSS endpoint for the RX audio monitor. Leave it empty to disable the audio button.

## Architecture

**Single-file monolith:** All backend logic, API routes, and the complete frontend HTML/CSS/JS are in [app.py](app.py). The frontend is an inline HTML string (`HTML`) rendered by the `/` route via `render_template_string`.

**Threading model:**
- `usrp_listener()` — daemon thread, binds UDP port 31002, receives USRP protocol packets for PTT start/stop. Sends a registration frame to port 31001 every 30 seconds to maintain the Analog Bridge connection.
- `tg_refresh_loop()` — daemon thread, reloads TG name lists from `/tmp` every 5 minutes.
- SSE push: `sse_clients` is a list of `queue.Queue` objects, one per connected browser. `push_event()` fans out to all queues under `sse_lock`.

**Mode detection:** `current_mode` is a module-level global updated by `get_status()` based on which systemd service is running — `stfu.service` running → BrandMeister, `mmdvm_bridge.service` running → TGIF. `get_active_mode()` reads this global; all TG lookups branch on it.

**TX detection flow:** On PTT=1, `usrp_listener` sleeps 200 ms then calls `get_current_tx_from_log()`, which tails the relevant log file (STFU.log for BM, MMDVM_Bridge log for TGIF) and regex-matches the most recent transmission entry to get callsign/TG. PTT=0 clears the active TX after a 3-second hold.

**TG name data:** Loaded from `/tmp/TGList_BM.txt`, `/tmp/TGList_TGIF.txt`, and `/tmp/TGIF_node_list.txt` into `tg_cache_bm` / `tg_cache_tgif` dicts. Falls back to `tg_names_cache.json` (local file) when `/tmp` sources are unavailable.

**Persistent state files** (written next to `app.py`):
- `favorites.json` — per-network favorite talkgroups `{"BM": [...], "TGIF": [...]}`
- `last_state.json` — last tuned TG, network, and time (survives restarts)
- `tg_names_cache.json` — snapshot of TG name dicts from last successful `/tmp` load

## Key Constants and Paths

All configurable paths are module-level constants near the top of `app.py`:

| Constant | Path |
|---|---|
| `ABINFO_ACTIVE` | `/tmp/ABInfo_31001.json` |
| `TGLIST_BM` | `/tmp/TGList_BM.txt` |
| `TGLIST_TGIF` | `/tmp/TGList_TGIF.txt` |
| `TGIF_NODE_LIST` | `/tmp/TGIF_node_list.txt` |
| `DMRIDS_FILE` | `/var/lib/mmdvm/DMRIds.dat` |
| Log dirs | `/var/log/mmdvm/`, `/var/log/dvswitch/` |

USRP UDP: transmit to `127.0.0.1:31001`, listen on `:31002`.

External scripts invoked at runtime:
- `/opt/MMDVM_Bridge/connectTGIF.sh` — switches to TGIF network
- `/opt/MMDVM_Bridge/connectBM.sh` — switches to BrandMeister network
- `/opt/MMDVM_Bridge/dvswitch.sh tune <TG>` — tunes to a talkgroup

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Main single-page UI |
| GET | `/api/stream` | SSE stream for real-time TX events (`tx_start` / `tx_end`) |
| GET | `/api/status` | Mode, active TG, service states, connection state |
| GET | `/api/lastheard` | 20 most recent heard entries (merged from all log sources) |
| GET | `/api/log/<key>` | Tail log file; keys: `mmdvm`, `analog`, `stfu`; `?lines=N` (max 500) |
| GET | `/api/favorites` | Return favorites dict |
| POST | `/api/favorites` | Add favorite `{tg, network}` |
| DELETE | `/api/favorites/<network>/<tg>` | Remove a favorite |
| GET | `/api/tune_history` | In-memory tune history (up to 20, cleared on restart) |
| POST | `/api/tgif` | Switch network to TGIF |
| POST | `/api/bm` | Switch network to BrandMeister |
| POST | `/api/restart` | Restart `stfu.service` |
| POST | `/api/restart_ab` | Restart `analog_bridge.service` (also re-sends USRP registration) |
| POST | `/api/restart_mmdvm` | Restart `mmdvm_bridge.service` |
| POST | `/api/tune` | Tune to a talkgroup `{tg}` |
| GET | `/api/debug/abinfo` | Dump raw ABInfo JSON for debugging |

## Frontend Notes

The frontend is pure vanilla JS with no build step. Audio playback uses the `DVSwitchPlayer` class from `static/pcm-player.min.js`, initialized with the `AUDIO_WS_URL` from config. The audio chain adds a high-pass BiquadFilter, a peaking EQ (presence), and a DynamicsCompressor on top of the player's built-in gain node. Volume, high-pass cutoff, and presence settings are persisted in `localStorage`.

## Allstar / IAX2

`iax2.py` is a self-contained IAX2 client (RX-only). It handles the full call setup sequence: NEW → optional CALLTOKEN retry → AUTHREQ/AUTHREP (MD5) → ACCEPT. Received VOICE frames and mini frames are decoded from ulaw to 16-bit PCM via `audioop.ulaw2lin` (stdlib) with a pure-Python fallback for Python 3.13+.

`AllstarManager` in `app.py` wraps the client, fans PCM out to a `queue.Queue` per connected WebSocket client, and exposes connect/disconnect/status. The `/ws/allstar-audio` WebSocket endpoint (flask-sock) streams raw 16-bit PCM at 8 kHz, directly consumable by the existing `PCMPlayer` from `static/pcm-player.min.js`.

Node linking uses `asterisk -rx "rpt fun <local> *3<remote>"` (monitor) or `*5` (transceive) / `*1` (unlink), the same `run()` helper as other service control.

Config needed in `config.py`: `ALLSTAR_HOST`, `ALLSTAR_PORT`, `ALLSTAR_USER`, `ALLSTAR_SECRET`, `ALLSTAR_NODE`.

## Dependencies

```bash
pip install flask flask-sock
```

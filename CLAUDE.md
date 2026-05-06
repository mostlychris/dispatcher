# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Flask-based Radio Dispatcher Console for monitoring and controlling DMR (Digital Mobile Radio) networks — specifically MMDVM Bridge, TGIF, and BrandMeister. It runs on a Linux system (Raspberry Pi or similar) managing radio gateway services.

## Running the Application

```bash
python app.py
```

Web UI available at `http://<host>:9090`. The app expects to run on the Linux radio host — file paths, systemctl commands, and socket ports are hardcoded for that environment.

## Architecture

**Single-file monolith:** All backend logic, API routes, and the complete frontend HTML/CSS/JS are in [app.py](app.py). The frontend is an inline Jinja2 template string returned by the `/` route.

**Threading model:**
- `usrp_listener()` runs as a daemon thread, listening on UDP port 31002 for USRP protocol packets indicating PTT start/stop
- Real-time TX state is shared via `active_tx` dict and broadcast to SSE clients via `sse_clients` (a list of `queue.Queue` objects)
- `/api/stream` is the SSE endpoint — each connected browser gets its own queue added to `sse_clients`

**Network mode detection:** `get_active_mode()` reads `/tmp/ABInfo_31001.json` (written by Analog Bridge) to determine whether the system is in TGIF or BrandMeister mode. Talkgroup lookups branch on this.

**Data flow for "last heard":** `get_last_heard()` parses three log files in parallel — MMDVM Bridge log (`/var/log/mmdvm/MMDVM-*.log`), Analog Bridge log (`/var/log/dvswitch/AnalogBridge.log`), and STFU log (`/var/log/dvswitch/STFU.log`) — merges by timestamp, and returns the 20 most recent entries.

## Key Constants and Paths

All configurable paths are defined as module-level constants near the top of `app.py`:

| Constant | Path |
|---|---|
| `ABINFO_ACTIVE` | `/tmp/ABInfo_31001.json` |
| `TGLIST_BM` | `/tmp/TGList_BM.txt` |
| `TGLIST_TGIF` | `/tmp/TGList_TGIF.txt` |
| `DMRIDS_FILE` | `/var/lib/mmdvm/DMRIds.dat` |
| Log dirs | `/var/log/mmdvm/`, `/var/log/dvswitch/` |

USRP UDP: transmit to `127.0.0.1:31001`, listen on `:31002`.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Main single-page UI |
| GET | `/api/stream` | SSE stream for real-time TX events |
| GET | `/api/status` | Mode, active TG, service states |
| GET | `/api/lastheard` | 20 most recent heard entries |
| GET | `/api/log/<key>` | Tail log file (`mmdvm`, `analog`, `stfu`) |
| POST | `/api/tgif` | Switch network to TGIF |
| POST | `/api/bm` | Switch network to BrandMeister |
| POST | `/api/restart` | Restart STFU service via systemctl |
| POST | `/api/tune` | Tune to a specific talkgroup |

## Dependencies

Only Flask is required beyond the standard library:

```bash
pip install flask
```

from flask import Flask, jsonify, request, render_template_string, Response
from flask_sock import Sock
import subprocess
import json
import os
import re
import socket
import struct
import threading
import time
import queue
from datetime import datetime
import logging
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(name)s %(levelname)s %(message)s')
logging.getLogger('werkzeug').setLevel(logging.WARNING)  # silence Flask request noise

from iax2 import IAX2Client

app  = Flask(__name__)
sock = Sock(app)

@app.after_request
def add_cache_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma']        = 'no-cache'
    response.headers['Expires']       = '0'
    return response

try:
    from config import AUDIO_WS_URL
except ImportError:
    AUDIO_WS_URL = ''

try:
    from config import ALLSTAR_HOST
except ImportError:
    ALLSTAR_HOST = '127.0.0.1'
try:
    from config import ALLSTAR_PORT
except ImportError:
    ALLSTAR_PORT = 4569
try:
    from config import ALLSTAR_USER
except ImportError:
    ALLSTAR_USER = 'iaxrpt'
try:
    from config import ALLSTAR_SECRET
except ImportError:
    ALLSTAR_SECRET = ''
try:
    from config import ALLSTAR_NODE
except ImportError:
    ALLSTAR_NODE = ''


FAVORITES_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'favorites.json')
LAST_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'last_state.json')
TG_NAMES_CACHE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tg_names_cache.json')

ABINFO_ACTIVE  = '/tmp/ABInfo_31001.json'
TGLIST_BM      = '/tmp/TGList_BM.txt'
TGLIST_TGIF    = '/tmp/TGList_TGIF.txt'
TGIF_NODE_LIST = '/tmp/TGIF_node_list.txt'
DMRIDS_FILE    = '/var/lib/mmdvm/DMRIds.dat'
USRP_HOST      = '127.0.0.1'
USRP_PORT      = 31001
USRP_LISTEN    = 31002

LOG_FILES = {
    'mmdvm':  '/var/log/mmdvm/MMDVM_Bridge-{date}.log',
    'analog': '/var/log/dvswitch/Analog_Bridge-{date}.log',
    'stfu':   '/var/log/dvswitch/STFU.log',
}

# -------------------------
# ACTIVE TX STATE
# -------------------------
active_tx = {
    "active":   False,
    "callsign": "",
    "dmr_id":   "",
    "tg":       "",
    "tg_name":  "",
    "started":  "",
}

usrp_state = {
    "connected":     False,
    "registered":    False,
    "last_packet":   0,
    "last_reg_sent": 0,
}

sse_clients = []
sse_lock    = threading.Lock()

def push_event(data):
    with sse_lock:
        for q in sse_clients:
            try:
                q.put_nowait(data)
            except:
                pass

# -------------------------
# TG NAME LOOKUP
# -------------------------
tg_cache_bm   = {}
tg_cache_tgif = {}

# current_mode is updated by get_status() based on service state (not ABInfo).
# get_active_mode() reads it so callers never need to shell out themselves.
current_mode = "TGIF"

def load_tg_names():
    global tg_cache_bm, tg_cache_tgif
    fresh_bm, fresh_tgif = {}, {}

    try:
        with open(TGLIST_BM) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split(';')
                if len(parts) >= 3:
                    fresh_bm[parts[0].strip()] = parts[2].strip()
    except Exception as e:
        print(f"Warning: could not load BM TG names: {e}")

    try:
        with open(TGLIST_TGIF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('TG'):
                    continue
                parts = line.split(',', 1)
                if len(parts) >= 2:
                    fresh_tgif[parts[0].strip()] = parts[1].strip()
    except Exception as e:
        print(f"Warning: could not load TGIF TG names: {e}")

    try:
        with open(TGIF_NODE_LIST) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('TG'):
                    continue
                parts = line.split('|||', 1)
                if len(parts) >= 2:
                    tg_id = parts[0].strip()
                    name  = parts[1].strip()
                    if tg_id not in fresh_tgif and name:
                        fresh_tgif[tg_id] = name
    except Exception as e:
        print(f"Warning: could not load TGIF node list: {e}")

    if fresh_bm or fresh_tgif:
        tg_cache_bm   = fresh_bm
        tg_cache_tgif = fresh_tgif
        try:
            with open(TG_NAMES_CACHE, 'w') as f:
                json.dump({"bm": tg_cache_bm, "tgif": tg_cache_tgif}, f)
        except Exception as e:
            print(f"Warning: could not save TG names cache: {e}")
        print(f"Loaded {len(tg_cache_bm)} BM TGs, {len(tg_cache_tgif)} TGIF TGs from /tmp")
    else:
        try:
            with open(TG_NAMES_CACHE) as f:
                data = json.load(f)
            tg_cache_bm   = data.get("bm",   {})
            tg_cache_tgif = data.get("tgif", {})
            print(f"Loaded {len(tg_cache_bm)} BM TGs, {len(tg_cache_tgif)} TGIF TGs from local cache")
        except Exception as e:
            print(f"Warning: no TG names available ({e})")

def get_active_mode():
    return current_mode

def _tg_norm(tg_id):
    s = str(tg_id).strip()
    return str(int(s)) if s.isdigit() else s

def lookup_tg(tg_id):
    mode = get_active_mode()
    if mode == "BrandMeister" and not tg_cache_bm:
        load_tg_names()
    elif mode != "BrandMeister" and not tg_cache_tgif:
        load_tg_names()
    tg = _tg_norm(tg_id)
    if mode == "BrandMeister":
        return tg_cache_bm.get(tg, '')
    else:
        return tg_cache_tgif.get(tg, '')

def lookup_tg_by_source(tg_id, source):
    tg = _tg_norm(tg_id)
    if source == 'BM':
        return tg_cache_bm.get(tg, '')
    return tg_cache_tgif.get(tg, '')

# -------------------------
# DMR ID LOOKUP
# -------------------------
dmrid_cache = {}

def load_dmr_ids():
    global dmrid_cache
    dmrid_cache = {}
    try:
        with open(DMRIDS_FILE, errors='replace') as f:
            for line in f:
                parts = line.strip().split(None, 2)
                if len(parts) >= 2:
                    dmrid_cache[parts[0]] = parts[1]
    except Exception as e:
        print(f"Warning: could not load DMR IDs: {e}")

def lookup_dmrid(dmr_id):
    if not dmrid_cache:
        load_dmr_ids()
    return dmrid_cache.get(str(dmr_id), str(dmr_id))

# -------------------------
# CURRENT TX FROM LOG
# -------------------------
def get_current_tx_from_log():
    today = datetime.now().strftime('%Y-%m-%d')
    mode  = get_active_mode()

    if mode == "BrandMeister":
        log_path     = '/var/log/dvswitch/STFU.log'
        pattern      = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*src\s*=\s*(\d+).*dst\s*=\s*(\d+)'
        use_callsign = False
    else:
        log_path     = f'/var/log/mmdvm/MMDVM_Bridge-{today}.log'
        pattern      = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*voice header from (\S+) to TG (\d+)'
        use_callsign = True

    try:
        result = subprocess.run(
            ['tail', '-n', '50', log_path],
            capture_output=True, timeout=3
        )
        text = result.stdout.decode('utf-8', errors='replace')
        for line in reversed(text.splitlines()):
            m = re.search(pattern, line)
            if m:
                if use_callsign:
                    tg = m.group(3)
                    return {"time": m.group(1), "callsign": m.group(2),
                            "dmr_id": "", "tg": tg, "tg_name": lookup_tg(tg)}
                else:
                    dmr_id = m.group(2)
                    tg     = m.group(3)
                    return {"time": m.group(1), "callsign": lookup_dmrid(dmr_id),
                            "dmr_id": dmr_id, "tg": tg, "tg_name": lookup_tg(tg)}
    except Exception as e:
        print(f"Log read error {log_path}: {e}")

    return {"callsign": "", "dmr_id": "", "tg": "", "tg_name": ""}

# -------------------------
# USRP LISTENER
# -------------------------
USRP_MAGIC = b'USRP'

def parse_usrp(data):
    if len(data) < 32 or data[:4] != USRP_MAGIC:
        return None
    return {
        "seq":     struct.unpack_from('>I', data, 4)[0],
        "tg":      struct.unpack_from('>I', data, 8)[0],
        "ptt":     struct.unpack_from('>I', data, 12)[0],
        "type":    struct.unpack_from('>I', data, 16)[0],
        "mpxid":   struct.unpack_from('>I', data, 20)[0],
        "payload": data[32:]
    }

def send_registration():
    sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    frame = struct.pack('>4sIIIII', USRP_MAGIC, 0, 0, 0, 0, 0) + bytes(4) + bytes(320)
    sock.sendto(frame, (USRP_HOST, USRP_PORT))
    sock.close()
    usrp_state["last_reg_sent"] = time.time()

def usrp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', USRP_LISTEN))
    sock.settimeout(1.0)
    print(f"USRP listener started on port {USRP_LISTEN}")

    last_reg = 0
    last_ptt = 0

    while True:
        now = time.time()

        if now - last_reg > 30:
            try:
                send_registration()
            except Exception as e:
                print(f"Registration error: {e}")
            last_reg = now

        if usrp_state["connected"] and (now - usrp_state["last_packet"]) > 35:
            usrp_state["connected"]  = False
            usrp_state["registered"] = False
            print("USRP connection lost")

        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except Exception as e:
            print(f"USRP recv error: {e}")
            continue

        frame = parse_usrp(data)
        if not frame:
            continue

        usrp_state["last_packet"] = time.time()
        if not usrp_state["connected"]:
            usrp_state["connected"]  = True
            usrp_state["registered"] = True
            print(f"USRP connected from {addr}")

        if frame['ptt'] == 1 and last_ptt != 1:
            last_ptt = 1
            time.sleep(0.2)
            tx_info  = get_current_tx_from_log()
            dmr_id   = tx_info.get('dmr_id', '')
            callsign = tx_info.get('callsign', '') or (lookup_dmrid(dmr_id) if dmr_id else 'UNKNOWN')
            tg       = tx_info.get('tg', '')
            tg_name  = tx_info.get('tg_name', '') or lookup_tg(tg)
            active_tx.update({
                "active": True, "callsign": callsign, "dmr_id": dmr_id,
                "tg": tg, "tg_name": tg_name,
                "started": datetime.now().strftime("%H:%M:%S"),
            })
            push_event({"event": "tx_start", **active_tx})

        elif frame['ptt'] == 0 and last_ptt != 0:
            last_ptt = 0
            def clear_tx():
                time.sleep(3)
                active_tx.update({
                    "active": False, "callsign": "", "dmr_id": "",
                    "tg": "", "tg_name": "", "started": "",
                })
                push_event({"event": "tx_end"})
            threading.Thread(target=clear_tx, daemon=True).start()

# -------------------------
# HELPERS
# -------------------------
def run(cmd):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return "ERROR: timeout"
    except Exception as e:
        return f"ERROR: {e}"


def svc(name):
    result = run(f"systemctl is-active {name}")
    return "RUNNING" if result.strip() == "active" else "STOPPED"

def get_svc_uptime(name):
    try:
        r = subprocess.run(
            ['systemctl', 'show', name, '--property=ActiveEnterTimestamp'],
            capture_output=True, text=True, timeout=5
        )
        ts = r.stdout.strip().split('=', 1)[-1].strip()
        # "Wed 2024-01-01 14:32:00 UTC" → "2024-01-01 14:32:00"
        parts = ts.split()
        if len(parts) >= 3 and parts[1] != 'n/a':
            return f"{parts[1]} {parts[2]}"
    except Exception:
        pass
    return ''

def get_log_path(log_key):
    pattern = LOG_FILES.get(log_key, '')
    today   = datetime.now().strftime('%Y-%m-%d')
    return pattern.replace('{date}', today)

# -------------------------
# STATUS
# -------------------------
def get_status():
    global current_mode
    tg, call, tg_name = "N/A", "", ""
    status_source = "live"

    # Pre-compute service states first — they determine mode reliably.
    # STFU running → BrandMeister stack; MMDVM Bridge running → TGIF stack.
    svc_stfu   = svc("stfu.service")
    svc_mmdvm  = svc("mmdvm_bridge.service")
    svc_analog = svc("analog_bridge.service")

    if svc_stfu == "RUNNING":
        mode = "BrandMeister"
    elif svc_mmdvm == "RUNNING":
        mode = "TGIF"
    else:
        # Neither service is up — keep the last known mode so lookups stay correct
        mode = current_mode

    # Update the global so get_active_mode() / lookup_tg() pick it up immediately
    current_mode = mode

    # Read TG/call from ABInfo (ground truth for what talkgroup is active)
    try:
        with open(ABINFO_ACTIVE) as f:
            abinfo = json.load(f)
        tg      = str(abinfo.get('digital', {}).get('tg', 'N/A'))
        call    = abinfo.get('digital', {}).get('call', '')
        tg_name = lookup_tg(tg)
    except Exception:
        # ABInfo unreadable — fall back to what the user last explicitly tuned
        status_source = "cached"
        if last_state.get("tg"):
            tg      = last_state["tg"]
            tg_name = last_state.get("tg_name") or lookup_tg(tg)

    if mode == "BrandMeister":
        connected_since = get_svc_uptime("stfu.service")
        core_up = svc_stfu == "RUNNING" and svc_analog == "RUNNING"
    else:
        connected_since = get_svc_uptime("mmdvm_bridge.service")
        core_up = svc_mmdvm == "RUNNING" and svc_analog == "RUNNING"

    # Connection state: answers "am I tuned or just quiet?"
    if active_tx["active"]:
        conn_state = "rx"           # audio actively flowing
    elif usrp_state["connected"] and core_up:
        conn_state = "idle"         # tuned and ready, no traffic
    elif core_up:
        conn_state = "starting"     # services up, USRP not yet connected
    else:
        conn_state = "offline"      # radio stack is down

    return {
        "mode":             mode,
        "tg":               tg,
        "tg_name":          tg_name,
        "call":             call,
        "connected_since":  connected_since,
        "svc_stfu":         svc_stfu,
        "svc_mmdvm":        svc_mmdvm,
        "svc_analog":       svc_analog,
        "usrp_connected":   usrp_state["connected"],
        "usrp_registered":  usrp_state["registered"],
        "status_source":    status_source,
        "conn_state":       conn_state,
        "last_tg":          last_state.get("tg", ""),
        "last_tg_name":     last_state.get("tg_name", ""),
        "last_network":     last_state.get("network", ""),
    }

# -------------------------
# LAST HEARD
# -------------------------
def get_last_heard():
    results = []
    today   = datetime.now().strftime('%Y-%m-%d')

    mmdvm_log = f'/var/log/mmdvm/MMDVM_Bridge-{today}.log'
    try:
        result = subprocess.run(
            ['grep', '-E', 'received network voice header', mmdvm_log],
            capture_output=True, timeout=5
        )
        text = result.stdout.decode('utf-8', errors='replace')
        for line in text.splitlines():
            m = re.search(
                r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*voice header from (\S+) to TG (\d+)', line)
            if m:
                tg = m.group(3)
                results.append({
                    "time": m.group(1), "callsign": m.group(2), "dmr_id": "",
                    "tg": tg,
                    "tg_name": lookup_tg_by_source(tg, 'TGIF'),
                    "source": "TGIF"
                })
    except Exception as e:
        print(f"MMDVM log error: {e}")

    ab_log = f'/var/log/dvswitch/Analog_Bridge-{today}.log'
    try:
        result = subprocess.run(
            ['grep', '-E', 'Begin TX', ab_log],
            capture_output=True, timeout=5
        )
        text = result.stdout.decode('utf-8', errors='replace')
        for line in text.splitlines():
            m = re.search(
                r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*src=(\d+).*dst=(\d+)', line)
            if m:
                dmr_id = m.group(2)
                tg     = m.group(3)
                results.append({
                    "time": m.group(1), "callsign": lookup_dmrid(dmr_id),
                    "dmr_id": dmr_id, "tg": tg,
                    "tg_name": lookup_tg_by_source(tg, 'TGIF'),
                    "source": "TGIF"
                })
    except Exception as e:
        print(f"AB log error: {e}")

    stfu_log = '/var/log/dvswitch/STFU.log'
    try:
        result = subprocess.run(
            ['grep', '-E', 'ODMR Begin Tx', stfu_log],
            capture_output=True, timeout=5
        )
        text = result.stdout.decode('utf-8', errors='replace')
        for line in text.splitlines():
            m = re.search(
                r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*src\s*=\s*(\d+).*dst\s*=\s*(\d+)', line)
            if m:
                dmr_id = m.group(2)
                tg     = m.group(3)
                results.append({
                    "time": m.group(1), "callsign": lookup_dmrid(dmr_id),
                    "dmr_id": dmr_id, "tg": tg,
                    "tg_name": lookup_tg_by_source(tg, 'BM'),
                    "source": "BM"
                })
    except Exception as e:
        print(f"STFU log error: {e}")

    results.sort(key=lambda x: x['time'], reverse=True)
    return results[:20]

# -------------------------
# LAST TUNED STATE
# -------------------------
last_state = {"tg": "", "tg_name": "", "network": "", "time": ""}

def load_last_state():
    global last_state
    try:
        with open(LAST_STATE_FILE) as f:
            last_state.update(json.load(f))
        print(f"Last state loaded: {last_state['network']} TG {last_state['tg']}")
    except:
        pass

def save_last_state():
    try:
        with open(LAST_STATE_FILE, 'w') as f:
            json.dump(last_state, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save last state: {e}")

# -------------------------
# FAVORITES & TUNE HISTORY
# -------------------------
favorites_lock = threading.Lock()
tune_history   = []
HISTORY_MAX    = 20

def load_favorites():
    try:
        with open(FAVORITES_FILE) as f:
            data = json.load(f)
        return {"BM": data.get("BM", []), "TGIF": data.get("TGIF", [])}
    except:
        return {"BM": [], "TGIF": []}

def save_favorites(data):
    try:
        with open(FAVORITES_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save favorites: {e}")

favorites = load_favorites()

# -------------------------
# ALLSTAR / IAX2
# -------------------------
class AllstarManager:
    def __init__(self):
        self.client     = None
        self._ws_qs     = []
        self._lock      = threading.Lock()
        self._last_audio = 0.0

    def _on_audio(self, pcm: bytes):
        self._last_audio = time.time()
        with self._lock:
            dead = []
            for q in self._ws_qs:
                try:
                    q.put_nowait(pcm)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._ws_qs.remove(q)

    def connect(self, node=None):
        if self.client and self.client.state in ('connecting', 'connected'):
            return False, 'Already connected'
        node = str(node or ALLSTAR_NODE).strip()
        if not node:
            return False, 'No node number configured'
        self.client = IAX2Client(
            ALLSTAR_HOST, ALLSTAR_PORT, ALLSTAR_USER, ALLSTAR_SECRET, node
        )
        self.client.on_audio(self._on_audio)
        self.client.connect()
        return True, f'Connecting to node {node}...'

    def disconnect(self):
        if self.client:
            self.client.disconnect()
            self.client = None

    @property
    def status(self):
        if not self.client:
            return {'state': 'idle', 'node': '', 'error': '', 'active': False}
        active = (self.client.state == 'connected' and
                  time.time() - self._last_audio < 0.6)
        return {
            'state':  self.client.state,
            'node':   self.client.node,
            'error':  self.client.error_msg,
            'active': active,
        }

    def send_dtmf(self, digits: str, inter_digit: float = 0.05):
        if not self.client or self.client.state != 'connected':
            raise RuntimeError('Not connected to Allstar')
        self.client.send_dtmf(digits, inter_digit=inter_digit)

    def add_listener(self, q):
        with self._lock:
            self._ws_qs.append(q)

    def remove_listener(self, q):
        with self._lock:
            if q in self._ws_qs:
                self._ws_qs.remove(q)


allstar_mgr = AllstarManager()


def tg_refresh_loop():
    while True:
        time.sleep(300)
        load_tg_names()

load_last_state()
load_tg_names()
load_dmr_ids()
usrp_thread      = threading.Thread(target=usrp_listener,  daemon=True)
tg_refresh_thread = threading.Thread(target=tg_refresh_loop, daemon=True)
usrp_thread.start()
tg_refresh_thread.start()

HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Radio Dispatcher</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script type="text/javascript" src="/static/pcm-player.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #0d0d0d; color: #fff; font-family: monospace; font-size: 13px; }

        /* ---- HEADER BAR ---- */
        .header-bar {
            background: #1a1a1a;
            border-bottom: 1px solid #2a2a2a;
            padding: 8px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .header-bar h1 { font-size: 14px; letter-spacing: 2px; color: #aaa; }
        .header-time   { font-size: 11px; color: #555; }

        /* ---- MAIN LAYOUT ---- */
        .app-body { display: grid; grid-template-columns: 220px 1fr; gap: 0; height: calc(100vh - 37px); }

        /* ---- LEFT SIDEBAR ---- */
        .sidebar {
            background: #141414;
            border-right: 1px solid #222;
            padding: 10px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .sidebar-section {
            background: #1a1a1a;
            border-radius: 6px;
            padding: 10px;
        }
        .sidebar-section h3 {
            font-size: 9px; color: #555;
            letter-spacing: 1px; margin-bottom: 8px;
            text-transform: uppercase;
        }

        .stat-row {
            display: flex; justify-content: space-between;
            align-items: center; padding: 3px 0;
            border-bottom: 1px solid #1f1f1f;
        }
        .stat-row:last-child { border-bottom: none; }
        .stat-key   { font-size: 10px; color: #555; }
        .stat-val   { font-size: 12px; color: #ccc; text-align: right; }

        .mode-badge {
            display: inline-block; padding: 1px 7px;
            border-radius: 3px; font-weight: bold; font-size: 11px;
        }
        .badge-tgif    { background: #1a3a1a; color: lime; }
        .badge-bm      { background: #1a1a3a; color: cyan; }
        .badge-unknown { background: #2a2a2a; color: #666; }

        .conn-badge { display: inline-block; padding: 1px 7px; border-radius: 3px; font-weight: bold; font-size: 11px; }
        .conn-active   { background: #0a2a0a; color: #4f4; }
        .rx-dot { display:inline-block; width:9px; height:9px; border-radius:50%; background:#333; margin-right:5px; vertical-align:middle; transition: background 0.15s; }
        .rx-dot.lit  { background:#4f4; box-shadow: 0 0 6px #4f4; animation: rx-pulse 0.6s ease-out; }
        @keyframes rx-pulse { 0%{box-shadow:0 0 10px #4f4;} 100%{box-shadow:0 0 4px #4f4;} }
        .conn-rx       { background: #0d2a0d; color: lime; box-shadow: 0 0 5px lime; animation: pulse 1s infinite; }
        .conn-idle     { background: #0d1a0d; color: #5c5; }
        .conn-starting { background: #2a2200; color: gold; }
        .conn-offline  { background: #1a1a1a; color: #444; }
        .conn-cached   { background: #1a1a1a; color: #555; font-style: italic; }

        .svc-dot {
            display: inline-block; width: 7px; height: 7px;
            border-radius: 50%; margin-right: 5px;
            background: #333;
        }
        .dot-on  { background: lime; }
        .dot-off { background: #444; }

        .svc-text-on  { color: lime; font-size: 11px; }
        .svc-text-off { color: #555; font-size: 11px; }

        /* ---- ACTIVE TX IN SIDEBAR ---- */
        .tx-block {
            background: #1a1a1a;
            border-radius: 6px;
            padding: 10px;
            border: 1px solid #222;
            transition: border-color 0.3s, background 0.3s;
        }
        .tx-block.active {
            background: #0d1f0d;
            border-color: #2a4a2a;
        }
        .tx-block h3 {
            font-size: 9px; color: #555;
            letter-spacing: 1px; margin-bottom: 8px;
            text-transform: uppercase;
            display: flex; align-items: center; gap: 6px;
        }
        .tx-pulse {
            width: 7px; height: 7px; border-radius: 50%;
            background: #333; flex-shrink: 0;
        }
        .tx-pulse.on {
            background: lime;
            box-shadow: 0 0 6px lime;
            animation: pulse 1s infinite;
        }
        @keyframes pulse {
            0%   { box-shadow: 0 0 3px lime; }
            50%  { box-shadow: 0 0 10px lime; }
            100% { box-shadow: 0 0 3px lime; }
        }
        .tx-callsign {
            font-size: 22px; font-weight: bold;
            color: #333; letter-spacing: 2px;
            line-height: 1.2; margin-bottom: 4px;
        }
        .tx-callsign.on { color: lime; }
        .tx-detail { font-size: 10px; color: #555; margin-top: 2px; }
        .tx-detail.on { color: #888; }

        /* ---- RIGHT CONTENT ---- */
        .content {
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        /* ---- CONTROLS BAR ---- */
        .controls-bar {
            background: #141414;
            border-bottom: 1px solid #222;
            padding: 8px 12px;
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            align-items: center;
        }

        button {
            padding: 5px 12px; border: none; border-radius: 4px;
            font-family: monospace; font-size: 12px;
            cursor: pointer; letter-spacing: 0.5px;
            background: #2a2a2a; color: #aaa;
        }
        button:hover    { background: #333; color: #fff; }
        button.btn-tgif { background: #1a3a1a; color: lime; }
        button.btn-tgif:hover { background: #234d23; }
        button.btn-bm   { background: #1a1a3a; color: cyan; }
        button.btn-bm:hover { background: #23234d; }
        button.btn-danger { background: #2a1a1a; color: #c66; }
        button.btn-danger:hover { background: #3d2323; }
        button.btn-tune { background: #2a2a1a; color: gold; }
        button.btn-tune:hover { background: #3d3d23; }
        button.btn-monitor { background: #1a2a3a; color: #7af; }
        button.btn-monitor:hover { background: #1e324a; }
        button.btn-monitor.active { background: #004400; color: lime; }
        button:disabled { opacity: 0.35; cursor: not-allowed; }

        .controls-sep { width: 1px; height: 24px; background: #2a2a2a; margin: 0 2px; }

        .tg-input {
            padding: 5px 8px; background: #222; border: 1px solid #333;
            color: #fff; font-family: monospace; font-size: 12px;
            border-radius: 4px; width: 130px;
        }

        /* ---- PANELS AREA ---- */
        .panels {
            flex: 1;
            overflow-y: auto;
            padding: 10px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        /* ---- DISPATCH LOG ---- */
        .dispatch-log {
            background: #000; border-radius: 4px; padding: 8px;
            height: 160px; overflow-y: auto;
            font-size: 11px; line-height: 1.5;
            border: 1px solid #1a1a1a;
        }
        .log-ok    { color: lime; }
        .log-error { color: tomato; }
        .log-warn  { color: gold; }
        .log-info  { color: #555; }

        /* ---- COLLAPSIBLE PANELS ---- */
        .collapse-panel { background: #141414; border-radius: 6px; overflow: hidden; border: 1px solid #1f1f1f; }
        .collapse-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 8px 12px; cursor: pointer; user-select: none;
            background: #1a1a1a;
        }
        .collapse-header:hover { background: #1f1f1f; }
        .collapse-header h3 { font-size: 10px; color: #666; letter-spacing: 1px; margin: 0; }
        .collapse-arrow { color: #444; font-size: 12px; transition: transform 0.2s; }
        .collapse-arrow.open { transform: rotate(180deg); }
        .collapse-body { display: none; padding: 10px; }
        .collapse-body.open { display: block; }

        /* ---- LAST HEARD ---- */
        #lastHeardTable { width: 100%; border-collapse: collapse; font-size: 11px; }
        #lastHeardTable th {
            text-align: left; color: #444; padding: 3px 8px;
            border-bottom: 1px solid #222;
            font-weight: normal; font-size: 10px; letter-spacing: 1px;
        }
        #lastHeardTable td { padding: 4px 8px; border-bottom: 1px solid #1a1a1a; }
        #lastHeardTable tr:hover td { background: #1f1f1f; }
        .lh-time     { color: #444; }
        .lh-callsign { color: orange; font-weight: bold; }
        .lh-dmrid    { color: #444; font-size: 10px; }
        .lh-tg       { color: lightgreen; }
        .lh-tgname   { color: #555; }
        .lh-tgif     { color: lime; font-size: 10px; }
        .lh-bm       { color: cyan; font-size: 10px; }

        /* ---- VOLUME SLIDER ---- */
        .vol-row {
            display: flex; justify-content: space-between;
            align-items: center; margin-bottom: 6px;
        }
        .vol-label { font-size: 10px; color: #555; }
        .vol-pct   { font-size: 11px; color: #aaa; }
        .vol-slider {
            width: 100%; cursor: pointer; outline: none;
            -webkit-appearance: none; appearance: none;
            height: 4px; border-radius: 2px;
            background: linear-gradient(to right, gold 0%, gold var(--vol-pct, 100%), #333 var(--vol-pct, 100%));
        }
        .vol-slider::-webkit-slider-thumb {
            -webkit-appearance: none; appearance: none;
            width: 13px; height: 13px; border-radius: 50%;
            background: gold; cursor: pointer; border: none;
        }
        .vol-slider::-moz-range-thumb {
            width: 13px; height: 13px; border-radius: 50%;
            background: gold; cursor: pointer; border: none;
        }

        .hpf-slider {
            width: 100%; cursor: pointer; outline: none;
            -webkit-appearance: none; appearance: none;
            height: 4px; border-radius: 2px;
            background: linear-gradient(to right, #fa8 0%, #fa8 var(--hpf-pct, 0%), #333 var(--hpf-pct, 0%));
        }
        .hpf-slider::-webkit-slider-thumb {
            -webkit-appearance: none; appearance: none;
            width: 13px; height: 13px; border-radius: 50%;
            background: #fa8; cursor: pointer; border: none;
        }
        .hpf-slider::-moz-range-thumb {
            width: 13px; height: 13px; border-radius: 50%;
            background: #fa8; cursor: pointer; border: none;
        }
        .pres-slider {
            width: 100%; cursor: pointer; outline: none;
            -webkit-appearance: none; appearance: none;
            height: 4px; border-radius: 2px;
            background: linear-gradient(to right, lime 0%, lime var(--pres-pct, 0%), #333 var(--pres-pct, 0%));
        }
        .pres-slider::-webkit-slider-thumb {
            -webkit-appearance: none; appearance: none;
            width: 13px; height: 13px; border-radius: 50%;
            background: lime; cursor: pointer; border: none;
        }
        .pres-slider::-moz-range-thumb {
            width: 13px; height: 13px; border-radius: 50%;
            background: lime; cursor: pointer; border: none;
        }

        /* ---- LOG VIEWER ---- */
        .log-tabs { display: flex; gap: 6px; margin-bottom: 8px; }
        .log-tab {
            padding: 3px 10px; border-radius: 3px; background: #1f1f1f; color: #666;
            cursor: pointer; font-size: 11px; border: 1px solid #2a2a2a; font-family: monospace;
        }
        .log-tab:hover { background: #252525; color: #aaa; }
        .log-tab.active { background: #252525; color: #aaa; border-color: #444; }
        .log-tab.tab-mmdvm.active  { border-color: #7af; color: #7af; }
        .log-tab.tab-analog.active { border-color: lime; color: lime; }
        .log-tab.tab-stfu.active   { border-color: cyan; color: cyan; }

        .log-controls { display: flex; gap: 6px; margin-bottom: 6px; align-items: center; }
        .log-controls label { font-size: 10px; color: #555; }
        .log-controls input[type=number] {
            width: 50px; padding: 3px 5px; background: #1f1f1f;
            border: 1px solid #333; color: #fff; font-family: monospace;
            font-size: 11px; border-radius: 3px;
        }
        .log-controls button { padding: 3px 8px; font-size: 11px; }
        .btn-autoscroll.on { background: #1a3a1a; color: lime; }

        #logFileContent {
            background: #000; border-radius: 4px; padding: 8px;
            color: #fff;
            height: 220px; overflow-y: auto;
            font-size: 10px; line-height: 1.5; color: #666;
            white-space: pre-wrap; word-break: break-all;
        }
        .log-line-error { color: tomato; }
        .log-line-warn  { color: gold; }
        .log-line-info  { color: #aaa; }
        .log-line-debug { color: #888; }
        .log-file-label { font-size: 9px; color: #555; margin-bottom: 4px; }

        /* ---- QUICK TUNE ---- */
        .qt-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-bottom: 10px;
        }
        .qt-section-label {
            font-size: 9px; color: #555;
            letter-spacing: 1px; margin-bottom: 5px;
            text-transform: uppercase;
        }
        .fav-entry {
            display: flex; align-items: stretch;
            gap: 3px; margin-bottom: 3px;
        }
        .btn-fav-tune {
            flex: 1; text-align: left;
            padding: 4px 7px; font-size: 11px;
            overflow: hidden; text-overflow: ellipsis;
            white-space: nowrap; background: #2a2a1a; color: gold;
        }
        .btn-fav-tune:hover { background: #3d3d23; }
        .btn-fav-del {
            background: #1f1515; color: #553333;
            padding: 4px 7px; font-size: 10px; flex-shrink: 0;
            border: none; border-radius: 4px; cursor: pointer;
            font-family: monospace;
        }
        .btn-fav-del:hover { background: #3d2323; color: #c66; }
        .btn-save-fav { background: #2a2a1a; color: gold; }
        .btn-save-fav:hover { background: #3d3d23; }
        .qt-empty { color: #333; font-size: 10px; padding: 2px 0; }
        .qt-hist-net {
            font-size: 9px; color: #555;
            margin-right: 5px; letter-spacing: 0.5px;
        }

        @media (max-width: 600px) {
            .app-body { grid-template-columns: 1fr; }
            .sidebar  { border-right: none; border-bottom: 1px solid #222; max-height: 50vh; }
        }
    </style>
</head>
<body>

    <div class="header-bar">
        <h1>&#9889; RADIO DISPATCHER</h1>
        <span class="header-time" id="headerTime">--</span>
    </div>

    <div class="app-body">

        <!-- SIDEBAR -->
        <div class="sidebar">

            <!-- ACTIVE TX -->
            <div class="tx-block" id="txBlock">
                <h3><span class="tx-pulse" id="txPulse"></span>ON AIR</h3>
                <div class="tx-callsign" id="txCallsign">STANDBY</div>
                <div class="tx-detail"   id="txDetail">&mdash;</div>
                <div class="tx-detail"   id="txTime"></div>
            </div>

            <!-- SYSTEM STATUS -->
            <div class="sidebar-section">
                <h3>Status</h3>
                <div class="stat-row">
                    <span class="stat-key">Network</span>
                    <span class="stat-val"><span class="mode-badge badge-unknown" id="modeValue">--</span></span>
                </div>
                <div class="stat-row">
                    <span class="stat-key">State</span>
                    <span class="stat-val"><span class="conn-badge conn-offline" id="connState">OFFLINE</span></span>
                </div>
                <div class="stat-row">
                    <span class="stat-key">Since</span>
                    <span class="stat-val" id="connectedSince" style="color:#666; font-size:10px;">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-key">Callsign</span>
                    <span class="stat-val" id="callValue" style="color:orange;">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-key">Talkgroup</span>
                    <span class="stat-val" style="text-align:right;">
                        <span style="color:lightgreen;" id="tgValue">--</span><br>
                        <span style="color:#4a4; font-size:10px;" id="tgName"></span>
                    </span>
                </div>
            </div>

            <!-- SERVICES -->
            <div class="sidebar-section">
                <h3>Services</h3>
                <div class="stat-row">
                    <span class="stat-key"><span class="svc-dot" id="dot_stfu"></span>STFU/BM</span>
                    <span class="stat-val svc-text-off" id="svc_stfu">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-key"><span class="svc-dot" id="dot_mmdvm"></span>MMDVM</span>
                    <span class="stat-val svc-text-off" id="svc_mmdvm">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-key"><span class="svc-dot" id="dot_analog"></span>Analog</span>
                    <span class="stat-val svc-text-off" id="svc_analog">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-key"><span class="svc-dot" id="dot_usrp"></span>USRP</span>
                    <span class="stat-val svc-text-off" id="svc_usrp">--</span>
                </div>
            </div>

            <!-- ALLSTAR STATUS -->
            <div class="sidebar-section">
                <h3>Allstar</h3>
                <div class="stat-row">
                    <span class="stat-key">State</span>
                    <span class="stat-val">
                        <span class="rx-dot" id="asRxDot" title="RX activity"></span>
                        <span class="conn-badge conn-offline" id="asStateBadge">OFFLINE</span>
                    </span>
                </div>
                <div class="stat-row">
                    <span class="stat-key">Node</span>
                    <span class="stat-val" id="asNodeBadge" style="color:#aaa;">--</span>
                </div>
            </div>

            <!-- AUDIO -->
            <div class="sidebar-section">
                <h3>Audio</h3>
                <div class="vol-row">
                    <span class="vol-label">RX Volume</span>
                    <span class="vol-pct" id="volDisplay">100%</span>
                </div>
                <input type="range" class="vol-slider" id="volSlider"
                       min="0" max="100" value="100"
                       oninput="setVolume(this.value)">
                <div class="vol-row" style="margin-top:8px;">
                    <span class="vol-label">High Pass</span>
                    <span class="vol-pct" id="hpfDisplay" style="color:#fa8;">OFF</span>
                </div>
                <input type="range" class="hpf-slider" id="hpfSlider"
                       min="100" max="400" step="10" value="200"
                       oninput="setHpFilter(this.value)">
                <div class="vol-row" style="margin-top:8px;">
                    <span class="vol-label">Presence</span>
                    <span class="vol-pct" id="presDisplay" style="color:lime;">0 dB</span>
                </div>
                <input type="range" class="pres-slider" id="presSlider"
                       min="0" max="12" step="0.5" value="0"
                       oninput="setPresence(this.value)">
            </div>
        </div>

        <!-- MAIN CONTENT -->
        <div class="content">

            <!-- CONTROLS BAR -->
            <div class="controls-bar">
                <button class="btn-tgif"   id="btnTGIF"      onclick="action('/api/tgif',         'Switching to TGIF...')">&#9654; TGIF</button>
                <button class="btn-bm"     id="btnBM"        onclick="action('/api/bm',            'Switching to BrandMeister...')">&#9654; BM</button>
                <div class="controls-sep"></div>
                <button class="btn-danger" id="btnRestart"   onclick="action('/api/restart',       'Restarting STFU...')">&#8634; STFU</button>
                <button class="btn-danger" id="btnRestartAB" onclick="action('/api/restart_ab',    'Restarting Analog Bridge...')">&#8634; Analog</button>
                <button class="btn-danger" id="btnRestartMM" onclick="action('/api/restart_mmdvm', 'Restarting MMDVM...')">&#8634; MMDVM</button>
                <div class="controls-sep"></div>
                <input  class="tg-input" type="text" id="tgInput" placeholder="Talkgroup...">
                <button class="btn-tune" onclick="tuneTG()">&#9654; Tune</button>
                <button class="btn-save-fav" onclick="saveFavorite()" title="Save to favorites for current network">&#9733; Fav</button>
                <div class="controls-sep"></div>
                <button class="btn-monitor" id="btnMonitor" onclick="toggleMonitor(this)">&#128264; AUDIO</button>
            </div>

            <!-- PANELS -->
            <div class="panels">

                <!-- DISPATCH LOG -->
                <div class="collapse-panel">
                    <div class="collapse-header" onclick="toggleDispatchLog()">
                        <h3>&#128225; DISPATCH LOG</h3>
                        <span class="collapse-arrow open" id="dispatchArrow">&#9660;</span>
                    </div>
                    <div class="collapse-body open" id="dispatchLogWrapper">
                        <div class="dispatch-log" id="dispatchLog"></div>
                    </div>
                </div>

                <!-- QUICK TUNE -->
                <div class="collapse-panel">
                    <div class="collapse-header" onclick="toggleQuickTune()">
                        <h3>&#9733; QUICK TUNE</h3>
                        <span class="collapse-arrow" id="quickTuneArrow">&#9660;</span>
                    </div>
                    <div class="collapse-body" id="quickTuneBody">
                        <div class="qt-grid">
                            <div>
                                <div class="qt-section-label">TGIF Favorites</div>
                                <div id="favsTGIF"><div class="qt-empty">None saved</div></div>
                            </div>
                            <div>
                                <div class="qt-section-label">BM Favorites</div>
                                <div id="favsBM"><div class="qt-empty">None saved</div></div>
                            </div>
                        </div>
                        <div class="qt-section-label" style="margin-top:6px;">Recent</div>
                        <div id="tuneHistory"><div class="qt-empty">No history yet</div></div>
                    </div>
                </div>

                <!-- LAST HEARD -->
                <div class="collapse-panel">
                    <div class="collapse-header" onclick="toggleLastHeard()">
                        <h3>&#128251; LAST HEARD</h3>
                        <span class="collapse-arrow" id="lastHeardArrow">&#9660;</span>
                    </div>
                    <div class="collapse-body" id="lastHeardBody_wrapper">
                        <table id="lastHeardTable">
                            <thead>
                                <tr>
                                    <th>TIME</th>
                                    <th>CALLSIGN</th>
                                    <th>DMR ID</th>
                                    <th>TG</th>
                                    <th>TG NAME</th>
                                    <th>NET</th>
                                </tr>
                            </thead>
                            <tbody id="lastHeardBody">
                                <tr><td colspan="6" style="color:#333; padding:8px;">Open to load...</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- ALLSTAR -->
                <div class="collapse-panel">
                    <div class="collapse-header" onclick="toggleAllstar()">
                        <h3>&#9889; ALLSTAR NODE</h3>
                        <span class="collapse-arrow" id="allstarArrow">&#9660;</span>
                    </div>
                    <div class="collapse-body" id="allstarBody">
                        <div style="display:flex; gap:6px; margin-bottom:10px; align-items:center; flex-wrap:wrap;">
                            <input class="tg-input" type="text" id="asNodeInput"
                                   placeholder="Node #..." style="width:110px;"
                                   onkeydown="if(event.key==='Enter') allstarConnect()">
                            <button class="btn-monitor" id="btnAsConnect"   onclick="allstarConnect()">&#9654; Connect</button>
                            <button class="btn-danger"  id="btnAsDisconnect" onclick="allstarDisconnect()" disabled>&#9632; Disconnect</button>
                            <button class="btn-monitor" id="btnAsAudio"     onclick="toggleAllstarAudio(this)">&#128264; Audio</button>
                        </div>
                        <div style="margin-bottom:10px;">
                            <div class="qt-section-label" style="margin-bottom:5px;">NODE LINKING</div>
                            <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
                                <input class="tg-input" type="text" id="asRemoteNode"
                                       placeholder="Remote node #..." style="width:140px;">
                                <button class="btn-tune"   onclick="allstarLink('monitor')">&#9654; Monitor</button>
                                <button class="btn-tune"   onclick="allstarLink('transceive')">&#9654; Xceive</button>
                                <button class="btn-danger" onclick="allstarUnlink()">&#9632; Unlink</button>
                            </div>
                        </div>
                        <div style="margin-bottom:10px;">
                            <div class="qt-section-label" style="margin-bottom:5px;">CONNECTED NODES</div>
                            <div id="asNodeList" style="font-size:12px; color:#aaa; min-height:16px;">--</div>
                        </div>
                        <div style="display:flex; gap:8px; align-items:center;">
                            <span class="vol-label" style="white-space:nowrap; flex-shrink:0;">AS Volume</span>
                            <input type="range" class="vol-slider" id="asVolSlider"
                                   min="0" max="100" value="100"
                                   oninput="setAllstarVolume(this.value)" style="flex:1;">
                            <span class="vol-pct" id="asVolDisplay" style="min-width:35px; text-align:right;">100%</span>
                        </div>
                    </div>
                </div>

                <!-- LOG VIEWER -->
                <div class="collapse-panel">
                    <div class="collapse-header" onclick="toggleLogViewer()">
                        <h3>&#128203; LOG VIEWER</h3>
                        <span class="collapse-arrow" id="collapseArrow">&#9660;</span>
                    </div>
                    <div class="collapse-body" id="logViewerBody">
                        <div class="log-tabs">
                            <div class="log-tab tab-mmdvm active" onclick="selectTab('mmdvm', this)">MMDVM</div>
                            <div class="log-tab tab-analog"       onclick="selectTab('analog', this)">Analog</div>
                            <div class="log-tab tab-stfu"         onclick="selectTab('stfu',   this)">STFU</div>
                        </div>
                        <div class="log-controls">
                            <label>Lines:</label>
                            <input type="number" id="logLines" value="50" min="10" max="500" step="10">
                            <button onclick="fetchLog()">&#8634; Refresh</button>
                            <button class="btn-autoscroll on" id="btnAutoScroll" onclick="toggleAutoScroll()">&#11015; Auto</button>
                        </div>
                        <div class="log-file-label" id="logFileLabel"></div>
                        <div id="logFileContent">Select a tab to load...</div>
                    </div>
                </div>

            </div>
        </div>
    </div>

    <script>
        // -------------------------
        // SSE - ACTIVE TX
        // -------------------------
        function connectSSE() {
            const es = new EventSource('/api/stream');
            es.onmessage = function(e) {
                const data = JSON.parse(e.data);
                if (data.event === 'tx_start') {
                    document.getElementById('txBlock').classList.add('active');
                    document.getElementById('txPulse').classList.add('on');
                    const cs = document.getElementById('txCallsign');
                    cs.textContent = data.callsign || data.dmr_id || 'UNKNOWN';
                    cs.classList.add('on');
                    const det = document.getElementById('txDetail');
                    det.textContent = 'TG: ' + data.tg + (data.tg_name ? ' → ' + data.tg_name : '');
                    det.classList.add('on');
                    document.getElementById('txTime').textContent = 'Since ' + data.started;
                    log('TX: ' + (data.callsign || data.dmr_id) + ' → TG ' + data.tg, 'ok');
                    if (lastHeardOpen) pollLastHeard();
                } else if (data.event === 'tx_end') {
                    document.getElementById('txBlock').classList.remove('active');
                    document.getElementById('txPulse').classList.remove('on');
                    const cs = document.getElementById('txCallsign');
                    cs.textContent = 'STANDBY';
                    cs.classList.remove('on');
                    document.getElementById('txDetail').textContent = '—';
                    document.getElementById('txDetail').classList.remove('on');
                    document.getElementById('txTime').textContent = '';
                    if (lastHeardOpen) pollLastHeard();
                }
            };
            es.onerror = function() { setTimeout(connectSSE, 5000); };
        }

        // -------------------------
        // RX MONITOR
        // -------------------------
        // -------------------------
        // VOLUME
        // -------------------------
        function setVolume(val) {
            val = parseInt(val);
            document.getElementById('volDisplay').textContent = val + '%';
            document.getElementById('volSlider').style.setProperty('--vol-pct', val + '%');
            if (dvsp && dvsp.player) dvsp.player.volume(val / 100);
            localStorage.setItem('rxVolume', val);
        }

        function applyStoredVolume() {
            const saved = localStorage.getItem('rxVolume');
            if (saved !== null) {
                document.getElementById('volSlider').value = saved;
                setVolume(saved);
            }
        }

        // -------------------------
        // DMR AUDIO FILTERS
        // -------------------------
        var hpFilter   = null;
        var presFilter = null;
        var compressor = null;

        function setupAudioFilters() {
            if (!dvsp || !dvsp.player || !dvsp.player.audioCtx) return;
            if (hpFilter) return;
            const player = dvsp.player;

            // Reduce flush interval: 2000ms chunks cause boundary thumps; 500ms is smooth
            player.option.flushingTime = 500;

            // High-pass: cuts AMBE low-frequency mud (vocoder artifacts below ~200 Hz)
            hpFilter = player.audioCtx.createBiquadFilter();
            hpFilter.type = 'highpass';
            hpFilter.Q.value = 0.7;

            // Peaking EQ: restores presence the AMBE vocoder rolls off
            presFilter = player.audioCtx.createBiquadFilter();
            presFilter.type = 'peaking';
            presFilter.frequency.value = 2500;
            presFilter.Q.value = 1.0;

            // Compressor: tames the uneven level swings typical of DMR codec frames
            compressor = player.audioCtx.createDynamicsCompressor();
            compressor.threshold.value = -24;  // start compressing at -24 dBFS
            compressor.knee.value      = 6;    // gentle knee
            compressor.ratio.value     = 4;    // 4:1 — transparent but effective
            compressor.attack.value    = 0.003;
            compressor.release.value   = 0.2;

            // gainNode → hpFilter → presFilter → compressor → destination
            player.gainNode.disconnect();
            player.gainNode.connect(hpFilter);
            hpFilter.connect(presFilter);
            presFilter.connect(compressor);
            compressor.connect(player.audioCtx.destination);

            // Apply whatever values the sliders already have
            const hpVal   = parseInt(document.getElementById('hpfSlider').value);
            const presVal = parseFloat(document.getElementById('presSlider').value);
            hpFilter.frequency.value = hpVal;
            presFilter.gain.value    = presVal;
        }

        function setHpFilter(val) {
            val = parseInt(val);
            const pct = ((val - 100) / 300 * 100).toFixed(1);
            document.getElementById('hpfDisplay').textContent = val + ' Hz';
            document.getElementById('hpfSlider').style.setProperty('--hpf-pct', pct + '%');
            if (hpFilter) hpFilter.frequency.value = val;
            localStorage.setItem('rxHpFilter', val);
        }

        function setPresence(val) {
            val = parseFloat(val);
            const pct = (val / 12 * 100).toFixed(1);
            document.getElementById('presDisplay').textContent = val === 0 ? '0 dB' : '+' + val.toFixed(1) + ' dB';
            document.getElementById('presSlider').style.setProperty('--pres-pct', pct + '%');
            if (presFilter) presFilter.gain.value = val;
            localStorage.setItem('rxPresence', val);
        }

        function applyStoredFilters() {
            const hpVal   = parseInt(localStorage.getItem('rxHpFilter')  ?? 200);
            const presVal = parseFloat(localStorage.getItem('rxPresence') ?? 0);
            document.getElementById('hpfSlider').value  = hpVal;
            document.getElementById('presSlider').value = presVal;
            setHpFilter(hpVal);
            setPresence(presVal);
        }

        var dvsp = null;
        function toggleMonitor(btn) {
            if (dvsp && dvsp.isPlaying()) {
                dvsp.stop();
                btn.classList.remove('active');
                log('RX Monitor stopped', 'warn');
            } else {
                if (!dvsp) {
                    dvsp = new DVSwitchPlayer(8080, btn);
                    dvsp.socketURL = '{{ audio_ws_url }}';
                    dvsp.ws = null;
                    applyStoredVolume();
                    setupAudioFilters();
                }
                dvsp.play();
                btn.classList.add('active');
                log('RX Monitor started', 'ok');
            }
        }

        // -------------------------
        // LAST HEARD
        // -------------------------
        var lastHeardOpen  = false;
        var lastHeardTimer = null;

        function toggleLastHeard() {
            lastHeardOpen = !lastHeardOpen;
            document.getElementById('lastHeardBody_wrapper').classList.toggle('open', lastHeardOpen);
            document.getElementById('lastHeardArrow').classList.toggle('open', lastHeardOpen);
            if (lastHeardOpen) {
                pollLastHeard();
                lastHeardTimer = setInterval(pollLastHeard, 10000);
            } else {
                clearInterval(lastHeardTimer);
            }
        }

        async function pollLastHeard() {
            try {
                const res  = await fetch('/api/lastheard');
                const rows = await res.json();
                const body = document.getElementById('lastHeardBody');
                if (!rows.length) {
                    body.innerHTML = '<tr><td colspan="6" style="color:#333; padding:6px;">No activity yet today</td></tr>';
                    return;
                }
                body.innerHTML = rows.map(r => `
                    <tr>
                        <td class="lh-time">${r.time.split(' ')[1]}</td>
                        <td class="lh-callsign">${r.callsign}</td>
                        <td class="lh-dmrid">${r.dmr_id || ''}</td>
                        <td class="lh-tg">${r.tg}</td>
                        <td class="lh-tgname">${r.tg_name || ''}</td>
                        <td class="${r.source === 'BM' ? 'lh-bm' : 'lh-tgif'}">${r.source}</td>
                    </tr>`).join('');
            } catch(e) { log('Last heard error: ' + e, 'error'); }
        }

        // -------------------------
        // LOG VIEWER
        // -------------------------
        var currentLog      = 'mmdvm';
        var autoScroll      = true;
        var logViewerOpen   = false;
        var logPollTimer    = null;
        var dispatchLogOpen = true;

        function toggleDispatchLog() {
            dispatchLogOpen = !dispatchLogOpen;
            document.getElementById('dispatchLogWrapper').classList.toggle('open', dispatchLogOpen);
            document.getElementById('dispatchArrow').classList.toggle('open', dispatchLogOpen);
        }

        function toggleLogViewer() {
            logViewerOpen = !logViewerOpen;
            document.getElementById('logViewerBody').classList.toggle('open', logViewerOpen);
            document.getElementById('collapseArrow').classList.toggle('open', logViewerOpen);
            if (logViewerOpen) { fetchLog(); logPollTimer = setInterval(fetchLog, 5000); }
            else { clearInterval(logPollTimer); }
        }

        function selectTab(logKey, el) {
            document.querySelectorAll('.log-tab').forEach(t => t.classList.remove('active'));
            el.classList.add('active');
            currentLog = logKey;
            fetchLog();
        }

        function toggleAutoScroll() {
            autoScroll = !autoScroll;
            const btn = document.getElementById('btnAutoScroll');
            btn.textContent = '⬇ Auto ' + (autoScroll ? 'ON' : 'OFF');
            btn.classList.toggle('on', autoScroll);
        }

        async function fetchLog() {
            const lines = document.getElementById('logLines').value || 50;
            try {
                const res  = await fetch(`/api/log/${currentLog}?lines=${lines}`);
                const data = await res.json();
                document.getElementById('logFileLabel').textContent = data.path;
                const content = document.getElementById('logFileContent');
                content.innerHTML = '';
                data.lines.forEach(line => {
                    const div = document.createElement('div');
                    const lo  = line.toLowerCase();
                    if      (lo.includes('error') || lo.includes('fail')) div.className = 'log-line-error';
                    else if (lo.includes('warn'))                          div.className = 'log-line-warn';
                    else if (lo.includes('debug'))                         div.className = 'log-line-debug';
                    else                                                   div.className = 'log-line-info';
                    div.textContent = line;
                    content.appendChild(div);
                });
                if (autoScroll) content.scrollTop = content.scrollHeight;
            } catch(e) {
                document.getElementById('logFileContent').textContent = 'Error: ' + e;
            }
        }

        // -------------------------
        // DISPATCH LOG
        // -------------------------
        function timestamp() {
            return new Date().toLocaleTimeString('en-US', {hour12: false});
        }

        function log(msg, type='info') {
            const div  = document.getElementById('dispatchLog');
            const line = document.createElement('div');
            line.className   = 'log-' + type;
            line.textContent = timestamp() + ' ' + msg;
            div.appendChild(line);
            div.scrollTop = div.scrollHeight;
        }

        // -------------------------
        // CONTROLS
        // -------------------------
        function setButtons(disabled) {
            ['btnTGIF','btnBM','btnRestart','btnRestartAB','btnRestartMM'].forEach(id => {
                document.getElementById(id).disabled = disabled;
            });
        }

        async function action(endpoint, msg) {
            log(msg);
            setButtons(true);
            try {
                const res  = await fetch(endpoint, {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
                });
                const data = await res.json();
                log(data.message, data.ok ? 'ok' : 'error');
            } catch(e) {
                log('Failed: ' + e, 'error');
            } finally {
                setButtons(false);
                pollStatus();
            }
        }

        function tuneTG() {
            const tg = document.getElementById('tgInput').value.trim();
            if (!tg) { log('No talkgroup entered', 'error'); return; }
            log('Tuning to TG ' + tg + '...');
            fetch('/api/tune', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tg})
            })
            .then(r => r.json())
            .then(d => { log(d.message, d.ok ? 'ok' : 'error'); pollStatus(); })
            .catch(e => log('Tune failed: ' + e, 'error'));
        }

        // -------------------------
        // QUICK TUNE
        // -------------------------
        var quickTuneOpen = false;
        var currentMode   = 'TGIF';

        function toggleQuickTune() {
            quickTuneOpen = !quickTuneOpen;
            document.getElementById('quickTuneBody').classList.toggle('open', quickTuneOpen);
            document.getElementById('quickTuneArrow').classList.toggle('open', quickTuneOpen);
            if (quickTuneOpen) loadQuickTune();
        }

        async function loadQuickTune() {
            try {
                const [fr, hr] = await Promise.all([
                    fetch('/api/favorites'), fetch('/api/tune_history')
                ]);
                const favs = await fr.json();
                const hist = await hr.json();
                renderFavs('TGIF', favs.TGIF || []);
                renderFavs('BM',   favs.BM   || []);
                renderHistory(hist);
            } catch(e) { log('Quick tune error: ' + e, 'error'); }
        }

        function renderFavs(network, list) {
            const el = document.getElementById('favs' + network);
            if (!list.length) {
                el.innerHTML = '<div class="qt-empty">None saved</div>';
                return;
            }
            el.innerHTML = list.map(f => {
                const label = f.tg + (f.name ? ' · ' + f.name : '');
                return `<div class="fav-entry">
                    <button class="btn-fav-tune" onclick="quickTune('${f.tg}')" title="${label}">${label}</button>
                    <button class="btn-fav-del"  onclick="removeFav('${network}','${f.tg}')">&#10005;</button>
                </div>`;
            }).join('');
        }

        function renderHistory(list) {
            const el = document.getElementById('tuneHistory');
            if (!list.length) {
                el.innerHTML = '<div class="qt-empty">No history yet</div>';
                return;
            }
            el.innerHTML = list.map(h => {
                const label = h.tg + (h.name ? ' · ' + h.name : '');
                return `<div class="fav-entry">
                    <button class="btn-fav-tune" onclick="quickTune('${h.tg}')" title="${label}">
                        <span class="qt-hist-net">${h.network}</span>${label}
                    </button>
                </div>`;
            }).join('');
        }

        function quickTune(tg) {
            document.getElementById('tgInput').value = tg;
            tuneTG();
            if (quickTuneOpen) setTimeout(loadQuickTune, 300);
        }

        async function saveFavorite() {
            const tg = document.getElementById('tgInput').value.trim();
            if (!tg) { log('Enter a talkgroup first', 'error'); return; }
            const network = currentMode === 'BrandMeister' ? 'BM' : 'TGIF';
            try {
                const res  = await fetch('/api/favorites', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tg, network})
                });
                const data = await res.json();
                log(data.message, data.ok ? 'ok' : 'warn');
                if (quickTuneOpen) loadQuickTune();
            } catch(e) { log('Save favorite failed: ' + e, 'error'); }
        }

        async function removeFav(network, tg) {
            try {
                const res  = await fetch('/api/favorites/' + network + '/' + tg, {method: 'DELETE'});
                const data = await res.json();
                log(data.message, data.ok ? 'ok' : 'error');
                if (quickTuneOpen) loadQuickTune();
            } catch(e) { log('Remove favorite failed: ' + e, 'error'); }
        }

        // -------------------------
        // STATUS POLLING
        // -------------------------
        var tgInputPopulated = false;

        async function pollStatus() {
            try {
                const res = await fetch('/api/status');
                const d   = await res.json();

                currentMode = d.mode;
                const modeEl = document.getElementById('modeValue');
                modeEl.textContent = d.mode;
                modeEl.className   = 'mode-badge ' + (
                    d.mode === 'TGIF'         ? 'badge-tgif' :
                    d.mode === 'BrandMeister' ? 'badge-bm'   : 'badge-unknown'
                );

                const connEl    = document.getElementById('connState');
                const connLabel = {rx:'RX', idle:'READY', starting:'STARTING', offline:'OFFLINE'};
                connEl.textContent = d.status_source === 'cached'
                    ? 'LAST KNOWN'
                    : (connLabel[d.conn_state] || '--');
                connEl.className = 'conn-badge ' + (
                    d.status_source === 'cached' ? 'conn-cached' : 'conn-' + d.conn_state
                );

                document.getElementById('callValue').textContent        = d.call            || '--';
                document.getElementById('tgValue').textContent          = d.tg              || '--';
                document.getElementById('tgName').textContent           = d.tg_name         || '';
                document.getElementById('connectedSince').textContent   = d.connected_since || '--';
                document.getElementById('headerTime').textContent = timestamp();

                if (!tgInputPopulated && d.last_tg) {
                    document.getElementById('tgInput').value = d.last_tg;
                    tgInputPopulated = true;
                }

                ['stfu', 'mmdvm', 'analog'].forEach(svc => {
                    const running = d['svc_' + svc] === 'RUNNING';
                    document.getElementById('svc_' + svc).textContent = running ? 'RUN' : 'STOP';
                    document.getElementById('svc_' + svc).className   = running ? 'stat-val svc-text-on' : 'stat-val svc-text-off';
                    document.getElementById('dot_' + svc).className   = 'svc-dot ' + (running ? 'dot-on' : 'dot-off');
                });

                const usrpEl  = document.getElementById('svc_usrp');
                const usrpDot = document.getElementById('dot_usrp');
                if (d.usrp_connected) {
                    usrpEl.textContent = 'CONN';
                    usrpEl.className   = 'stat-val svc-text-on';
                    usrpDot.className  = 'svc-dot dot-on';
                } else if (d.usrp_registered) {
                    usrpEl.textContent = 'REG';
                    usrpEl.className   = 'stat-val svc-text-off';
                    usrpDot.className  = 'svc-dot dot-off';
                } else {
                    usrpEl.textContent = 'OFF';
                    usrpEl.className   = 'stat-val svc-text-off';
                    usrpDot.className  = 'svc-dot dot-off';
                }
            } catch(e) {
                log('Poll failed: ' + e, 'error');
            }
        }

        // -------------------------
        // ALLSTAR
        // -------------------------
        var asPlayer    = null;
        var allstarOpen = false;

        function toggleAllstar() {
            allstarOpen = !allstarOpen;
            document.getElementById('allstarBody').classList.toggle('open', allstarOpen);
            document.getElementById('allstarArrow').classList.toggle('open', allstarOpen);
            if (allstarOpen) pollAllstarStatus();
        }

        function allstarConnect() {
            const node = document.getElementById('asNodeInput').value.trim();
            fetch('/api/allstar/connect', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({node})
            })
            .then(r => r.json())
            .then(d => {
                log(d.message, d.ok ? 'ok' : 'error');
                setTimeout(pollAllstarStatus, 1500);
                setTimeout(pollAllstarStatus, 4000);
            })
            .catch(e => log('Allstar connect: ' + e, 'error'));
        }

        function allstarDisconnect() {
            fetch('/api/allstar/disconnect', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
            })
            .then(r => r.json())
            .then(d => {
                log(d.message, 'warn');
                if (asPlayer && asPlayer.isPlaying()) {
                    asPlayer.stop();
                    document.getElementById('btnAsAudio').classList.remove('active');
                }
                pollAllstarStatus();
            });
        }

        function allstarLink(mode) {
            const remote = document.getElementById('asRemoteNode').value.trim();
            if (!remote) { log('Enter a remote node number', 'error'); return; }
            fetch('/api/allstar/link', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({node: remote, mode})
            })
            .then(r => r.json())
            .then(d => log(d.message, d.ok ? 'ok' : 'error'))
            .catch(e => log('Allstar link: ' + e, 'error'));
        }

        function allstarUnlink() {
            const remote = document.getElementById('asRemoteNode').value.trim();
            if (!remote) { log('Enter a remote node number', 'error'); return; }
            fetch('/api/allstar/unlink', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({node: remote})
            })
            .then(r => r.json())
            .then(d => log(d.message, d.ok ? 'ok' : 'error'))
            .catch(e => log('Allstar unlink: ' + e, 'error'));
        }

        function toggleAllstarAudio(btn) {
            if (!asPlayer) asPlayer = new AllstarPlayer();
            if (asPlayer.isPlaying()) {
                asPlayer.stop();
                btn.classList.remove('active');
                log('Allstar audio stopped', 'warn');
            } else {
                asPlayer.play();
                btn.classList.add('active');
                log('Allstar audio started', 'ok');
            }
        }

        function setAllstarVolume(val) {
            val = parseInt(val);
            document.getElementById('asVolDisplay').textContent = val + '%';
            document.getElementById('asVolSlider').style.setProperty('--vol-pct', val + '%');
            if (asPlayer) asPlayer.setVolume(val);
        }

        var _asPoller = null;
        var _asNodePoller = null;

        async function pollAllstarNodes() {
            try {
                const res = await fetch('/api/allstar/nodes');
                const d   = await res.json();
                const el  = document.getElementById('asNodeList');
                if (!d.nodes || d.nodes.length === 0) {
                    el.textContent = '(none)';
                    return;
                }
                const modeLabel = {R: 'Mon', T: 'Xcv', M: 'Mon', L: 'Loc'};
                el.innerHTML = d.nodes.map(n =>
                    `<span style="display:inline-block;margin-right:10px;">
                        <span style="color:#5c5;">${n.node}</span>
                        <span style="color:#666;font-size:10px;">${modeLabel[n.mode] || n.mode}</span>
                    </span>`
                ).join('');
            } catch(e) {}
        }

        async function pollAllstarStatus() {
            try {
                const res = await fetch('/api/allstar/status');
                const d   = await res.json();
                const badge  = document.getElementById('asStateBadge');
                const nodeEl = document.getElementById('asNodeBadge');
                const dot    = document.getElementById('asRxDot');
                const sMap = {
                    idle:       ['OFFLINE',    'conn-offline'],
                    connecting: ['CONNECTING', 'conn-starting'],
                    connected:  ['CONNECTED',  'conn-idle'],
                    error:      ['ERROR',      'conn-offline'],
                };
                const [label, cls] = sMap[d.state] || ['--', 'conn-offline'];
                if (d.state === 'connected' && d.active) {
                    badge.textContent = 'RX';
                    badge.className   = 'conn-badge conn-active';
                } else {
                    badge.textContent = d.error || label;
                    badge.className   = 'conn-badge ' + cls;
                }
                if (dot) dot.className = 'rx-dot' + (d.active ? ' lit' : '');
                nodeEl.textContent = d.node || '--';

                const btnConn = document.getElementById('btnAsConnect');
                const btnDisc = document.getElementById('btnAsDisconnect');
                if (btnConn) btnConn.disabled = (d.state === 'connected' || d.state === 'connecting');
                if (btnDisc) btnDisc.disabled = (d.state === 'idle' || d.state === 'error');

                if (d.node && !document.getElementById('asNodeInput').value) {
                    document.getElementById('asNodeInput').value = d.node;
                }

                // keep polling while connected; stop when offline
                if (d.state === 'connected' || d.state === 'connecting') {
                    if (!_asPoller) _asPoller = setInterval(pollAllstarStatus, 400);
                    if (!_asNodePoller) { pollAllstarNodes(); _asNodePoller = setInterval(pollAllstarNodes, 3000); }
                } else {
                    if (_asPoller)     { clearInterval(_asPoller);     _asPoller     = null; }
                    if (_asNodePoller) { clearInterval(_asNodePoller); _asNodePoller = null; }
                    document.getElementById('asNodeList').textContent = '--';
                }
            } catch(e) { /* sidebar badge stays stale — non-fatal */ }
        }

        class AllstarPlayer {
            constructor() {
                this.ws      = null;
                this.player  = null;
                this.playing = false;
                this._vol    = 1.0;
            }

            play() {
                const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
                const url   = proto + '//' + location.host + '/ws/allstar-audio';
                this.player = new PCMPlayer({
                    encoding:    '16bitInt',
                    channels:    1,
                    sampleRate:  8000,
                    flushingTime: 500,
                });
                this.player.volume(this._vol);
                this.ws = new WebSocket(url);
                this.ws.binaryType = 'arraybuffer';
                this.ws.onmessage  = (e) => {
                    if (this.player) this.player.feed(new Uint8Array(e.data));
                };
                this.ws.onclose = () => {
                    if (this.playing) setTimeout(() => this.play(), 3000);
                };
                this.playing = true;
                this.player.play();
            }

            stop() {
                this.playing = false;
                if (this.ws)     { this.ws.close(); this.ws = null; }
                if (this.player) { this.player.stop(); this.player = null; }
            }

            isPlaying() { return this.playing; }

            setVolume(v) {
                this._vol = v / 100;
                if (this.player) this.player.volume(this._vol);
            }
        }

        // Poll Allstar sidebar status every 10 s
        pollAllstarStatus();
        setInterval(pollAllstarStatus, 10000);

        // -------------------------
        // STARTUP
        // -------------------------
        connectSSE();
        setInterval(pollStatus, 5000);
        pollStatus();
        applyStoredVolume();
        applyStoredFilters();
        log('Dispatcher ready', 'ok');
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML, audio_ws_url=AUDIO_WS_URL)

@app.route('/api/stream')
def stream():
    def event_stream():
        q = queue.Queue()
        with sse_lock:
            sse_clients.append(q)
        try:
            yield f"data: {json.dumps({'event': 'tx_start' if active_tx['active'] else 'tx_end', **active_tx})}\n\n"
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with sse_lock:
                sse_clients.remove(q)
    return Response(event_stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/status')
def status():
    s = get_status()
    s['time'] = datetime.now().strftime("%H:%M:%S")
    return jsonify(s)

@app.route('/api/lastheard')
def last_heard():
    return jsonify(get_last_heard())

@app.route('/api/log/<log_key>')
def get_log(log_key):
    if log_key not in LOG_FILES:
        return jsonify({"error": "Unknown log"}), 404
    lines = min(int(request.args.get('lines', 50)), 500)
    path  = get_log_path(log_key)
    if not os.path.exists(path):
        return jsonify({"path": path, "lines": [f"Log file not found: {path}"]})
    try:
        result = subprocess.run(['tail', '-n', str(lines), path], capture_output=True, timeout=5)
        text   = result.stdout.decode('utf-8', errors='replace')
        return jsonify({"path": path, "lines": text.splitlines()})
    except Exception as e:
        return jsonify({"path": path, "lines": [f"Error reading log: {e}"]})

@app.route('/api/tgif', methods=['POST'])
def tgif():
    subprocess.Popen(
        ['/bin/bash', '/opt/MMDVM_Bridge/connectTGIF.sh'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    last_state["network"] = "TGIF"
    save_last_state()
    return jsonify({"ok": True, "message": "Switching to TGIF (allow 20 seconds)..."})

@app.route('/api/bm', methods=['POST'])
def bm():
    subprocess.Popen(
        ['/bin/bash', '/opt/MMDVM_Bridge/connectBM.sh'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    last_state["network"] = "BM"
    save_last_state()
    return jsonify({"ok": True, "message": "Switching to BrandMeister (allow 20 seconds)..."})

@app.route('/api/restart', methods=['POST'])
def restart():
    run("sudo systemctl restart stfu.service")
    return jsonify({"ok": True, "message": "STFU service restarted"})

@app.route('/api/restart_ab', methods=['POST'])
def restart_ab():
    run("sudo systemctl restart analog_bridge.service")
    def reregister():
        time.sleep(5)
        try:
            send_registration()
        except Exception as e:
            print(f"Re-registration error: {e}")
    threading.Thread(target=reregister, daemon=True).start()
    return jsonify({"ok": True, "message": "Analog Bridge restarted"})

@app.route('/api/restart_mmdvm', methods=['POST'])
def restart_mmdvm():
    run("sudo systemctl restart mmdvm_bridge.service")
    return jsonify({"ok": True, "message": "MMDVM Bridge restarted"})

@app.route('/api/tune', methods=['POST'])
def tune():
    data = request.get_json()
    tg   = data.get('tg', '').strip()
    if not tg:
        return jsonify({"ok": False, "message": "No talkgroup provided"})
    if not tg.isdigit():
        return jsonify({"ok": False, "message": "Invalid talkgroup"})

    mode    = get_active_mode()
    network = "BM" if mode == "BrandMeister" else "TGIF"
    tg_name = lookup_tg(tg)
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    entry   = {"tg": tg, "name": tg_name, "network": network,
               "time": datetime.now().strftime("%H:%M:%S")}
    tune_history[:] = [h for h in tune_history if not (h['tg'] == tg and h['network'] == network)]
    tune_history.insert(0, entry)
    if len(tune_history) > HISTORY_MAX:
        tune_history.pop()

    last_state.update({"tg": tg, "tg_name": tg_name, "network": network, "time": now})
    save_last_state()

    run(f"/opt/MMDVM_Bridge/dvswitch.sh tune {tg}")
    return jsonify({"ok": True, "message": f"Tuned to {tg}"})

@app.route('/api/favorites', methods=['GET'])
def get_favs():
    with favorites_lock:
        return jsonify(favorites)

@app.route('/api/favorites', methods=['POST'])
def add_fav():
    data    = request.get_json()
    tg      = data.get('tg', '').strip()
    network = data.get('network', '').strip().upper()
    if not tg or not tg.isdigit():
        return jsonify({"ok": False, "message": "Invalid talkgroup"})
    if network not in ('BM', 'TGIF'):
        return jsonify({"ok": False, "message": "Invalid network"})
    cache = tg_cache_bm if network == 'BM' else tg_cache_tgif
    name  = cache.get(tg, '')
    with favorites_lock:
        fav = favorites.setdefault(network, [])
        if any(f['tg'] == tg for f in fav):
            return jsonify({"ok": True, "message": f"TG {tg} already in {network} favorites"})
        fav.append({"tg": tg, "name": name})
        save_favorites(favorites)
    return jsonify({"ok": True, "message": f"Saved TG {tg} to {network} favorites"})

@app.route('/api/favorites/<network>/<tg>', methods=['DELETE'])
def remove_fav(network, tg):
    network = network.upper()
    if network not in ('BM', 'TGIF'):
        return jsonify({"ok": False, "message": "Invalid network"})
    with favorites_lock:
        favorites[network] = [f for f in favorites.get(network, []) if f['tg'] != tg]
        save_favorites(favorites)
    return jsonify({"ok": True, "message": f"Removed TG {tg} from {network} favorites"})

@app.route('/api/tune_history', methods=['GET'])
def get_tune_history():
    return jsonify(tune_history)

@app.route('/api/debug/abinfo')
def debug_abinfo():
    try:
        with open(ABINFO_ACTIVE) as f:
            raw = json.load(f)
        return jsonify({"ok": True, "path": ABINFO_ACTIVE, "data": raw})
    except Exception as e:
        return jsonify({"ok": False, "path": ABINFO_ACTIVE, "error": str(e)})

# -------------------------
# ALLSTAR ROUTES
# -------------------------
@app.route('/api/allstar/status')
def allstar_status():
    return jsonify(allstar_mgr.status)


@app.route('/api/allstar/nodes')
def allstar_nodes():
    node = allstar_mgr.client.node if allstar_mgr.client else ALLSTAR_NODE
    out  = run(f'asterisk -rx "rpt nodes {node}"')
    nodes = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('*') or line.startswith('-'):
            continue
        # each entry looks like: R556982, Tiaxrpt
        for part in line.split(','):
            part = part.strip()
            if not part:
                continue
            mode   = part[0] if part else '?'
            number = part[1:].strip()
            if number.isdigit():
                nodes.append({'node': number, 'mode': mode})
    return jsonify({'nodes': nodes})


@app.route('/api/allstar/debug')
def allstar_debug():
    c = allstar_mgr.client
    if not c:
        return jsonify({'client': None})
    return jsonify({
        'state':      c.state,
        'error':      c.error_msg,
        'node':       c.node,
        'host':       c.host,
        'port':       c.port,
        'username':   c.username,
        'context':    c.context,
        'running':    c._running,
        'src_call':   c._src_call,
        'dst_call':   c._dst_call,
        'oseqno':     c._oseqno,
        'iseqno':     c._iseqno,
        'call_token': c._call_token is not None,
    })


@app.route('/api/allstar/connect', methods=['POST'])
def allstar_connect():
    data    = request.get_json() or {}
    node    = str(data.get('node', '')).strip() or ALLSTAR_NODE
    ok, msg = allstar_mgr.connect(node)
    return jsonify({'ok': ok, 'message': msg})


@app.route('/api/allstar/disconnect', methods=['POST'])
def allstar_disconnect():
    allstar_mgr.disconnect()
    return jsonify({'ok': True, 'message': 'Disconnected from Allstar'})


@app.route('/api/allstar/link', methods=['POST'])
def allstar_link():
    data   = request.get_json() or {}
    remote = str(data.get('node', '')).strip()
    mode   = data.get('mode', 'monitor')
    if not remote.isdigit():
        return jsonify({'ok': False, 'message': 'Invalid node number'})
    if not allstar_mgr.client or allstar_mgr.client.state != 'connected':
        return jsonify({'ok': False, 'message': 'Not connected to Allstar'})
    prefix = '*2' if mode == 'monitor' else '*3'
    try:
        allstar_mgr.send_dtmf(prefix + remote)
        return jsonify({'ok': True, 'message': f'Linking to {remote} ({mode})...'})
    except RuntimeError as e:
        return jsonify({'ok': False, 'message': str(e)})


@app.route('/api/allstar/unlink', methods=['POST'])
def allstar_unlink():
    data   = request.get_json() or {}
    remote = str(data.get('node', '')).strip()
    if not remote.isdigit():
        return jsonify({'ok': False, 'message': 'Invalid node number'})
    if not allstar_mgr.client or allstar_mgr.client.state != 'connected':
        return jsonify({'ok': False, 'message': 'Not connected to Allstar'})
    try:
        allstar_mgr.send_dtmf('*1' + remote)
        return jsonify({'ok': True, 'message': f'Unlinking {remote}...'})
    except RuntimeError as e:
        return jsonify({'ok': False, 'message': str(e)})


@sock.route('/ws/allstar-audio')
def allstar_audio_ws(ws):
    q = queue.Queue(maxsize=100)
    allstar_mgr.add_listener(q)
    try:
        while True:
            try:
                pcm = q.get(timeout=5)
            except queue.Empty:
                continue
            ws.send(pcm)
    except Exception:
        pass
    finally:
        allstar_mgr.remove_listener(q)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9090, debug=False, threaded=True)

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

try:
    from config import API_KEY
except ImportError:
    API_KEY = ''

try:
    from config import LISTEN_PORT
except ImportError:
    LISTEN_PORT = 9090

try:
    from config import DVSWITCH_SCRIPT
except ImportError:
    DVSWITCH_SCRIPT = '/opt/MMDVM_Bridge/dvswitch.sh'
try:
    from config import CONNECT_TGIF_SCRIPT
except ImportError:
    CONNECT_TGIF_SCRIPT = '/opt/MMDVM_Bridge/connectTGIF.sh'
try:
    from config import CONNECT_BM_SCRIPT
except ImportError:
    CONNECT_BM_SCRIPT = '/opt/MMDVM_Bridge/connectBM.sh'
try:
    from config import STFU_SERVICE
except ImportError:
    STFU_SERVICE = 'stfu.service'
try:
    from config import ANALOG_BRIDGE_SERVICE
except ImportError:
    ANALOG_BRIDGE_SERVICE = 'analog_bridge.service'
try:
    from config import MMDVM_SERVICE
except ImportError:
    MMDVM_SERVICE = 'mmdvm_bridge.service'
try:
    from config import DVSWITCHPLAYER_PORT
except ImportError:
    DVSWITCHPLAYER_PORT = 8080


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
clear_tx_gen = [0]  # mutable counter; increment to cancel pending clear_tx threads

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
            clear_tx_gen[0] += 1          # cancel any pending clear_tx
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
            clear_tx_gen[0] += 1
            my_gen = clear_tx_gen[0]
            def clear_tx(gen=my_gen):
                time.sleep(3)
                if clear_tx_gen[0] != gen:
                    return          # PTT came back up — don't clear
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

def require_key():
    """Return a 403 response if the X-Api-Key header doesn't match API_KEY, else None."""
    if not API_KEY:
        return jsonify({'ok': False, 'message': 'API_KEY not configured on server'}), 403
    if request.headers.get('X-Api-Key') != API_KEY:
        return jsonify({'ok': False, 'message': 'Unauthorized'}), 403
    return None


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
ALLSTAR_STATE_FILE = os.path.join(os.path.dirname(__file__), 'allstar_state.json')

def _load_allstar_state():
    try:
        with open(ALLSTAR_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_allstar_state(data):
    try:
        with open(ALLSTAR_STATE_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Warning: could not save allstar state: {e}")

class AllstarManager:
    def __init__(self):
        self.client       = None
        self._ws_qs       = []
        self._lock        = threading.Lock()
        self._last_audio  = 0.0
        self.linked_nodes = []   # updated by 'L ' TEXT frames from app_rpt
        self.direct_links = []   # cleared on startup; restored only after a live connect+link

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

    def _on_text(self, msg: str):
        # app_rpt sends 'L node1,node2,...' to report currently linked nodes.
        # An empty list arrives as 'L ' (space only).
        if msg.startswith('L '):
            raw = msg[2:].strip()
            nodes = []
            if raw:
                for part in raw.split(','):
                    part = part.strip()
                    if not part:
                        continue
                    mode   = part[0] if part else '?'
                    number = part[1:].strip()
                    if number.isdigit():
                        nodes.append({'node': number, 'mode': mode})
            self.linked_nodes = nodes

    def connect(self, node=None):
        if self.client and self.client.state in ('connecting', 'connected'):
            return False, 'Already connected'
        node = str(node or ALLSTAR_NODE).strip()
        if not node:
            return False, 'No node number configured'
        self.linked_nodes = []
        self.client = IAX2Client(
            ALLSTAR_HOST, ALLSTAR_PORT, ALLSTAR_USER, ALLSTAR_SECRET, node
        )
        self.client.on_audio(self._on_audio)
        self.client.on_text(self._on_text)
        self.client.connect()
        return True, f'Connecting to node {node}...'

    def disconnect(self):
        if self.client:
            self.client.disconnect()
            self.client = None
        self._set_direct_links([])

    def send_voice(self, pcm_bytes: bytes):
        if self.client and self.client.state == 'connected':
            self.client.send_voice(pcm_bytes)

    def add_direct_link(self, node: str):
        if node not in self.direct_links:
            self.direct_links.append(node)
        _save_allstar_state({'direct_links': self.direct_links})

    def remove_direct_link(self, node: str):
        self.direct_links = [n for n in self.direct_links if n != node]
        _save_allstar_state({'direct_links': self.direct_links})

    def _set_direct_links(self, nodes: list):
        self.direct_links = nodes
        _save_allstar_state({'direct_links': self.direct_links})

    @property
    def status(self):
        if not self.client:
            return {'state': 'idle', 'node': '', 'error': '', 'active': False, 'direct_links': self.direct_links}
        active = (self.client.state == 'connected' and
                  time.time() - self._last_audio < 0.6)
        return {
            'state':        self.client.state,
            'node':         self.client.node,
            'error':        self.client.error_msg,
            'active':       active,
            'direct_links': self.direct_links,
        }

    def send_dtmf(self, digits: str, inter_digit: float = 0.0):
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
        .header-bar h1 { font-size: 14px; letter-spacing: 2px; color: #ddd; }
        .header-time   { font-size: 11px; color: #888; }

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
            border: 1px solid #2e2e2e;
        }
        .sidebar-section h3 {
            font-size: 9px; color: #888;
            letter-spacing: 1px; margin-bottom: 8px;
            text-transform: uppercase;
        }

        .stat-row {
            display: flex; justify-content: space-between;
            align-items: center; padding: 3px 0;
            border-bottom: 1px solid #1f1f1f;
        }
        .stat-row:last-child { border-bottom: none; }
        .stat-key   { font-size: 10px; color: #999; }
        .stat-val   { font-size: 12px; color: #eee; text-align: right; }

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
        .sidebar-section.as-rx { background: #0a2010; border-radius: 6px; box-shadow: 0 0 6px #0f0; transition: background 0.3s, box-shadow 0.3s; }
        .collapse-panel.as-rx { box-shadow: 0 0 6px #0f0; transition: box-shadow 0.3s; }
        .status-strip { display:flex; align-items:center; gap:14px; padding:6px 12px; background:#1a1a1a; flex-wrap:wrap; }
        .strip-label { font-size:9px; color:#888; letter-spacing:1px; text-transform:uppercase; flex-shrink:0; }
        .strip-item { display:flex; align-items:center; gap:4px; font-size:10px; color:#bbb; }
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
        .svc-text-off { color: #999; font-size: 11px; }

        /* ---- ACTIVE TX IN SIDEBAR ---- */
        #dmrSection.active {
            background: #0d1f0d;
            border-color: #2a4a2a;
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
        button.btn-ptt { background: #1a1a1a; color: #888; border-color: #444; font-weight: bold; letter-spacing: 1px; }
        button.btn-ptt:hover:not(:disabled) { background: #2a1a1a; color: #c66; }
        button.btn-ptt.keyed { background: #cc2200; color: #fff; border-color: #ff4400; box-shadow: 0 0 12px #ff4400; animation: ptt-pulse 0.6s infinite alternate; }
        button.btn-ptt:disabled { opacity: 0.35; cursor: not-allowed; }
        @keyframes ptt-pulse { from { box-shadow: 0 0 8px #ff4400; } to { box-shadow: 0 0 20px #ff6600; } }
        button.btn-tune { background: #2a2a1a; color: gold; }
        button.btn-tune:hover { background: #3d3d23; }
        button.btn-monitor { background: #1a1a1a; color: #777; border-color: #333; }
        button.btn-monitor:hover { background: #222; color: #aaa; }
        button.btn-monitor.active { background: #006600; color: #fff; border-color: #00aa00; font-weight: bold; }
        button.btn-monitor.streaming,
        button.btn-monitor.active.streaming { background: #0055cc; color: #fff; border-color: #0055cc; font-weight: bold; box-shadow: 0 0 10px #0077ff; }
        button:disabled { opacity: 0.35; cursor: not-allowed; }
        .btn-sidebar-sm {
            font-size: 10px; padding: 2px 7px;
            border-radius: 3px; border: 1px solid #444;
            background: #222; color: #aaa;
            cursor: pointer; font-family: monospace;
        }
        .btn-sidebar-sm:hover { background: #333; color: #fff; }
        .btn-sidebar-sm.btn-monitor { background: #1a1a1a; color: #777; border-color: #333; }
        .btn-sidebar-sm.btn-monitor:hover { background: #222; color: #aaa; }
        .btn-sidebar-sm.btn-monitor.active { background: #006600; color: #fff; border-color: #00aa00; font-weight: bold; }
        .btn-sidebar-sm.btn-monitor.streaming,
        .btn-sidebar-sm.btn-monitor.active.streaming { background: #0055cc; color: #fff; border-color: #0055cc; font-weight: bold; box-shadow: 0 0 10px #0077ff; }
        .btn-restart-sm {
            font-size: 10px; padding: 2px 7px;
            border-radius: 3px; border: 1px solid #5a2020;
            background: #2a1a1a; color: #e88;
            cursor: pointer; font-family: monospace;
            width: 100%;
        }
        .btn-restart-sm:hover { background: #3d2323; color: #faa; }

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
        .log-info  { color: #aaa; }

        /* ---- COLLAPSIBLE PANELS ---- */
        .collapse-panel { background: #141414; border-radius: 6px; overflow: visible; border: 1px solid #1f1f1f; }
        .collapse-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 8px 12px; cursor: pointer; user-select: none;
            background: #1a1a1a;
        }
        .collapse-header:hover { background: #1f1f1f; }
        .collapse-header h3 { font-size: 10px; color: #bbb; letter-spacing: 1px; margin: 0; }
        .collapse-arrow { color: #888; font-size: 12px; transition: transform 0.2s; }
        .collapse-arrow.open { transform: rotate(180deg); }
        .collapse-body { display: none; padding: 10px; }
        .collapse-body.open { display: block; max-height: 60vh; overflow-y: auto; }

        /* ---- LAST HEARD ---- */
        #lastHeardTable { width: 100%; border-collapse: collapse; font-size: 11px; }
        #lastHeardTable th {
            text-align: left; color: #888; padding: 3px 8px;
            border-bottom: 1px solid #2a2a2a;
            font-weight: normal; font-size: 10px; letter-spacing: 1px;
        }
        #lastHeardTable td { padding: 4px 8px; border-bottom: 1px solid #222; }
        #lastHeardTable tr:hover td { background: #1f1f1f; }
        .lh-time     { color: #999; }
        .lh-callsign { color: orange; font-weight: bold; }
        .lh-dmrid    { color: #888; font-size: 10px; }
        .lh-tg       { color: lightgreen; }
        .lh-tgname   { color: #aaa; }
        .lh-tgif     { color: lime; font-size: 10px; }
        .lh-bm       { color: cyan; font-size: 10px; }

        /* ---- VOLUME SLIDER ---- */
        .vol-row {
            display: flex; justify-content: space-between;
            align-items: center; margin-bottom: 6px;
        }
        .vol-label { font-size: 10px; color: #aaa; }
        .vol-pct   { font-size: 11px; color: #ddd; }
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
            padding: 3px 10px; border-radius: 3px; background: #1f1f1f; color: #aaa;
            cursor: pointer; font-size: 11px; border: 1px solid #2a2a2a; font-family: monospace;
        }
        .log-tab:hover { background: #252525; color: #ddd; }
        .log-tab.active { background: #252525; color: #ddd; border-color: #555; }
        .log-tab.tab-mmdvm.active  { border-color: #7af; color: #7af; }
        .log-tab.tab-analog.active { border-color: lime; color: lime; }
        .log-tab.tab-stfu.active   { border-color: cyan; color: cyan; }

        .log-controls { display: flex; gap: 6px; margin-bottom: 6px; align-items: center; }
        .log-controls label { font-size: 10px; color: #aaa; }
        .log-controls input[type=number] {
            width: 50px; padding: 3px 5px; background: #1f1f1f;
            border: 1px solid #333; color: #fff; font-family: monospace;
            font-size: 11px; border-radius: 3px;
        }
        .log-controls button { padding: 3px 8px; font-size: 11px; }
        .btn-autoscroll.on { background: #1a3a1a; color: lime; }

        #logFileContent {
            background: #000; border-radius: 4px; padding: 8px;
            height: 220px; overflow-y: auto;
            font-size: 10px; line-height: 1.5; color: #bbb;
            white-space: pre-wrap; word-break: break-all;
        }
        .log-line-error { color: tomato; }
        .log-line-warn  { color: gold; }
        .log-line-info  { color: #ccc; }
        .log-line-debug { color: #aaa; }
        .log-file-label { font-size: 9px; color: #999; margin-bottom: 4px; }

        /* ---- QUICK TUNE ---- */
        .qt-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-bottom: 10px;
        }
        .qt-section-label {
            font-size: 9px; color: #999;
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
            background: #1f1515; color: #a06060;
            padding: 4px 7px; font-size: 10px; flex-shrink: 0;
            border: none; border-radius: 4px; cursor: pointer;
            font-family: monospace;
        }
        .btn-fav-del:hover { background: #3d2323; color: #e88; }
        .btn-save-fav { background: #2a2a1a; color: gold; }
        .btn-save-fav:hover { background: #3d3d23; }
        .qt-empty { color: #777; font-size: 10px; padding: 2px 0; }
        .qt-hist-net {
            font-size: 9px; color: #999;
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


            <!-- DMR AUDIO -->
            <div class="sidebar-section">
                <h3>DMR Audio</h3>
                <div class="vol-row">
                    <span class="vol-label">Volume</span>
                    <button id="dmrMuteBtn" onclick="toggleMuteDmr()" class="btn-sidebar-sm">Mute</button>
                    <button id="btnMonitor" onclick="toggleMonitor(this)" class="btn-sidebar-sm btn-monitor">&#128264; Monitor</button>
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
                       min="100" max="600" step="10" value="220"
                       oninput="setHpFilter(this.value)">
                <div class="vol-row" style="margin-top:8px;">
                    <span class="vol-label">Presence</span>
                    <span class="vol-pct" id="presDisplay" style="color:lime;">0 dB</span>
                </div>
                <input type="range" class="pres-slider" id="presSlider"
                       min="0" max="12" step="0.5" value="0"
                       oninput="setPresence(this.value)">
                <div class="vol-row" style="margin-top:8px;">
                    <span class="vol-label">Noise Gate</span>
                    <label style="cursor:pointer; color:#ddd; font-size:11px;">
                        <input type="checkbox" id="gateToggle" onchange="setGate(this.checked)"> Enable
                    </label>
                </div>
                <div id="audioStats" style="font-size:10px; color:#999; margin-top:8px;">Buffer: -- | Underruns: --</div>
            </div>

            <!-- ALLSTAR AUDIO -->
            <div class="sidebar-section">
                <h3>Allstar Audio</h3>
                <div class="vol-row">
                    <span class="vol-label">Volume</span>
                    <button id="asMuteBtn" onclick="toggleMuteAllstar()" class="btn-sidebar-sm">Mute</button>
                    <button id="btnAsAudioSidebar" onclick="toggleAllstarAudio(this)" class="btn-sidebar-sm btn-monitor">&#128264; Monitor</button>
                    <span class="vol-pct" id="asVolDisplay">100%</span>
                </div>
                <input type="range" class="vol-slider" id="asVolSlider"
                       min="0" max="100" value="100"
                       oninput="setAllstarVolume(this.value)">
                <div class="vol-row" style="margin-top:6px;">
                    <span class="vol-label" style="flex-shrink:0;">Mic</span>
                    <select id="micDeviceSelect" style="flex:1;background:#1a1a1a;color:#ccc;border:1px solid #333;border-radius:4px;font-size:11px;padding:2px 4px;" onchange="onMicDeviceChange()">
                        <option value="">-- select mic --</option>
                    </select>
                </div>
                <div style="margin-top:5px;display:flex;align-items:center;gap:6px;">
                    <span class="vol-label" style="flex-shrink:0;">Level</span>
                    <div id="micMeter" style="flex:1;height:8px;background:#111;border:1px solid #333;border-radius:4px;overflow:hidden;">
                        <div id="micMeterBar" style="height:100%;width:0%;background:#00cc44;border-radius:4px;transition:width 0.05s;"></div>
                    </div>
                </div>
            </div>

            <!-- RESTART BUTTONS -->
            <div class="sidebar-section">
                <h3>Services</h3>
                <div style="display:flex; flex-direction:column; gap:4px;">
                    <button id="btnRestart"   class="btn-restart-sm" onclick="action('/api/restart',       'Restarting STFU...')">&#8634; Restart STFU</button>
                    <button id="btnRestartAB" class="btn-restart-sm" onclick="action('/api/restart_ab',    'Restarting Analog Bridge...')">&#8634; Restart Analog</button>
                    <button id="btnRestartMM" class="btn-restart-sm" onclick="action('/api/restart_mmdvm', 'Restarting MMDVM...')">&#8634; Restart MMDVM</button>
                </div>
            </div>
        </div>

        <!-- MAIN CONTENT -->
        <div class="content">


            <!-- PANELS -->
            <div class="panels">

                <!-- DMR STATUS -->
                <div class="collapse-panel" id="dmrSection">
                    <div class="collapse-header" onclick="toggleDmrSection()">
                        <h3>&#128251; DMR</h3>
                        <span id="dmrActiveCall" style="color:orange;font-size:13px;font-weight:bold;margin-left:10px;letter-spacing:1px;"></span>
                        <span style="display:flex;align-items:center;gap:6px;margin-left:auto;margin-right:8px;">
                            <span class="tx-pulse" id="txPulse"></span>
                            <span class="mode-badge badge-unknown" id="modeValue">--</span>
                            <span class="conn-badge conn-offline" id="connState">OFFLINE</span>
                            <span id="tgValue" style="color:lightgreen;font-size:11px;font-weight:bold;"></span>
                            <span id="tgValueName" style="color:#6c6;font-size:10px;"></span>
                        </span>
                        <span class="collapse-arrow open" id="dmrArrow">&#9660;</span>
                    </div>
                    <div class="collapse-body open" id="dmrBody">
                        <div class="status-strip" style="padding:6px 0 4px; background:transparent;">
                            <span class="strip-item"><span id="txCallsign" style="color:lime;font-weight:bold;font-size:20px;letter-spacing:2px;">STANDBY</span></span>
                            <span class="strip-item"><span id="txDetail" style="color:#bbb;">&mdash;</span></span>
                            <span class="strip-item"><span id="tgName" style="color:#6c6;font-size:10px;"></span></span>
                            <span class="strip-item">Since <span id="connectedSince" style="color:#aaa;">--</span></span>
                            <span class="strip-item"><span id="txTime" style="color:#999;font-size:10px;">&nbsp;</span></span>
                        </div>
                        <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; padding-top:8px; border-top:1px solid #2a2a2a; margin-top:4px;">
                            <button class="btn-tgif" id="btnTGIF" onclick="action('/api/tgif', 'Switching to TGIF...')">&#9654; TGIF</button>
                            <button class="btn-bm"   id="btnBM"   onclick="action('/api/bm',   'Switching to BrandMeister...')">&#9654; BM</button>
                            <div class="controls-sep"></div>
                            <input class="tg-input" type="text" id="tgInput" placeholder="Talkgroup...">
                            <button class="btn-tune"     onclick="tuneTG()">&#9654; Tune</button>
                            <button class="btn-save-fav" onclick="saveFavorite()" title="Save to favorites for current network">&#9733; Fav</button>
                        </div>
                    </div>
                </div>

                <!-- STATUS STRIP -->
                <div class="collapse-panel">
                    <div class="status-strip">
                        <span class="strip-label">SERVICES</span>
                        <span class="strip-item"><span class="svc-dot" id="dot_stfu"></span>STFU <span id="svc_stfu" class="svc-text-off">--</span></span>
                        <span class="strip-item"><span class="svc-dot" id="dot_mmdvm"></span>MMDVM <span id="svc_mmdvm" class="svc-text-off">--</span></span>
                        <span class="strip-item"><span class="svc-dot" id="dot_analog"></span>Analog <span id="svc_analog" class="svc-text-off">--</span></span>
                        <span class="strip-item"><span class="svc-dot" id="dot_usrp"></span>USRP <span id="svc_usrp" class="svc-text-off">--</span></span>
                    </div>
                </div>

                <!-- ALLSTAR -->
                <div class="collapse-panel" id="asSidebarSection">
                    <div class="collapse-header" onclick="toggleAllstar()">
                        <h3>&#9889; ALLSTAR NODE</h3>
                        <span style="display:flex;align-items:center;gap:5px;margin-left:auto;margin-right:8px;">
                            <span class="rx-dot" id="asRxDot" title="RX activity"></span>
                            <span class="conn-badge conn-offline" id="asStateBadge">OFFLINE</span>
                            <span id="asNodeBadge" style="color:#ccc;font-size:11px;letter-spacing:0;font-weight:bold;">--</span>
                            <span id="asDirectLinkBadge" style="display:none;color:#4fc3f7;font-size:11px;font-weight:bold;">&#8594; <span id="asDirectLinkNode"></span></span>
                        </span>
                        <span class="collapse-arrow" id="allstarArrow">&#9660;</span>
                    </div>
                    <div class="collapse-body" id="allstarBody">
                        <div style="display:flex; gap:6px; margin-bottom:10px; align-items:center; flex-wrap:wrap;">
                            <button class="btn-monitor" id="btnAsConnect"   onclick="allstarConnect()">&#9654; Connect</button>
                            <button class="btn-danger"  id="btnAsDisconnect" onclick="allstarDisconnect()" disabled>&#9632; Disconnect</button>
                            <button class="btn-monitor" id="btnAsAudio"     onclick="toggleAllstarAudio(this)">&#128264; Audio</button>
                            <button class="btn-ptt" id="btnPTT" disabled
                                    onmousedown="pttStart()" onmouseup="pttStop()"
                                    ontouchstart="pttStart()" ontouchend="pttStop()"
                                    onmouseleave="pttStop()">&#127908; PTT</button>
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
                            <div class="qt-section-label" style="margin-bottom:5px;">COMMAND</div>
                            <div style="display:flex; gap:6px; align-items:center;">
                                <input class="tg-input" type="text" id="asCommand"
                                       placeholder="e.g. *70" style="width:120px;"
                                       onkeydown="if(event.key==='Enter') allstarCommand()">
                                <button class="btn-tune" onclick="allstarCommand()">&#9654; Send</button>
                                <button class="btn-tune" onclick="allstarSendCmd('*70')" title="Node status">&#9432; Status</button>
                            </div>
                        </div>
                        <div style="margin-bottom:10px;">
                            <div class="qt-section-label" style="margin-bottom:5px;">CONNECTED NODES</div>
                            <div id="asNodeList" style="font-size:12px; color:#ddd; min-height:16px;">--</div>
                        </div>
                    </div>
                </div>

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
                                <tr><td colspan="6" style="color:#777; padding:8px;">Open to load...</td></tr>
                            </tbody>
                        </table>
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
                    const cs = data.callsign || data.dmr_id || 'UNKNOWN';
                    document.getElementById('dmrSection').classList.add('active');
                    document.getElementById('txPulse').classList.add('on');
                    document.getElementById('dmrActiveCall').textContent = cs;
                    document.getElementById('txCallsign').textContent    = cs;
                    document.getElementById('txDetail').textContent      = 'TG: ' + data.tg + (data.tg_name ? ' → ' + data.tg_name : '');
                    document.getElementById('txTime').textContent        = 'Since ' + data.started;
                    log('TX: ' + cs + ' → TG ' + data.tg, 'ok');
                    if (lastHeardOpen) pollLastHeard();
                } else if (data.event === 'tx_end') {
                    document.getElementById('dmrSection').classList.remove('active');
                    document.getElementById('txPulse').classList.remove('on');
                    document.getElementById('dmrActiveCall').textContent = '';
                    document.getElementById('txCallsign').textContent    = 'STANDBY';
                    document.getElementById('txDetail').textContent      = '—';
                    document.getElementById('txTime').innerHTML          = '&nbsp;';
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
        var _dmrMuted = false;
        var _asMuted  = false;

        function setVolume(val) {
            val = parseInt(val);
            document.getElementById('volDisplay').textContent = val + '%';
            document.getElementById('volSlider').style.setProperty('--vol-pct', val + '%');
            if (!_dmrMuted && dvsp && dvsp.player) dvsp.player.volume(val / 100);
            localStorage.setItem('rxVolume', val);
        }

        function toggleMuteDmr() {
            _dmrMuted = !_dmrMuted;
            const btn = document.getElementById('dmrMuteBtn');
            btn.textContent = _dmrMuted ? 'Unmute' : 'Mute';
            btn.style.color = _dmrMuted ? '#f88' : '#aaa';
            btn.style.borderColor = _dmrMuted ? '#f44' : '#444';
            const vol = _dmrMuted ? 0 : parseInt(document.getElementById('volSlider').value) / 100;
            if (dvsp && dvsp.player) dvsp.player.volume(vol);
        }

        function toggleMuteAllstar() {
            _asMuted = !_asMuted;
            const btn = document.getElementById('asMuteBtn');
            btn.textContent = _asMuted ? 'Unmute' : 'Mute';
            btn.style.color = _asMuted ? '#f88' : '#aaa';
            btn.style.borderColor = _asMuted ? '#f44' : '#444';
            const vol = _asMuted ? 0 : parseInt(document.getElementById('asVolSlider').value);
            if (asPlayer) asPlayer.setVolume(vol);
        }

        function applyStoredVolume() {
            const dmrVol = localStorage.getItem('rxVolume');
            if (dmrVol !== null) {
                document.getElementById('volSlider').value = dmrVol;
                setVolume(dmrVol);
            }
            const asVol = localStorage.getItem('asVolume');
            if (asVol !== null) {
                document.getElementById('asVolSlider').value = asVol;
                setAllstarVolume(asVol);
            }
        }

        // -------------------------
        // DMR AUDIO — AudioWorklet ring-buffer player
        // -------------------------
        const WORKLET_CODE = `
class PCMRingProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        const sr = (options && options.processorOptions && options.processorOptions.sampleRate) || 8000;
        this._size  = sr * 3;
        this._buf   = new Float32Array(this._size);
        this._w     = 0;
        this._r     = 0;
        this._underruns   = 0;
        this._frame       = 0;
        this._reportEvery = Math.round(sr / 4);
        this._gateEnabled = false;
        this._gateOpen    = false;
        this._gateGain    = 0;
        // Track when audio last arrived so underruns during silence are ignored.
        // _silenceFrames counts consecutive silent render cycles; underruns only
        // count when this is below the threshold (i.e. signal was recently active).
        this._silenceFrames   = 0;
        this._silenceThreshold = Math.round(sr * 0.5);  // 500ms of silence = inactive
        this.port.onmessage = ({ data }) => {
            if (!data) return;
            if (data.pcm) {
                const pcm = data.pcm;
                for (let i = 0; i < pcm.length; i++) {
                    this._buf[this._w] = pcm[i];
                    this._w = (this._w + 1) % this._size;
                }
                this._silenceFrames = 0;  // reset silence counter on new data
            }
            if (data.gate !== undefined) this._gateEnabled = data.gate;
        };
    }
    get _avail() { return (this._w - this._r + this._size) % this._size; }
    process(inputs, outputs) {
        const out   = outputs[0][0];
        const n     = out.length;
        const avail = this._avail;
        if (avail < n) {
            out.fill(0);
            this._silenceFrames += n;
            if (this._silenceFrames <= this._silenceThreshold) {
                this._underruns++;
                this.port.postMessage({ underrun: true, buffered: avail, underruns: this._underruns });
            }
        } else {
            for (let i = 0; i < n; i++) {
                out[i]  = this._buf[this._r];
                this._r = (this._r + 1) % this._size;
            }
            if (this._gateEnabled) {
                let sumSq = 0;
                for (let i = 0; i < n; i++) sumSq += out[i] * out[i];
                const rms = Math.sqrt(sumSq / n);
                if (rms > 0.008) this._gateOpen = true;
                if (rms < 0.003) this._gateOpen = false;
                const target = this._gateOpen ? 1.0 : 0.0;
                for (let i = 0; i < n; i++) {
                    if (this._gateGain < target) this._gateGain = Math.min(target, this._gateGain + 0.05);
                    else this._gateGain = Math.max(target, this._gateGain - 0.002);
                    out[i] *= this._gateGain;
                }
            }
        }
        this._frame += n;
        if (this._frame % this._reportEvery < n) {
            this.port.postMessage({ stats: { buffered: this._avail, underruns: this._underruns } });
        }
        return true;
    }
}
registerProcessor('pcm-ring-processor', PCMRingProcessor);
`;

        class WorkletPlayer {
            constructor(sampleRate, onStats) {
                this.sampleRate  = sampleRate || 8000;
                this.option      = { sampleRate: this.sampleRate };  // DVSwitchPlayer reads this
                this._onStats    = onStats || null;
                this.audioCtx    = null;
                this.workletNode = null;
                this.gainNode    = null;
                this._ready      = false;
                this._queue      = [];
            }
            async init() {
                this.audioCtx = new (window.AudioContext || window.webkitAudioContext)({
                    sampleRate:  this.sampleRate,
                    latencyHint: 'interactive',
                });
                this.gainNode = this.audioCtx.createGain();
                this.gainNode.gain.value = 1;
                this.gainNode.connect(this.audioCtx.destination);

                const blob = new Blob([WORKLET_CODE], { type: 'application/javascript' });
                const url  = URL.createObjectURL(blob);
                await this.audioCtx.audioWorklet.addModule(url);
                URL.revokeObjectURL(url);

                this.workletNode = new AudioWorkletNode(this.audioCtx, 'pcm-ring-processor', {
                    processorOptions:   { sampleRate: this.sampleRate },
                    numberOfInputs:     0,
                    numberOfOutputs:    1,
                    outputChannelCount: [1],
                });
                this.workletNode.port.onmessage = ({ data }) => {
                    if (data && data.stats && this._onStats) this._onStats(data.stats);
                };
                this.workletNode.connect(this.gainNode);

                this._ready = true;
                for (const chunk of this._queue) this._post(chunk);
                this._queue = [];
            }
            feed(uint8) {
                const int16   = new Int16Array(uint8.buffer, uint8.byteOffset, uint8.byteLength >> 1);
                const float32 = new Float32Array(int16.length);
                for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
                if (this._ready) this._post(float32);
                else             this._queue.push(float32);
            }
            _post(f32) {
                this.workletNode.port.postMessage({ pcm: f32 }, [f32.buffer]);
            }
            volume(v) { if (this.gainNode) this.gainNode.gain.value = v; }
            play()    { /* audio is continuous via worklet process() */ }
            stop()    {
                this._ready = false;
                this._queue = [];
                if (this.audioCtx) { this.audioCtx.close(); this.audioCtx = null; }
                this.workletNode = null;
                this.gainNode    = null;
            }
        }

        var _workletPlayer = null;  // reference held so gate/stats can reach it

        function updateAudioStats(stats) {
            const el = document.getElementById('audioStats');
            if (!el) return;
            const ms = Math.round((stats.buffered || 0) / 8000 * 1000);
            el.textContent = 'Buffer: ' + ms + 'ms | Underruns: ' + (stats.underruns || 0);
            el.style.color = stats.underruns > 0 ? '#f80' : '#444';
        }

        function setGate(enabled) {
            if (_workletPlayer && _workletPlayer.workletNode)
                _workletPlayer.workletNode.port.postMessage({ gate: enabled });
            localStorage.setItem('rxGate', enabled ? '1' : '0');
        }

        var shelfFilter = null;
        var notchFilter = null;
        var hpFilter    = null;
        var presFilter  = null;
        var compressor  = null;

        function setupAudioFilters(player) {
            if (!player || !player.audioCtx) return;
            if (hpFilter) return;
            const ctx = player.audioCtx;

            shelfFilter = ctx.createBiquadFilter();
            shelfFilter.type = 'lowshelf';
            shelfFilter.frequency.value = 200;
            shelfFilter.gain.value      = -9;

            notchFilter = ctx.createBiquadFilter();
            notchFilter.type = 'peaking';
            notchFilter.frequency.value = 150;
            notchFilter.Q.value         = 2.0;
            notchFilter.gain.value      = -6;

            hpFilter = ctx.createBiquadFilter();
            hpFilter.type    = 'highpass';
            hpFilter.Q.value = 0.7;

            // Wider presence (Q 0.7 vs 1.0) for more natural speech restoration
            presFilter = ctx.createBiquadFilter();
            presFilter.type = 'peaking';
            presFilter.frequency.value = 2500;
            presFilter.Q.value         = 0.7;

            // Lighter compression: 3:1 ratio, -20 dBFS threshold, softer knee
            compressor = ctx.createDynamicsCompressor();
            compressor.threshold.value = -20;
            compressor.knee.value      = 8;
            compressor.ratio.value     = 3;
            compressor.attack.value    = 0.003;
            compressor.release.value   = 0.45;

            // gainNode → shelf → notch → hp → presence → compressor → destination
            player.gainNode.disconnect();
            player.gainNode.connect(shelfFilter);
            shelfFilter.connect(notchFilter);
            notchFilter.connect(hpFilter);
            hpFilter.connect(presFilter);
            presFilter.connect(compressor);
            compressor.connect(ctx.destination);

            hpFilter.frequency.value = parseInt(document.getElementById('hpfSlider').value);
            presFilter.gain.value    = parseFloat(document.getElementById('presSlider').value);
        }

        function setHpFilter(val) {
            val = parseInt(val);
            const pct = ((val - 100) / 500 * 100).toFixed(1);
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
            const hpVal   = parseInt(localStorage.getItem('rxHpFilter')  ?? 220);
            const presVal = parseFloat(localStorage.getItem('rxPresence') ?? 0);
            const gateOn  = localStorage.getItem('rxGate') === '1';
            document.getElementById('hpfSlider').value  = hpVal;
            document.getElementById('presSlider').value = presVal;
            if (document.getElementById('gateToggle')) document.getElementById('gateToggle').checked = gateOn;
            setHpFilter(hpVal);
            setPresence(presVal);
        }

        var dvsp = null;
        async function toggleMonitor(btn) {
            if (dvsp && dvsp.isPlaying()) {
                dvsp.stop();
                // WorkletPlayer.stop() closes the AudioContext; reset filter refs
                // so setupAudioFilters() will rewire them on next play.
                hpFilter = shelfFilter = notchFilter = presFilter = compressor = null;
                btn.classList.remove('active');
                log('RX Monitor stopped', 'warn');
            } else {
                if (!dvsp) {
                    dvsp = new DVSwitchPlayer({{ dvswitchplayer_port }}, document.createElement('button'));
                    dvsp.socketURL = '{{ audio_ws_url }}';
                    dvsp.ws = null;
                }
                // (Re-)create the WorkletPlayer each time so the AudioContext is fresh.
                _workletPlayer = new WorkletPlayer(dvsp.sampleRate, updateAudioStats);
                try {
                    await _workletPlayer.init();
                    dvsp.player = _workletPlayer;
                    setupAudioFilters(_workletPlayer);
                    // Restore gate state
                    const gateOn = localStorage.getItem('rxGate') === '1';
                    if (gateOn) setGate(true);
                } catch(e) {
                    log('AudioWorklet init failed: ' + e, 'error');
                    _workletPlayer = null;
                }
                applyStoredVolume();
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
                    body.innerHTML = '<tr><td colspan="6" style="color:#777; padding:6px;">No activity yet today</td></tr>';
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

        var dmrSectionOpen = true;
        function toggleDmrSection() {
            dmrSectionOpen = !dmrSectionOpen;
            document.getElementById('dmrBody').classList.toggle('open', dmrSectionOpen);
            document.getElementById('dmrArrow').classList.toggle('open', dmrSectionOpen);
        }

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
        const API_KEY = '{{ api_key }}';

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
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-Api-Key': API_KEY},
                    body: '{}'
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

                document.getElementById('tgValue').textContent     = d.tg              || '--';
                document.getElementById('tgValueName').textContent = d.tg_name         || '';
                document.getElementById('tgName').textContent      = d.tg_name         || '';
                document.getElementById('connectedSince').textContent   = d.connected_since || '--';
                document.getElementById('headerTime').textContent = timestamp();
                const dmrBtn = document.getElementById('btnMonitor');
                dmrBtn.classList.toggle('streaming', d.conn_state === 'rx' && dmrBtn.classList.contains('active'));

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
            fetch('/api/allstar/connect', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: '{}'
            })
            .then(r => r.json())
            .then(d => {
                log(d.message, d.ok ? 'ok' : 'error');
                setTimeout(pollAllstarStatus, 1500);
                setTimeout(pollAllstarStatus, 4000);
            })
            .catch(e => log('Allstar connect: ' + e, 'error'));
        }

        var _asDirectLink = null;

        function _setDirectLink(nodes) {
            // accepts a single node string, array of node strings, or null/empty
            const list = Array.isArray(nodes) ? nodes.filter(Boolean) : (nodes ? [nodes] : []);
            _asDirectLink = list.length ? list : null;
            const badge = document.getElementById('asDirectLinkBadge');
            const nodeEl = document.getElementById('asDirectLinkNode');
            if (_asDirectLink) {
                nodeEl.textContent = _asDirectLink.join(' · ');
                badge.style.display = '';
            } else {
                badge.style.display = 'none';
                nodeEl.textContent = '';
            }
        }

        function allstarDisconnect() {
            fetch('/api/allstar/disconnect', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
            })
            .then(r => r.json())
            .then(d => {
                log(d.message, 'warn');
                _setDirectLink(null);
                if (asPlayer && asPlayer.isPlaying()) {
                    asPlayer.stop();
                    _syncAsAudioBtns(false);
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
            .then(d => {
                log(d.message, d.ok ? 'ok' : 'error');
                if (d.ok) _setDirectLink(remote);
            })
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
            .then(d => {
                log(d.message, d.ok ? 'ok' : 'error');
                if (d.ok) _setDirectLink(null);
            })
            .catch(e => log('Allstar unlink: ' + e, 'error'));
        }

        function allstarSendCmd(cmd) {
            fetch('/api/allstar/command', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({cmd})
            })
            .then(r => r.json())
            .then(d => log(d.message, d.ok ? 'ok' : 'error'))
            .catch(e => log('Allstar command: ' + e, 'error'));
        }

        function allstarCommand() {
            const cmd = document.getElementById('asCommand').value.trim();
            if (!cmd) { log('Enter a command', 'error'); return; }
            fetch('/api/allstar/command', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({cmd})
            })
            .then(r => r.json())
            .then(d => log(d.message, d.ok ? 'ok' : 'error'))
            .catch(e => log('Allstar command: ' + e, 'error'));
        }

        function _syncAsAudioBtns(active) {
            ['btnAsAudio', 'btnAsAudioSidebar'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.classList.toggle('active', active);
            });
        }

        async function toggleAllstarAudio(btn) {
            if (!asPlayer) asPlayer = new AllstarPlayer();
            if (asPlayer.isPlaying()) {
                asPlayer.stop();
                _syncAsAudioBtns(false);
                log('Allstar audio stopped', 'warn');
            } else {
                await asPlayer.play();
                _syncAsAudioBtns(true);
                log('Allstar audio started', 'ok');
            }
        }

        function setAllstarVolume(val) {
            val = parseInt(val);
            document.getElementById('asVolDisplay').textContent = val + '%';
            document.getElementById('asVolSlider').style.setProperty('--vol-pct', val + '%');
            if (!_asMuted && asPlayer) asPlayer.setVolume(val);
            localStorage.setItem('asVolume', val);
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
                        <span style="color:#7e7;">${n.node}</span>
                        <span style="color:#aaa;font-size:10px;">${modeLabel[n.mode] || n.mode}</span>
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
                _setDirectLink(d.state === 'connected' && d.direct_links && d.direct_links.length ? d.direct_links : null);
                const asSec = document.getElementById('asSidebarSection');
                if (asSec) asSec.classList.toggle('as-rx', !!(d.state === 'connected' && d.active));
                ['btnAsAudio', 'btnAsAudioSidebar'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el) el.classList.toggle('streaming', !!(d.state === 'connected' && d.active));
                });

                const btnConn = document.getElementById('btnAsConnect');
                const btnDisc = document.getElementById('btnAsDisconnect');
                const btnPTT  = document.getElementById('btnPTT');
                if (btnConn) btnConn.disabled = (d.state === 'connected' || d.state === 'connecting');
                if (btnDisc) btnDisc.disabled = (d.state === 'idle' || d.state === 'error');
                if (btnPTT)  btnPTT.disabled  = (d.state !== 'connected');
                if (d.state !== 'connected') pttStop();

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
                this._wp     = null;
                this.playing = false;
                this._vol    = 1.0;
            }

            async play() {
                this._wp = new WorkletPlayer(8000);
                try {
                    await this._wp.init();
                } catch(e) {
                    log('Allstar AudioWorklet failed: ' + e, 'error');
                    this._wp = null;
                    return;
                }
                this._wp.volume(this._vol);

                const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
                const url   = proto + '//' + location.host + '/ws/allstar-audio';
                this.ws = new WebSocket(url);
                this.ws.binaryType = 'arraybuffer';
                this.ws.onmessage  = (e) => {
                    if (this._wp) this._wp.feed(new Uint8Array(e.data));
                };
                this.ws.onclose = () => {
                    if (this.playing) setTimeout(() => this.play(), 3000);
                };
                this.playing = true;
            }

            stop() {
                this.playing = false;
                if (this.ws)  { this.ws.close(); this.ws = null; }
                if (this._wp) { this._wp.stop(); this._wp = null; }
            }

            isPlaying() { return this.playing; }

            setVolume(v) {
                this._vol = v / 100;
                if (this._wp) this._wp.volume(this._vol);
            }
        }

        setInterval(pollAllstarStatus, 10000);

        // -------------------------
        // PTT HOLD-TO-TALK
        // -------------------------
        let _pttCtx = null, _pttStream = null, _pttWs = null, _pttNode = null, _pttActive = false;
        let _micDeviceId = null;   // selected deviceId or null = default
        let _micMonCtx = null, _micMonStream = null, _micMonAnalyser = null, _micMonRaf = null;

        const PTT_WORKLET_CODE = `
class MicDownsampler extends AudioWorkletProcessor {
    constructor(options) {
        super();
        this._ratio = options.processorOptions.ratio;
        this._buf   = [];
    }
    process(inputs) {
        const ch = inputs[0][0];
        if (!ch) return true;
        for (let i = 0; i < ch.length; i += this._ratio) {
            let s = 0, n = 0;
            for (let j = 0; j < this._ratio && (i+j) < ch.length; j++, n++) s += ch[i+j];
            this._buf.push(Math.max(-1, Math.min(1, s / n)));
            if (this._buf.length >= 160) {
                const out = new Int16Array(160);
                for (let k = 0; k < 160; k++) out[k] = Math.round(this._buf[k] * 32767);
                this.port.postMessage(out.buffer, [out.buffer]);
                this._buf = [];
            }
        }
        return true;
    }
}
registerProcessor('mic-downsampler', MicDownsampler);
`;

        // Populate mic device list (called once on page load and after getUserMedia grants permission)
        async function populateMicDevices() {
            try {
                const devices = await navigator.mediaDevices.enumerateDevices();
                const sel = document.getElementById('micDeviceSelect');
                const prev = sel.value;
                // Remove all options except the placeholder
                while (sel.options.length > 1) sel.remove(1);
                let found = false;
                devices.filter(d => d.kind === 'audioinput').forEach(d => {
                    const opt = document.createElement('option');
                    opt.value = d.deviceId;
                    opt.textContent = d.label || ('Mic ' + d.deviceId.slice(0, 8));
                    sel.appendChild(opt);
                    if (d.deviceId === prev) { opt.selected = true; found = true; }
                });
                if (!found && sel.options.length > 1) {
                    // auto-select default
                    sel.selectedIndex = 1;
                    _micDeviceId = sel.value;
                }
                // Restart level meter with new device if not currently in PTT
                if (!_pttActive) startMicMeter();
            } catch(e) { console.warn('enumerateDevices failed:', e); }
        }

        function onMicDeviceChange() {
            const sel = document.getElementById('micDeviceSelect');
            _micDeviceId = sel.value || null;
            if (!_pttActive) { stopMicMeter(); startMicMeter(); }
        }

        // Always-on mic level meter (separate from PTT stream)
        async function startMicMeter() {
            stopMicMeter();
            if (!_micDeviceId && document.getElementById('micDeviceSelect').options.length <= 1) return;
            try {
                const constraints = { audio: _micDeviceId
                    ? { deviceId: { exact: _micDeviceId } }
                    : { echoCancellation: false, noiseSuppression: false } };
                _micMonStream = await navigator.mediaDevices.getUserMedia(constraints);
                // After first grant, repopulate with labelled devices
                await populateMicDevices();
                _micMonCtx    = new AudioContext();
                _micMonAnalyser = _micMonCtx.createAnalyser();
                _micMonAnalyser.fftSize = 256;
                const src = _micMonCtx.createMediaStreamSource(_micMonStream);
                src.connect(_micMonAnalyser);
                const buf = new Uint8Array(_micMonAnalyser.frequencyBinCount);
                const bar = document.getElementById('micMeterBar');
                function tick() {
                    _micMonRaf = requestAnimationFrame(tick);
                    _micMonAnalyser.getByteTimeDomainData(buf);
                    let peak = 0;
                    for (let i = 0; i < buf.length; i++) {
                        const v = Math.abs(buf[i] - 128) / 128;
                        if (v > peak) peak = v;
                    }
                    const pct = Math.min(100, peak * 200);
                    bar.style.width = pct + '%';
                    bar.style.background = pct > 80 ? '#ff4400' : pct > 50 ? '#ffaa00' : '#00cc44';
                }
                tick();
            } catch(e) {
                console.warn('Mic meter failed:', e);
            }
        }

        function stopMicMeter() {
            if (_micMonRaf)    { cancelAnimationFrame(_micMonRaf); _micMonRaf = null; }
            if (_micMonAnalyser) { _micMonAnalyser = null; }
            if (_micMonStream) { _micMonStream.getTracks().forEach(t => t.stop()); _micMonStream = null; }
            if (_micMonCtx)    { try { _micMonCtx.close(); } catch(e) {} _micMonCtx = null; }
            const bar = document.getElementById('micMeterBar');
            if (bar) bar.style.width = '0%';
        }

        async function pttStart() {
            const btn = document.getElementById('btnPTT');
            if (!btn || btn.disabled || _pttActive) return;
            _pttActive = true;
            btn.classList.add('keyed');
            // Stop the monitor stream — PTT will open its own
            stopMicMeter();

            try {
                if (!_pttCtx) {
                    _pttCtx = new AudioContext({ sampleRate: 48000 });
                    const blob = new Blob([PTT_WORKLET_CODE], { type: 'application/javascript' });
                    const url  = URL.createObjectURL(blob);
                    await _pttCtx.audioWorklet.addModule(url);
                    URL.revokeObjectURL(url);
                }
                if (_pttCtx.state === 'suspended') await _pttCtx.resume();

                const audioConstraints = _micDeviceId
                    ? { deviceId: { exact: _micDeviceId }, echoCancellation: true, noiseSuppression: true }
                    : { echoCancellation: true, noiseSuppression: true };
                _pttStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });

                const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
                _pttWs = new WebSocket(proto + '//' + location.host + '/ws/allstar-tx');
                _pttWs.binaryType = 'arraybuffer';
                await new Promise((res, rej) => {
                    _pttWs.onopen  = res;
                    _pttWs.onerror = rej;
                });

                const ratio = Math.round(_pttCtx.sampleRate / 8000);
                _pttNode = new AudioWorkletNode(_pttCtx, 'mic-downsampler', {
                    processorOptions: { ratio }
                });

                // Drive the meter bar from the PTT stream while keyed
                const pttAnalyser = _pttCtx.createAnalyser();
                pttAnalyser.fftSize = 256;
                const pttBuf = new Uint8Array(pttAnalyser.frequencyBinCount);
                const bar = document.getElementById('micMeterBar');
                let rafId;
                function meterTick() {
                    rafId = requestAnimationFrame(meterTick);
                    pttAnalyser.getByteTimeDomainData(pttBuf);
                    let peak = 0;
                    for (let i = 0; i < pttBuf.length; i++) {
                        const v = Math.abs(pttBuf[i] - 128) / 128;
                        if (v > peak) peak = v;
                    }
                    const pct = Math.min(100, peak * 200);
                    if (bar) { bar.style.width = pct + '%'; bar.style.background = pct > 80 ? '#ff4400' : pct > 50 ? '#ffaa00' : '#00cc44'; }
                }
                meterTick();
                // Store rafId so pttStop can cancel it
                _pttNode._meterRaf = rafId;
                _pttNode._meterRafFn = () => cancelAnimationFrame(rafId);

                _pttNode.port.onmessage = (e) => {
                    if (_pttWs && _pttWs.readyState === WebSocket.OPEN) {
                        _pttWs.send(e.data);
                    }
                };

                const src = _pttCtx.createMediaStreamSource(_pttStream);
                src.connect(pttAnalyser);
                src.connect(_pttNode);
                _pttNode.connect(_pttCtx.destination);

            } catch(err) {
                console.error('PTT start failed:', err);
                log('PTT error: ' + err.message, 'error');
                pttStop();
            }
        }

        function pttStop() {
            _pttActive = false;
            const btn = document.getElementById('btnPTT');
            if (btn) btn.classList.remove('keyed');
            if (_pttNode) {
                if (_pttNode._meterRafFn) _pttNode._meterRafFn();
                try { _pttNode.disconnect(); } catch(e) {}
                _pttNode = null;
            }
            if (_pttStream) { _pttStream.getTracks().forEach(t => t.stop()); _pttStream = null; }
            if (_pttWs)     { try { _pttWs.close(); } catch(e) {} _pttWs = null; }
            // Resume the always-on level meter
            startMicMeter();
        }

        // -------------------------
        // STARTUP
        // -------------------------
        connectSSE();
        setInterval(pollStatus, 5000);
        pollStatus();
        applyStoredVolume();
        applyStoredFilters();
        log('Dispatcher ready', 'ok');
        // Enumerate mic devices (labels only available after permission granted via startMicMeter)
        navigator.mediaDevices.enumerateDevices().then(devices => {
            const inputs = devices.filter(d => d.kind === 'audioinput');
            if (inputs.length) populateMicDevices().then(() => startMicMeter());
        }).catch(() => {});
        // Auto-connect to the configured Allstar node
        pollAllstarStatus().then(() => {
            const btn = document.getElementById('btnAsConnect');
            if (btn && !btn.disabled) allstarConnect();
        });
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML, audio_ws_url=AUDIO_WS_URL,
                                  dvswitchplayer_port=DVSWITCHPLAYER_PORT,
                                  api_key=API_KEY)

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
        ['/bin/bash', CONNECT_TGIF_SCRIPT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    last_state["network"] = "TGIF"
    save_last_state()
    return jsonify({"ok": True, "message": "Switching to TGIF (allow 20 seconds)..."})

@app.route('/api/bm', methods=['POST'])
def bm():
    subprocess.Popen(
        ['/bin/bash', CONNECT_BM_SCRIPT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    last_state["network"] = "BM"
    save_last_state()
    return jsonify({"ok": True, "message": "Switching to BrandMeister (allow 20 seconds)..."})

@app.route('/api/restart', methods=['POST'])
def restart():
    err = require_key()
    if err:
        return err
    run(f"sudo systemctl restart {STFU_SERVICE}")
    return jsonify({"ok": True, "message": "STFU service restarted"})

@app.route('/api/restart_ab', methods=['POST'])
def restart_ab():
    err = require_key()
    if err:
        return err
    run(f"sudo systemctl restart {ANALOG_BRIDGE_SERVICE}")
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
    err = require_key()
    if err:
        return err
    run(f"sudo systemctl restart {MMDVM_SERVICE}")
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

    run(f"{DVSWITCH_SCRIPT} tune {tg}")
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
    return jsonify({'nodes': allstar_mgr.linked_nodes})


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
    allstar_mgr.disconnect()  # also clears direct_links
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
        allstar_mgr.add_direct_link(remote)
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
        allstar_mgr.remove_direct_link(remote)
        return jsonify({'ok': True, 'message': f'Unlinking {remote}...'})
    except RuntimeError as e:
        return jsonify({'ok': False, 'message': str(e)})


@app.route('/api/allstar/command', methods=['POST'])
def allstar_command():
    data = request.get_json() or {}
    cmd  = str(data.get('cmd', '')).strip()
    if not cmd:
        return jsonify({'ok': False, 'message': 'No command'})
    if not allstar_mgr.client or allstar_mgr.client.state != 'connected':
        return jsonify({'ok': False, 'message': 'Not connected to Allstar'})
    try:
        allstar_mgr.send_dtmf(cmd)
        return jsonify({'ok': True, 'message': f'Sent: {cmd}'})
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


@sock.route('/ws/allstar-tx')
def allstar_tx_ws(ws):
    """Receive Int16 PCM from the browser and send it up the IAX2 stack."""
    try:
        while True:
            data = ws.receive()
            if data is None:
                break
            if isinstance(data, bytes) and len(data) >= 2:
                allstar_mgr.send_voice(data)
    except Exception:
        pass


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=LISTEN_PORT, debug=False, threaded=True)

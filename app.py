from flask import Flask, jsonify, request, render_template_string, Response, send_file
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
import uuid
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

try:
    from config import TR_API_KEY
except ImportError:
    TR_API_KEY = ''
try:
    from config import TR_AUDIO_DIR
except ImportError:
    TR_AUDIO_DIR = '/tmp/dispatcher-tr-audio'
try:
    from config import TR_MAX_CALLS
except ImportError:
    TR_MAX_CALLS = 5000
try:
    from config import TR_SYSTEMS
except ImportError:
    # Map short_name → display label. Auto-populated from uploads if not set.
    TR_SYSTEMS = {}
try:
    from config import SDR_SCANNER_URL
except ImportError:
    SDR_SCANNER_URL = 'http://172.31.10.192:8080'


FAVORITES_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'favorites.json')
AS_FAVORITES_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'as_favorites.json')
LAST_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'last_state.json')
TG_NAMES_CACHE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tg_names_cache.json')

# Trunk Recorder audio storage
os.makedirs(TR_AUDIO_DIR, exist_ok=True)
TR_CALLS_FILE = os.path.join(TR_AUDIO_DIR, 'calls.json')

# Call history (newest first). Persisted to TR_CALLS_FILE across restarts.
tr_calls_lock = threading.Lock()

def _load_tr_calls():
    """Load call history from disk, dropping entries whose audio file is gone."""
    try:
        with open(TR_CALLS_FILE) as f:
            calls = json.load(f)
        existing = set(os.listdir(TR_AUDIO_DIR))
        return [c for c in calls if c.get('audio') and c['audio'] in existing]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

def _save_tr_calls():
    """Persist current call list to disk (called under tr_calls_lock)."""
    try:
        with open(TR_CALLS_FILE, 'w') as f:
            json.dump(tr_calls, f)
    except OSError:
        pass

tr_calls = _load_tr_calls()

# Known systems: short_name → display label (seeded from config, grows on upload)
tr_systems = dict(TR_SYSTEMS)

# Talkgroups seen from live calls: short_name → {tg_id(int) → {tag, label, group, description}}
tr_seen_tgs = {}

# Rebuild tr_systems and tr_seen_tgs from persisted call history so they
# survive restarts without waiting for the next live upload.
for _c in tr_calls:
    _sn = _c.get('system', '')
    if _sn and _sn not in tr_systems:
        tr_systems[_sn] = _c.get('system_label') or _sn
    if _sn and _c.get('talkgroup') is not None:
        tr_seen_tgs.setdefault(_sn, {}).setdefault(int(_c['talkgroup']), {
            'tag':         _c.get('talkgroup_tag', ''),
            'label':       _c.get('talkgroup_label', ''),
            'group':       _c.get('talkgroup_group', ''),
            'description': _c.get('talkgroup_description', ''),
        })

# Talkgroup lookup: short_name → {talkgroup_id(int) → {tag, description, group}}
tr_talkgroups      = {}
tr_talkgroups_lock = threading.Lock()
TR_TG_DIR          = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tr_talkgroups')
os.makedirs(TR_TG_DIR, exist_ok=True)

def _tg_file(short_name):
    return os.path.join(TR_TG_DIR, short_name.replace('/', '_') + '.json')

def _load_all_tg_files():
    for fname in os.listdir(TR_TG_DIR):
        if not fname.endswith('.json'):
            continue
        short_name = fname[:-5]
        try:
            with open(os.path.join(TR_TG_DIR, fname)) as f:
                data = json.load(f)
            # keys stored as strings in JSON; convert to int on load
            tr_talkgroups[short_name] = {int(k): v for k, v in data.items()}
        except Exception as e:
            print(f'Warning: could not load TG file {fname}: {e}')

_load_all_tg_files()

ABINFO_ACTIVE  = '/tmp/ABInfo_31001.json'
TGLIST_BM      = '/tmp/TGList_BM.txt'
TGLIST_TGIF    = '/tmp/TGList_TGIF.txt'
TGIF_NODE_LIST = '/tmp/TGIF_node_list.txt'
DMRIDS_FILE    = '/var/lib/mmdvm/DMRIds.dat'
USRP_HOST      = '127.0.0.1'
USRP_PORT      = 31001
USRP_LISTEN    = 31002

LOG_FILES = {
    'mmdvm':    '/var/log/mmdvm/MMDVM_Bridge-{date}.log',
    'analog':   '/var/log/dvswitch/Analog_Bridge-{date}.log',
    'stfu':     '/var/log/dvswitch/STFU.log',
    'watchdog': '/var/log/dispatcher-watchdog.log',
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
favorites_lock    = threading.Lock()
as_favorites_lock = threading.Lock()
tune_history      = []

def load_as_favorites():
    try:
        with open(AS_FAVORITES_FILE) as f:
            return json.load(f)
    except:
        return []

def save_as_favorites(data):
    try:
        with open(AS_FAVORITES_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save Allstar favorites: {e}")

as_favorites = load_as_favorites()
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
        self.direct_links = []   # not restored from disk — live L frames are authoritative

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

    def reset_voice_ts(self):
        if self.client:
            self.client.reset_voice_ts()

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
            return {'state': 'idle', 'node': '', 'error': '', 'active': False,
                    'direct_links': self.direct_links, 'linked_nodes': self.linked_nodes}
        active = (self.client.state == 'connected' and
                  time.time() - self._last_audio < 0.6)
        return {
            'state':        self.client.state,
            'node':         self.client.node,
            'error':        self.client.error_msg,
            'active':       active,
            'direct_links': self.direct_links,
            'linked_nodes': self.linked_nodes,
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

# -------------------------
# SDR SCANNER RELAY
# -------------------------
import urllib.request
import urllib.error

_sdr_state = {
    'connected': False,
    'freq':      None,
    'label':     None,
    'active':    False,
    'db':        None,
    'holdFreq':  None,
    'channels':  {},
    'skipped':   [],
}
_sdr_state_lock = threading.Lock()

def _sdr_ws_url():
    base = SDR_SCANNER_URL.rstrip('/')
    return base.replace('http://', 'ws://').replace('https://', 'wss://') + '/ws'

def _sdr_api_url(path):
    return SDR_SCANNER_URL.rstrip('/') + path

def _sdr_relay_loop():
    """Background thread: connects to scanner /ws, relays events into dispatcher SSE."""
    import json as _json
    # Use websocket-client if available, else simple HTTP upgrade
    try:
        import websocket as _ws_lib
        _HAS_WS = True
    except ImportError:
        _HAS_WS = False

    while True:
        if not _HAS_WS:
            time.sleep(30)
            continue
        try:
            ws = _ws_lib.WebSocketApp(
                _sdr_ws_url(),
                on_open=_sdr_on_open,
                on_message=_sdr_on_message,
                on_close=_sdr_on_close,
                on_error=_sdr_on_error,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f'SDR relay error: {e}')
        with _sdr_state_lock:
            _sdr_state['connected'] = False
        push_event({'event': 'sdr_state', **_sdr_state_snapshot()})
        time.sleep(5)

def _sdr_state_snapshot():
    with _sdr_state_lock:
        return dict(_sdr_state)

def _sdr_on_open(ws):
    with _sdr_state_lock:
        _sdr_state['connected'] = True
    push_event({'event': 'sdr_state', **_sdr_state_snapshot()})

def _sdr_on_close(ws, code, msg):
    with _sdr_state_lock:
        _sdr_state['connected'] = False
        _sdr_state['active']    = False
    push_event({'event': 'sdr_state', **_sdr_state_snapshot()})

def _sdr_on_error(ws, err):
    pass  # reconnect handled by run_forever restart

def _sdr_on_message(ws, raw):
    import json as _json
    try:
        m = _json.loads(raw)
    except Exception:
        return
    t = m.get('type', '')
    with _sdr_state_lock:
        if t == 'freq_change':
            _sdr_state['freq']  = m.get('freq')
            _sdr_state['label'] = m.get('label') or m.get('freq')
            _sdr_state['active'] = False
        elif t == 'freq_clear':
            _sdr_state['active'] = False
            _sdr_state['db']     = None
        elif t == 'signal':
            _sdr_state['active'] = m.get('active', False)
            _sdr_state['db']     = m.get('db')
        elif t == 'channels_update':
            _sdr_state['channels'] = m.get('channels', {})
            _sdr_state['skipped']  = m.get('skipped', [])
            _sdr_state['holdFreq'] = m.get('holdFreq')
        elif t == 'hold_update':
            _sdr_state['holdFreq'] = m.get('holdFreq')
    push_event({'event': 'sdr_' + t, **m})

def _start_sdr_relay():
    try:
        import websocket  # noqa
    except ImportError:
        print('SDR relay disabled: install websocket-client (pip install websocket-client)')
        return
    t = threading.Thread(target=_sdr_relay_loop, daemon=True)
    t.start()


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
_start_sdr_relay()

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
        .app-body { display: grid; grid-template-columns: 260px 1fr; gap: 0; height: calc(100vh - 37px); transition: grid-template-columns 0.25s ease; }
        .app-body.sidebar-collapsed { grid-template-columns: 0px 1fr; }
        @media (max-width: 600px) {
            .app-body, .app-body.sidebar-collapsed { grid-template-columns: 1fr !important; }
        }

        /* ---- LEFT SIDEBAR ---- */
        .sidebar {
            background: #141414;
            border-right: 1px solid #222;
            padding: 10px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            gap: 6px;
            position: relative;
            min-width: 0;
            transition: padding 0.25s ease;
        }
        .app-body.sidebar-collapsed .sidebar { padding: 0; border-right: none; }
        .sidebar-toggle {
            position: fixed;
            top: 50%;
            left: 260px;
            transform: translateY(-50%);
            transition: background 0.15s;
            z-index: 100;
            width: 16px;
            height: 48px;
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: 0 4px 4px 0;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #888;
            font-size: 10px;
            transition: left 0.25s ease, background 0.15s;
            user-select: none;
        }
        .sidebar-toggle:hover { background: #383838; color: #ccc; }
        .app-body.sidebar-collapsed .sidebar-toggle { left: 0px; border-radius: 0 4px 4px 0; }

        .sidebar-section {
            background: #1a1a1a;
            border-radius: 6px;
            padding: 10px;
            border: 1px solid #2e2e2e;
        }
        .sidebar-section h3 {
            font-size: 13px; color: #bbb;
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
        .sidebar-section.as-rx { background: #001833; border-radius: 6px; box-shadow: 0 0 10px #0077ff; transition: background 0.3s, box-shadow 0.3s; }
        .collapse-panel.as-rx { box-shadow: 0 0 10px #0077ff; transition: box-shadow 0.3s; }
        .collapse-panel.as-rx .collapse-header { background: #0055cc; transition: background 0.3s; }
        .collapse-panel.as-rx .collapse-header:hover { background: #0066ee; }
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
            background: #001833;
            border-color: #004488;
            box-shadow: 0 0 10px #0077ff;
        }
        #dmrSection.active .collapse-header { background: #0055cc; transition: background 0.3s; }
        #dmrSection.active .collapse-header:hover { background: #0066ee; }
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
            min-width: 0;
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
        button.btn-monitor.active.streaming { background: #0055cc; color: #fff; border-color: #0055cc; font-weight: bold; box-shadow: 0 0 10px #0077ff; }
        button:disabled { opacity: 0.35; cursor: not-allowed; }
        .btn-sidebar-sm {
            font-size: 10px; padding: 2px 7px;
            white-space: nowrap;
            border-radius: 3px; border: 1px solid #444;
            background: #222; color: #aaa;
            cursor: pointer; font-family: monospace;
        }
        .btn-sidebar-sm:hover { background: #333; color: #fff; }
        .btn-sidebar-sm.btn-monitor { background: #1a1a1a; color: #777; border-color: #333; }
        .btn-sidebar-sm.btn-monitor:hover { background: #222; color: #aaa; }
        .btn-sidebar-sm.btn-monitor.active { background: #006600; color: #fff; border-color: #00aa00; font-weight: bold; }
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
        .log-tab.tab-mmdvm.active    { border-color: #7af; color: #7af; }
        .log-tab.tab-analog.active   { border-color: lime; color: lime; }
        .log-tab.tab-stfu.active     { border-color: cyan; color: cyan; }
        .log-tab.tab-watchdog.active { border-color: #fa8; color: #fa8; }

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
        .qt-fav-row { display:flex; align-items:center; gap:6px; padding:5px 0; border-bottom:1px solid #2a2a2a; }
        .qt-fav-row:last-child { border-bottom:none; }
        .qt-fav-label { flex:1; font-size:12px; color:#ccc; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
        .btn-danger-sm { background:#3a1010; color:#ff8888; border:1px solid #662222; border-radius:4px; padding:2px 6px; font-size:11px; cursor:pointer; }
        .btn-danger-sm:hover { background:#4a1414; }

        /* TG console grid */
        .tr-console-group { margin-bottom:14px; }
        .tr-console-group-label { font-size:9px; color:#666; letter-spacing:1px; text-transform:uppercase; margin-bottom:5px; }
        .tr-console-buttons { display:flex; flex-wrap:wrap; gap:5px; }
        .tr-tg-btn {
            font-size:10px; padding:5px 9px; border-radius:4px; cursor:pointer;
            border:1px solid #3a5a3a; background:#1a2a1a; color:#88cc88;
            width:160px; text-align:left; transition:background 0.1s;
            display:flex; flex-direction:column; gap:1px; flex-shrink:0;
        }
        .tr-tg-btn:hover { background:#223a22; }
        .tr-tg-btn.disabled { background:#222; border-color:#444; color:#555; }
        .tr-tg-btn.disabled .tr-tg-btn-num { color:#444; }
        .tr-tg-btn.avoided { background:#2a1010; border-color:#662222; color:#cc6666; }
        .tr-tg-btn.avoided .tr-tg-btn-num { color:#882222; }
        .tr-tg-btn-name { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-weight:bold; }
        .tr-tg-btn-num  { font-size:9px; color:#4a8a4a; font-family:monospace; }

        /* Scanner call log */
        .tr-call-row {
            display:flex; align-items:flex-start; gap:8px; padding:6px 0;
            border-bottom:1px solid #222; cursor:pointer;
        }
        .tr-call-row:last-child { border-bottom:none; }
        .tr-call-row:hover { background:#1f1f1f; border-radius:4px; }
        .tr-call-row.playing { background:#0d1a1f; border-radius:4px; }
        .tr-sys-badge {
            font-size:9px; font-weight:bold; letter-spacing:0.5px; padding:2px 5px;
            border-radius:3px; background:#1a2a3a; color:#4fc3f7; white-space:nowrap;
            margin-top:2px; flex-shrink:0;
        }
        .tr-call-row.emergency .tr-sys-badge { background:#3a1a1a; color:#ff6666; }
        .tr-call-info { flex:1; min-width:0; }
        .tr-tg-name { font-size:12px; color:#ddd; font-weight:bold; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .tr-tg-sub  { font-size:10px; color:#777; margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .tr-call-meta { font-size:10px; color:#666; text-align:right; white-space:nowrap; flex-shrink:0; }
        .tr-emerg-tag { font-size:9px; color:#ff6666; font-weight:bold; margin-left:4px; }
        .qt-hist-net {
            font-size: 9px; color: #999;
            margin-right: 5px; letter-spacing: 0.5px;
        }

        .mobile-action-bar { display: none; }

        /* Audio controls overlay */
        #audioOverlayBtn {
            position: fixed; bottom: 70px; right: 12px; z-index: 9000;
            width: 44px; height: 44px; border-radius: 50%;
            background: #1a1a1a; border: 1px solid #555; color: #ccc;
            font-size: 20px; cursor: pointer; display: flex;
            align-items: center; justify-content: center;
            box-shadow: 0 2px 8px rgba(0,0,0,0.5);
        }
        #audioOverlayBtn:hover { background: #2a2a2a; border-color: #888; }
        @media (min-width: 601px) {
            #audioOverlayBtn { bottom: 16px; }
        }
        #audioOverlay {
            display: none; position: fixed; inset: 0; z-index: 50000;
            background: rgba(0,0,0,0.6);
        }
        #audioOverlay.open { display: flex; align-items: center; justify-content: center; }
        #audioOverlayPanel {
            background: #1a1a1a; border: 1px solid #444; border-radius: 10px;
            padding: 16px 20px; width: min(360px, 94vw); max-height: 90vh;
            overflow-y: auto; position: relative;
            box-shadow: 0 8px 32px rgba(0,0,0,0.7);
        }
        #audioOverlayPanel .audio-section { background: #222; border-radius: 7px; padding: 10px 12px; margin-bottom: 10px; }
        #audioOverlayPanel .audio-section:last-child { margin-bottom: 0; }
        #audioOverlayPanel .audio-section-hdr {
            display: flex; align-items: center; gap: 7px;
            font-size: 12px; font-weight: bold; letter-spacing: 1.5px; text-transform: uppercase;
            margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid #333; }
        #audioOverlayPanel .audio-section-hdr .section-icon { font-size: 15px; }
        #audioOverlayPanel .audio-section-hdr.hdr-dmr     { color: #7af; }
        #audioOverlayPanel .audio-section-hdr.hdr-allstar { color: #af7; }
        #audioOverlayPanel .audio-section-hdr.hdr-scanner { color: #fa7; }
        #audioOverlayCloseBtn {
            position: absolute; top: 10px; right: 12px;
            background: none; border: none; color: #888; font-size: 20px; cursor: pointer;
        }
        #audioOverlayCloseBtn:hover { color: #ccc; }

        /* Node connect/disconnect toast */
        #nodeToastContainer {
            position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
            z-index: 100000; display: flex; flex-direction: column;
            align-items: center; gap: 10px; pointer-events: none;
        }
        .node-toast {
            background: #0d2b0d; border: 3px solid #22cc22; color: #88ff88;
            padding: 22px 40px; border-radius: 10px; font-size: clamp(14px, 2.5vw, 22px);
            font-family: monospace; font-weight: bold; letter-spacing: 1px;
            box-shadow: 0 0 40px rgba(0,200,0,0.4), 0 4px 24px rgba(0,0,0,0.8);
            opacity: 1; transition: opacity 0.5s ease;
            text-align: center; min-width: min(300px, 90vw); max-width: 90vw;
            white-space: nowrap;
        }
        .node-toast.disconnect {
            background: #2b0d0d; border-color: #cc2222; color: #ff8888;
            box-shadow: 0 0 40px rgba(200,0,0,0.4), 0 4px 24px rgba(0,0,0,0.8);
        }
        .node-toast.dmr {
            background: #1a1400; border-color: #ccaa00; color: #ffe066;
            box-shadow: 0 0 40px rgba(200,160,0,0.4), 0 4px 24px rgba(0,0,0,0.8);
        }
        .node-toast.fade-out { opacity: 0; }

        @media (max-width: 600px) {
            /* Layout: single column, sidebar hidden */
            .app-body, .app-body.sidebar-collapsed { grid-template-columns: 1fr !important; }
            .sidebar         { display: none !important; }
            .sidebar-toggle  { display: none !important; }

            /* Panels fill the screen */
            .content  { overflow-y: auto; }
            .panels   { padding: 6px; gap: 6px; }

            /* Larger tap targets on collapse headers */
            .collapse-header { min-height: 48px; padding: 0 10px; }
            .collapse-header h3 { font-size: 11px; }

            /* Make key status badges more readable at a glance */
            #connState, #asStateBadge { font-size: 11px !important; padding: 2px 6px; }
            #dmrActiveCall            { font-size: 15px !important; }
            #tgValue                  { font-size: 13px !important; }
            #tgValueName              { font-size: 9px !important; }
            #asNodeBadge              { font-size: 13px !important; }
            #asDirectLinkBadge        { font-size: 12px !important; }
            #modeValue                { display: none; }

            /* Hide non-essential panels entirely on mobile */
            .mobile-hide { display: none !important; }

            /* Dispatch log: shorter on mobile */
            .dispatch-log { height: 100px; }

            /* Modals: full-width, reduced padding, max-height safe zone */
            .modal-panel {
                width: calc(100vw - 16px) !important;
                max-width: calc(100vw - 16px) !important;
                max-height: 88vh !important;
                padding: 10px 10px !important;
                border-radius: 8px !important;
            }
            .modal-panel table { font-size: 10px; }
            .modal-panel th, .modal-panel td { padding: 3px 4px !important; }
            .modal-header-row {
                flex-wrap: nowrap !important;
                gap: 4px !important;
            }
            .modal-header-row h3 { font-size: 11px !important; }
            .modal-header-row select { font-size: 10px !important; max-width: 90px; }

            /* Scanner bar: icon-only buttons on mobile */
            .btn-label { display: none !important; }
            #trTgBadge { font-size: 13px !important; }

            /* Log viewer content: scrollable */
            #logFileContent { font-size: 10px; }

            /* Last heard table: horizontal scroll */
            .lh-table-wrap { overflow-x: auto; }

            /* TG console buttons smaller */
            .tr-tg-btn { width: 120px !important; font-size: 9px !important; padding: 4px 6px !important; }

            /* Modals: anchor near top so content isn't hidden behind nav chrome */
            [id$="Modal"] { align-items: flex-start !important; padding-top: 8px; }

            /* Close button always visible and not squeezed */
            .modal-header-row button[onclick*="close"], .modal-header-row button[onclick*="Close"] {
                flex-shrink: 0 !important;
                margin-left: auto !important;
            }

            /* PTT button: full-width easy tap target */
            .btn-ptt { width: 100% !important; font-size: 14px !important; padding: 10px !important; }

            /* Bottom action bar — always visible on mobile */
            .mobile-action-bar {
                display: flex !important;  /* overrides desktop display:none */
                position: fixed;
                bottom: 0; left: 0; right: 0;
                box-sizing: border-box;
                height: 52px;
                background: #111;
                border-top: 1px solid #333;
                z-index: 200;
                gap: 4px;
                padding: 5px 5px;
                align-items: stretch;
            }
            .mobile-action-bar .mob-btn {
                flex: 1;
                min-width: 0;
                font-size: 10px;
                font-weight: bold;
                border-radius: 5px;
                border: 1px solid #444;
                background: #1a1a1a;
                color: #aaa;
                cursor: pointer;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                gap: 1px;
                overflow: hidden;
                -webkit-tap-highlight-color: transparent;
                padding: 3px 2px;
            }
            .mobile-action-bar .mob-btn.active   { background: #006600; color: #fff; border-color: #00aa00; }
            .mobile-action-bar .mob-btn.active.streaming { background: #0055cc; color: #fff; border-color: #0077ff; box-shadow: 0 0 8px #0077ff; }
            .mobile-action-bar .mob-btn.mob-ptt  { background: #1a1a1a; color: #888; border-color: #444; font-weight: bold; letter-spacing: 1px; }
            .mobile-action-bar .mob-btn.mob-ptt.keyed { background: #cc2200; color: #fff; border-color: #ff4400; box-shadow: 0 0 12px #ff4400; }
            .mobile-action-bar .mob-btn:disabled { opacity: 0.35; }
            /* Push panel content up so it isn't hidden behind the bar */
            .content { padding-bottom: 64px; }
        }
    </style>
</head>
<body>

    <div class="header-bar">
        <h1>&#9889; RADIO DISPATCHER</h1>
        <span style="display:flex;align-items:center;gap:10px;">
            <button id="wakeLockBtn" onclick="toggleWakeLock()" title="Keep screen awake"
                    style="background:#1a1a1a;border:1px solid #333;color:#666;border-radius:5px;
                           padding:3px 9px;font-size:12px;cursor:pointer;display:none;">&#9728;</button>
            <span class="header-time" id="headerTime">--</span>
        </span>
    </div>

    <div class="app-body" id="appBody">

        <div class="sidebar-toggle" id="sidebarToggle" onclick="toggleSidebar()" title="Toggle audio panel">&#10094;</div>

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
                <div style="margin-top:8px;">
                    <button class="btn-ptt" id="btnPTTSidebar" disabled
                            style="width:100%;font-size:11px;">&#127908; PTT — Hold to Talk</button>
                </div>
                <div style="margin-top:6px;display:flex;align-items:center;gap:6px;">
                    <span class="vol-label" style="flex-shrink:0;">Level</span>
                    <div style="flex:1;height:8px;background:#111;border:1px solid #333;border-radius:4px;overflow:hidden;">
                        <div id="micMeterBar" style="height:100%;width:0%;background:#00cc44;border-radius:4px;transition:width 0.05s;"></div>
                    </div>
                    <button id="btnMicTest" class="btn-sidebar-sm" style="flex-shrink:0;font-size:10px;padding:2px 5px;" onclick="testMic()">Test</button>
                </div>
            </div>

            <!-- RESTART BUTTONS -->
            <div class="sidebar-section">
                <h3>Tools</h3>
                <div style="display:flex; flex-direction:column; gap:4px;">
                    <button class="btn-sidebar-sm" onclick="openDispatchModal()">&#128225; Dispatch Log</button>
                    <button class="btn-sidebar-sm" onclick="openLastHeardModal()">&#128251; Last Heard</button>
                    <button class="btn-sidebar-sm" onclick="openLogModal()">&#128203; Log Viewer</button>
                </div>
            </div>

            <div class="sidebar-section">
                <h3>Services</h3>
                <div style="display:flex; flex-direction:column; gap:4px;">
                    <button id="btnRestart"   class="btn-restart-sm" onclick="action('/api/restart',       'Restarting STFU...')">&#8634; Restart STFU</button>
                    <button id="btnRestartAB" class="btn-restart-sm" onclick="action('/api/restart_ab',    'Restarting Analog Bridge...')">&#8634; Restart Analog</button>
                    <button id="btnRestartMM" class="btn-restart-sm" onclick="action('/api/restart_mmdvm', 'Restarting MMDVM...')">&#8634; Restart MMDVM</button>
                </div>
            </div>
        </div>

    <!-- MOBILE BOTTOM ACTION BAR (must be before <script> so getElementById works at parse time) -->
    <div id="nodeToastContainer"></div>

    <!-- Audio controls overlay (accessible at any screen size) -->
    <button id="audioOverlayBtn" onclick="toggleAudioOverlay()" title="Audio controls">&#127911;</button>
    <div id="audioOverlay" onclick="closeAudioOverlayIfBackdrop(event)">
        <div id="audioOverlayPanel">
            <button id="audioOverlayCloseBtn" onclick="toggleAudioOverlay()">&#10005;</button>

            <div class="audio-section">
                <div class="audio-section-hdr hdr-dmr">
                    <span class="section-icon">📻</span> DMR Audio
                </div>
                <div class="vol-row">
                    <span class="vol-label">Volume</span>
                    <button id="dmrMuteBtnOv" onclick="toggleMuteDmr()" class="btn-sidebar-sm">Mute</button>
                    <button id="btnMonitorOv" onclick="toggleMonitor(this)" class="btn-sidebar-sm btn-monitor">&#128264; Monitor</button>
                    <span class="vol-pct" id="volDisplayOv">100%</span>
                </div>
                <input type="range" class="vol-slider" id="volSliderOv" min="0" max="100" value="100"
                       oninput="setVolume(this.value)">
                <div class="vol-row" style="margin-top:8px;">
                    <span class="vol-label">High Pass</span>
                    <span class="vol-pct" id="hpfDisplayOv" style="color:#fa8;">OFF</span>
                </div>
                <input type="range" class="hpf-slider" id="hpfSliderOv" min="100" max="600" step="10" value="220"
                       oninput="setHpFilter(this.value)">
                <div class="vol-row" style="margin-top:8px;">
                    <span class="vol-label">Presence</span>
                    <span class="vol-pct" id="presDisplayOv" style="color:lime;">0 dB</span>
                </div>
                <input type="range" class="pres-slider" id="presSliderOv" min="0" max="12" step="0.5" value="0"
                       oninput="setPresence(this.value)">
                <div class="vol-row" style="margin-top:8px;">
                    <span class="vol-label">Noise Gate</span>
                    <label style="cursor:pointer; color:#ddd; font-size:11px;">
                        <input type="checkbox" id="gateToggleOv" onchange="setGate(this.checked)"> Enable
                    </label>
                </div>
            </div>

            <div class="audio-section">
                <div class="audio-section-hdr hdr-allstar">
                    <span class="section-icon">⚡</span> Allstar Audio
                </div>
                <div class="vol-row">
                    <span class="vol-label">Volume</span>
                    <button id="asMuteBtnOv" onclick="toggleMuteAllstar()" class="btn-sidebar-sm">Mute</button>
                    <button id="btnAsAudioOv" onclick="toggleAllstarAudio(this)" class="btn-sidebar-sm btn-monitor">&#128264; Monitor</button>
                    <span class="vol-pct" id="asVolDisplayOv">100%</span>
                </div>
                <input type="range" class="vol-slider" id="asVolSliderOv" min="0" max="100" value="100"
                       oninput="setAllstarVolume(this.value)">
                <div style="margin-top:8px;">
                    <button class="btn-ptt" id="btnPTTOv" disabled
                            style="width:100%;font-size:11px;">&#127908; PTT — Hold to Talk</button>
                </div>
            </div>

            <div class="audio-section">
                <div class="audio-section-hdr hdr-scanner">
                    <span class="section-icon">📡</span> Trunk RX Audio
                </div>
                <div class="vol-row">
                    <span class="vol-label">Volume</span>
                    <button id="trAudioToggleOv" onclick="trToggleAudio()" class="btn-sidebar-sm btn-monitor">&#128264; Enable</button>
                    <span class="vol-pct" id="trVolDisplayOv">100%</span>
                </div>
                <input type="range" class="vol-slider" id="trVolSliderOv" min="0" max="100" value="100"
                       oninput="setTrVolumeOv(this.value)">
            </div>

            <div class="audio-section">
                <div class="audio-section-hdr" style="color:#7df;">
                    <span class="section-icon">📡</span> SDR Scanner Audio
                </div>
                <div class="vol-row">
                    <span class="vol-label">Volume</span>
                    <button id="sdrAudioToggleOv" onclick="sdrToggleAudio()" class="btn-sidebar-sm btn-monitor">&#128264; Enable</button>
                    <span class="vol-pct" id="sdrVolDisplayOv">100%</span>
                </div>
                <input type="range" class="vol-slider" id="sdrVolSliderOv" min="0" max="100" value="100"
                       oninput="sdrSetVolume(this.value)">
            </div>
        </div>
    </div>

    <div class="mobile-action-bar">
        <button class="mob-btn btn-monitor" id="mobBtnDmrMonitor"
                onclick="toggleMonitor(this)"><span style="font-size:16px;">&#128264;</span><span>DMR</span></button>
        <button class="mob-btn btn-monitor" id="mobBtnAsMonitor"
                onclick="toggleAllstarAudio(this)"><span style="font-size:16px;">&#128264;</span><span>Allstar</span></button>
        <button class="mob-btn btn-monitor" id="mobBtnTrAudio"
                onclick="trToggleAudio()"><span style="font-size:16px;">&#128251;</span><span>Trunk</span></button>
        <button class="mob-btn btn-monitor" id="mobBtnSdrAudio"
                onclick="sdrToggleAudio()"><span style="font-size:16px;">📡</span><span>SDR</span></button>
        <button class="mob-btn mob-ptt" id="mobBtnPTT" disabled><span style="font-size:16px;">&#127908;</span><span>PTT</span></button>
    </div>

        <!-- MAIN CONTENT -->
        <div class="content">


            <!-- PANELS -->
            <div class="panels">


                <!-- DMR STATUS BAR -->
                <div class="collapse-panel" id="dmrSection">
                    <div class="collapse-header" style="cursor:default;flex-direction:column;align-items:stretch;gap:4px;padding:8px 12px;">
                        <!-- Row 1: title + buttons -->
                        <div style="display:flex;align-items:center;gap:6px;">
                            <h3 style="margin:0;">&#128251; DMR</h3>
                            <span class="tx-pulse" id="txPulse"></span>
                            <span id="dmrActiveCall" style="color:orange;font-size:13px;font-weight:bold;letter-spacing:1px;"></span>
                            <span style="margin-left:auto;display:flex;gap:5px;">
                                <button onclick="openQuickTuneModal()"
                                        style="background:#222;border:1px solid #444;color:#aaa;border-radius:4px;
                                               padding:2px 7px;font-size:13px;cursor:pointer;"
                                        title="Quick Tune">&#9733;</button>
                                <button onclick="openDmrModal()"
                                        style="background:#222;border:1px solid #444;color:#aaa;border-radius:4px;
                                               padding:2px 7px;font-size:13px;cursor:pointer;"
                                        title="DMR Controls">&#9881;</button>
                            </span>
                        </div>
                        <!-- Row 2: connection info -->
                        <div style="display:flex;align-items:center;gap:6px;padding-top:5px;border-top:1px solid #222;">
                            <span class="mode-badge badge-unknown" id="modeValue">--</span>
                            <span class="conn-badge conn-offline" id="connState">OFFLINE</span>
                            <span id="tgValue" style="color:lightgreen;font-size:13px;font-weight:bold;"></span>
                            <span id="tgValueName" style="color:#6c6;font-size:11px;"></span>
                        </div>
                    </div>
                </div>

                <!-- DMR MODAL -->
                <div id="dmrModal" onclick="closeDmrModalIfBackdrop(event)"
                     style="display:none;position:fixed;inset:0;z-index:50000;background:rgba(0,0,0,0.6);
                            align-items:center;justify-content:center;">
                    <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                                padding:16px 20px;width:min(480px,94vw);max-height:90vh;
                                overflow-y:auto;position:relative;box-shadow:0 8px 32px rgba(0,0,0,0.7);">
                        <div class="modal-header-row" style="display:flex;align-items:center;margin-bottom:12px;gap:10px;">
                            <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">&#128251; DMR CONTROLS</h3>
                            <span id="txCallsign" style="color:lime;font-weight:bold;font-size:16px;letter-spacing:2px;margin-left:4px;">STANDBY</span>
                            <button onclick="closeDmrModal()"
                                    style="margin-left:auto;background:none;border:none;color:#888;font-size:20px;cursor:pointer;">&#10005;</button>
                        </div>
                        <div style="color:#bbb;font-size:12px;margin-bottom:4px;"><span id="txDetail">&mdash;</span></div>
                        <div style="color:#999;font-size:10px;margin-bottom:10px;">
                            <span id="tgName"></span>
                            &nbsp;Since <span id="connectedSince">--</span>
                            &nbsp;<span id="txTime">&nbsp;</span>
                        </div>
                        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding-top:8px;border-top:1px solid #2a2a2a;margin-top:4px;">
                            <button class="btn-tgif" id="btnTGIF" onclick="action('/api/tgif', 'Switching to TGIF...')">&#9654; TGIF</button>
                            <button class="btn-bm"   id="btnBM"   onclick="action('/api/bm',   'Switching to BrandMeister...')">&#9654; BM</button>
                            <div class="controls-sep"></div>
                            <input class="tg-input" type="text" id="tgInput" placeholder="Talkgroup...">
                            <button class="btn-tune"     onclick="tuneTG()">&#9654; Tune</button>
                            <button class="btn-save-fav" onclick="saveFavorite()" title="Save to favorites for current network">&#9733; Fav</button>
                        </div>
                    </div>
                </div>

                <!-- ALLSTAR STATUS BAR -->
                <div class="collapse-panel" id="asSidebarSection">
                    <div class="collapse-header" style="cursor:default;flex-direction:column;align-items:stretch;gap:4px;padding:8px 12px;">
                        <!-- Row 1: title + pulse + node (active call) + buttons -->
                        <div style="display:flex;align-items:center;gap:6px;">
                            <h3 style="flex-shrink:0;margin:0;">&#9889; ALLSTAR</h3>
                            <span class="rx-dot" id="asRxDot" title="RX activity"></span>
                            <span id="asNodeBadge" style="color:#fff;font-size:16px;font-weight:bold;
                                  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0;
                                  letter-spacing:0.3px;">--</span>
                            <span style="margin-left:auto;display:flex;gap:5px;flex-shrink:0;">
                                <button onclick="openAsQuickTuneModal()"
                                        style="background:#222;border:1px solid #444;color:#aaa;border-radius:4px;
                                               padding:2px 7px;font-size:13px;cursor:pointer;"
                                        title="Allstar Favorites">&#9733;</button>
                                <button onclick="openAsModal()"
                                        style="background:#222;border:1px solid #444;color:#aaa;border-radius:4px;
                                               padding:2px 7px;font-size:13px;cursor:pointer;"
                                        title="Allstar Controls">&#9881;</button>
                            </span>
                        </div>
                        <!-- Row 2: connection info -->
                        <div style="display:flex;align-items:center;gap:6px;padding-top:5px;border-top:1px solid #222;">
                            <span class="conn-badge conn-offline" id="asStateBadge">OFFLINE</span>
                            <span id="asDirectLinkBadge" style="display:none;color:#4fc3f7;font-size:13px;font-weight:bold;">&#8594; <span id="asDirectLinkNode"></span></span>
                        </div>
                    </div>
                </div>

                <!-- ALLSTAR CONTROLS MODAL -->
                <div id="asModal" onclick="closeAsModalIfBackdrop(event)"
                     style="display:none;position:fixed;inset:0;z-index:50000;background:rgba(0,0,0,0.6);
                            align-items:center;justify-content:center;">
                    <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                                padding:16px 20px;width:min(480px,94vw);
                                max-height:85vh;display:flex;flex-direction:column;
                                position:relative;box-shadow:0 8px 32px rgba(0,0,0,0.7);">
                        <div class="modal-header-row" style="display:flex;align-items:center;margin-bottom:12px;flex-shrink:0;">
                            <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">&#9889; ALLSTAR CONTROLS</h3>
                            <button onclick="closeAsModal()"
                                    style="margin-left:auto;background:none;border:none;color:#888;font-size:20px;cursor:pointer;">&#10005;</button>
                        </div>
                        <div style="overflow-y:auto;flex:1;min-height:0;">
                            <div style="display:flex;gap:6px;margin-bottom:10px;align-items:center;flex-wrap:wrap;">
                                <button class="btn-monitor" id="btnAsConnect"   onclick="allstarConnect()">&#9654; Connect</button>
                                <button class="btn-danger"  id="btnAsDisconnect" onclick="allstarDisconnect()" disabled>&#9632; Disconnect</button>
                                <button class="btn-monitor" id="btnAsAudio"     onclick="toggleAllstarAudio(this)">&#128264; Audio</button>
                            </div>
                            <div style="margin-bottom:10px;">
                                <div class="qt-section-label" style="margin-bottom:5px;">TRANSMIT</div>
                                <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                                    <select id="micDeviceSelect" style="flex:1;min-width:120px;background:#1a1a1a;color:#ccc;border:1px solid #333;border-radius:4px;font-size:11px;padding:3px 5px;" onchange="onMicDeviceChange()">
                                        <option value="">-- select mic --</option>
                                    </select>
                                    <button class="btn-ptt" id="btnPTT" disabled>&#127908; PTT — Hold to Talk</button>
                                </div>
                            </div>
                            <div style="margin-bottom:10px;">
                                <div class="qt-section-label" style="margin-bottom:5px;">NODE LINKING</div>
                                <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
                                    <input class="tg-input" type="text" id="asRemoteNode"
                                           placeholder="Remote node #..." style="width:140px;">
                                    <button class="btn-tune"   onclick="allstarLink('monitor')">&#9654; Monitor</button>
                                    <button class="btn-tune"   onclick="allstarLink('transceive')">&#9654; Xceive</button>
                                    <button class="btn-danger" onclick="allstarUnlink()">&#9632; Unlink</button>
                                </div>
                            </div>
                            <div style="margin-bottom:10px;">
                                <div class="qt-section-label" style="margin-bottom:5px;">COMMAND</div>
                                <div style="display:flex;gap:6px;align-items:center;">
                                    <input class="tg-input" type="text" id="asCommand"
                                           placeholder="e.g. *70" style="width:120px;"
                                           onkeydown="if(event.key==='Enter') allstarCommand()">
                                    <button class="btn-tune" onclick="allstarCommand()">&#9654; Send</button>
                                    <button class="btn-tune" onclick="allstarSendCmd('*70')" title="Node status">&#9432; Status</button>
                                </div>
                            </div>
                            <div style="margin-bottom:10px;">
                                <div class="qt-section-label" style="margin-bottom:5px;">CONNECTED NODES</div>
                                <div id="asNodeList" style="font-size:12px;color:#ddd;min-height:16px;">--</div>
                            </div>
                            <div style="margin-top:8px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                                <label style="font-size:11px;color:#888;white-space:nowrap;">Node alert duration (s)</label>
                                <input type="number" id="nodeToastDurationInput" min="2" max="60" value="10"
                                    style="width:54px;background:#111;border:1px solid #444;color:#ccc;
                                           border-radius:4px;padding:2px 5px;font-size:12px;"
                                    onchange="setNodeToastDuration(this.value)">
                                <label style="font-size:11px;color:#888;white-space:nowrap;display:flex;align-items:center;gap:4px;cursor:pointer;">
                                    <input type="checkbox" id="nodeAlertSoundChk" onchange="setNodeAlertSound(this.checked)">
                                    Alert sound
                                </label>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- ALLSTAR QUICK TUNE MODAL -->
                <div id="asQuickTuneModal" onclick="closeAsQuickTuneModalIfBackdrop(event)"
                     style="display:none;position:fixed;inset:0;z-index:50000;background:rgba(0,0,0,0.6);
                            align-items:center;justify-content:center;">
                    <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                                padding:16px 20px;width:min(400px,94vw);
                                max-height:85vh;display:flex;flex-direction:column;
                                position:relative;box-shadow:0 8px 32px rgba(0,0,0,0.7);">
                        <div class="modal-header-row" style="display:flex;align-items:center;margin-bottom:12px;flex-shrink:0;">
                            <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">&#9733; ALLSTAR FAVORITES</h3>
                            <button onclick="closeAsQuickTuneModal()"
                                    style="margin-left:auto;background:none;border:none;color:#888;font-size:20px;cursor:pointer;">&#10005;</button>
                        </div>
                        <div style="display:flex;gap:6px;align-items:center;margin-bottom:10px;flex-shrink:0;">
                            <input class="tg-input" type="text" id="asNodeFavInput" placeholder="Node #..." style="width:110px;">
                            <input class="tg-input" type="text" id="asNodeFavLabel" placeholder="Label (optional)" style="flex:1;">
                            <button class="btn-save-fav" onclick="saveAsFavorite()" title="Save to favorites">&#9733; Save</button>
                        </div>
                        <div style="overflow-y:auto;flex:1;min-height:0;">
                            <div class="qt-section-label" style="margin-bottom:6px;">Saved Nodes</div>
                            <div id="asFavList"><div class="qt-empty">None saved</div></div>
                        </div>
                    </div>
                </div>

                <!-- TRUNK RX STATUS BAR -->
                <div class="collapse-panel" id="trSection">
                    <div class="collapse-header" style="cursor:default;flex-direction:column;align-items:stretch;gap:4px;padding:8px 12px;">
                        <!-- Row 1: title + buttons right-aligned -->
                        <div style="display:flex;align-items:center;gap:6px;">
                            <h3 style="flex-shrink:0;margin:0;">&#128250; TRUNK RX</h3>
                            <span style="margin-left:auto;display:flex;gap:5px;flex-shrink:0;">
                                <button onclick="trSkip()" title="Skip current call"
                                        style="background:#1a1a1a;border:1px solid #333;color:#777;border-radius:3px;
                                               padding:2px 7px;font-size:10px;cursor:pointer;">⏭<span class="btn-label"> Skip</span></button>
                                <button onclick="trAvoid()" title="Avoid this talkgroup"
                                        style="background:#1a1010;border:1px solid #442222;color:#aa6666;border-radius:3px;
                                               padding:2px 7px;font-size:10px;cursor:pointer;">&#128683;<span class="btn-label"> Avoid</span></button>
                                <button onclick="trLockSysToggle()" id="trLockSysBtn"
                                        style="background:#1a1a1a;border:1px solid #333;color:#777;border-radius:3px;
                                               padding:2px 7px;font-size:10px;cursor:pointer;"
                                        title="Lock to current system">&#128274;<span class="btn-label"> Lock Sys</span></button>
                                <button onclick="trPauseToggle()" id="trPauseBtn"
                                        style="background:#1a1a1a;border:1px solid #333;color:#777;border-radius:3px;
                                               padding:2px 7px;font-size:10px;cursor:pointer;"
                                        title="Pause / Resume">⏸<span class="btn-label"> Pause</span></button>
                                <button onclick="openTrConsoleModal()"
                                        style="background:#1a1a1a;border:1px solid #333;color:#777;border-radius:3px;
                                               padding:2px 7px;font-size:10px;cursor:pointer;"
                                        title="Talkgroup Console">&#9783;<span class="btn-label"> TG Console</span></button>
                                <button onclick="openTrModal()"
                                        style="background:#1a1a1a;border:1px solid #333;color:#777;border-radius:3px;
                                               padding:2px 7px;font-size:10px;cursor:pointer;"
                                        title="Call Log">&#9776;<span class="btn-label"> Call Log</span></button>
                            </span>
                        </div>
                        <!-- Row 2: pulse · TG name · badges · system -->
                        <div style="display:flex;align-items:center;gap:6px;min-width:0;padding-top:5px;border-top:1px solid #222;">
                            <span class="tx-pulse" id="trPulse"></span>
                            <span id="trTgBadge" style="font-size:16px;color:#fff;font-weight:bold;
                                  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0;
                                  letter-spacing:0.3px;">--</span>
                            <span id="trLockedBadge" style="display:none;font-size:9px;font-weight:bold;
                                  background:#003a00;border:1px solid #00aa00;color:#4f4;
                                  border-radius:3px;padding:1px 5px;letter-spacing:0.5px;flex-shrink:0;"></span>
                            <span id="trPausedBadge" style="display:none;font-size:9px;font-weight:bold;
                                  background:#3a2a00;border:1px solid #886600;color:#ffcc44;
                                  border-radius:3px;padding:1px 5px;letter-spacing:0.5px;flex-shrink:0;">PAUSED</span>
                            <span id="trQueueCount" style="display:none;font-size:9px;font-weight:bold;
                                  background:#1a1a2a;border:1px solid #446;color:#88aaff;
                                  border-radius:3px;padding:1px 5px;flex-shrink:0;"></span>
                            <span id="trSkippedBadge" style="display:none;font-size:9px;font-weight:bold;
                                  background:#1a1a1a;border:1px solid #666;color:#aaa;
                                  border-radius:3px;padding:1px 5px;letter-spacing:0.5px;flex-shrink:0;">SKIPPED</span>
                            <span id="trSystemBadge" style="font-size:10px;color:#888;font-weight:bold;
                                  letter-spacing:0.5px;flex-shrink:0;text-align:right;">--</span>
                        </div>
                    </div>
                </div>

                <!-- SDR SCANNER STATUS BAR -->
                <div class="collapse-panel" id="sdrSection">
                    <div class="collapse-header" style="cursor:default;flex-direction:column;align-items:stretch;gap:4px;padding:8px 12px;">
                        <!-- Row 1: title + buttons right-aligned -->
                        <div style="display:flex;align-items:center;gap:6px;">
                            <h3 style="flex-shrink:0;margin:0;">📡 SDR</h3>
                            <span id="sdrOfflineBadge" style="font-size:9px;font-weight:bold;
                                  background:#2a0000;border:1px solid #660000;color:#f88;
                                  border-radius:3px;padding:1px 5px;letter-spacing:0.5px;">OFFLINE</span>
                            <span style="margin-left:auto;display:flex;gap:5px;flex-shrink:0;">
                                <button onclick="sdrSkip()" title="Skip to next frequency"
                                        style="background:#1a1a1a;border:1px solid #333;color:#777;border-radius:3px;
                                               padding:2px 7px;font-size:10px;cursor:pointer;">⏭<span class="btn-label"> Skip</span></button>
                                <button onclick="sdrHoldToggle()" id="sdrHoldBtn" title="Hold / unhold current frequency"
                                        style="background:#1a1a1a;border:1px solid #333;color:#777;border-radius:3px;
                                               padding:2px 7px;font-size:10px;cursor:pointer;">🔒<span class="btn-label"> Hold</span></button>
                                <button onclick="openSdrModal()" title="SDR Channels"
                                        style="background:#1a1a1a;border:1px solid #333;color:#777;border-radius:3px;
                                               padding:2px 7px;font-size:10px;cursor:pointer;">⊞<span class="btn-label"> Channels</span></button>
                            </span>
                        </div>
                        <!-- Row 2: pulse · freq · label · dB -->
                        <div style="display:flex;align-items:center;gap:6px;min-width:0;padding-top:5px;border-top:1px solid #222;">
                            <span class="tx-pulse" id="sdrPulse"></span>
                            <span id="sdrFreqBadge" style="font-size:13px;color:#fff;font-weight:bold;
                                  white-space:nowrap;flex-shrink:0;">--</span>
                            <span id="sdrLabelBadge" style="font-size:14px;color:#fff;font-weight:bold;
                                  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0;">--</span>
                            <span id="sdrDbBadge" style="font-size:10px;color:#888;flex-shrink:0;"></span>
                            <span id="sdrHoldBadge" style="display:none;font-size:9px;font-weight:bold;
                                  background:#003a00;border:1px solid #00aa00;color:#4f4;
                                  border-radius:3px;padding:1px 5px;letter-spacing:0.5px;flex-shrink:0;">HOLD</span>
                        </div>
                    </div>
                </div>

                <!-- SDR CHANNEL MODAL -->
                <div id="sdrModal" onclick="closeSdrModalIfBackdrop(event)"
                     style="display:none;position:fixed;inset:0;z-index:50000;background:rgba(0,0,0,0.6);
                            align-items:center;justify-content:center;">
                    <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                                padding:16px 20px;width:min(560px,96vw);max-height:90vh;
                                display:flex;flex-direction:column;position:relative;
                                box-shadow:0 8px 32px rgba(0,0,0,0.7);">
                        <!-- Header row -->
                        <div style="display:flex;align-items:center;margin-bottom:10px;flex-shrink:0;gap:8px;">
                            <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">📡 SDR CHANNELS</h3>
                            <button onclick="sdrShowAddForm()"
                                    style="margin-left:auto;background:#1a2a1a;border:1px solid #3a6a3a;color:#4f4;
                                           border-radius:4px;padding:2px 10px;font-size:11px;cursor:pointer;">+ Add</button>
                            <button onclick="closeSdrModal()"
                                    style="background:none;border:none;color:#888;font-size:20px;cursor:pointer;">✕</button>
                        </div>
                        <!-- Add channel form (hidden by default) -->
                        <div id="sdrAddForm" style="display:none;background:#222;border-radius:6px;padding:10px;margin-bottom:10px;flex-shrink:0;">
                            <div style="font-size:11px;color:#aaa;margin-bottom:8px;font-weight:bold;letter-spacing:1px;">ADD CHANNEL</div>
                            <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:6px;">
                                <div>
                                    <div style="font-size:10px;color:#888;margin-bottom:2px;">Frequency (MHz)</div>
                                    <input id="sdrAddFreq" type="text" placeholder="446.000"
                                           style="width:100%;box-sizing:border-box;background:#111;border:1px solid #444;color:#ddd;
                                                  border-radius:3px;padding:4px 6px;font-size:12px;">
                                </div>
                                <div>
                                    <div style="font-size:10px;color:#888;margin-bottom:2px;">Label</div>
                                    <input id="sdrAddLabel" type="text" placeholder="Channel name"
                                           style="width:100%;box-sizing:border-box;background:#111;border:1px solid #444;color:#ddd;
                                                  border-radius:3px;padding:4px 6px;font-size:12px;">
                                </div>
                                <div>
                                    <div style="font-size:10px;color:#888;margin-bottom:2px;">Squelch RMS (0.0–1.0)</div>
                                    <input id="sdrAddSq" type="number" placeholder="default" step="0.001" min="0" max="1"
                                           style="width:100%;box-sizing:border-box;background:#111;border:1px solid #444;color:#ddd;
                                                  border-radius:3px;padding:4px 6px;font-size:12px;">
                                </div>
                                <div>
                                    <div style="font-size:10px;color:#888;margin-bottom:2px;">Gain (dB or "auto")</div>
                                    <input id="sdrAddGain" type="text" placeholder="auto"
                                           style="width:100%;box-sizing:border-box;background:#111;border:1px solid #444;color:#ddd;
                                                  border-radius:3px;padding:4px 6px;font-size:12px;">
                                </div>
                                <div>
                                    <div style="font-size:10px;color:#888;margin-bottom:2px;">PL Tone (Hz, 0=off)</div>
                                    <input id="sdrAddPL" type="number" placeholder="0" step="0.1" min="0"
                                           style="width:100%;box-sizing:border-box;background:#111;border:1px solid #444;color:#ddd;
                                                  border-radius:3px;padding:4px 6px;font-size:12px;">
                                </div>
                            </div>
                            <div style="display:flex;gap:6px;">
                                <button onclick="sdrAddChannel()"
                                        style="background:#006600;border:1px solid #00aa00;color:#fff;
                                               border-radius:4px;padding:3px 12px;font-size:11px;cursor:pointer;">Save</button>
                                <button onclick="sdrHideAddForm()"
                                        style="background:#1a1a1a;border:1px solid #444;color:#aaa;
                                               border-radius:4px;padding:3px 12px;font-size:11px;cursor:pointer;">Cancel</button>
                            </div>
                        </div>
                        <!-- Channel list -->
                        <div id="sdrChannelList" style="overflow-y:auto;flex:1;"></div>
                    </div>
                </div>
                <!-- SDR edit channel inline form template (rendered per-row by JS) -->

                <!-- SCANNER CALL LOG MODAL -->
                <div id="trModal" onclick="closeTrModalIfBackdrop(event)"
                     style="display:none;position:fixed;inset:0;z-index:50000;background:rgba(0,0,0,0.6);
                            align-items:center;justify-content:center;">
                    <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                                padding:16px 20px;width:min(560px,96vw);
                                max-height:90vh;display:flex;flex-direction:column;
                                position:relative;box-shadow:0 8px 32px rgba(0,0,0,0.7);">
                        <div class="modal-header-row" style="display:flex;align-items:center;margin-bottom:10px;flex-shrink:0;gap:8px;">
                            <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">&#128250; TRUNK RX</h3>
                            <select id="trSystemFilter"
                                    style="background:#111;border:1px solid #444;color:#ccc;border-radius:4px;
                                           font-size:11px;padding:2px 6px;margin-left:4px;"
                                    onchange="_trSystemFilterChanged()">
                                <option value="">All systems</option>
                            </select>
                            <select id="trTgFilter"
                                    style="background:#111;border:1px solid #444;color:#ccc;border-radius:4px;
                                           font-size:11px;padding:2px 6px;"
                                    onchange="renderTrCalls()">
                                <option value="">All TGs</option>
                            </select>
                            <button onclick="openTrImportModal()"
                                    style="background:#222;border:1px solid #444;color:#aaa;border-radius:4px;
                                           padding:2px 7px;font-size:11px;cursor:pointer;white-space:nowrap;"
                                    title="Import RadioReference talkgroup CSV">&#128196; TG Import</button>
                            <label style="font-size:11px;color:#888;display:flex;align-items:center;gap:4px;margin-left:auto;cursor:pointer;">
                                <input type="checkbox" id="trAutoplayChk" onchange="saveTrPrefs()"> Auto-play
                            </label>
                            <button onclick="closeTrModal()"
                                    style="background:none;border:none;color:#888;font-size:20px;cursor:pointer;margin-left:4px;">&#10005;</button>
                        </div>
                        <!-- Audio player + controls -->
                        <div style="margin-bottom:10px;flex-shrink:0;" id="trPlayerWrap">
                            <audio id="trAudio" controls
                                   style="width:100%;height:36px;accent-color:#4fc3f7;background:#111;border-radius:4px;">
                            </audio>
                            <div style="display:flex;align-items:center;gap:6px;margin-top:6px;flex-wrap:wrap;">
                                <div id="trNowPlaying" style="font-size:10px;color:#888;flex:1;min-width:100px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                                    No call selected
                                </div>
                                <button onclick="trSkip()" title="Skip current call"
                                        style="background:#222;border:1px solid #444;color:#aaa;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer;white-space:nowrap;">
                                    ⏭ Skip
                                </button>
                                <button onclick="trAvoid()" title="Avoid this talkgroup"
                                        style="background:#2a1010;border:1px solid #662222;color:#ff8888;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer;white-space:nowrap;">
                                    &#128683; Avoid
                                </button>
                                <button onclick="trPauseToggle()" id="trPauseBtnModal"
                                        style="background:#222;border:1px solid #444;color:#aaa;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer;white-space:nowrap;">
                                    ⏸ Pause
                                </button>
                            </div>
                            <div style="display:flex;align-items:center;gap:8px;margin-top:5px;">
                                <span id="trQueueBadge" style="font-size:10px;color:#666;"></span>
                                <span style="flex:1;"></span>
                                <span style="font-size:10px;color:#666;">&#128264;</span>
                                <input type="range" id="trVolSlider" min="0" max="100" value="100"
                                       style="width:80px;accent-color:#4fc3f7;cursor:pointer;"
                                       oninput="setTrVolume(this.value)">
                                <span id="trVolDisplay" style="font-size:10px;color:#888;width:28px;text-align:right;">100%</span>
                            </div>
                        </div>
                        <!-- Call log -->
                        <div style="overflow-y:auto;flex:1;min-height:0;" id="trCallLog">
                            <div class="qt-empty">No calls received yet</div>
                        </div>
                    </div>
                </div>

                <!-- TG IMPORT MODAL -->
                <div id="trImportModal" onclick="closeTrImportModalIfBackdrop(event)"
                     style="display:none;position:fixed;inset:0;z-index:60000;background:rgba(0,0,0,0.7);
                            align-items:center;justify-content:center;">
                    <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                                padding:16px 20px;width:min(420px,94vw);
                                max-height:80vh;display:flex;flex-direction:column;
                                box-shadow:0 8px 32px rgba(0,0,0,0.8);">
                        <div class="modal-header-row" style="display:flex;align-items:center;margin-bottom:12px;flex-shrink:0;">
                            <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">&#128196; TALKGROUP IMPORT</h3>
                            <button onclick="closeTrImportModal()"
                                    style="margin-left:auto;background:none;border:none;color:#888;font-size:20px;cursor:pointer;">&#10005;</button>
                        </div>
                        <div style="font-size:11px;color:#888;margin-bottom:10px;flex-shrink:0;">
                            Import a RadioReference talkgroup CSV for a system. Replaces existing talkgroup data for that system.
                            Expected columns: <span style="color:#ccc;">Decimal, Hex, Mode, Alpha Tag, Description, Tag, Group</span>
                        </div>
                        <div style="display:flex;gap:6px;align-items:center;margin-bottom:10px;flex-shrink:0;flex-wrap:wrap;">
                            <input list="trImportSystemList" id="trImportSystem"
                                   placeholder="System short name (e.g. lcraboerne)"
                                   style="flex:1;min-width:160px;background:#111;border:1px solid #444;color:#ccc;
                                          border-radius:4px;font-size:11px;padding:3px 6px;">
                            <datalist id="trImportSystemList"></datalist>
                            <label style="background:#222;border:1px solid #444;color:#aaa;border-radius:4px;
                                          padding:3px 10px;font-size:11px;cursor:pointer;white-space:nowrap;">
                                &#128196; Choose CSV
                                <input type="file" id="trImportFile" accept=".csv,.txt" style="display:none;"
                                       onchange="onTrImportFileChosen()">
                            </label>
                        </div>
                        <div id="trImportFileName" style="font-size:10px;color:#666;margin-bottom:8px;flex-shrink:0;">No file chosen</div>
                        <button onclick="doTrImport()"
                                style="background:#1a2a1a;border:1px solid #2a6a2a;color:#88cc88;border-radius:4px;
                                       padding:6px 14px;font-size:12px;cursor:pointer;flex-shrink:0;margin-bottom:12px;">
                            &#9654; Import
                        </button>
                        <div style="overflow-y:auto;flex:1;min-height:0;">
                            <div class="qt-section-label" style="margin-bottom:6px;">Loaded Systems</div>
                            <div id="trTgSummary"><div class="qt-empty">No talkgroup data loaded</div></div>
                        </div>
                    </div>
                </div>

                <!-- TG CONSOLE MODAL -->
                <div id="trConsoleModal" onclick="closeTrConsoleModalIfBackdrop(event)"
                     style="display:none;position:fixed;inset:0;z-index:50000;background:rgba(0,0,0,0.6);
                            align-items:center;justify-content:center;">
                    <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                                padding:16px 20px;width:min(640px,96vw);
                                max-height:90vh;display:flex;flex-direction:column;
                                box-shadow:0 8px 32px rgba(0,0,0,0.8);">
                        <!-- TG console header: row 1 = title + close, row 2 = controls -->
                        <div style="display:flex;align-items:center;margin-bottom:6px;flex-shrink:0;gap:6px;">
                            <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">&#9783; TALKGROUP CONSOLE</h3>
                            <button onclick="closeTrConsoleModal()"
                                    style="background:none;border:none;color:#888;font-size:20px;cursor:pointer;margin-left:auto;flex-shrink:0;">&#10005;</button>
                        </div>
                        <div style="display:flex;align-items:center;flex-wrap:wrap;gap:5px;margin-bottom:8px;flex-shrink:0;">
                            <select id="trConsoleSystem"
                                    style="background:#111;border:1px solid #444;color:#ccc;border-radius:4px;
                                           font-size:11px;padding:2px 6px;"
                                    onchange="renderTrConsole()">
                                <option value="">All systems</option>
                            </select>
                            <select id="trConsoleFilter"
                                    style="background:#111;border:1px solid #444;color:#ccc;border-radius:4px;
                                           font-size:11px;padding:2px 6px;"
                                    onchange="renderTrConsole()">
                                <option value="all">All</option>
                                <option value="active">Active only</option>
                                <option value="disabled">Disabled only</option>
                            </select>
                            <button onclick="trConsoleEnableAll()"
                                    style="background:#1a2a1a;border:1px solid #2a6a2a;color:#88cc88;border-radius:4px;
                                           padding:2px 8px;font-size:11px;cursor:pointer;">Enable All</button>
                            <button onclick="trConsoleDisableAll()"
                                    style="background:#2a1a1a;border:1px solid #6a2a2a;color:#cc8888;border-radius:4px;
                                           padding:2px 8px;font-size:11px;cursor:pointer;">Disable All</button>
                        </div>
                        <div style="font-size:10px;color:#666;margin-bottom:8px;flex-shrink:0;">
                            Disabled talkgroups are silently dropped. Click a button to toggle.
                        </div>
                        <div style="overflow-y:auto;flex:1;min-height:0;" id="trConsoleGrid">
                            <div class="qt-empty">No talkgroups known yet — import a CSV or wait for calls</div>
                        </div>
                    </div>
                </div>

                <!-- STATUS STRIP -->
                <div class="collapse-panel mobile-hide">
                    <div class="status-strip">
                        <span class="strip-label">SERVICES</span>
                        <span class="strip-item"><span class="svc-dot" id="dot_stfu"></span>STFU <span id="svc_stfu" class="svc-text-off">--</span></span>
                        <span class="strip-item"><span class="svc-dot" id="dot_mmdvm"></span>MMDVM <span id="svc_mmdvm" class="svc-text-off">--</span></span>
                        <span class="strip-item"><span class="svc-dot" id="dot_analog"></span>Analog <span id="svc_analog" class="svc-text-off">--</span></span>
                        <span class="strip-item"><span class="svc-dot" id="dot_usrp"></span>USRP <span id="svc_usrp" class="svc-text-off">--</span></span>
                    </div>
                </div>

                <!-- QUICK TUNE MODAL -->
                <div id="quickTuneModal" onclick="closeQuickTuneModalIfBackdrop(event)"
                     style="display:none;position:fixed;inset:0;z-index:50000;background:rgba(0,0,0,0.6);
                            align-items:center;justify-content:center;">
                    <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                                padding:16px 20px;width:min(480px,94vw);
                                max-height:85vh;display:flex;flex-direction:column;
                                position:relative;box-shadow:0 8px 32px rgba(0,0,0,0.7);">
                        <div class="modal-header-row" style="display:flex;align-items:center;margin-bottom:12px;flex-shrink:0;">
                            <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">&#9733; QUICK TUNE</h3>
                            <button onclick="closeQuickTuneModal()"
                                    style="margin-left:auto;background:none;border:none;color:#888;font-size:20px;cursor:pointer;">&#10005;</button>
                        </div>
                        <div style="overflow-y:auto;flex:1;min-height:0;">
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
                            <div class="qt-section-label" style="margin-top:10px;">Recent</div>
                            <div id="tuneHistory"><div class="qt-empty">No history yet</div></div>
                        </div>
                    </div>
                </div>

            </div>
        </div>
    </div>

    <!-- DISPATCH LOG MODAL -->
    <div id="dispatchLogModal" onclick="if(event.target===this)closeDispatchModal()"
         style="display:none;position:fixed;inset:0;z-index:50000;background:rgba(0,0,0,0.6);
                align-items:center;justify-content:center;">
        <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                    padding:16px 20px;width:min(680px,96vw);max-height:85vh;
                    display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.7);">
            <div class="modal-header-row" style="display:flex;align-items:center;margin-bottom:10px;flex-shrink:0;">
                <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">&#128225; DISPATCH LOG</h3>
                <button onclick="closeDispatchModal()"
                        style="margin-left:auto;background:none;border:none;color:#888;font-size:20px;cursor:pointer;">&#10005;</button>
            </div>
            <div class="dispatch-log" id="dispatchLog" style="flex:1;overflow-y:auto;"></div>
        </div>
    </div>

    <!-- LAST HEARD MODAL -->
    <div id="lastHeardModal" onclick="if(event.target===this)closeLastHeardModal()"
         style="display:none;position:fixed;inset:0;z-index:50000;background:rgba(0,0,0,0.6);
                align-items:center;justify-content:center;">
        <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                    padding:16px 20px;width:min(680px,96vw);max-height:85vh;
                    display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.7);">
            <div class="modal-header-row" style="display:flex;align-items:center;margin-bottom:10px;flex-shrink:0;">
                <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">&#128251; LAST HEARD</h3>
                <button onclick="closeLastHeardModal()"
                        style="margin-left:auto;background:none;border:none;color:#888;font-size:20px;cursor:pointer;">&#10005;</button>
            </div>
            <div class="lh-table-wrap" style="overflow:auto;flex:1;">
                <table id="lastHeardTable">
                    <thead>
                        <tr>
                            <th>TIME</th><th>CALLSIGN</th><th>DMR ID</th>
                            <th>TG</th><th>TG NAME</th><th>NET</th>
                        </tr>
                    </thead>
                    <tbody id="lastHeardBody">
                        <tr><td colspan="6" style="color:#777;padding:8px;">Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- LOG VIEWER MODAL -->
    <div id="logViewerModal" onclick="if(event.target===this)closeLogModal()"
         style="display:none;position:fixed;inset:0;z-index:50000;background:rgba(0,0,0,0.6);
                align-items:center;justify-content:center;">
        <div class="modal-panel" style="background:#1a1a1a;border:1px solid #444;border-radius:10px;
                    padding:16px 20px;width:min(860px,96vw);max-height:85vh;
                    display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.7);">
            <div class="modal-header-row" style="display:flex;align-items:center;margin-bottom:10px;flex-shrink:0;">
                <h3 style="margin:0;font-size:14px;color:#aaa;letter-spacing:1px;">&#128203; LOG VIEWER</h3>
                <button onclick="closeLogModal()"
                        style="margin-left:auto;background:none;border:none;color:#888;font-size:20px;cursor:pointer;">&#10005;</button>
            </div>
            <div class="log-tabs" style="flex-shrink:0;">
                <div class="log-tab tab-mmdvm active" onclick="selectTab('mmdvm',    this)">MMDVM</div>
                <div class="log-tab tab-analog"       onclick="selectTab('analog',   this)">Analog</div>
                <div class="log-tab tab-stfu"         onclick="selectTab('stfu',     this)">STFU</div>
                <div class="log-tab tab-watchdog"     onclick="selectTab('watchdog', this)">Watchdog</div>
            </div>
            <div class="log-controls" style="flex-shrink:0;">
                <label>Lines:</label>
                <input type="number" id="logLines" value="50" min="10" max="500" step="10">
                <button onclick="fetchLog()">&#8634; Refresh</button>
                <button class="btn-autoscroll on" id="btnAutoScroll" onclick="toggleAutoScroll()">&#11015; Auto</button>
            </div>
            <div class="log-file-label" id="logFileLabel" style="flex-shrink:0;"></div>
            <div id="logFileContent" style="flex:1;overflow-y:auto;">Select a tab to load...</div>
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
                    const connEl = document.getElementById('connState');
                    if (connEl) { connEl.textContent = 'RX'; connEl.className = 'conn-badge conn-rx'; }
                    log('TX: ' + cs + ' → TG ' + data.tg, 'ok');
                    if (data.tg !== _activeDmrTg) {
                        if (_activeDmrTg) _showDmrToast(_activeDmrTg, _activeDmrTgName, null, 'disconnect');
                        _activeDmrTg     = data.tg;
                        _activeDmrTgName = data.tg_name;
                        _showDmrToast(data.tg, data.tg_name, null, 'connect');
                    }
                    if (lastHeardOpen) pollLastHeard();
                } else if (data.event === 'tx_end') {
                    document.getElementById('dmrSection').classList.remove('active');
                    document.getElementById('txPulse').classList.remove('on');
                    document.getElementById('dmrActiveCall').textContent = '';
                    document.getElementById('txCallsign').textContent    = 'STANDBY';
                    document.getElementById('txDetail').textContent      = '—';
                    document.getElementById('txTime').innerHTML          = '&nbsp;';
                    const connEl = document.getElementById('connState');
                    if (connEl) { connEl.textContent = 'READY'; connEl.className = 'conn-badge conn-idle'; }
                    if (lastHeardOpen) pollLastHeard();
                } else if (data.event === 'tr_call') {
                    _onTrCall(data);
                } else if (data.event === 'sdr_state') {
                    _onSdrState(data);
                } else if (data.event === 'sdr_freq_change') {
                    _onSdrFreqChange(data);
                } else if (data.event === 'sdr_freq_clear') {
                    _sdrActive = false;
                    document.getElementById('sdrPulse').classList.remove('on');
                    document.getElementById('sdrDbBadge').textContent = '';
                    _updateSdrAudioBtn();
                } else if (data.event === 'sdr_signal') {
                    _onSdrSignal(data);
                } else if (data.event === 'sdr_channels_update') {
                    _onSdrChannelsUpdate(data);
                } else if (data.event === 'sdr_hold_update') {
                    _onSdrChannelsUpdate(data);
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
            document.getElementById('volSlider').value = val;
            document.getElementById('volSlider').style.setProperty('--vol-pct', val + '%');
            if (!_dmrMuted && dvsp && dvsp.player) dvsp.player.volume(val / 100);
            localStorage.setItem('rxVolume', val);
        }

        const SIDEBAR_W = 260;
        function _applySidebarState(collapsed) {
            const body = document.getElementById('appBody');
            const btn  = document.getElementById('sidebarToggle');
            const w = collapsed ? 0 : SIDEBAR_W;
            body.style.gridTemplateColumns = w + 'px 1fr';
            if (btn) { btn.style.left = w + 'px'; btn.innerHTML = collapsed ? '&#10095;' : '&#10094;'; }
        }
        function toggleSidebar() {
            const body = document.getElementById('appBody');
            const collapsed = body.classList.toggle('sidebar-collapsed');
            _applySidebarState(collapsed);
            localStorage.setItem('sidebarCollapsed', collapsed ? '1' : '');
        }
        // Restore sidebar state on load
        (function() {
            const collapsed = !!localStorage.getItem('sidebarCollapsed');
            if (collapsed) document.getElementById('appBody').classList.add('sidebar-collapsed');
            _applySidebarState(collapsed);
        })();

        // ── Audio overlay ────────────────────────────────────────────
        function toggleAudioOverlay() {
            const ov = document.getElementById('audioOverlay');
            const opening = !ov.classList.contains('open');
            ov.classList.toggle('open', opening);
            if (opening) _syncAudioOverlay();
        }
        function closeAudioOverlayIfBackdrop(e) {
            if (e.target === document.getElementById('audioOverlay')) toggleAudioOverlay();
        }

        // Copy current slider/checkbox values from sidebar into overlay (or vice-versa).
        function _syncAudioOverlay() {
            [['volSlider','volSliderOv'], ['hpfSlider','hpfSliderOv'],
             ['presSlider','presSliderOv'], ['asVolSlider','asVolSliderOv']].forEach(([src, dst]) => {
                const s = document.getElementById(src), d = document.getElementById(dst);
                if (s && d) d.value = s.value;
            });
            [['volDisplay','volDisplayOv'], ['hpfDisplay','hpfDisplayOv'],
             ['presDisplay','presDisplayOv'], ['asVolDisplay','asVolDisplayOv']].forEach(([src, dst]) => {
                const s = document.getElementById(src), d = document.getElementById(dst);
                if (s && d) d.textContent = s.textContent;
            });
            const gc = document.getElementById('gateToggle'), go = document.getElementById('gateToggleOv');
            if (gc && go) go.checked = gc.checked;
            // Sync monitor/mute button states
            ['btnMonitor','btnAsAudioSidebar'].forEach((sid, i) => {
                const ovId = ['btnMonitorOv','btnAsAudioOv'][i];
                const s = document.getElementById(sid), d = document.getElementById(ovId);
                if (s && d) { d.className = s.className; }
            });
        }

        // Keep overlay display labels in sync when sidebar sliders change.
        function _syncOverlayDisplays() {
            [['volDisplay','volDisplayOv'], ['hpfDisplay','hpfDisplayOv'],
             ['presDisplay','presDisplayOv'], ['asVolDisplay','asVolDisplayOv']].forEach(([src, dst]) => {
                const s = document.getElementById(src), d = document.getElementById(dst);
                if (s && d && d.textContent !== s.textContent) d.textContent = s.textContent;
            });
            [['volSlider','volSliderOv'], ['hpfSlider','hpfSliderOv'],
             ['presSlider','presSliderOv'], ['asVolSlider','asVolSliderOv']].forEach(([src, dst]) => {
                const s = document.getElementById(src), d = document.getElementById(dst);
                if (s && d && d.value !== s.value) d.value = s.value;
            });
        }
        setInterval(function() {
            if (document.getElementById('audioOverlay').classList.contains('open')) _syncOverlayDisplays();
        }, 200);

        function toggleMuteDmr() {
            _dmrMuted = !_dmrMuted;
            const btn = document.getElementById('dmrMuteBtn');
            btn.textContent = _dmrMuted ? 'Unmute' : 'Mute';
            btn.style.color = _dmrMuted ? '#f88' : '#aaa';
            btn.style.borderColor = _dmrMuted ? '#f44' : '#444';
            const vol = _dmrMuted ? 0 : parseInt(document.getElementById('volSlider').value) / 100;
            if (dvsp && dvsp.player) dvsp.player.volume(vol);
            const mob = document.getElementById('mobBtnDmrMonitor');
            if (mob) { mob.classList.toggle('muted', _dmrMuted); mob.textContent = _dmrMuted ? '🔇 DMR' : '📢 DMR'; }
        }

        function toggleMuteAllstar() {
            _asMuted = !_asMuted;
            const btn = document.getElementById('asMuteBtn');
            btn.textContent = _asMuted ? 'Unmute' : 'Mute';
            btn.style.color = _asMuted ? '#f88' : '#aaa';
            btn.style.borderColor = _asMuted ? '#f44' : '#444';
            const vol = _asMuted ? 0 : parseInt(document.getElementById('asVolSlider').value);
            if (asPlayer) asPlayer.setVolume(vol);
            const mob = document.getElementById('mobBtnAsMonitor');
            if (mob) { mob.classList.toggle('muted', _asMuted); mob.textContent = _asMuted ? '🔇 Allstar' : '📢 Allstar'; }
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
            document.getElementById('hpfSlider').value = val;
            document.getElementById('hpfSlider').style.setProperty('--hpf-pct', pct + '%');
            if (hpFilter) hpFilter.frequency.value = val;
            localStorage.setItem('rxHpFilter', val);
        }

        function setPresence(val) {
            val = parseFloat(val);
            const pct = (val / 12 * 100).toFixed(1);
            document.getElementById('presDisplay').textContent = val === 0 ? '0 dB' : '+' + val.toFixed(1) + ' dB';
            document.getElementById('presSlider').value = val;
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
        function _syncDmrMonitorBtns(active) {
            ['btnMonitor', 'mobBtnDmrMonitor', 'btnMonitorOv'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.classList.toggle('active', active);
            });
        }

        async function toggleMonitor(btn) {
            if (dvsp && dvsp.isPlaying()) {
                dvsp.stop();
                // WorkletPlayer.stop() closes the AudioContext; reset filter refs
                // so setupAudioFilters() will rewire them on next play.
                hpFilter = shelfFilter = notchFilter = presFilter = compressor = null;
                _syncDmrMonitorBtns(false);
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
                _syncDmrMonitorBtns(true);
                log('RX Monitor started', 'ok');
            }
        }

        // -------------------------
        // LAST HEARD
        // -------------------------
        var lastHeardOpen  = false;
        var lastHeardTimer = null;

        function openLastHeardModal() {
            document.getElementById('lastHeardModal').style.display = 'flex';
            lastHeardOpen = true;
            pollLastHeard();
            if (!lastHeardTimer) lastHeardTimer = setInterval(pollLastHeard, 10000);
        }
        function closeLastHeardModal() {
            document.getElementById('lastHeardModal').style.display = 'none';
            lastHeardOpen = false;
            clearInterval(lastHeardTimer);
            lastHeardTimer = null;
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
                body.innerHTML = rows.map(r => {
                    const utcStr = r.time.replace(' ', 'T') + 'Z';
                    const localTime = new Date(utcStr).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
                    return `
                    <tr>
                        <td class="lh-time">${localTime}</td>
                        <td class="lh-callsign">${r.callsign}</td>
                        <td class="lh-dmrid">${r.dmr_id || ''}</td>
                        <td class="lh-tg">${r.tg}</td>
                        <td class="lh-tgname">${r.tg_name || ''}</td>
                        <td class="${r.source === 'BM' ? 'lh-bm' : 'lh-tgif'}">${r.source}</td>
                    </tr>`;
                }).join('');
            } catch(e) { log('Last heard error: ' + e, 'error'); }
        }

        // -------------------------
        // LOG VIEWER
        // -------------------------
        var currentLog      = 'mmdvm';
        var autoScroll      = true;
        var logViewerOpen   = false;
        var logPollTimer    = null;
        var dispatchLogOpen = false;

        function openDmrModal() {
            document.getElementById('dmrModal').style.display = 'flex';
        }
        function closeDmrModal() {
            document.getElementById('dmrModal').style.display = 'none';
        }
        function closeDmrModalIfBackdrop(e) {
            if (e.target === document.getElementById('dmrModal')) closeDmrModal();
        }

        function openDispatchModal()  { document.getElementById('dispatchLogModal').style.display = 'flex'; }
        function closeDispatchModal() { document.getElementById('dispatchLogModal').style.display = 'none'; }

        function openLogModal() {
            document.getElementById('logViewerModal').style.display = 'flex';
            fetchLog();
            if (!logPollTimer) logPollTimer = setInterval(fetchLog, 5000);
        }
        function closeLogModal() {
            document.getElementById('logViewerModal').style.display = 'none';
            clearInterval(logPollTimer);
            logPollTimer = null;
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
        // ---- SDR SCANNER ----
        var _sdrAudioEnabled = false;
        var _sdrActive       = false;
        var _sdrAudioEl      = null;
        var _sdrVolume       = 100;

        function _initSdr() {
            _sdrAudioEl = new Audio();
            _sdrAudioEl.volume = _sdrVolume / 100;
            const sv = parseInt(localStorage.getItem('sdrVolume') ?? '100');
            sdrSetVolume(sv);
        }

        function sdrToggleAudio() {
            _sdrAudioEnabled = !_sdrAudioEnabled;
            localStorage.setItem('sdrAudioEnabled', _sdrAudioEnabled ? '1' : '0');
            if (_sdrAudioEnabled) {
                _sdrConnectAudio();
            } else {
                _sdrDisconnectAudio();
            }
            _updateSdrAudioBtn();
        }

        function _sdrConnectAudio() {
            if (!_sdrAudioEl) return;
            const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const audioWsUrl = wsProto + '//' + location.host + '/ws/sdr-audio';
            // Use MediaSource if available for streaming PCM, otherwise fall back
            // to a simple approach using the proxy WS
            _sdrAudioEl.src = '';
            // We'll use a simple approach: proxy WS feeds an AudioContext
            if (_sdrAudioCtx) { try { _sdrAudioCtx.close(); } catch(e){} }
            _sdrAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
            _sdrGainNode = _sdrAudioCtx.createGain();
            _sdrGainNode.gain.value = _sdrVolume / 100;
            _sdrGainNode.connect(_sdrAudioCtx.destination);
            var _sdrNextTime = 0;
            _sdrWs = new WebSocket(audioWsUrl);
            _sdrWs.binaryType = 'arraybuffer';
            _sdrWs.onmessage = function(e) {
                if (!_sdrAudioEnabled) return;
                const int16 = new Int16Array(e.data);
                const float32 = new Float32Array(int16.length);
                for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
                const buf = _sdrAudioCtx.createBuffer(1, float32.length, 24000);
                buf.copyToChannel(float32, 0);
                const src = _sdrAudioCtx.createBufferSource();
                src.buffer = buf;
                src.connect(_sdrGainNode);
                // Schedule sequentially; keep a small ahead-buffer to absorb jitter
                const now = _sdrAudioCtx.currentTime;
                if (_sdrNextTime < now + 0.05) _sdrNextTime = now + 0.05;
                src.start(_sdrNextTime);
                _sdrNextTime += buf.duration;
            };
            _sdrWs.onclose = function() {
                if (_sdrAudioEnabled) setTimeout(_sdrConnectAudio, 3000);
            };
        }

        function _sdrDisconnectAudio() {
            if (_sdrWs) { try { _sdrWs.close(); } catch(e){} _sdrWs = null; }
            if (_sdrAudioCtx) { try { _sdrAudioCtx.close(); } catch(e){} _sdrAudioCtx = null; }
        }

        var _sdrAudioCtx = null;
        var _sdrGainNode = null;
        var _sdrWs       = null;

        function sdrSetVolume(val) {
            val = parseInt(val);
            _sdrVolume = val;
            if (_sdrGainNode) _sdrGainNode.gain.value = val / 100;
            document.getElementById('sdrVolDisplayOv').textContent = val + '%';
            document.getElementById('sdrVolSliderOv').value = val;
            localStorage.setItem('sdrVolume', val);
        }

        function _updateSdrAudioBtn() {
            const streaming = _sdrAudioEnabled && _sdrActive;
            ['mobBtnSdrAudio', 'sdrAudioToggleOv'].forEach(id => {
                const el = document.getElementById(id);
                if (!el) return;
                el.classList.toggle('active', _sdrAudioEnabled);
                el.classList.toggle('streaming', streaming);
            });
            const mob = document.getElementById('mobBtnSdrAudio');
            if (mob) mob.innerHTML = '<span style="font-size:16px;">📡</span><span>SDR</span>';
            const ov = document.getElementById('sdrAudioToggleOv');
            if (ov) ov.textContent = _sdrAudioEnabled ? '🔊 Enable' : '🔇 Muted';
        }

        function _onSdrState(d) {
            const connected = d.connected !== false;
            const offBadge  = document.getElementById('sdrOfflineBadge');
            const section   = document.getElementById('sdrSection');
            if (offBadge) offBadge.style.display = connected ? 'none' : '';
            document.getElementById('sdrFreqBadge').textContent  = connected ? (d.freq  || '--') : '--';
            document.getElementById('sdrLabelBadge').textContent = connected ? (d.label || '--') : '--';
            document.getElementById('sdrDbBadge').textContent    = '';
            const holdBadge = document.getElementById('sdrHoldBadge');
            if (holdBadge) holdBadge.style.display = d.holdFreq ? '' : 'none';
            const holdBtn = document.getElementById('sdrHoldBtn');
            if (holdBtn) {
                holdBtn.style.background  = d.holdFreq ? '#003a00' : '#1a1a1a';
                holdBtn.style.borderColor = d.holdFreq ? '#00aa00' : '#333';
                holdBtn.style.color       = d.holdFreq ? '#4f4'    : '#777';
            }
            if (d.channels) _renderSdrChannels(d);
        }

        function _onSdrFreqChange(m) {
            document.getElementById('sdrFreqBadge').textContent  = m.freq  || '--';
            document.getElementById('sdrLabelBadge').textContent = m.label || m.freq || '--';
            document.getElementById('sdrDbBadge').textContent    = '';
            document.getElementById('sdrPulse').classList.remove('on');
            _sdrActive = false;
            _updateSdrAudioBtn();
        }

        function _onSdrSignal(m) {
            _sdrActive = !!m.active;
            document.getElementById('sdrPulse').classList.toggle('on', _sdrActive);
            if (m.db != null)
                document.getElementById('sdrDbBadge').textContent = _sdrActive ? m.db + ' dB' : '';
            _updateSdrAudioBtn();
        }

        function _onSdrChannelsUpdate(m) {
            const holdBadge = document.getElementById('sdrHoldBadge');
            if (holdBadge) holdBadge.style.display = m.holdFreq ? '' : 'none';
            const holdBtn = document.getElementById('sdrHoldBtn');
            if (holdBtn) {
                holdBtn.style.background  = m.holdFreq ? '#003a00' : '#1a1a1a';
                holdBtn.style.borderColor = m.holdFreq ? '#00aa00' : '#333';
                holdBtn.style.color       = m.holdFreq ? '#4f4'    : '#777';
            }
            _renderSdrChannels(m);
        }

        var _sdrEditFreq = null;
        var _sdrLastChannelData = {channels:{}, skipped:[], holdFreq:null};

        function _renderSdrChannels(d) {
            _sdrLastChannelData = d;
            const el = document.getElementById('sdrChannelList');
            if (!el) return;
            const channels = d.channels || {};
            const skipped  = new Set(d.skipped  || []);
            const holdFreq = d.holdFreq || null;
            const freqs = Object.keys(channels).sort((a,b) => parseFloat(a)-parseFloat(b));
            if (!freqs.length) { el.innerHTML = '<div style="color:#666;padding:8px;">No channels configured</div>'; return; }
            el.innerHTML = freqs.map(f => {
                const ch    = channels[f];
                const isObj = typeof ch === 'object' && ch !== null;
                const label = isObj ? (ch.label || f) : (ch || f);
                const sqRms = isObj && ch.squelch_rms != null ? ch.squelch_rms : '';
                const gain  = isObj && ch.gain        != null ? ch.gain        : '';
                const pl    = isObj && ch.pl          != null ? ch.pl          : '';
                const skp   = skipped.has(f);
                const held  = holdFreq === f;
                const editing = _sdrEditFreq === f;
                const rowBg = held ? '#001a00' : (skp ? '#1a1000' : 'transparent');
                const rowHtml = editing ? `
                    <div style="padding:8px 4px;border-bottom:1px solid #2a2a2a;background:#1a1a2a;border-radius:4px;">
                        <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
                            <span style="font-size:12px;font-weight:bold;color:#7af;min-width:80px;">${escHtml(f)}</span>
                            <span style="font-size:10px;color:#555;">Editing</span>
                        </div>
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px;">
                            <label style="font-size:10px;color:#777;">Label
                                <input id="sdrELabel_${escHtml(f)}" value="${escHtml(String(label))}"
                                    style="display:block;width:100%;background:#111;border:1px solid #444;
                                           color:#ccc;border-radius:3px;padding:3px 5px;font-size:11px;box-sizing:border-box;margin-top:2px;">
                            </label>
                            <label style="font-size:10px;color:#777;">Squelch RMS (0.0–1.0)
                                <input id="sdrESq_${escHtml(f)}" value="${escHtml(String(sqRms))}" placeholder="default"
                                    style="display:block;width:100%;background:#111;border:1px solid #444;
                                           color:#ccc;border-radius:3px;padding:3px 5px;font-size:11px;box-sizing:border-box;margin-top:2px;">
                            </label>
                            <label style="font-size:10px;color:#777;">Gain (dB or auto)
                                <input id="sdrEGain_${escHtml(f)}" value="${escHtml(String(gain))}" placeholder="auto"
                                    style="display:block;width:100%;background:#111;border:1px solid #444;
                                           color:#ccc;border-radius:3px;padding:3px 5px;font-size:11px;box-sizing:border-box;margin-top:2px;">
                            </label>
                            <label style="font-size:10px;color:#777;">PL Tone (Hz)
                                <input id="sdrEPl_${escHtml(f)}" value="${escHtml(String(pl))}" placeholder="none"
                                    style="display:block;width:100%;background:#111;border:1px solid #444;
                                           color:#ccc;border-radius:3px;padding:3px 5px;font-size:11px;box-sizing:border-box;margin-top:2px;">
                            </label>
                        </div>
                        <div style="display:flex;gap:6px;">
                            <button onclick="sdrSaveChannel('${escHtml(f)}')"
                                    style="font-size:11px;padding:3px 12px;background:#003a00;border:1px solid #00aa00;
                                           color:#4f4;border-radius:3px;cursor:pointer;">Save</button>
                            <button onclick="sdrCancelEdit()"
                                    style="font-size:11px;padding:3px 12px;background:#1a1a1a;border:1px solid #333;
                                           color:#777;border-radius:3px;cursor:pointer;">Cancel</button>
                            <button onclick="sdrDeleteChannel('${escHtml(f)}')"
                                    style="font-size:11px;padding:3px 12px;background:#3a0000;border:1px solid #aa0000;
                                           color:#f44;border-radius:3px;cursor:pointer;margin-left:auto;">Delete</button>
                        </div>
                    </div>` : `
                    <div style="display:flex;align-items:center;gap:6px;padding:6px 4px;
                                border-bottom:1px solid #2a2a2a;background:${rowBg};">
                        <span style="font-size:12px;font-weight:bold;color:${held?'#4f4':'#ccc'};min-width:80px;">${escHtml(f)}</span>
                        <span style="flex:1;font-size:12px;color:${skp?'#555':'#aaa'};
                                     text-decoration:${skp?'line-through':'none'};">${escHtml(String(label))}</span>
                        <button onclick="sdrSkipChannel('${escHtml(f)}')"
                                style="font-size:10px;padding:2px 7px;background:${skp?'#2a1000':'#1a1a1a'};
                                       border:1px solid ${skp?'#663300':'#333'};color:${skp?'#f84':'#777'};
                                       border-radius:3px;cursor:pointer;"
                                title="${skp?'Include in scan':'Skip frequency'}">${skp?'▶':'⊘'}</button>
                        <button onclick="sdrHoldFreq('${escHtml(f)}')"
                                style="font-size:10px;padding:2px 7px;background:${held?'#003a00':'#1a1a1a'};
                                       border:1px solid ${held?'#00aa00':'#333'};color:${held?'#4f4':'#777'};
                                       border-radius:3px;cursor:pointer;"
                                title="${held?'Release hold':'Hold this frequency'}">${held?'🔓':'🔒'}</button>
                        <button onclick="sdrEditChannel('${escHtml(f)}')"
                                style="font-size:10px;padding:2px 7px;background:#1a1a2a;border:1px solid #334;
                                       color:#7af;border-radius:3px;cursor:pointer;" title="Edit">✎</button>
                    </div>`;
                return rowHtml;
            }).join('');
        }

        function sdrEditChannel(f) { _sdrEditFreq = f; _renderSdrChannels(_sdrLastChannelData); }
        function sdrCancelEdit()   { _sdrEditFreq = null; _renderSdrChannels(_sdrLastChannelData); }

        function sdrSaveChannel(f) {
            const label  = document.getElementById('sdrELabel_'  + f)?.value.trim();
            const sqRms  = document.getElementById('sdrESq_'     + f)?.value.trim();
            const gain   = document.getElementById('sdrEGain_'   + f)?.value.trim();
            const pl     = document.getElementById('sdrEPl_'     + f)?.value.trim();
            const body = {freq: f};
            if (label) body.label = label;
            if (sqRms) body.squelch_rms = parseFloat(sqRms);
            if (gain)  body.gain = gain;
            if (pl)    body.pl = parseFloat(pl);
            fetch('/api/sdr/channel', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
                .then(() => { _sdrEditFreq = null; });
        }

        function sdrDeleteChannel(f) {
            if (!confirm('Delete channel ' + f + '?')) return;
            fetch('/api/sdr/channel/' + encodeURIComponent(f), {method:'DELETE'})
                .then(() => { _sdrEditFreq = null; });
        }

        function sdrShowAddForm() {
            const el = document.getElementById('sdrAddForm');
            if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
        }
        function sdrHideAddForm() {
            const el = document.getElementById('sdrAddForm');
            if (el) el.style.display = 'none';
        }

        function sdrAddChannel() {
            const freq  = document.getElementById('sdrAddFreq')?.value.trim();
            const label = document.getElementById('sdrAddLabel')?.value.trim();
            const sqRms = document.getElementById('sdrAddSq')?.value.trim();
            const gain  = document.getElementById('sdrAddGain')?.value.trim();
            const pl    = document.getElementById('sdrAddPL')?.value.trim();
            if (!freq) { alert('Frequency is required'); return; }
            const body = {freq};
            if (label) body.label = label;
            if (sqRms) body.squelch_rms = parseFloat(sqRms);
            if (gain)  body.gain = gain;
            if (pl)    body.pl = parseFloat(pl);
            fetch('/api/sdr/channel', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
                .then(r => {
                    if (r.ok) {
                        ['sdrAddFreq','sdrAddLabel','sdrAddSq','sdrAddGain','sdrAddPL'].forEach(id => {
                            const el = document.getElementById(id);
                            if (el) el.value = '';
                        });
                        sdrHideAddForm();
                    }
                });
        }

        function sdrSkip()    { fetch('/api/sdr/skip',   {method:'POST'}); }
        function sdrHoldToggle() {
            const held = document.getElementById('sdrHoldBadge').style.display !== 'none';
            fetch('/api/sdr/hold', {method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({freq: held ? null : document.getElementById('sdrFreqBadge').textContent})});
        }
        function sdrSkipChannel(f) { fetch('/api/sdr/skip', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({freq:f})}); }
        function sdrHoldFreq(f) {
            const held = document.getElementById('sdrHoldBadge').style.display !== 'none'
                      && document.getElementById('sdrFreqBadge').textContent === f;
            fetch('/api/sdr/hold', {method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({freq: held ? null : f})});
        }
        function openSdrModal() {
            document.getElementById('sdrModal').style.display = 'flex';
            fetch('/api/sdr/state').then(r => r.json()).then(d => _renderSdrChannels(d)).catch(() => {});
        }
        function closeSdrModal()            { document.getElementById('sdrModal').style.display = 'none'; }
        function closeSdrModalIfBackdrop(e) { if (e.target===document.getElementById('sdrModal')) closeSdrModal(); }

        // ---- Wake Lock ----
        var _wakeLock = null;
        (function _initWakeLock() {
            const btn = document.getElementById('wakeLockBtn');
            if (!('wakeLock' in navigator) || !btn) return;
            btn.style.display = '';  // only show when supported
            document.addEventListener('visibilitychange', async () => {
                if (_wakeLock && document.visibilityState === 'visible') {
                    try { _wakeLock = await navigator.wakeLock.request('screen'); }
                    catch(e) {}
                }
            });
        })();

        async function toggleWakeLock() {
            const btn = document.getElementById('wakeLockBtn');
            if (_wakeLock) {
                await _wakeLock.release();
                _wakeLock = null;
                btn.style.background = '#1a1a1a';
                btn.style.borderColor = '#333';
                btn.style.color = '#666';
                btn.title = 'Keep screen awake';
            } else {
                try {
                    _wakeLock = await navigator.wakeLock.request('screen');
                    btn.style.background = '#003a00';
                    btn.style.borderColor = '#00aa00';
                    btn.style.color = '#4f4';
                    btn.title = 'Screen wake lock active — tap to release';
                } catch(e) {}
            }
        }

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
        var currentMode = 'TGIF';

        function openQuickTuneModal() {
            document.getElementById('quickTuneModal').style.display = 'flex';
            loadQuickTune();
        }
        function closeQuickTuneModal() {
            document.getElementById('quickTuneModal').style.display = 'none';
        }
        function closeQuickTuneModalIfBackdrop(e) {
            if (e.target === document.getElementById('quickTuneModal')) closeQuickTuneModal();
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
            const open = document.getElementById('quickTuneModal').style.display === 'flex';
            if (open) setTimeout(loadQuickTune, 300);
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
                const open = document.getElementById('quickTuneModal').style.display === 'flex';
                if (open) loadQuickTune();
            } catch(e) { log('Save favorite failed: ' + e, 'error'); }
        }

        async function removeFav(network, tg) {
            try {
                const res  = await fetch('/api/favorites/' + network + '/' + tg, {method: 'DELETE'});
                const data = await res.json();
                log(data.message, data.ok ? 'ok' : 'error');
                const open = document.getElementById('quickTuneModal').style.display === 'flex';
                if (open) loadQuickTune();
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

                // Keep header color and pulse in sync with poll — SSE is real-time but
                // can miss events on reconnect; the poll corrects any drift.
                const dmrRx = d.conn_state === 'rx';
                document.getElementById('dmrSection').classList.toggle('active', dmrRx);
                document.getElementById('txPulse').classList.toggle('on', dmrRx);
                // Reset TG tracking when DMR goes offline so next connect fires the toast.
                if (d.conn_state === 'offline') { _activeDmrTg = null; _activeDmrTgName = null; }

                document.getElementById('tgValue').textContent     = d.tg              || '--';
                document.getElementById('tgValueName').textContent = d.tg_name         || '';
                document.getElementById('tgName').textContent      = d.tg_name         || '';
                document.getElementById('connectedSince').textContent   = d.connected_since || '--';
                document.getElementById('headerTime').textContent = timestamp();
                const dmrBtn = document.getElementById('btnMonitor');
                const dmrRxAndMonitoring = d.conn_state === 'rx' && dmrBtn.classList.contains('active');
                ['btnMonitor', 'mobBtnDmrMonitor', 'btnMonitorOv'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el) el.classList.toggle('streaming', dmrRxAndMonitoring && el.classList.contains('active'));
                });

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

        function toggleAllstar() { openAsModal(); }

        // Allstar Controls modal
        function openAsModal() {
            document.getElementById('asModal').style.display = 'flex';
            pollAllstarStatus();
        }
        function closeAsModal() { document.getElementById('asModal').style.display = 'none'; }
        function closeAsModalIfBackdrop(e) {
            if (e.target === document.getElementById('asModal')) closeAsModal();
        }

        // Allstar Quick Tune / Favorites modal
        function openAsQuickTuneModal() {
            document.getElementById('asQuickTuneModal').style.display = 'flex';
            loadAsFavorites();
        }
        function closeAsQuickTuneModal() { document.getElementById('asQuickTuneModal').style.display = 'none'; }
        function closeAsQuickTuneModalIfBackdrop(e) {
            if (e.target === document.getElementById('asQuickTuneModal')) closeAsQuickTuneModal();
        }

        async function loadAsFavorites() {
            try {
                const r = await fetch('/api/as_favorites');
                const favs = await r.json();
                renderAsFavs(favs);
            } catch(e) { console.error('loadAsFavorites:', e); }
        }

        function renderAsFavs(favs) {
            const container = document.getElementById('asFavList');
            if (!favs || favs.length === 0) {
                container.innerHTML = '<div class="qt-empty">None saved</div>';
                return;
            }
            container.innerHTML = favs.map(f => `
                <div class="qt-fav-row">
                    <span class="qt-fav-label">${f.label ? escHtml(f.label) : ''} <span style="color:#888;font-size:11px;">${escHtml(f.node)}</span></span>
                    <button class="btn-tune" onclick="quickConnectNode('${escHtml(f.node)}')">&#9654; Connect</button>
                    <button class="btn-danger-sm" onclick="removeAsFav('${escHtml(f.node)}')" title="Remove">&#10005;</button>
                </div>`).join('');
        }

        async function saveAsFavorite() {
            const node  = document.getElementById('asNodeFavInput').value.trim();
            const label = document.getElementById('asNodeFavLabel').value.trim();
            if (!node) return;
            const r = await fetch('/api/as_favorites', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({node, label})
            });
            const d = await r.json();
            log(d.message, d.ok ? 'ok' : 'error');
            if (d.ok) {
                document.getElementById('asNodeFavInput').value = '';
                document.getElementById('asNodeFavLabel').value = '';
                loadAsFavorites();
            }
        }

        async function removeAsFav(node) {
            const r = await fetch('/api/as_favorites/' + encodeURIComponent(node), {method: 'DELETE'});
            const d = await r.json();
            if (d.ok) loadAsFavorites();
        }

        async function quickConnectNode(node) {
            closeAsQuickTuneModal();
            openAsModal();

            // Unlink the first connected node only — downstream nodes drop automatically
            const firstLinked = _asDirectLink && _asDirectLink[0];
            if (firstLinked) {
                log('Quick connect: unlinking ' + firstLinked + '…', 'ok');
                try {
                    const r = await fetch('/api/allstar/unlink', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({node: firstLinked})
                    });
                    const d = await r.json();
                    log(d.message, d.ok ? 'ok' : 'error');
                } catch(e) { log('Unlink error: ' + e, 'error'); }
                // Short delay to let the node settle before relinking
                await new Promise(res => setTimeout(res, 3000));
            }

            document.getElementById('asRemoteNode').value = node;
            allstarLink('transceive');
        }

        function escHtml(s) {
            return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
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
        var _prevLinkedNodes = null;  // tracks last known set for change detection
        var _nodeToastDuration = parseInt(localStorage.getItem('nodeToastDuration') || '10', 10);
        var _nodeAlertSound    = localStorage.getItem('nodeAlertSound') !== '0';  // default on
        var _alertAudioCtx     = null;

        function _getAlertCtx() {
            if (!_alertAudioCtx) _alertAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
            return _alertAudioCtx;
        }

        function _playNodeAlert(type) {
            if (!_nodeAlertSound) return;
            try {
                const ctx   = _getAlertCtx();
                const freqs = type === 'connect' ? [523, 659, 784] : [784, 659, 523];  // C5→E5→G5 or reverse
                freqs.forEach((freq, i) => {
                    const osc  = ctx.createOscillator();
                    const gain = ctx.createGain();
                    osc.type = 'sine';
                    osc.frequency.value = freq;
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    const t = ctx.currentTime + i * 0.18;
                    gain.gain.setValueAtTime(0, t);
                    gain.gain.linearRampToValueAtTime(0.45, t + 0.01);
                    gain.gain.setValueAtTime(0.45, t + 0.12);
                    gain.gain.exponentialRampToValueAtTime(0.001, t + 0.17);
                    osc.start(t);
                    osc.stop(t + 0.18);
                });
            } catch(e) {}
        }

        function setNodeToastDuration(val) {
            const n = Math.max(2, Math.min(60, parseInt(val, 10) || 10));
            _nodeToastDuration = n;
            localStorage.setItem('nodeToastDuration', n);
            const el = document.getElementById('nodeToastDurationInput');
            if (el) el.value = n;
        }

        function setNodeAlertSound(enabled) {
            _nodeAlertSound = !!enabled;
            localStorage.setItem('nodeAlertSound', enabled ? '1' : '0');
        }

        (function() {
            const dur = document.getElementById('nodeToastDurationInput');
            if (dur) dur.value = _nodeToastDuration;
            const chk = document.getElementById('nodeAlertSoundChk');
            if (chk) chk.checked = _nodeAlertSound;
        })();

        var _nodeToastTimer = null;

        function _showNodeToast(lines) {
            // lines: array of {text, type} — up to 4, shown in one popup.
            // type 'connect' or 'disconnect' per line; popup border color from first entry.
            const c = document.getElementById('nodeToastContainer');
            if (!c) return;
            // Remove any existing toast immediately.
            while (c.firstChild) c.removeChild(c.firstChild);
            if (_nodeToastTimer) { clearTimeout(_nodeToastTimer); _nodeToastTimer = null; }
            if (!lines.length) return;
            const dominantType = lines[0].type;
            const t = document.createElement('div');
            t.className = 'node-toast' + (dominantType === 'disconnect' ? ' disconnect' : '');
            t.innerHTML = lines.map(l => {
                const color = l.type === 'connect' ? '#88ff88' : '#ff8888';
                return `<div style="color:${color}">${l.text}</div>`;
            }).join('');
            c.appendChild(t);
            _nodeToastTimer = setTimeout(() => {
                t.classList.add('fade-out');
                setTimeout(() => { if (c.contains(t)) c.removeChild(t); }, 500);
                _nodeToastTimer = null;
            }, _nodeToastDuration * 1000);
        }

        function _checkNodeChanges(nodes) {
            const newSet = new Set((nodes || []).map(n => n.node));
            const oldSet = new Set((_prevLinkedNodes || []).map(n => n.node));
            if (_prevLinkedNodes !== null) {
                const lines = [];
                newSet.forEach(n => { if (!oldSet.has(n) && lines.length < 4) lines.push({text: 'Node ' + n + ' connected',    type: 'connect'}); });
                oldSet.forEach(n => { if (!newSet.has(n) && lines.length < 4) lines.push({text: 'Node ' + n + ' disconnected', type: 'disconnect'}); });
                if (lines.length) {
                    _showNodeToast(lines);
                    _playNodeAlert(lines[0].type);
                }
            }
            _prevLinkedNodes = nodes ? [...nodes] : [];
        }

        var _dmrToastTimer = null;
        var _activeDmrTg     = null;
        var _activeDmrTgName = null;

        function _showDmrToast(tg, tgName, callsign, type) {
            const c = document.getElementById('nodeToastContainer');
            if (!c) return;
            // Remove any existing DMR toast
            const old = document.getElementById('dmrToast');
            if (old) old.remove();
            if (_dmrToastTimer) { clearTimeout(_dmrToastTimer); _dmrToastTimer = null; }
            const t = document.createElement('div');
            t.id = 'dmrToast';
            t.className = 'node-toast dmr';
            const label    = type === 'disconnect' ? 'TG Disconnected' : 'TG Connected';
            const color    = type === 'disconnect' ? '#ff8888' : '#ffe066';
            const tgLine   = `<div style="color:${color}">${label}</div>`;
            const numLine  = `<div>TG ${tg}</div>`;
            const nameLine = tgName ? `<div style="font-size:0.75em;color:#ffd040;">${tgName}</div>` : '';
            t.innerHTML = tgLine + numLine + nameLine;
            c.appendChild(t);
            _dmrToastTimer = setTimeout(() => {
                t.classList.add('fade-out');
                setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 500);
                _dmrToastTimer = null;
            }, _nodeToastDuration * 1000);
        }

        function _setDirectLink(nodes) {
            // accepts a single node string, array of node strings, or null/empty
            const list = Array.isArray(nodes) ? nodes.filter(Boolean) : (nodes ? [nodes] : []);
            _asDirectLink = list.length ? list : null;
            const badge = document.getElementById('asDirectLinkBadge');
            const nodeEl = document.getElementById('asDirectLinkNode');
            if (_asDirectLink) {
                const shown = _asDirectLink.slice(0, 4);
                const extra = _asDirectLink.length - shown.length;
                nodeEl.textContent = shown.join(' · ') + (extra > 0 ? ' +' + extra : '');
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
            ['btnAsAudio', 'btnAsAudioSidebar', 'mobBtnAsMonitor', 'btnAsAudioOv'].forEach(id => {
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
            document.getElementById('asVolSlider').value = val;
            document.getElementById('asVolSlider').style.setProperty('--vol-pct', val + '%');
            if (!_asMuted && asPlayer) asPlayer.setVolume(val);
            localStorage.setItem('asVolume', val);
        }

        var _asPoller = null;

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
                // Prefer linked_nodes (live IAX2 'L' frames) over direct_links (dispatcher-managed).
                // linked_nodes reflects external changes; direct_links is only updated by this app.
                const liveNodes = d.linked_nodes && d.linked_nodes.length
                    ? d.linked_nodes.map(n => n.node)
                    : (d.direct_links && d.direct_links.length ? d.direct_links : null);
                _checkNodeChanges(d.state === 'connected' ? (d.linked_nodes || []) : []);
                _setDirectLink(d.state === 'connected' && liveNodes ? liveNodes : null);
                // Also update the panel node list from the same data so both are always in sync.
                const nodeListEl = document.getElementById('asNodeList');
                if (nodeListEl) {
                    if (d.state === 'connected' && d.linked_nodes && d.linked_nodes.length) {
                        const modeLabel = {R: 'Mon', T: 'Xcv', M: 'Mon', L: 'Loc'};
                        nodeListEl.innerHTML = d.linked_nodes.map(n =>
                            `<span style="display:inline-block;margin-right:10px;">` +
                            `<span style="color:#7e7;">${n.node}</span>` +
                            `<span style="color:#aaa;font-size:10px;">${modeLabel[n.mode] || n.mode}</span>` +
                            `</span>`
                        ).join('');
                    } else {
                        nodeListEl.textContent = d.state === 'connected' ? '(none)' : '--';
                    }
                }
                const asSec = document.getElementById('asSidebarSection');
                const wasActive = asSec && asSec.classList.contains('as-rx');
                const nowActive = !!(d.state === 'connected' && d.active);
                if (nowActive && !wasActive) log('Allstar RX: signal on node ' + (d.node || '--'), 'ok');
                if (!nowActive && wasActive) log('Allstar RX: signal cleared', 'info');
                if (asSec) asSec.classList.toggle('as-rx', nowActive);
                ['btnAsAudio', 'btnAsAudioSidebar', 'mobBtnAsMonitor', 'btnAsAudioOv'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el) el.classList.toggle('streaming', !!(d.state === 'connected' && d.active) && el.classList.contains('active'));
                });

                const btnConn = document.getElementById('btnAsConnect');
                const btnDisc = document.getElementById('btnAsDisconnect');
                const connected = (d.state === 'connected');
                if (btnConn) btnConn.disabled = (d.state === 'connected' || d.state === 'connecting');
                if (btnDisc) btnDisc.disabled = (d.state === 'idle' || d.state === 'error');
                ['btnPTT', 'btnPTTSidebar', 'mobBtnPTT', 'btnPTTOv'].forEach(id => {
                    const b = document.getElementById(id);
                    if (b) b.disabled = !connected;
                });
                if (!connected) pttStop();

                // keep polling while connected; stop when offline
                if (d.state === 'connected' || d.state === 'connecting') {
                    if (!_asPoller) _asPoller = setInterval(pollAllstarStatus, 400);
                } else {
                    if (_asPoller) { clearInterval(_asPoller); _asPoller = null; }
                    _prevLinkedNodes = null;
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
        let _micDeviceId = null;
        let _micTestRaf = null;
        let _pttWorkletUrl = null;  // cached blob URL for worklet module

        // Worklet runs at whatever rate the AudioContext actually uses (device native,
        // typically 48000 Hz). It decimates to 8000 Hz using a box-filter FIR
        // (simple averaging over `ratio` input samples per output sample — adequate
        // for voice) and emits 160-sample (20 ms) Int16 frames.
        const PTT_WORKLET_CODE = `
class MicDecimator extends AudioWorkletProcessor {
    constructor(options) {
        super();
        this._ratio   = options.processorOptions.ratio; // e.g. 6 for 48k→8k
        this._acc     = 0.0;
        this._accN    = 0;
        this._outBuf  = new Float32Array(160);
        this._outPos  = 0;
    }
    process(inputs) {
        const ch = inputs[0][0];
        if (!ch) return true;
        for (let i = 0; i < ch.length; i++) {
            this._acc += ch[i];
            this._accN++;
            if (this._accN === this._ratio) {
                const s = this._acc / this._ratio;
                this._outBuf[this._outPos++] = s;
                this._acc  = 0.0;
                this._accN = 0;
                if (this._outPos === 160) {
                    const out = new Int16Array(160);
                    for (let k = 0; k < 160; k++)
                        out[k] = Math.max(-32768, Math.min(32767, this._outBuf[k] * 32767 | 0));
                    this.port.postMessage(out.buffer, [out.buffer]);
                    this._outPos = 0;
                }
            }
        }
        return true;
    }
}
registerProcessor('mic-decimator', MicDecimator);
`;

        async function populateMicDevices() {
            try {
                const devices = await navigator.mediaDevices.enumerateDevices();
                const sel = document.getElementById('micDeviceSelect');
                const prev = sel.value;
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
                    sel.selectedIndex = 1;
                    _micDeviceId = sel.value;
                }
            } catch(e) { console.warn('enumerateDevices failed:', e); }
        }

        function onMicDeviceChange() {
            _micDeviceId = document.getElementById('micDeviceSelect').value || null;
        }

        // Retry getUserMedia up to 3 times for Bluetooth HFP devices that need time
        // to switch profiles or release an exclusive Windows audio lock.
        const _BT_RETRYABLE = new Set(['OverconstrainedError', 'NotFoundError', 'NotReadableError']);
        async function _getMicWithRetry(audioConstraints, label) {
            const maxTries = 4, delayMs = 1500;
            for (let attempt = 1; attempt <= maxTries; attempt++) {
                try {
                    return await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
                } catch(e) {
                    const retryable = _BT_RETRYABLE.has(e.name) && audioConstraints.deviceId;
                    if (retryable && attempt < maxTries) {
                        log(label + ': device not ready (' + e.name + ') — attempt ' + attempt + '/' + maxTries + ', retrying in ' + (delayMs/1000) + 's...', 'warn');
                        await new Promise(r => setTimeout(r, delayMs));
                    } else {
                        if (retryable) log(label + ': could not open device after ' + maxTries + ' attempts. Check that no other app (Teams, Discord, Windows voice features) has the mic open exclusively.', 'error');
                        throw e;
                    }
                }
            }
        }

        // One-shot mic test: open mic for 3 seconds, show level, then release device
        async function testMic() {
            if (_pttActive) { log('Release PTT before testing mic', 'warn'); return; }
            const btn = document.getElementById('btnMicTest');
            if (btn) btn.disabled = true;
            let stream, ctx;
            try {
                const baseAudio = { echoCancellation: false, noiseSuppression: false };
                const audioConstraints = _micDeviceId
                    ? { ...baseAudio, deviceId: { exact: _micDeviceId } }
                    : baseAudio;
                stream = await _getMicWithRetry(audioConstraints, 'Mic test');
                await populateMicDevices();  // grant → labels now available
                const track = stream.getAudioTracks()[0];
                log('Mic test: ' + (track ? track.label : 'no track') + ' | ready=' + (track && track.readyState) + ' muted=' + (track && track.muted), 'ok');
                ctx = new AudioContext();
                await ctx.resume();
                log('Mic test: AudioContext rate=' + ctx.sampleRate + ' state=' + ctx.state, 'ok');
                const analyser = ctx.createAnalyser();
                analyser.fftSize = 256;
                ctx.createMediaStreamSource(stream).connect(analyser);
                const buf = new Uint8Array(analyser.frequencyBinCount);
                const bar = document.getElementById('micMeterBar');
                const end = Date.now() + 5000;
                await new Promise(resolve => {
                    (function tick() {
                        _micTestRaf = requestAnimationFrame(() => {
                            analyser.getByteTimeDomainData(buf);
                            let peak = 0;
                            for (let i = 0; i < buf.length; i++) peak = Math.max(peak, Math.abs(buf[i]-128)/128);
                            const pct = Math.min(100, peak * 200);
                            if (bar) { bar.style.width = pct + '%'; bar.style.background = pct > 80 ? '#ff4400' : pct > 50 ? '#ffaa00' : '#00cc44'; }
                            if (Date.now() < end) tick(); else resolve();
                        });
                    })();
                });
            } catch(e) {
                log('Mic test failed: ' + e.message, 'error');
            } finally {
                if (_micTestRaf) { cancelAnimationFrame(_micTestRaf); _micTestRaf = null; }
                if (stream) stream.getTracks().forEach(t => t.stop());
                if (ctx) { try { ctx.close(); } catch(e) {} }
                const bar = document.getElementById('micMeterBar');
                if (bar) bar.style.width = '0%';
                if (btn) btn.disabled = false;
            }
        }

        function _setPTTKeyed(keyed) {
            ['btnPTT', 'btnPTTSidebar', 'mobBtnPTT', 'btnPTTOv'].forEach(id => {
                const b = document.getElementById(id);
                if (b) b.classList.toggle('keyed', keyed);
            });
        }


        async function pttStart() {
            if (_pttActive) return;
            // Check either button is enabled (connected)
            const btnMain = document.getElementById('btnPTT');
            if (btnMain && btnMain.disabled) return;
            _pttActive = true;
            _setPTTKeyed(true);
            log('Allstar TX: keyed up', 'ok');

            try {
                // Open mic first; BT devices may switch HFP profile here.
                const pttAudio = _micDeviceId
                    ? { deviceId: { exact: _micDeviceId }, echoCancellation: true, noiseSuppression: true }
                    : { echoCancellation: true, noiseSuppression: true };
                _pttStream = await _getMicWithRetry(pttAudio, 'PTT');

                // Fresh AudioContext on every PTT press — prevents zombie nodes from
                // previous presses accumulating in the graph and avoids any stale
                // worklet scope state that causes ratio=1 (no decimation) on TX2+.
                if (_pttCtx) { try { await _pttCtx.close(); } catch(e) {} }
                _pttCtx = new AudioContext();
                await _pttCtx.resume();
                // Cache blob URL so addModule has a live URL to load each time.
                if (!_pttWorkletUrl) {
                    const blob = new Blob([PTT_WORKLET_CODE], { type: 'application/javascript' });
                    _pttWorkletUrl = URL.createObjectURL(blob);
                }
                await _pttCtx.audioWorklet.addModule(_pttWorkletUrl);

                const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
                _pttWs = new WebSocket(proto + '//' + location.host + '/ws/allstar-tx');
                _pttWs.binaryType = 'arraybuffer';
                await new Promise((res, rej) => {
                    _pttWs.onopen  = res;
                    _pttWs.onerror = rej;
                });

                const ratio = Math.max(1, Math.round(_pttCtx.sampleRate / 8000));
                log('PTT: ctx rate=' + _pttCtx.sampleRate + ' ratio=' + ratio, 'ok');
                _pttNode = new AudioWorkletNode(_pttCtx, 'mic-decimator', {
                    processorOptions: { ratio }
                });

                const pttAnalyser = _pttCtx.createAnalyser();
                pttAnalyser.fftSize = 256;
                const pttBuf = new Uint8Array(pttAnalyser.frequencyBinCount);
                const bar = document.getElementById('micMeterBar');
                let rafId;
                (function meterTick() {
                    rafId = requestAnimationFrame(meterTick);
                    pttAnalyser.getByteTimeDomainData(pttBuf);
                    let peak = 0;
                    for (let i = 0; i < pttBuf.length; i++) {
                        const v = Math.abs(pttBuf[i] - 128) / 128;
                        if (v > peak) peak = v;
                    }
                    const pct = Math.min(100, peak * 200);
                    if (bar) { bar.style.width = pct + '%'; bar.style.background = pct > 80 ? '#ff4400' : pct > 50 ? '#ffaa00' : '#00cc44'; }
                })();
                _pttNode._stopMeter = () => cancelAnimationFrame(rafId);

                _pttNode.port.onmessage = (e) => {
                    if (_pttWs && _pttWs.readyState === WebSocket.OPEN) _pttWs.send(e.data);
                };

                const src = _pttCtx.createMediaStreamSource(_pttStream);
                src.connect(pttAnalyser);
                src.connect(_pttNode);
                _pttNode.connect(_pttCtx.destination);

            } catch(err) {
                console.error('PTT start failed:', err);
                log('PTT error [' + err.name + ']: ' + err.message, 'error');
                pttStop();
            }
        }

        function pttStop() {
            if (!_pttActive) return;
            _pttActive = false;
            _setPTTKeyed(false);
            log('Allstar TX: unkeyed', 'info');
            if (_pttNode) {
                if (_pttNode._stopMeter) _pttNode._stopMeter();
                try { _pttNode.disconnect(); } catch(e) {}
                _pttNode = null;
            }
            if (_pttStream) { _pttStream.getTracks().forEach(t => t.stop()); _pttStream = null; }
            if (_pttWs)     { try { _pttWs.close(); } catch(e) {} _pttWs = null; }
            const bar = document.getElementById('micMeterBar');
            if (bar) bar.style.width = '0%';
        }

        // Wire PTT buttons — start on the button, stop on document so release
        // is always caught even if the cursor/finger leaves the element.
        // Avoid pointercancel: it fires when the keyed CSS animation shifts layout.
        function _wirePTTButton(id) {
            const btn = document.getElementById(id);
            if (!btn) return;
            btn.addEventListener('mousedown', (e) => {
                if (e.button !== 0 || btn.disabled) return;
                e.preventDefault();
                pttStart();
            });
            btn.addEventListener('touchstart', (e) => {
                if (btn.disabled) return;
                e.preventDefault();  // prevent ghost mousedown after touch
                pttStart();
            }, { passive: false });
        }
        // Single document-level release handlers — always fires regardless of where
        // the pointer ends up, avoiding any element-level event-capture issues.
        document.addEventListener('mouseup',     () => pttStop());
        document.addEventListener('touchend',    () => pttStop());
        document.addEventListener('touchcancel', () => pttStop());

        // -------------------------
        // STARTUP
        // -------------------------

        // On mobile, collapse all panels for a clean minimal view
        if (window.innerWidth <= 600) {
            dispatchLogOpen = false;
            // allstarBody already starts collapsed by default
        }

        // -------------------------
        // SCANNER (TRUNK RECORDER)
        // -------------------------
        var _trCalls      = [];       // history mirror (newest first)
        var _trSystems    = {};       // short_name → label
        var _trQueue      = [];       // playback queue (oldest first)
        var _trPlaying    = null;     // call object currently playing
        var _trLastCall   = null;     // last call played (persists after audio ends)
        var _trPaused     = false;
        var _trAutoplay   = true;
        var _trAudioEnabled = true;
        var _trDisabled   = {};       // 'system:tg' → true (disabled), 'avoided' → true (avoided/reddish)
        var _trLockedSystem = null;   // when set, only calls from this system are played
        var _trConsoleTgs = {};       // system → [{id,tag,label,group,description}] for console grid
        var TR_INTER_CALL_MS = 800;   // gap between calls in ms

        // Persist & restore prefs
        (function _initTrPrefs() {
            const ap = localStorage.getItem('trAutoplay');
            _trAutoplay = ap === null ? true : ap === 'true';
            document.getElementById('trAutoplayChk').checked = _trAutoplay;
            try { _trDisabled = JSON.parse(localStorage.getItem('trDisabled') || '{}'); } catch(e) {}
            const ae = localStorage.getItem('trAudioEnabled');
            _trAudioEnabled = ae === null ? true : ae === 'true';
            _updateTrAudioBtn();
            const sv = parseInt(localStorage.getItem('trVolume') ?? '100');
            setTrVolume(sv);
        })();

        function saveTrPrefs() {
            _trAutoplay = document.getElementById('trAutoplayChk').checked;
            localStorage.setItem('trAutoplay', _trAutoplay);
        }

        function _saveTrDisabled() {
            localStorage.setItem('trDisabled', JSON.stringify(_trDisabled));
        }

        function _trKey(call) { return call.system + ':' + call.talkgroup; }

        function _isTrDisabled(call) { return !!_trDisabled[_trKey(call)]; }

        // ---- Volume ----
        function setTrVolume(val) {
            val = parseInt(val);
            document.getElementById('trVolDisplay').textContent = val + '%';
            document.getElementById('trVolSlider').value = val;
            document.getElementById('trVolDisplayOv').textContent = val + '%';
            document.getElementById('trVolSliderOv').value = val;
            document.getElementById('trAudio').volume = val / 100;
            localStorage.setItem('trVolume', val);
        }

        function setTrVolumeOv(val) { setTrVolume(val); }

        // ---- Scanner audio enable/disable ----
        function trToggleAudio() {
            _trAudioEnabled = !_trAudioEnabled;
            localStorage.setItem('trAudioEnabled', _trAudioEnabled);
            const audio = document.getElementById('trAudio');
            if (!_trAudioEnabled) {
                audio.pause();
                audio.src = '';
                _trQueue = [];
                _trPlaying = null;
                document.getElementById('trPulse').classList.remove('on');
                _updateTrQueueBadge();
            }
            _updateTrAudioBtn();
        }

        function _updateTrAudioBtn() {
            const playing = !!_trPlaying && _trAudioEnabled;
            ['mobBtnTrAudio', 'trAudioToggleOv'].forEach(id => {
                const el = document.getElementById(id);
                if (!el) return;
                el.classList.toggle('active', _trAudioEnabled);
                el.classList.toggle('streaming', playing);
            });
            const mob = document.getElementById('mobBtnTrAudio');
            if (mob) {
                mob.innerHTML = _trAudioEnabled
                    ? '<span style="font-size:16px;">&#128251;</span><span>Scanner</span>'
                    : '<span style="font-size:16px;">&#128263;</span><span>Scanner</span>';
            }
            const ov = document.getElementById('trAudioToggleOv');
            if (ov) ov.textContent = _trAudioEnabled ? '🔊 Enable' : '🔇 Muted';
        }

        // ---- System Lock ----
        function trLockSysToggle() {
            if (_trLockedSystem) {
                _trLockedSystem = null;
            } else {
                const target = _trPlaying || _trLastCall;
                if (!target) return;
                _trLockedSystem = target.system;
                // Flush queued calls from other systems
                _trQueue = _trQueue.filter(c => c.system === _trLockedSystem);
                _updateTrQueueBadge();
            }
            _updateTrLockUI();
        }

        function _updateTrLockUI() {
            const btn = document.getElementById('trLockSysBtn');
            const badge = document.getElementById('trLockedBadge');
            if (btn) {
                btn.style.background   = _trLockedSystem ? '#003a00' : '#1a1a1a';
                btn.style.borderColor  = _trLockedSystem ? '#00aa00' : '#333';
                btn.style.color        = _trLockedSystem ? '#4f4'    : '#777';
            }
            if (badge) {
                badge.style.display = _trLockedSystem ? '' : 'none';
                badge.textContent   = _trLockedSystem
                    ? 'LOCKED: ' + (_trSystems[_trLockedSystem] || _trLockedSystem).toUpperCase()
                    : '';
            }
        }

        // ---- Pause / Resume ----
        function trPauseToggle() {
            _trPaused = !_trPaused;
            _updateTrPauseUI();
            if (!_trPaused) _trDequeue();  // drain queue on resume
        }

        function _updateTrPauseUI() {
            const label = _trPaused ? '▶ Resume' : '⏸ Pause';
            document.getElementById('trPauseBtnModal').innerHTML = label;
            document.getElementById('trPauseBtn').innerHTML = _trPaused
                ? '▶<span class="btn-label"> Resume</span>'
                : '⏸<span class="btn-label"> Pause</span>';
            document.getElementById('trPausedBadge').style.display = _trPaused ? '' : 'none';
            _updateTrQueueBadge();
        }

        function _updateTrQueueBadge() {
            // Modal badge (text)
            const el = document.getElementById('trQueueBadge');
            el.textContent = _trQueue.length ? _trQueue.length + ' queued' + (_trPaused ? ' (paused)' : '') : '';
            // Status bar badge (count number)
            const cnt = document.getElementById('trQueueCount');
            if (_trQueue.length) {
                cnt.textContent = _trQueue.length;
                cnt.style.display = '';
            } else {
                cnt.style.display = 'none';
            }
        }

        // ---- Skip / Avoid ----
        var _trSkippedTimer = null;
        function trSkip() {
            const target = _trPlaying || _trLastCall;
            const audio = document.getElementById('trAudio');
            audio.pause();
            audio.src = '';
            _trPlaying = null;
            _updateTrAudioBtn();
            // Skip only removes the currently playing call — queue stays intact
            // (Avoid is what purges future calls from the same channel)
            _updateTrQueueBadge();
            renderTrCalls();
            // Flash SKIPPED badge
            if (target) {
                const badge = document.getElementById('trSkippedBadge');
                badge.style.display = '';
                clearTimeout(_trSkippedTimer);
                _trSkippedTimer = setTimeout(() => { badge.style.display = 'none'; }, 2500);
            }
            setTimeout(_trDequeue, 300);
        }

        function trAvoid() {
            const target = _trPlaying || _trLastCall;
            if (!target) return;
            const key = _trKey(target);
            _trDisabled[key] = 'avoided';
            _saveTrDisabled();
            _trQueue = _trQueue.filter(c => _trKey(c) !== key);
            _updateTrQueueBadge();
            renderTrConsole();
            trSkip();
        }

        // ---- Playback queue ----
        function _trEnqueue(call) {
            if (!call.audio) return;
            if (_isTrDisabled(call)) return;
            if (_trLockedSystem && call.system !== _trLockedSystem) return;
            _trQueue.push(call);
            _updateTrQueueBadge();
            if (!_trPlaying && !_trPaused) _trDequeue();
        }

        function _trDequeue() {
            if (_trPaused || _trQueue.length === 0) return;
            // Drain any queued calls that don't match the locked system
            while (_trQueue.length && _trLockedSystem && _trQueue[0].system !== _trLockedSystem)
                _trQueue.shift();
            if (_trQueue.length === 0) { _updateTrQueueBadge(); return; }
            const call = _trQueue.shift();
            _updateTrQueueBadge();
            _trStartPlay(call);
        }

        function _trStartPlay(call) {
            _trPlaying = call;
            _trLastCall = call;
            document.getElementById('trSkippedBadge').style.display = 'none';
            clearTimeout(_trSkippedTimer);
            renderTrCalls();
            const audio = document.getElementById('trAudio');
            const vol = parseInt(localStorage.getItem('trVolume') ?? '100');
            audio.volume = vol / 100;
            audio.src = '/api/tr/audio/' + encodeURIComponent(call.audio);
            audio.play().catch(() => {});
            _updateTrAudioBtn();

            const tgLabel = call.talkgroup_label || call.talkgroup_tag || ('TG ' + call.talkgroup);
            const sys     = _trSystems[call.system] || call.system || '';

            // Status bar — show what's actually playing
            document.getElementById('trPulse').classList.add('on');
            document.getElementById('trSystemBadge').textContent = sys.toUpperCase() || '--';
            const errHtml = (call.error_count > 0)
                ? ' <span style="font-size:10px;color:#f88;font-weight:normal;">Err:' + call.error_count + '</span>'
                : '';
            document.getElementById('trTgBadge').innerHTML =
                escHtml(tgLabel) + ' <span style="font-size:11px;color:#aaa;font-weight:normal;">' + call.talkgroup + '</span>' + errHtml;

            // Modal now-playing label
            document.getElementById('trNowPlaying').textContent =
                sys + ' · ' + tgLabel + (call.talkgroup_group ? ' · ' + call.talkgroup_group : '');
        }

        // Wire audio ended event
        document.getElementById('trAudio').addEventListener('ended', function() {
            document.getElementById('trPulse').classList.remove('on');
            _trLastCall = _trPlaying || _trLastCall;
            _trPlaying = null;
            _updateTrAudioBtn();
            renderTrCalls();
            // Status bar badges intentionally left showing the last played call
            setTimeout(_trDequeue, TR_INTER_CALL_MS);
        });

        // ---- Incoming call ----
        function _onTrCall(call) {
            _trCalls.unshift(call);
            if (_trCalls.length > 200) _trCalls.pop();

            if (call.system && !_trSystems[call.system]) {
                _trSystems[call.system] = call.system_label || call.system;
                _rebuildSystemFilter();
                _rebuildConsoleSystemSelect();
            }

            _trSystemFilterChanged();  // refresh TG dropdown with new call's TG

            if (_trAutoplay && _trAudioEnabled) _trEnqueue(call);
        }

        // ---- Modals ----
        function openTrModal() {
            document.getElementById('trModal').style.display = 'flex';
            const v = parseInt(localStorage.getItem('trVolume') ?? '100');
            setTrVolume(v);
            _updateTrPauseUI();
            _trSystemFilterChanged();  // populate TG dropdown on open
        }
        function closeTrModal() { document.getElementById('trModal').style.display = 'none'; }
        function closeTrModalIfBackdrop(e) { if (e.target===document.getElementById('trModal')) closeTrModal(); }

        // TG Console modal
        function openTrConsoleModal() {
            document.getElementById('trConsoleModal').style.display = 'flex';
            _rebuildConsoleSystemSelect();
            renderTrConsole();
        }
        function closeTrConsoleModal() { document.getElementById('trConsoleModal').style.display = 'none'; }
        function closeTrConsoleModalIfBackdrop(e) { if (e.target===document.getElementById('trConsoleModal')) closeTrConsoleModal(); }

        function _rebuildConsoleSystemSelect() {
            const sel = document.getElementById('trConsoleSystem');
            const cur = sel.value;
            while (sel.options.length > 1) sel.remove(1);
            Object.entries(_trSystems).forEach(([k,v]) => {
                const o = document.createElement('option');
                o.value = k; o.textContent = v; sel.appendChild(o);
            });
            sel.value = cur;
        }

        async function renderTrConsole() {
            const sys    = document.getElementById('trConsoleSystem').value;
            const filter = document.getElementById('trConsoleFilter').value;
            const el     = document.getElementById('trConsoleGrid');

            // Fetch TG list for selected system(s)
            const systems = sys ? [sys] : Object.keys(_trSystems);
            if (!systems.length) {
                el.innerHTML = '<div class="qt-empty">No talkgroups known yet</div>';
                return;
            }

            // Load missing TG data from server
            for (const s of systems) {
                if (!_trConsoleTgs[s]) {
                    try {
                        const r = await fetch('/api/tr/talkgroups/' + encodeURIComponent(s));
                        _trConsoleTgs[s] = await r.json();
                    } catch(e) { _trConsoleTgs[s] = []; }
                }
            }

            let html = '';
            for (const s of systems) {
                const tgs = _trConsoleTgs[s] || [];
                if (!tgs.length) continue;
                // Group by group label
                const groups = {};
                tgs.forEach(tg => {
                    const g = tg.group || 'Ungrouped';
                    (groups[g] = groups[g] || []).push(tg);
                });
                const sysLabel = escHtml(_trSystems[s] || s);
                if (systems.length > 1)
                    html += `<div style="font-size:11px;color:#4fc3f7;font-weight:bold;margin:8px 0 4px;">${sysLabel}</div>`;
                for (const [grp, items] of Object.entries(groups)) {
                    html += `<div class="tr-console-group">
                        <div class="tr-console-group-label">${escHtml(grp)}</div>
                        <div class="tr-console-buttons">`;
                    items.forEach(tg => {
                        const key   = s + ':' + tg.id;
                        const state = _trDisabled[key];
                        const isDisabled = !!state;
                        if (filter === 'active'   &&  isDisabled) return;
                        if (filter === 'disabled' && !isDisabled) return;
                        const cls   = state === 'avoided' ? 'avoided' : state ? 'disabled' : '';
                        const name = tg.label || tg.description || tg.tag || '';
                        const sub  = tg.group || tg.tag || '';
                        html += `<button class="tr-tg-btn ${cls}" onclick="trToggleTg('${escHtml(s)}',${tg.id})"
                                    title="TG ${tg.id}${name ? ' · ' + name : ''}${sub ? ' [' + sub + ']' : ''}">
                            <span class="tr-tg-btn-name">${escHtml(name || ('TG ' + tg.id))}</span>
                            <span class="tr-tg-btn-num">TG ${tg.id}${sub ? ' · ' + escHtml(sub) : ''}</span>
                        </button>`;
                    });
                    html += `</div></div>`;
                }
            }
            el.innerHTML = html || '<div class="qt-empty">No talkgroups known yet — import a CSV or wait for calls</div>';
        }

        function trToggleTg(sys, tgId) {
            const key = sys + ':' + tgId;
            if (_trDisabled[key]) {
                delete _trDisabled[key];
            } else {
                _trDisabled[key] = true;
                // Purge queued calls for this TG
                _trQueue = _trQueue.filter(c => !(c.system === sys && c.talkgroup === tgId));
                _updateTrQueueBadge();
            }
            _saveTrDisabled();
            renderTrConsole();
        }

        function trConsoleEnableAll() {
            const sys = document.getElementById('trConsoleSystem').value;
            const systems = sys ? [sys] : Object.keys(_trSystems);
            systems.forEach(s => {
                Object.keys(_trDisabled).filter(k => k.startsWith(s + ':')).forEach(k => delete _trDisabled[k]);
            });
            _saveTrDisabled();
            renderTrConsole();
        }

        function trConsoleDisableAll() {
            const sys = document.getElementById('trConsoleSystem').value;
            const systems = sys ? [sys] : Object.keys(_trSystems);
            systems.forEach(s => {
                (_trConsoleTgs[s] || []).forEach(tg => { _trDisabled[s + ':' + tg.id] = true; });
            });
            _saveTrDisabled();
            _trQueue = [];
            _updateTrQueueBadge();
            renderTrConsole();
        }

        // TG Import modal
        var _trImportFile = null;

        function openTrImportModal() {
            document.getElementById('trImportModal').style.display = 'flex';
            _populateTrImportSystems();
            _loadTrTgSummary();
        }
        function closeTrImportModal() { document.getElementById('trImportModal').style.display = 'none'; }
        function closeTrImportModalIfBackdrop(e) { if (e.target===document.getElementById('trImportModal')) closeTrImportModal(); }

        function _populateTrImportSystems() {
            const dl = document.getElementById('trImportSystemList');
            dl.innerHTML = '';
            Object.keys(_trSystems).forEach(k => {
                const opt = document.createElement('option');
                opt.value = k; opt.label = _trSystems[k] || k;
                dl.appendChild(opt);
            });
        }

        function onTrImportFileChosen() {
            const input = document.getElementById('trImportFile');
            _trImportFile = input.files[0] || null;
            document.getElementById('trImportFileName').textContent =
                _trImportFile ? _trImportFile.name : 'No file chosen';
        }

        async function doTrImport() {
            const sys = document.getElementById('trImportSystem').value.trim();
            if (!sys) { alert('Enter the system short name (e.g. lcraboerne)'); return; }
            if (!_trImportFile) { alert('Choose a CSV file first'); return; }
            const fd = new FormData();
            fd.append('file', _trImportFile);
            try {
                const r = await fetch('/api/tr/talkgroups/' + encodeURIComponent(sys), {method: 'POST', body: fd});
                const d = await r.json();
                log(d.message, d.ok ? 'ok' : 'error');
                if (d.ok) {
                    _trImportFile = null;
                    document.getElementById('trImportFile').value = '';
                    document.getElementById('trImportFileName').textContent = 'No file chosen';
                    delete _trConsoleTgs[sys];  // force console reload
                    _loadTrTgSummary();
                }
            } catch(e) { log('TG import error: ' + e, 'error'); }
        }

        async function _loadTrTgSummary() {
            try {
                const r = await fetch('/api/tr/talkgroups');
                const systems = await r.json();
                const el = document.getElementById('trTgSummary');
                if (!systems.length) {
                    el.innerHTML = '<div class="qt-empty">No talkgroup data loaded</div>';
                    return;
                }
                el.innerHTML = systems.map(s =>
                    `<div class="qt-fav-row">
                        <span class="qt-fav-label">${escHtml(s.label)} <span style="color:#888;font-size:10px;">${escHtml(s.short_name)}</span></span>
                        <span style="font-size:11px;color:#4fc3f7;white-space:nowrap;">${s.count} TGs</span>
                     </div>`
                ).join('');
            } catch(e) { console.error('_loadTrTgSummary:', e); }
        }

        async function _fetchTrCalls() {
            try {
                const [callsR, sysR] = await Promise.all([
                    fetch('/api/tr/calls'),
                    fetch('/api/tr/systems'),
                ]);
                _trCalls  = await callsR.json();
                const sysList = await sysR.json();
                sysList.forEach(s => { _trSystems[s.short_name] = s.label; });
                _rebuildSystemFilter();
                _rebuildConsoleSystemSelect();
                _trSystemFilterChanged();
            } catch(e) { console.error('_fetchTrCalls:', e); }
        }

        function _rebuildSystemFilter() {
            const sel = document.getElementById('trSystemFilter');
            const cur = sel.value;
            while (sel.options.length > 1) sel.remove(1);
            Object.entries(_trSystems).forEach(([k,v]) => {
                const opt = document.createElement('option');
                opt.value = k; opt.textContent = v; sel.appendChild(opt);
            });
            sel.value = cur;
        }

        // Manual play from call log (bypasses queue, plays immediately)
        function _playTrCallById(id) {
            const call = _trCalls.find(c => String(c.id) === String(id));
            if (call) _playTrCall(call);
        }

        function _playTrCall(call) {
            if (!call.audio) return;
            const sysFilter = document.getElementById('trSystemFilter').value;
            const tgFilter  = document.getElementById('trTgFilter').value;
            const visible = _trCalls.filter(c =>
                (!sysFilter || c.system === sysFilter) &&
                (!tgFilter  || String(c.talkgroup) === tgFilter)
            );
            const idx = visible.findIndex(c => c.id === call.id);
            // Queue calls newer than the clicked one (lower indices), oldest-first
            _trQueue = (idx > 0 ? visible.slice(0, idx).reverse() : [])
                .filter(c => c.audio && !_isTrDisabled(c));
            _updateTrQueueBadge();
            _trStartPlay(call);
        }

        function _trSystemFilterChanged() {
            // Repopulate TG filter for the selected system, then re-render
            const sys = document.getElementById('trSystemFilter').value;
            const tgSel = document.getElementById('trTgFilter');
            const prevTg = tgSel.value;
            // Collect TGs from current call history for this system
            const tgMap = {};
            _trCalls.filter(c => !sys || c.system === sys).forEach(c => {
                if (c.talkgroup != null) {
                    const label = c.talkgroup_label || c.talkgroup_tag || ('TG ' + c.talkgroup);
                    tgMap[c.talkgroup] = label;
                }
            });
            const opts = ['<option value="">All TGs</option>'];
            Object.keys(tgMap).sort((a,b) => tgMap[a].localeCompare(tgMap[b])).forEach(id => {
                const sel = String(id) === prevTg ? ' selected' : '';
                opts.push(`<option value="${escHtml(String(id))}"${sel}>${escHtml(tgMap[id])} (${id})</option>`);
            });
            tgSel.innerHTML = opts.join('');
            renderTrCalls();
        }

        function renderTrCalls() {
            const filter  = document.getElementById('trSystemFilter').value;
            const tgFilter = document.getElementById('trTgFilter').value;
            const el      = document.getElementById('trCallLog');
            const visible = _trCalls.filter(c =>
                (!filter || c.system === filter) &&
                (!tgFilter || String(c.talkgroup) === tgFilter)
            );
            if (!visible.length) {
                el.innerHTML = '<div class="qt-empty">No calls received yet</div>';
                return;
            }
            el.innerHTML = visible.map(c => {
                const tgLabel  = c.talkgroup_label || c.talkgroup_tag || ('TG ' + c.talkgroup);
                const sub      = [c.talkgroup_description, c.talkgroup_group].filter(Boolean).join(' · ') || ('TG ' + c.talkgroup);
                const dur      = c.call_length ? c.call_length + 's' : '';
                const freq     = c.freq ? (c.freq / 1e6).toFixed(4) + ' MHz' : '';
                const ts       = new Date(c.start_time * 1000).toLocaleTimeString();
                const emerg    = c.emergency ? '<span class="tr-emerg-tag">EMERG</span>' : '';
                const playing  = (_trPlaying && _trPlaying.id === c.id) ? ' playing' : '';
                const disabled = _isTrDisabled(c) ? ' style="opacity:0.35"' : '';
                const sys      = escHtml(_trSystems[c.system] || c.system || '');
                return `<div class="tr-call-row${c.emergency ? ' emergency' : ''}${playing}"${disabled} data-callid="${escHtml(String(c.id))}" onclick="_playTrCallById(this.dataset.callid)">
                    <div class="tr-sys-badge">${escHtml(sys)}</div>
                    <div class="tr-call-info">
                        <div class="tr-tg-name">${escHtml(tgLabel)}${emerg}</div>
                        <div class="tr-tg-sub">${escHtml(sub)}</div>
                    </div>
                    <div class="tr-call-meta">${escHtml(ts)}<br>${escHtml(dur)}${dur && freq ? ' · ' : ''}${escHtml(freq)}</div>
                </div>`;
            }).join('');
        }

        connectSSE();
        setInterval(pollStatus, 5000);
        pollStatus();
        applyStoredVolume();
        applyStoredFilters();
        _fetchTrCalls();
        _initSdr();
        fetch('/api/sdr/state').then(r=>r.json()).then(_onSdrState).catch(()=>{});
        log('Dispatcher ready', 'ok');
        // Populate device list on load (labels appear only after mic permission granted via Test or PTT)
        populateMicDevices().catch(() => {});
        _wirePTTButton('btnPTT');
        _wirePTTButton('btnPTTSidebar');
        _wirePTTButton('mobBtnPTT');
        _wirePTTButton('btnPTTOv');
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

@app.route('/api/as_favorites', methods=['GET'])
def get_as_favs():
    with as_favorites_lock:
        return jsonify(as_favorites)

@app.route('/api/as_favorites', methods=['POST'])
def add_as_fav():
    data  = request.get_json()
    node  = str(data.get('node', '')).strip()
    label = data.get('label', '').strip()
    if not node.isdigit():
        return jsonify({"ok": False, "message": "Invalid node number"})
    with as_favorites_lock:
        if any(f['node'] == node for f in as_favorites):
            return jsonify({"ok": True, "message": f"Node {node} already in favorites"})
        as_favorites.append({"node": node, "label": label})
        save_as_favorites(as_favorites)
    return jsonify({"ok": True, "message": f"Saved node {node} to favorites"})

@app.route('/api/as_favorites/<node>', methods=['DELETE'])
def remove_as_fav(node):
    with as_favorites_lock:
        before = len(as_favorites)
        as_favorites[:] = [f for f in as_favorites if f['node'] != node]
        save_as_favorites(as_favorites)
    return jsonify({"ok": True, "message": f"Removed node {node} from favorites"})

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


# -------------------------
# TRUNK RECORDER ENDPOINTS
# -------------------------

@app.route('/api/call-upload', methods=['POST'])
def tr_call_upload():
    """Compatible with Trunk Recorder's rdio-scanner uploader plugin."""
    key = request.form.get('key', '')
    valid_keys = set(TR_API_KEY) if isinstance(TR_API_KEY, list) else ({TR_API_KEY} if TR_API_KEY else set())
    if valid_keys and key not in valid_keys:
        return jsonify({'error': 'unauthorized'}), 401

    f = request.form

    # SDRTrunk sends a startup probe with {'system': '2', 'test': '1'} — ignore it
    if f.get('test'):
        return 'Incomplete call data: no talkgroup\n', 200

    # TR sends systemLabel; SDRTrunk sends system — accept both
    short_name = f.get('systemLabel') or f.get('system') or 'unknown'
    tg_id_raw  = f.get('talkgroup', '0')
    try:
        tg_id = int(tg_id_raw)
    except ValueError:
        tg_id = 0

    ts_raw = f.get('dateTime', '')
    try:
        start_time = int(ts_raw)
    except ValueError:
        start_time = int(time.time())

    try:
        freq = int(f.get('frequency', 0))
    except ValueError:
        freq = 0

    try:
        src_list = json.loads(f.get('sources', '[]'))
    except Exception:
        src_list = []
    # SDRTrunk sends a single 'source' string instead of a 'sources' JSON array
    if not src_list and f.get('source'):
        try:
            src_list = [{'src': int(f.get('source'))}]
        except (ValueError, TypeError):
            pass

    # Register system label on first appearance
    if short_name not in tr_systems:
        tr_systems[short_name] = TR_SYSTEMS.get(short_name, short_name)

    # Enrich talkgroup fields from imported RR data if TR didn't supply them
    with tr_talkgroups_lock:
        tg_lookup = tr_talkgroups.get(short_name, {}).get(tg_id, {})
    tg_tag   = f.get('talkgroupTag', '')   or tg_lookup.get('tag', '')
    tg_group = f.get('talkgroupGroup', '') or tg_lookup.get('group', '')
    tg_desc  = f.get('talkgroupName', '')  or tg_lookup.get('description', '')
    tg_label = f.get('talkgroupLabel', '')

    # Sum error count across all frequency slots
    try:
        freq_list = json.loads(f.get('frequencies', '[]'))
        error_count = sum(int(fq.get('errorCount', 0)) for fq in freq_list)
    except Exception:
        error_count = 0

    # Save audio file
    audio_filename = None
    audio_file = request.files.get('audio')
    if audio_file and audio_file.filename:
        ext = os.path.splitext(audio_file.filename)[1] or '.m4a'
        audio_filename = f"{short_name}_{tg_id}_{start_time}_{uuid.uuid4().hex[:6]}{ext}"
        audio_file.save(os.path.join(TR_AUDIO_DIR, audio_filename))

    call = {
        'id':                    uuid.uuid4().hex[:8],
        'system':                short_name,
        'system_label':          tr_systems.get(short_name, short_name),
        'talkgroup':             tg_id,
        'talkgroup_tag':         tg_tag,
        'talkgroup_label':       tg_label,
        'talkgroup_group':       tg_group,
        'talkgroup_description': tg_desc,
        'start_time':            start_time,
        'call_length':           0,
        'emergency':             False,
        'encrypted':             False,
        'freq':                  freq,
        'error_count':           error_count,
        'srcList':               src_list,
        'audio':                 audio_filename,
        'received':              time.time(),
    }

    # Track seen talkgroups for the TG console
    with tr_talkgroups_lock:
        tr_seen_tgs.setdefault(short_name, {})[tg_id] = {
            'tag':         tg_tag,
            'label':       tg_label,
            'group':       tg_group,
            'description': tg_desc,
        }

    evicted_audio = None
    with tr_calls_lock:
        tr_calls.insert(0, call)
        if len(tr_calls) > TR_MAX_CALLS:
            old = tr_calls.pop()
            evicted_audio = old.get('audio')
        _save_tr_calls()

    if evicted_audio:
        try:
            os.remove(os.path.join(TR_AUDIO_DIR, evicted_audio))
        except OSError:
            pass

    push_event({'event': 'tr_call', **call})
    return 'Call imported successfully.\n', 200


@app.route('/api/tr/calls')
def tr_calls_list():
    with tr_calls_lock:
        return jsonify(list(tr_calls))


@app.route('/api/tr/systems')
def tr_systems_list():
    return jsonify([{'short_name': k, 'label': v} for k, v in tr_systems.items()])


@app.route('/api/tr/audio/<path:filename>')
def tr_audio(filename):
    filename = os.path.basename(filename)  # strip any path traversal
    path = os.path.join(TR_AUDIO_DIR, filename)
    if not os.path.isfile(path):
        return '', 404
    return send_file(path)


@app.route('/api/tr/talkgroups/<short_name>', methods=['POST'])
def tr_tg_import(short_name):
    """Accept a RadioReference CSV upload and store talkgroup data for a system."""
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'message': 'No file provided'}), 400

    import csv, io
    text   = f.read().decode('utf-8-sig', errors='replace')
    reader = csv.DictReader(io.StringIO(text))

    # Normalise header names — RadioReference uses "Alpha Tag", "Description", "Tag", "Group", "Dec"/"Decimal"
    tg_data = {}
    rows    = 0

    # Column name aliases (case-insensitive) to handle minor RR format variations
    def _col(row, *names):
        for n in names:
            for k, v in row.items():
                if k and k.strip().lower() == n.lower():
                    return v.strip() if v else ''
        return ''

    for row in reader:
        dec = _col(row, 'Decimal', 'Dec', 'DEC')
        if not dec:
            continue
        try:
            tg_id = int(dec)
        except ValueError:
            continue
        tg_data[tg_id] = {
            'label':       _col(row, 'Alpha Tag', 'Alpha', 'Label'),
            'description': _col(row, 'Description', 'Desc'),
            'group':       _col(row, 'Group'),
            'tag':         _col(row, 'Tag'),   # category (Fire, Law, EMS, etc.)
        }
        rows += 1

    if not rows:
        return jsonify({'ok': False, 'message': 'No valid talkgroup rows found — check CSV format (expected columns: Decimal, Alpha Tag, Description, Tag, Group)'})

    with tr_talkgroups_lock:
        tr_talkgroups[short_name] = tg_data

    # Persist to disk
    try:
        with open(_tg_file(short_name), 'w') as out:
            json.dump({str(k): v for k, v in tg_data.items()}, out, indent=2)
    except Exception as e:
        return jsonify({'ok': True, 'message': f'Loaded {rows} talkgroups (disk save failed: {e})', 'count': rows})

    # Register system label if not already known
    if short_name not in tr_systems:
        tr_systems[short_name] = TR_SYSTEMS.get(short_name, short_name)

    return jsonify({'ok': True, 'message': f'Imported {rows} talkgroups for {short_name}', 'count': rows})


@app.route('/api/tr/talkgroups/<short_name>', methods=['GET'])
def tr_tg_get(short_name):
    """Return full talkgroup dict for a system (imported + seen from calls)."""
    with tr_talkgroups_lock:
        imported = tr_talkgroups.get(short_name, {})
        seen     = tr_seen_tgs.get(short_name, {})
    merged = {**imported}
    for tg_id, info in seen.items():
        if tg_id not in merged:
            merged[tg_id] = info
    return jsonify([{'id': k, **v} for k, v in sorted(merged.items())])


@app.route('/api/tr/talkgroups')
def tr_tg_summary():
    """Return count of loaded talkgroups per system."""
    with tr_talkgroups_lock:
        return jsonify([{'short_name': k, 'label': tr_systems.get(k, k), 'count': len(v)}
                        for k, v in tr_talkgroups.items()])


@sock.route('/ws/allstar-tx')
def allstar_tx_ws(ws):
    """Receive Int16 PCM from the browser and send it up the IAX2 stack."""
    allstar_mgr.reset_voice_ts()  # seed fresh timestamp sequence for this PTT press
    try:
        while True:
            data = ws.receive()
            if data is None:
                break
            if isinstance(data, bytes) and len(data) >= 2:
                allstar_mgr.send_voice(data)
    except Exception:
        pass


# -------------------------
# SDR SCANNER PROXY ENDPOINTS
# -------------------------

@app.route('/api/sdr/state')
def sdr_state():
    return jsonify(_sdr_state_snapshot())

@app.route('/api/sdr/skip', methods=['POST'])
def sdr_skip():
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(_sdr_api_url('/api/skip'), method='POST', data=b''),
            timeout=3)
        return jsonify({'ok': True}), r.status
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 502

@app.route('/api/sdr/resume', methods=['POST'])
def sdr_resume():
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(_sdr_api_url('/api/resume'), method='POST', data=b''),
            timeout=3)
        return jsonify({'ok': True}), r.status
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 502

@app.route('/api/sdr/hold', methods=['POST'])
def sdr_hold():
    data = request.get_json() or {}
    payload = json.dumps(data).encode()
    try:
        req = urllib.request.Request(
            _sdr_api_url('/api/hold'), method='POST', data=payload,
            headers={'Content-Type': 'application/json'})
        r = urllib.request.urlopen(req, timeout=3)
        return jsonify({'ok': True}), r.status
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 502

@app.route('/api/sdr/channel', methods=['PUT'])
def sdr_channel_put():
    data = request.get_json() or {}
    payload = json.dumps(data).encode()
    try:
        req = urllib.request.Request(
            _sdr_api_url('/api/channel'), method='PUT', data=payload,
            headers={'Content-Type': 'application/json'})
        r = urllib.request.urlopen(req, timeout=3)
        return jsonify({'ok': True}), r.status
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 502

@app.route('/api/sdr/channel/<freq>', methods=['DELETE'])
def sdr_channel_delete(freq):
    try:
        req = urllib.request.Request(
            _sdr_api_url(f'/api/channel/{freq}'), method='DELETE', data=b'')
        r = urllib.request.urlopen(req, timeout=3)
        return jsonify({'ok': True}), r.status
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 502

@sock.route('/ws/sdr-audio')
def sdr_audio_proxy(ws):
    """Proxy the scanner's raw PCM audio WebSocket to browser clients."""
    try:
        import websocket as _wsc
    except ImportError:
        return
    sdr_ws_audio = _sdr_ws_url().replace('/ws', '/ws/audio')
    try:
        client = _wsc.create_connection(sdr_ws_audio, timeout=5)
    except Exception:
        return
    try:
        while True:
            data = client.recv()
            if data is None:
                break
            ws.send(data)
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=LISTEN_PORT, debug=False, threaded=True)

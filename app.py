from flask import Flask, jsonify, request, render_template_string, Response
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

app = Flask(__name__)

ABINFO_ACTIVE = '/tmp/ABInfo_31001.json'
TGLIST_BM        = '/tmp/TGList_BM.txt'
TGLIST_TGIF      = '/tmp/TGList_TGIF.txt'
TGIF_NODE_LIST   = '/tmp/TGIF_node_list.txt'
DMRIDS_FILE   = '/var/lib/mmdvm/DMRIds.dat'
USRP_HOST     = '127.0.0.1'
USRP_PORT     = 31001
USRP_LISTEN   = 31002

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


def load_tg_names():
    global tg_cache_bm, tg_cache_tgif

    # BrandMeister: semicolon delimited
    # Format: TG;option;name;description
    tg_cache_bm = {}
    try:
        with open(TGLIST_BM) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split(';')
                if len(parts) >= 3:
                    tg_cache_bm[parts[0].strip()] = parts[2].strip()
    except Exception as e:
        print(f"Warning: could not load BM TG names: {e}")

    # TGIF: comma delimited
    # Format: TG Number,TG Name
    tg_cache_tgif = {}
    try:
        with open(TGLIST_TGIF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('TG'):
                    continue
                parts = line.split(',', 1)
                if len(parts) >= 2:
                    tg_cache_tgif[parts[0].strip()] = parts[1].strip()
    except Exception as e:
        print(f"Warning: could not load TGIF TG names: {e}")

    # TGIF node list: pipe delimited, fills in any gaps
    # Format: TG Number|||TG Name
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
                    # Only add if not already in TGIF list
                    if tg_id not in tg_cache_tgif and name:
                        tg_cache_tgif[tg_id] = name
    except Exception as e:
        print(f"Warning: could not load TGIF node list: {e}")

    print(f"Loaded {len(tg_cache_bm)} BM TGs, {len(tg_cache_tgif)} TGIF TGs")

def get_active_mode():
    try:
        with open(ABINFO_ACTIVE) as f:
            abinfo = json.load(f)
        ambe_mode = abinfo.get('tlv', {}).get('ambe_mode', '')
        return "BrandMeister" if ambe_mode == 'STFU' else "TGIF"
    except:
        return "TGIF"

def lookup_tg(tg_id):
    if not tg_cache_bm and not tg_cache_tgif:
        load_tg_names()
    mode   = get_active_mode()
    tg_str = str(tg_id).strip()
    tg_int = str(int(tg_str)) if tg_str.isdigit() else tg_str

    if mode == "BrandMeister":
        return tg_cache_bm.get(tg_int, tg_cache_bm.get(tg_str, ''))
    else:
        return tg_cache_tgif.get(tg_int, tg_cache_tgif.get(tg_str, ''))
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
        log_path = '/var/log/dvswitch/STFU.log'
        pattern  = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*src\s*=\s*(\d+).*dst\s*=\s*(\d+)'
        use_callsign = False
    else:
        log_path = f'/var/log/mmdvm/MMDVM_Bridge-{today}.log'
        pattern  = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*voice header from (\S+) to TG (\d+)'
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
                    return {
                        "time":     m.group(1),
                        "callsign": m.group(2),
                        "dmr_id":   "",
                        "tg":       tg,
                        "tg_name":  lookup_tg(tg),
                    }
                else:
                    dmr_id = m.group(2)
                    tg     = m.group(3)
                    return {
                        "time":     m.group(1),
                        "callsign": lookup_dmrid(dmr_id),
                        "dmr_id":   dmr_id,
                        "tg":       tg,
                        "tg_name":  lookup_tg(tg),
                    }
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
    seq   = struct.unpack_from('>I', data, 4)[0]
    tg    = struct.unpack_from('>I', data, 8)[0]
    ptt   = struct.unpack_from('>I', data, 12)[0]
    ptype = struct.unpack_from('>I', data, 16)[0]
    mpxid = struct.unpack_from('>I', data, 20)[0]
    return {
        "seq":     seq,
        "tg":      tg,
        "ptt":     ptt,
        "type":    ptype,
        "mpxid":   mpxid,
        "payload": data[32:]
    }

def send_registration():
    sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    frame = struct.pack('>4sIIIII', USRP_MAGIC, 0, 0, 0, 0, 0) + bytes(4) + bytes(320)
    sock.sendto(frame, (USRP_HOST, USRP_PORT))
    sock.close()

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

        # PTT ON — only trigger on state change
        if frame['ptt'] == 1 and last_ptt != 1:
            last_ptt = 1
            time.sleep(0.2)
            tx_info  = get_current_tx_from_log()
            dmr_id   = tx_info.get('dmr_id', '')
            callsign = tx_info.get('callsign', '') or (lookup_dmrid(dmr_id) if dmr_id else 'UNKNOWN')
            tg       = tx_info.get('tg', '')
            tg_name  = tx_info.get('tg_name', '') or lookup_tg(tg)

            active_tx.update({
                "active":   True,
                "callsign": callsign,
                "dmr_id":   dmr_id,
                "tg":       tg,
                "tg_name":  tg_name,
                "started":  datetime.now().strftime("%H:%M:%S"),
            })
            push_event({"event": "tx_start", **active_tx})

        # PTT OFF — only trigger on state change
        elif frame['ptt'] == 0 and last_ptt != 0:
            last_ptt = 0
            def clear_tx():
                time.sleep(3)
                active_tx.update({
                    "active":   False,
                    "callsign": "",
                    "dmr_id":   "",
                    "tg":       "",
                    "tg_name":  "",
                    "started":  "",
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

def get_log_path(log_key):
    pattern = LOG_FILES.get(log_key, '')
    today   = datetime.now().strftime('%Y-%m-%d')
    return pattern.replace('{date}', today)

# -------------------------
# STATUS
# -------------------------
def get_status():
    tg, mode, call, tg_name = "N/A", "UNKNOWN", "", ""
    try:
        with open(ABINFO_ACTIVE) as f:
            abinfo = json.load(f)
        ambe_mode = abinfo.get('tlv', {}).get('ambe_mode', '')
        mode    = "BrandMeister" if ambe_mode == 'STFU' else "TGIF"
        tg      = abinfo.get('digital', {}).get('tg', 'N/A')
        call    = abinfo.get('digital', {}).get('call', '')
        tg_name = lookup_tg(tg)
    except Exception as e:
        mode = f"ERROR: {e}"

    return {
        "mode":       mode,
        "tg":         tg,
        "tg_name":    tg_name,
        "call":       call,
        "svc_stfu":   svc("stfu.service"),
        "svc_mmdvm":  svc("mmdvm_bridge.service"),
        "svc_analog": svc("analog_bridge.service"),
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
                r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*voice header from (\S+) to TG (\d+)',
                line
            )
            if m:
                tg = m.group(3)
                results.append({
                    "time":     m.group(1),
                    "callsign": m.group(2),
                    "dmr_id":   "",
                    "tg":       tg,
                    "tg_name":  tg_cache_tgif.get(tg.lstrip('0') or '0',
                                tg_cache_tgif.get(tg,
                                tg_cache_bm.get(tg, ''))),
                    "source":   "TGIF"
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
                r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*src=(\d+).*dst=(\d+)',
                line
            )
            if m:
                dmr_id = m.group(2)
                tg     = m.group(3)
                results.append({
                    "time":     m.group(1),
                    "callsign": lookup_dmrid(dmr_id),
                    "dmr_id":   dmr_id,
                    "tg":       tg,
                    "tg_name":  tg_cache_tgif.get(tg.lstrip('0') or '0',
                                tg_cache_tgif.get(tg,
                                tg_cache_bm.get(tg, ''))),
                    "source":   "TGIF"
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
                r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*src\s*=\s*(\d+).*dst\s*=\s*(\d+)',
                line
            )
            if m:
                dmr_id = m.group(2)
                tg     = m.group(3)
                results.append({
                    "time":     m.group(1),
                    "callsign": lookup_dmrid(dmr_id),
                    "dmr_id":   dmr_id,
                    "tg":       tg,
                    "tg_name":  tg_cache_bm.get(tg, ''),
                    "source":   "BM"
                })
    except Exception as e:
        print(f"STFU log error: {e}")

    results.sort(key=lambda x: x['time'], reverse=True)
    return results[:20]

# Load lookup tables at startup
load_tg_names()
load_dmr_ids()

# Start USRP listener thread
usrp_thread = threading.Thread(target=usrp_listener, daemon=True)
usrp_thread.start()

HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Radio Dispatcher Console</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script type="text/javascript" src="/static/pcm-player.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #121212; color: #fff; font-family: monospace; padding: 20px; }
        h1 { font-size: 18px; margin-bottom: 20px; letter-spacing: 2px; color: #fff; }
        .grid { display: grid; grid-template-columns: 1fr 1fr 2fr; gap: 20px; }

        .panel { background: #1e1e1e; border-radius: 8px; padding: 16px; }
        .panel h2 { font-size: 12px; color: #888; letter-spacing: 1px; margin-bottom: 12px; }

        .stat-label { font-size: 11px; color: #666; margin-top: 10px; }
        .stat-value { font-size: 14px; margin-top: 2px; }

        #modeValue { color: cyan; }
        #callValue { color: orange; }
        #tgValue   { color: lightgreen; }
        #tgName    { color: #6c6; font-size: 11px; margin-top: 2px; }
        #timeValue { color: #888; font-size: 11px; }

        .status-dot {
            display: inline-block; width: 8px; height: 8px;
            border-radius: 50%; margin-right: 6px; background: gray;
        }
        .dot-active   { background: lime; }
        .dot-inactive { background: #555; }

        .svc-value   { font-size: 13px; }
        .svc-running { color: lime; }
        .svc-stopped { color: #888; }

        button {
            display: block; width: 100%; padding: 10px;
            margin-bottom: 10px; border: none; border-radius: 6px;
            background: #333; color: #fff; font-family: monospace;
            font-size: 14px; cursor: pointer; letter-spacing: 1px;
        }
        button:hover        { background: #444; }
        button.tgif         { background: #1a3a1a; color: lime; }
        button.tgif:hover   { background: #234d23; }
        button.bm           { background: #1a1a3a; color: cyan; }
        button.bm:hover     { background: #23234d; }
        button.danger       { background: #3a1a1a; color: tomato; }
        button.danger:hover { background: #4d2323; }
        button.tune         { background: #2a2a1a; color: gold; }
        button.tune:hover   { background: #3d3d23; }
        button.monitor        { background: #1a2a3a; color: #7af; }
        button.monitor:hover  { background: #1e324a; }
        button.monitor.active { background: #004400; color: lime; }
        button:disabled     { opacity: 0.4; cursor: not-allowed; }

        input[type=text] {
            width: 100%; padding: 9px; margin-bottom: 10px;
            background: #2a2a2a; border: 1px solid #444;
            color: #fff; font-family: monospace; font-size: 14px;
            border-radius: 6px;
        }

        #log {
            background: #000; border-radius: 6px; padding: 12px;
            height: 400px; overflow-y: auto;
            font-size: 12px; line-height: 1.6;
        }
        .log-ok    { color: lime; }
        .log-error { color: tomato; }
        .log-warn  { color: gold; }
        .log-info  { color: #aaa; }

        .mode-badge {
            display: inline-block; padding: 3px 10px;
            border-radius: 4px; font-weight: bold; font-size: 13px;
        }
        .badge-tgif    { background: #1a3a1a; color: lime; }
        .badge-bm      { background: #1a1a3a; color: cyan; }
        .badge-unknown { background: #333;    color: #aaa; }

        hr.divider { border-color: #333; margin: 14px 0; }

        /* ---- ACTIVE TX ---- */
        .tx-panel {
            margin-top: 20px; background: #1e1e1e;
            border-radius: 8px; padding: 16px;
            transition: background 0.3s;
            border: 1px solid transparent;
        }
        .tx-panel.transmitting {
            background: #1a2a1a;
            border-color: #2a4a2a;
        }
        .tx-panel h2 { font-size: 12px; color: #888; letter-spacing: 1px; margin-bottom: 12px; }
        .tx-indicator { display: flex; align-items: center; gap: 16px; }
        .tx-dot {
            width: 16px; height: 16px; border-radius: 50%;
            background: #333; flex-shrink: 0; transition: background 0.2s;
        }
        .tx-dot.active {
            background: lime;
            box-shadow: 0 0 8px lime;
            animation: pulse 1s infinite;
        }
        @keyframes pulse {
            0%   { box-shadow: 0 0 4px lime; }
            50%  { box-shadow: 0 0 14px lime; }
            100% { box-shadow: 0 0 4px lime; }
        }
        .tx-info      { flex: 1; }
        .tx-callsign  { font-size: 28px; color: lime; font-weight: bold; letter-spacing: 2px; }
        .tx-callsign.idle { color: #333; font-size: 16px; }
        .tx-details   { font-size: 12px; color: #666; margin-top: 4px; }
        .tx-details.active { color: #aaa; }
        .tx-time      { font-size: 11px; color: #555; margin-top: 4px; }

        /* ---- COLLAPSIBLE PANELS ---- */
        .log-viewer {
            margin-top: 20px; background: #1e1e1e;
            border-radius: 8px; overflow: hidden;
        }
        .log-viewer-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 12px 16px; cursor: pointer;
            user-select: none; background: #252525;
        }
        .log-viewer-header:hover { background: #2a2a2a; }
        .log-viewer-header h2 { font-size: 12px; color: #888; letter-spacing: 1px; margin: 0; }
        .collapse-arrow { color: #666; font-size: 14px; transition: transform 0.2s; }
        .collapse-arrow.open { transform: rotate(180deg); }
        .log-viewer-body { display: none; padding: 16px; }
        .log-viewer-body.open { display: block; }

        /* ---- LAST HEARD TABLE ---- */
        #lastHeardTable { width: 100%; border-collapse: collapse; font-size: 12px; }
        #lastHeardTable th {
            text-align: left; color: #555; padding: 4px 10px;
            border-bottom: 1px solid #2a2a2a;
            font-weight: normal; letter-spacing: 1px; font-size: 11px;
        }
        #lastHeardTable td { padding: 5px 10px; border-bottom: 1px solid #1a1a1a; }
        #lastHeardTable tr:hover td { background: #252525; }
        .lh-time        { color: #555; }
        .lh-callsign    { color: orange; font-weight: bold; }
        .lh-dmrid       { color: #555; font-size: 10px; }
        .lh-tg          { color: lightgreen; }
        .lh-tgname      { color: #666; }
        .lh-source-tgif { color: lime; font-size: 11px; }
        .lh-source-bm   { color: cyan; font-size: 11px; }

        /* ---- LOG VIEWER INTERNALS ---- */
        .log-tabs { display: flex; gap: 8px; margin-bottom: 12px; }
        .log-tab {
            padding: 6px 16px; border-radius: 4px; background: #2a2a2a; color: #888;
            cursor: pointer; font-size: 12px; border: 1px solid #333; font-family: monospace;
        }
        .log-tab:hover  { background: #333; color: #fff; }
        .log-tab.active { background: #333; color: #fff; border-color: #555; }
        .log-tab.tab-mmdvm.active  { border-color: #7af; color: #7af; }
        .log-tab.tab-analog.active { border-color: lime; color: lime; }
        .log-tab.tab-stfu.active   { border-color: cyan; color: cyan; }

        .log-controls { display: flex; gap: 8px; margin-bottom: 8px; align-items: center; }
        .log-controls label { font-size: 11px; color: #666; }
        .log-controls input[type=number] {
            width: 60px; padding: 4px 6px; margin: 0;
            background: #2a2a2a; border: 1px solid #444;
            color: #fff; font-family: monospace; font-size: 12px; border-radius: 4px;
        }
        .log-controls button {
            display: inline-block; width: auto; padding: 4px 12px; margin: 0; font-size: 12px;
        }
        .btn-refresh       { background: #2a2a2a; color: #aaa; }
        .btn-refresh:hover { background: #333; }
        .btn-autoscroll    { background: #2a2a2a; color: #aaa; }
        .btn-autoscroll.on { background: #1a3a1a; color: lime; }

        #logFileContent {
            background: #000; border-radius: 6px; padding: 12px;
            height: 300px; overflow-y: auto;
            font-size: 11px; line-height: 1.5; color: #aaa;
            white-space: pre-wrap; word-break: break-all;
        }
        .log-line-error { color: tomato; }
        .log-line-warn  { color: gold; }
        .log-line-info  { color: #aaa; }
        .log-line-debug { color: #555; }
        .log-file-label { font-size: 10px; color: #444; margin-bottom: 6px; }

        @media (max-width: 768px) {
            .grid { grid-template-columns: 1fr; }
            #log  { height: 200px; }
            #logFileContent { height: 200px; }
        }
    </style>
</head>
<body>
    <h1>⚡ RADIO DISPATCHER CONSOLE</h1>
    <div class="grid">

        <!-- STATUS PANEL -->
        <div class="panel">
            <h2>SYSTEM STATUS</h2>
            <div class="stat-label">Network</div>
            <div class="stat-value">
                <span class="mode-badge badge-unknown" id="modeValue">--</span>
            </div>
            <div class="stat-label">Callsign</div>
            <div class="stat-value" id="callValue">--</div>
            <div class="stat-label">Talkgroup</div>
            <div class="stat-value" id="tgValue">--</div>
            <div id="tgName"></div>
            <hr class="divider">
            <h2>SERVICES</h2>
            <div class="stat-label">STFU (BrandMeister)</div>
            <div class="stat-value">
                <span class="status-dot" id="dot_stfu"></span>
                <span class="svc-value" id="svc_stfu">--</span>
            </div>
            <div class="stat-label">MMDVM Bridge</div>
            <div class="stat-value">
                <span class="status-dot" id="dot_mmdvm"></span>
                <span class="svc-value" id="svc_mmdvm">--</span>
            </div>
            <div class="stat-label">Analog Bridge</div>
            <div class="stat-value">
                <span class="status-dot" id="dot_analog"></span>
                <span class="svc-value" id="svc_analog">--</span>
            </div>
            <hr class="divider">
            <div class="stat-value" id="timeValue">--</div>
        </div>

        <!-- CONTROLS PANEL -->
        <div class="panel">
            <h2>CONTROLS</h2>
            <button class="tgif"   id="btnTGIF"    onclick="action('/api/tgif',    'Switching to TGIF...')">▶ TGIF</button>
            <button class="bm"     id="btnBM"      onclick="action('/api/bm',      'Switching to BrandMeister...')">▶ BrandMeister</button>
            <button class="danger" id="btnRestart" onclick="action('/api/restart', 'Restarting STFU service...')">↺ Restart STFU</button>
            <hr class="divider">
            <input type="text" id="tgInput" placeholder="Enter Talkgroup...">
            <button class="tune" onclick="tuneTG()">⟶ TUNE</button>
            <hr class="divider">
            <button class="monitor" id="btnMonitor" onclick="toggleMonitor(this)">🔈 RX Monitor: OFF</button>
        </div>

        <!-- DISPATCH LOG PANEL -->
        <div class="panel">
            <h2>DISPATCH LOG</h2>
            <div id="log"></div>
        </div>

    </div>

    <!-- ACTIVE TX -->
    <div class="tx-panel" id="txPanel">
        <h2>📡 ACTIVE TRANSMISSION</h2>
        <div class="tx-indicator">
            <div class="tx-dot" id="txDot"></div>
            <div class="tx-info">
                <div class="tx-callsign idle" id="txCallsign">STANDBY</div>
                <div class="tx-details"       id="txDetails">No active transmission</div>
                <div class="tx-time"          id="txTime"></div>
            </div>
        </div>
    </div>

    <!-- LAST HEARD -->
    <div class="log-viewer">
        <div class="log-viewer-header" onclick="toggleLastHeard()">
            <h2>📻 LAST HEARD</h2>
            <span class="collapse-arrow" id="lastHeardArrow">▼</span>
        </div>
        <div class="log-viewer-body" id="lastHeardBody_wrapper">
            <table id="lastHeardTable">
                <thead>
                    <tr>
                        <th>TIME</th>
                        <th>CALLSIGN</th>
                        <th>DMR ID</th>
                        <th>TG</th>
                        <th>TG NAME</th>
                        <th>SOURCE</th>
                    </tr>
                </thead>
                <tbody id="lastHeardBody">
                    <tr><td colspan="6" style="color:#555; padding:8px;">Open to load...</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- LOG VIEWER -->
    <div class="log-viewer">
        <div class="log-viewer-header" onclick="toggleLogViewer()">
            <h2>📋 LOG VIEWER</h2>
            <span class="collapse-arrow" id="collapseArrow">▼</span>
        </div>
        <div class="log-viewer-body" id="logViewerBody">
            <div class="log-tabs">
                <div class="log-tab tab-mmdvm active" onclick="selectTab('mmdvm', this)">MMDVM Bridge</div>
                <div class="log-tab tab-analog"       onclick="selectTab('analog', this)">Analog Bridge</div>
                <div class="log-tab tab-stfu"         onclick="selectTab('stfu',   this)">STFU</div>
            </div>
            <div class="log-controls">
                <label>Lines:</label>
                <input type="number" id="logLines" value="50" min="10" max="500" step="10">
                <button class="btn-refresh" onclick="fetchLog()">↺ Refresh</button>
                <button class="btn-autoscroll on" id="btnAutoScroll" onclick="toggleAutoScroll()">⬇ Auto-scroll: ON</button>
            </div>
            <div class="log-file-label" id="logFileLabel"></div>
            <div id="logFileContent">Select a log tab to load...</div>
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
                    document.getElementById('txDot').classList.add('active');
                    document.getElementById('txPanel').classList.add('transmitting');

                    const cs = document.getElementById('txCallsign');
                    cs.textContent = data.callsign || data.dmr_id || 'UNKNOWN';
                    cs.classList.remove('idle');

                    const det = document.getElementById('txDetails');
                    det.textContent = `DMR ID: ${data.dmr_id}  TG: ${data.tg}${data.tg_name ? ' (' + data.tg_name + ')' : ''}`;
                    det.classList.add('active');

                    document.getElementById('txTime').textContent = `Started: ${data.started}`;
                    log(`TX: ${data.callsign || data.dmr_id} on TG ${data.tg}`, 'ok');
                    if (lastHeardOpen) pollLastHeard();

                } else if (data.event === 'tx_end') {
                    document.getElementById('txDot').classList.remove('active');
                    document.getElementById('txPanel').classList.remove('transmitting');

                    const cs = document.getElementById('txCallsign');
                    cs.textContent = 'STANDBY';
                    cs.classList.add('idle');

                    document.getElementById('txDetails').textContent = 'No active transmission';
                    document.getElementById('txDetails').classList.remove('active');
                    document.getElementById('txTime').textContent = '';

                    if (lastHeardOpen) pollLastHeard();
                }
            };

            es.onerror = function() {
                setTimeout(connectSSE, 5000);
            };
        }

        // -------------------------
        // RX MONITOR
        // -------------------------
        var dvsp = null;

        function toggleMonitor(btn) {
            if (dvsp && dvsp.isPlaying()) {
                dvsp.stop();
                btn.textContent = '🔈 RX Monitor: OFF';
                btn.classList.remove('active');
                log('RX Monitor stopped', 'warn');
            } else {
                if (!dvsp) { dvsp = new DVSwitchPlayer(8080, btn); }
                dvsp.play();
                btn.textContent = '🔊 RX Monitor: ON';
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
                    body.innerHTML = '<tr><td colspan="6" style="color:#555; padding:8px;">No activity yet today</td></tr>';
                    return;
                }
                body.innerHTML = rows.map((r) => `
                    <tr>
                        <td class="lh-time">${r.time.split(' ')[1]}</td>
                        <td class="lh-callsign">${r.callsign}</td>
                        <td class="lh-dmrid">${r.dmr_id || ''}</td>
                        <td class="lh-tg">${r.tg}</td>
                        <td class="lh-tgname">${r.tg_name || ''}</td>
                        <td class="${r.source === 'BM' ? 'lh-source-bm' : 'lh-source-tgif'}">${r.source}</td>
                    </tr>
                `).join('');
            } catch(e) {
                log('Last heard poll failed: ' + e, 'error');
            }
        }

        // -------------------------
        // LOG VIEWER
        // -------------------------
        var currentLog    = 'mmdvm';
        var autoScroll    = true;
        var logViewerOpen = false;
        var logPollTimer  = null;

        function toggleLogViewer() {
            logViewerOpen = !logViewerOpen;
            document.getElementById('logViewerBody').classList.toggle('open', logViewerOpen);
            document.getElementById('collapseArrow').classList.toggle('open', logViewerOpen);
            if (logViewerOpen) {
                fetchLog();
                logPollTimer = setInterval(fetchLog, 5000);
            } else {
                clearInterval(logPollTimer);
            }
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
            btn.textContent = `⬇ Auto-scroll: ${autoScroll ? 'ON' : 'OFF'}`;
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
                document.getElementById('logFileContent').textContent = 'Error fetching log: ' + e;
            }
        }

        // -------------------------
        // DISPATCH LOG
        // -------------------------
        function timestamp() {
            return new Date().toLocaleTimeString('en-US', {hour12: false});
        }

        function log(msg, type='info') {
            const div  = document.getElementById('log');
            const line = document.createElement('div');
            line.className   = `log-${type}`;
            line.textContent = `${timestamp()} [${type.toUpperCase()}] ${msg}`;
            div.appendChild(line);
            div.scrollTop = div.scrollHeight;
        }

        // -------------------------
        // CONTROLS
        // -------------------------
        function setButtons(disabled) {
            ['btnTGIF', 'btnBM', 'btnRestart'].forEach(id => {
                document.getElementById(id).disabled = disabled;
            });
        }

        async function action(endpoint, msg) {
            log(msg);
            setButtons(true);
            try {
                const res  = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: '{}'
                });
                const data = await res.json();
                log(data.message, data.ok ? 'ok' : 'error');
            } catch(e) {
                log('Request failed: ' + e, 'error');
            } finally {
                setButtons(false);
                pollStatus();
            }
        }

        function tuneTG() {
            const tg = document.getElementById('tgInput').value.trim();
            if (!tg) { log('No talkgroup entered', 'error'); return; }
            log(`Tuning to TG ${tg}...`);
            fetch('/api/tune', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tg})
            })
            .then(r => r.json())
            .then(d => { log(d.message, d.ok ? 'ok' : 'error'); pollStatus(); })
            .catch(e => log('Tune failed: ' + e, 'error'));
        }

        // -------------------------
        // STATUS POLLING
        // -------------------------
        async function pollStatus() {
            try {
                const res = await fetch('/api/status');
                const d   = await res.json();

                const modeEl = document.getElementById('modeValue');
                modeEl.textContent = d.mode;
                modeEl.className   = 'mode-badge ' + (
                    d.mode === 'TGIF'         ? 'badge-tgif' :
                    d.mode === 'BrandMeister' ? 'badge-bm'   : 'badge-unknown'
                );

                document.getElementById('callValue').textContent = d.call    || '--';
                document.getElementById('tgValue').textContent   = d.tg      || '--';
                document.getElementById('tgName').textContent    = d.tg_name || '';
                document.getElementById('timeValue').textContent = 'Updated: ' + timestamp();

                ['stfu', 'mmdvm', 'analog'].forEach(svc => {
                    const val     = d[`svc_${svc}`];
                    const running = val === 'RUNNING';
                    const svcEl   = document.getElementById(`svc_${svc}`);
                    const dotEl   = document.getElementById(`dot_${svc}`);
                    svcEl.textContent = val;
                    svcEl.className   = `svc-value ${running ? 'svc-running' : 'svc-stopped'}`;
                    dotEl.className   = `status-dot ${running ? 'dot-active' : 'dot-inactive'}`;
                });
            } catch(e) {
                log('Poll failed: ' + e, 'error');
            }
        }

        // -------------------------
        // STARTUP
        // -------------------------
        connectSSE();
        setInterval(pollStatus, 5000);
        pollStatus();
        log('Radio Dispatcher Console started', 'ok');
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML)

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
        result = subprocess.run(
            ['tail', '-n', str(lines), path],
            capture_output=True, timeout=5
        )
        text = result.stdout.decode('utf-8', errors='replace')
        return jsonify({"path": path, "lines": text.splitlines()})
    except Exception as e:
        return jsonify({"path": path, "lines": [f"Error reading log: {e}"]})

@app.route('/api/tgif', methods=['POST'])
def tgif():
    run("/opt/MMDVM_Bridge/connectTGIF.sh")
    return jsonify({"ok": True, "message": "Switched to TGIF"})

@app.route('/api/bm', methods=['POST'])
def bm():
    run("/opt/MMDVM_Bridge/connectBM.sh")
    return jsonify({"ok": True, "message": "Switched to BrandMeister"})

@app.route('/api/restart', methods=['POST'])
def restart():
    run("sudo systemctl restart stfu.service")
    return jsonify({"ok": True, "message": "STFU service restarted"})

@app.route('/api/tune', methods=['POST'])
def tune():
    data = request.get_json()
    tg   = data.get('tg', '').strip()
    if not tg:
        return jsonify({"ok": False, "message": "No talkgroup provided"})
    run(f"/opt/MMDVM_Bridge/dvswitch.sh tune {tg}")
    return jsonify({"ok": True, "message": f"Tuned to {tg}"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9090, debug=False, threaded=True)

#!/usr/bin/env python3
"""
Dispatcher watchdog — runs every 2 minutes via cron.
Restarts dead services and recovers USRP disconnection.

Restart order: stfu → analog_bridge → mmdvm_bridge
USRP recovery: if all services are RUNNING but usrp_connected is
               false for USRP_GRACE_SECS, restart analog_bridge.
"""

import json
import os
import subprocess
import sys
import time

API_URL         = 'http://127.0.0.1:9090/api/status'
STATE_FILE      = '/tmp/dispatcher_watchdog.json'
LOG_FILE        = '/var/log/dispatcher-watchdog.log'
USRP_GRACE_SECS = 90   # seconds of usrp_connected=false before restarting AB
LOG_TAG         = 'dispatcher-watchdog'

SERVICES = [
    'stfu.service',
    'analog_bridge.service',
    'mmdvm_bridge.service',
]

# Restart order: if multiple are down, restart in this sequence with a delay.
RESTART_ORDER  = ['stfu.service', 'analog_bridge.service', 'mmdvm_bridge.service']
RESTART_DELAY  = 5   # seconds between restarts when multiple services are down


def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} [{LOG_TAG}] {msg}'
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception as e:
        print(f'Log write error: {e}', flush=True)
    try:
        subprocess.run(['logger', '-t', LOG_TAG, msg], timeout=3)
    except Exception:
        pass


def systemctl(action, service):
    result = subprocess.run(
        ['systemctl', action, service],
        capture_output=True, timeout=30
    )
    return result.returncode == 0


def is_running(service):
    result = subprocess.run(
        ['systemctl', 'is-active', '--quiet', service],
        timeout=5
    )
    return result.returncode == 0


def get_api_status():
    try:
        import urllib.request
        with urllib.request.urlopen(API_URL, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f'API unreachable: {e}')
        return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        log(f'Could not save state: {e}')


def main():
    state = load_state()
    restarted_any = False

    # ── Step 1: restart any dead services ─────────────────────────────
    down = [s for s in RESTART_ORDER if not is_running(s)]
    if down:
        log(f'Dead services detected: {", ".join(down)}')
        for svc in RESTART_ORDER:
            if svc in down:
                log(f'Restarting {svc}...')
                ok = systemctl('restart', svc)
                log(f'  {"OK" if ok else "FAILED"}: {svc}')
                if ok:
                    restarted_any = True
                time.sleep(RESTART_DELAY)
        # Reset USRP timer after restarts — give services time to settle.
        state.pop('usrp_disconnected_since', None)
        save_state(state)
        return

    # ── Step 2: check USRP connectivity via API ────────────────────────
    status = get_api_status()
    if status is None:
        # API down but services appear up — nothing safe to do yet.
        return

    usrp_ok = status.get('usrp_connected', False)

    if usrp_ok:
        # All good — clear any pending USRP timer.
        if 'usrp_disconnected_since' in state:
            log('USRP reconnected — clearing watchdog timer.')
            state.pop('usrp_disconnected_since', None)
            save_state(state)
        return

    # USRP is disconnected while all services are running.
    now = time.time()
    disconnected_since = state.get('usrp_disconnected_since', now)
    state['usrp_disconnected_since'] = disconnected_since
    elapsed = now - disconnected_since

    log(f'USRP disconnected for {elapsed:.0f}s (grace={USRP_GRACE_SECS}s)')

    if elapsed >= USRP_GRACE_SECS:
        log('Grace period exceeded — restarting analog_bridge.service...')
        ok = systemctl('restart', 'analog_bridge.service')
        log(f'  {"OK" if ok else "FAILED"}: analog_bridge.service')
        state.pop('usrp_disconnected_since', None)  # reset timer

    save_state(state)


if __name__ == '__main__':
    main()

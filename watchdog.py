#!/usr/bin/env python3
"""
Dispatcher watchdog — runs every 2 minutes via cron.
Restarts dead services within the currently active network stack.

Network stacks (mutually exclusive):
  BM   stack: stfu.service + analog_bridge.service
  TGIF stack: analog_bridge.service + mmdvm_bridge.service

Active stack is determined by which network-specific service is running.
stfu.service is BM-only — it must never be restarted when on TGIF, because
the dispatcher uses stfu.service running as the signal that BM is active.
"""

import json
import subprocess
import time

LOG_FILE      = '/var/log/dispatcher-watchdog.log'
LOG_TAG       = 'dispatcher-watchdog'
RESTART_DELAY = 5   # seconds between restarts when multiple services are down

# Services that identify each network stack (mutually exclusive)
BM_ANCHOR   = 'stfu.service'
TGIF_ANCHOR = 'mmdvm_bridge.service'
SHARED_SVC  = 'analog_bridge.service'

BM_STACK   = [BM_ANCHOR,   SHARED_SVC]
TGIF_STACK = [TGIF_ANCHOR, SHARED_SVC]

# TGIF re-tune — run after mmdvm_bridge restarts, or when it's running but
# connected to the wrong server (e.g. after a midnight automatic restart).
DVSWITCH       = '/opt/MMDVM_Bridge/dvswitch.sh'
TGIF_SERVER    = '31BEC09E9EF14A69@tgif.network:62031'
TGIF_LOCAL_ID  = '3223583'
# Analog_Bridge info file; tlv.rx_port == TGIF_TLV_PORT means AB is wired to
# MMDVM_Bridge (TGIF), not to STFU (BM port 36100).
ABINFO_PATH    = '/tmp/ABInfo_31001.json'
TGIF_TLV_PORT  = '31100'


def tgif_retune():
    """Re-connect MMDVM_Bridge to TGIF after an unexpected restart.

    Runs the same dvswitch.sh tune sequence as connectTGIF.sh, minus the
    service-stop steps (services are already in their correct run state when
    the watchdog calls this).
    """
    steps = [
        ([DVSWITCH, 'mode', 'DMR'],         3),
        ([DVSWITCH, 'tune', TGIF_SERVER],   5),
        ([DVSWITCH, 'tune', TGIF_LOCAL_ID], 0),
    ]
    for cmd, delay in steps:
        log(f'  retune: {" ".join(cmd[1:])}')
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=15)
            log(f'    {"OK" if r.returncode == 0 else "FAILED"}')
        except Exception as e:
            log(f'    ERROR: {e}')
        if delay:
            time.sleep(delay)


def tgif_actually_connected():
    """Return True if Analog_Bridge is wired to MMDVM_Bridge (TGIF TLV port).

    When mmdvm_bridge restarts and reconnects to BrandMeister by default,
    ABInfo still shows the old TLV rx_port (36100 = BM/STFU, 31100 = TGIF).
    """
    try:
        with open(ABINFO_PATH) as f:
            info = json.load(f)
        return info.get('tlv', {}).get('rx_port') == TGIF_TLV_PORT
    except Exception:
        return False


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


def active_stack():
    """Return the service list for the currently active network stack.

    Priority: if the BM anchor is up → BM stack; if the TGIF anchor is up →
    TGIF stack.  If both anchors are down, infer from the stack whose anchor
    was most recently active (via systemctl show ExecMainStartTimestamp).
    Falls back to TGIF if indeterminate.
    """
    bm_up   = is_running(BM_ANCHOR)
    tgif_up = is_running(TGIF_ANCHOR)

    if bm_up and not tgif_up:
        return 'BM', BM_STACK
    if tgif_up and not bm_up:
        return 'TGIF', TGIF_STACK

    # Both anchors down — check which one exited more recently
    def last_start(svc):
        try:
            r = subprocess.run(
                ['systemctl', 'show', svc, '--property=ExecMainStartTimestamp'],
                capture_output=True, timeout=5
            )
            ts = r.stdout.decode().strip().split('=', 1)[-1]
            return ts if ts else ''
        except Exception:
            return ''

    bm_ts   = last_start(BM_ANCHOR)
    tgif_ts = last_start(TGIF_ANCHOR)

    # Lexicographic comparison of systemd timestamps works for recency
    if bm_ts > tgif_ts:
        return 'BM', BM_STACK
    return 'TGIF', TGIF_STACK


def main():
    network, stack = active_stack()
    down = [s for s in stack if not is_running(s)]

    if not down:
        for s in stack:
            log(f'  OK: {s}')
        # On TGIF, also verify the bridge is actually tuned to TGIF — catches
        # the case where mmdvm_bridge restarted externally and reconnected to
        # BrandMeister by default while the service still shows as running.
        if network == 'TGIF' and not tgif_actually_connected():
            log('TGIF stack running but AB not wired to TGIF — re-tuning...')
            tgif_retune()
        return

    log(f'Network: {network} — dead services: {", ".join(down)}')
    restarted_anchor = False
    for svc in stack:
        if svc in down:
            log(f'Restarting {svc}...')
            ok = systemctl('restart', svc)
            log(f'  {"OK" if ok else "FAILED"}: {svc}')
            if ok and svc == TGIF_ANCHOR:
                restarted_anchor = True
            if RESTART_DELAY and svc != stack[-1]:
                time.sleep(RESTART_DELAY)

    if network == 'TGIF' and restarted_anchor:
        log('Re-tuning MMDVM_Bridge to TGIF after restart...')
        tgif_retune()


if __name__ == '__main__':
    main()

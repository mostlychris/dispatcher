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
        return

    log(f'Network: {network} — dead services: {", ".join(down)}')
    for svc in stack:
        if svc in down:
            log(f'Restarting {svc}...')
            ok = systemctl('restart', svc)
            log(f'  {"OK" if ok else "FAILED"}: {svc}')
            if RESTART_DELAY and svc != stack[-1]:
                time.sleep(RESTART_DELAY)


if __name__ == '__main__':
    main()

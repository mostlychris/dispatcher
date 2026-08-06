#!/usr/bin/env python3
"""
Dispatcher watchdog — runs every 2 minutes via cron.
Restarts dead services and recovers USRP disconnection.

Restart order: stfu → analog_bridge → mmdvm_bridge
USRP recovery: if all services are RUNNING but usrp_connected is
               false for USRP_GRACE_SECS, restart analog_bridge.
"""

import subprocess
import time

LOG_FILE      = '/var/log/dispatcher-watchdog.log'
LOG_TAG       = 'dispatcher-watchdog'
RESTART_ORDER = ['stfu.service', 'analog_bridge.service', 'mmdvm_bridge.service']
RESTART_DELAY = 5   # seconds between restarts when multiple services are down


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



def main():
    down = [s for s in RESTART_ORDER if not is_running(s)]
    if not down:
        return  # all services healthy, nothing to do

    log(f'Dead services detected: {", ".join(down)}')
    for svc in RESTART_ORDER:
        if svc in down:
            log(f'Restarting {svc}...')
            ok = systemctl('restart', svc)
            log(f'  {"OK" if ok else "FAILED"}: {svc}')
            time.sleep(RESTART_DELAY)


if __name__ == '__main__':
    main()

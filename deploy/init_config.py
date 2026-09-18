#!/usr/bin/env python3
"""Create local deployment secrets without printing them to the terminal."""
from pathlib import Path
import os
import secrets


root = Path(__file__).resolve().parents[1]
source = root / '.env'
target = root / 'deploy' / 'backend.env'

if target.exists():
    print(f'{target} ist bereits vorhanden; keine Änderung vorgenommen.')
    raise SystemExit(0)

existing = {}
if source.exists():
    for line in source.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            existing[key] = value

values = {
    'MODE': 'active',
    'API_TOKEN': existing.get('API_TOKEN') or secrets.token_urlsafe(32),
    'CONTROLLER_TOKEN': existing.get('CONTROLLER_TOKEN') or secrets.token_urlsafe(32),
    'CONTROLLER_URL': 'http://127.0.0.1:8091',
    'DB_PATH': '/var/lib/keba/loadmanager.sqlite',
    'STATIONS_CONFIG_PATH': '/etc/keba/stations.json',
    'WEB_ALLOWED_ORIGINS': '',
}
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as handle:
    handle.writelines(f'{key}={value}\n' for key, value in values.items())
print(f'{target} wurde mit Dateirechten 0600 angelegt.')

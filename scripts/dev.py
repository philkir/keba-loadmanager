#!/usr/bin/env python3
"""One local command starts the independently supervised simulation services."""
import argparse
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--backend-only',action='store_true')
parser.add_argument('--mode',choices=['simulation','commissioning'])
args=parser.parse_args()
env_file=ROOT/'.env'
if not env_file.exists():
    fd=os.open(env_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:
        f.write('API_TOKEN='+secrets.token_urlsafe(32)+'\nCONTROLLER_TOKEN='+secrets.token_urlsafe(32)+'\nMODE=simulation\n')
env=os.environ.copy()
for line in env_file.read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        key,value=line.split('=',1);env[key]=value
if args.mode:
    env['MODE']=args.mode
env['PYTHONPATH']=str(ROOT/'backend')
env['DB_PATH']=str(ROOT/'data'/('commissioning.sqlite' if env.get('MODE')=='commissioning' else 'loadmanager.sqlite'))
processes=[]
try:
    for module,port in [('controller','8091'),('api','8000')]:
        processes.append(subprocess.Popen([sys.executable,'-m','uvicorn',f'loadmanager.{module}:app','--host','127.0.0.1','--port',port,'--no-access-log'],cwd=ROOT,env=env))
    if not args.backend_only:
        processes.append(subprocess.Popen(['npm','run','dev'],cwd=ROOT,env=env))
    label='Inbetriebnahme' if env.get('MODE')=='commissioning' else 'Simulation'
    print(f'\n{label}: http://127.0.0.1:5173 · Beenden mit Strg+C\n',flush=True)
    while all(p.poll() is None for p in processes):time.sleep(0.5)
finally:
    for p in processes:
        if p.poll() is None:p.send_signal(signal.SIGINT)
    for p in processes:
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:p.kill()

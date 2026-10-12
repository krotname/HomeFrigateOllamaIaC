#!/usr/bin/env python3
"""Explicit local-node cutover steps; never transfers or prints credentials."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STATE = Path('/run/frigate-black-cutover')
NGINX = Path('/etc/nginx')


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def install(source, target, mode):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(mode)


def write(path, text, mode=0o600):
    path = Path(path)
    temporary = path.with_name(path.name + '.cutover-tmp')
    with temporary.open('w') as handle:
        os.chmod(temporary, mode)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def prepare_state(node, token):
    if not re.fullmatch(r'[0-9a-f-]{36}', token):
        raise ValueError('A fresh UUID cutover token is required')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    write(STATE / 'token', token + '\n')
    install(HERE / f'rollback-{node}.sh', STATE / f'rollback-{node}.sh', 0o700)
    install(HERE / 'arm-rollback.sh', STATE / 'arm-rollback.sh', 0o700)


def proxy_config(text):
    old = 'https://127.0.0.1:18971'
    new = 'https://192.168.1.242:18971'
    if old not in text and new not in text:
        raise ValueError('Expected existing Frigate proxy is absent')
    return text.replace(old, new)


def black(token, sources):
    if not sources or len(sources) > 2:
        raise ValueError('Specify one or two verified White proxy source addresses')
    for source in sources:
        ipaddress.IPv4Address(source)
    Path('/media/frigate').mkdir(parents=True, exist_ok=True)
    install(HERE / 'media-frigate.mount', '/etc/systemd/system/media-frigate.mount', 0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'media-frigate.mount')
    run('mountpoint', '-q', '/media/frigate')
    Path('/opt/frigate').mkdir(mode=0o700, exist_ok=True)
    for name in ['compose.yml', 'compose.cpu.yml', 'caption-compose.yml', 'prepare.py']:
        install(HERE / name, '/opt/frigate/' + name, 0o600)
    install(HERE / 'protect-backend.sh', '/opt/frigate/protect-backend.sh', 0o700)
    write('/opt/frigate/proxy-sources', '\n'.join(sources) + '\n')
    for name in ['media-frigate.mount', 'frigate-black.service', 'krt-camera-watchdog.service']:
        install(HERE / name, '/etc/systemd/system/' + name, 0o644)
    files = ROOT / 'ansible/roles/frigate_vm/files'
    install(files / 'camera-watchdog.sh', '/usr/local/sbin/krt-camera-watchdog.sh', 0o755)
    install(files / 'krt-camera-watchdog.timer', '/etc/systemd/system/krt-camera-watchdog.timer', 0o644)
    Path('/var/lib/frigate-caption').mkdir(mode=0o700, exist_ok=True)
    Path('/var/lib/frigate-caption').chmod(0o700)
    prepare_state('black', token)
    run('systemctl', 'daemon-reload')


def white_prepare(token):
    if run('docker', 'inspect', 'frigate', '--format', '{{.State.Running}}') != 'true':
        raise ValueError('Prepare rollback only from the working White recorder')
    prepare_state('white', token)
    for name in ['home-ai-proxies', 'frigate-white-domain']:
        install(NGINX / 'sites-available' / name, STATE / name, 0o600)
    install(NGINX / 'nginx.conf', STATE / 'nginx.conf', 0o600)


def white_stop():
    run('systemctl', 'is-active', '--quiet', 'frigate-white-rollback.timer')
    token = (STATE / 'token').read_text()
    write('/opt/frigate/moved-to-black', token)
    run('systemctl', 'stop', 'krt-container-watchdog.timer', 'krt-camera-watchdog.timer',
        'krt-container-watchdog.service', 'krt-camera-watchdog.service')
    run('docker', 'compose', '-f', '/opt/frigate/docker-compose.yml', 'stop', '-t', '30', 'frigate')
    if run('docker', 'inspect', 'frigate', '--format', '{{.State.Running}}') != 'false':
        raise RuntimeError('White writer did not stop')


def white_switch(commit):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Specify the verified main commit')
    if run('docker', 'inspect', 'frigate', '--format', '{{.State.Running}}') != 'false':
        raise ValueError('Stop White before switching nginx')
    run('systemctl', 'is-active', '--quiet', 'frigate-white-rollback.timer')
    (STATE / 'token').read_text()
    with urllib.request.urlopen('https://192.168.1.242:18971/api/stats',
                                context=ssl._create_unverified_context(), timeout=10) as response:
        stats = json.load(response)
    if len(stats.get('cameras', {})) != 3 or any(c.get('camera_fps', 0) <= 0 for c in stats['cameras'].values()):
        raise ValueError('All three Black cameras must be running')
    for name in ['home-ai-proxies', 'frigate-white-domain']:
        path = NGINX / 'sites-available' / name
        write(path, proxy_config(path.read_text()), path.stat().st_mode & 0o777)
    install(HERE / 'white-streams.conf', NGINX / 'frigate-black-streams.conf', 0o644)
    path = NGINX / 'nginx.conf'
    text = path.read_text()
    include = 'include /etc/nginx/frigate-black-streams.conf;'
    if include not in text:
        write(path, text.rstrip() + '\n\n' + include + '\n', path.stat().st_mode & 0o777)
    run('nginx', '-t')
    run('systemctl', 'reload', 'nginx')
    write('/opt/frigate/moved-to-black', commit + '\n')


def white_finish():
    for suffix in ['timer', 'service']:
        if subprocess.run(['systemctl', 'is-active', '--quiet', 'frigate-white-rollback.' + suffix]).returncode == 0:
            raise ValueError('Cancel rollback only after independent acceptance')
    run('systemctl', 'disable', '--now', 'krt-container-watchdog.timer', 'krt-camera-watchdog.timer')


def captions():
    with urllib.request.urlopen('https://127.0.0.1/openapi.json',
                                context=ssl._create_unverified_context(), timeout=5) as response:
        if '/v1/frigate-caption' not in json.load(response)['paths']:
            raise ValueError('Update OCR API before starting the caption queue')
    run('docker', 'compose', '-f', '/opt/frigate/caption-compose.yml', 'up', '-d', '--no-build', '--pull', 'never')
    if run('docker', 'inspect', 'frigate-caption', '--format', '{{.State.Running}}') != 'true':
        raise RuntimeError('Caption worker did not start')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['black', 'white-prepare', 'white-stop', 'white-switch', 'white-finish', 'captions'])
    parser.add_argument('--token', default='')
    parser.add_argument('--commit', default='')
    parser.add_argument('--proxy-source', action='append', default=[])
    options = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo')
    if options.action == 'black':
        black(options.token, options.proxy_source)
    elif options.action == 'white-prepare':
        white_prepare(options.token)
    elif options.action == 'white-switch':
        white_switch(options.commit)
    else:
        {'white-stop': white_stop, 'white-finish': white_finish, 'captions': captions}[options.action]()
    print(options.action + ': OK')


if __name__ == '__main__':
    main()

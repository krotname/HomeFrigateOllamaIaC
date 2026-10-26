"""Read-only barrier observation. This process has no control/RF interface."""

import argparse
import json
import math
import os
import signal
import time
import urllib.request
from pathlib import Path


UNKNOWN, CLOSED, OPEN = 0, 1, 2


class EvidenceGate:
    """Require a changing camera clock and consecutive fresh, agreeing observations."""

    def __init__(self, settle=3, frozen_after=2.5):
        self.settle = settle
        self.frozen_after = frozen_after
        self.clock = None
        self.changed_at = None
        self.changed_wall = 0
        self.pending = UNKNOWN
        self.count = 0

    def observe(self, candidate, clock, now, wall, healthy=True):
        if not healthy or clock is None:
            self.reset()
            return UNKNOWN, 'camera_unavailable'
        changed = self.clock is not None and clock != self.clock
        first = self.clock is None
        if self.changed_at is not None and now - self.changed_at > self.frozen_after:
            self.pending, self.count = UNKNOWN, 0
        if first or changed:
            self.clock = clock
            if changed:
                self.changed_at, self.changed_wall = now, wall
        if self.changed_at is None or now - self.changed_at > self.frozen_after:
            self.pending, self.count = UNKNOWN, 0
            return UNKNOWN, 'camera_clock_unproven' if self.changed_at is None else 'frozen_frame'
        if candidate not in (CLOSED, OPEN):
            self.pending, self.count = UNKNOWN, 0
            return UNKNOWN, 'ambiguous_or_moving'
        if self.pending != candidate:
            self.pending, self.count = candidate, 0
        if changed:
            self.count += 1
        if self.count < self.settle:
            return UNKNOWN, 'settling'
        return candidate, 'confirmed'

    def reset(self):
        self.clock = None
        self.changed_at = None
        self.changed_wall = 0
        self.pending, self.count = UNKNOWN, 0


def camera_healthy(stats, camera, wall):
    try:
        updated = float(stats['service']['last_updated'])
        fps = float(stats['cameras'][camera]['camera_fps'])
        return math.isfinite(updated) and -2 <= wall - updated <= 45 and fps >= 0.25
    except (KeyError, TypeError, ValueError):
        return False


def consensus(states):
    return states[0] if states and states[0] in (CLOSED, OPEN) and all(s == states[0] for s in states) else UNKNOWN


class PoseClassifier:
    def __init__(self, config):
        import cv2
        import numpy as np
        self.cv, self.np, self.config = cv2, np, config
        cv2.setNumThreads(1)
        self.templates = {
            kind: {state: [np.array(t, dtype=np.float32) for t in bank]
                   for state, bank in states.items()}
            for kind, states in config['templates'].items()
        }
        self.previous = None
        self.clock_patch = None
        self.clock_counter = 0

    def read(self, content):
        frame = self.cv.imdecode(self.np.frombuffer(content, self.np.uint8), self.cv.IMREAD_GRAYSCALE)
        if frame is None or list(reversed(frame.shape)) != self.config['size']:
            self.previous = None
            return UNKNOWN, None, {'reason': 'invalid_frame'}
        x1, y1, x2, y2 = self.config.get('clock_region', [464, 59, 524, 86])
        clock_patch = self.cv.GaussianBlur(frame[y1:y2, x1:x2], (3, 3), 0)
        if self.clock_patch is None:
            self.clock_patch = clock_patch.copy()
        difference = self.np.abs(clock_patch.astype(self.np.int16) - self.clock_patch.astype(self.np.int16))
        if int((difference > 32).sum()) >= max(20, clock_patch.size * 0.02):
            self.clock_counter += 1
            self.clock_patch = clock_patch.copy()
        clock = self.clock_counter.to_bytes(8, 'big')
        scores = {}
        winners = []
        pose = None
        for kind, region in self.config['regions'].items():
            x1, y1, x2, y2 = region
            crop = frame[y1:y2, x1:x2].astype(self.np.float32)
            if crop.size == 0 or float(crop.std()) < 10:
                self.previous = None
                return UNKNOWN, clock, {'reason': 'poor_visibility'}
            scores[kind] = {state: max(float(self.cv.matchTemplate(crop, template, self.cv.TM_CCOEFF_NORMED)[0, 0]) for template in bank)
                            for state, bank in self.templates[kind].items()}
            ordered = sorted(scores[kind], key=scores[kind].get, reverse=True)
            best, other = ordered[:2]
            minimum = self.config.get('minimum_score', {}).get(kind, 0.90)
            margin = self.config.get('minimum_margin', {}).get(kind, 0.06)
            winners.append({'closed': CLOSED, 'open': OPEN}[best] if scores[kind][best] >= minimum and scores[kind][best] - scores[kind][other] >= margin else UNKNOWN)
            if kind == 'pose':
                pose = crop
        state = consensus(winners)
        moving = self.previous is not None and pose is not None and float(self.np.mean(self.np.abs(pose - self.previous))) > 12
        self.previous = None if pose is None else pose.copy()
        if moving:
            state = UNKNOWN
        return state, clock, {'scores': scores, 'moving': moving}


def fetch(base, path):
    with urllib.request.urlopen(base + path, timeout=3) as response:
        return response.read(8 * 1024 * 1024 + 1), response.headers


def atomic(path, text):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


def publish(directory, state, reason, gate, wall, detail):
    atomic(directory / 'barrier.prom',
           '# HELP barrier_state 0 unknown, 1 closed, 2 open; camera observation only.\n'
           '# TYPE barrier_state gauge\n'
           f'barrier_state{{barrier="entrance"}} {state}\n'
           '# TYPE barrier_observation_timestamp_seconds gauge\n'
           f'barrier_observation_timestamp_seconds{{barrier="entrance"}} {wall:.3f}\n'
           '# TYPE barrier_frame_change_timestamp_seconds gauge\n'
           f'barrier_frame_change_timestamp_seconds{{barrier="entrance"}} {gate.changed_wall:.3f}\n')
    atomic(directory / 'status.json', json.dumps({'state': state, 'reason': reason, 'updated': wall, 'frame_changed': gate.changed_wall, **detail}, ensure_ascii=False))


def run(config, base, directory):
    directory.mkdir(parents=True, exist_ok=True)
    gate = EvidenceGate()
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    classifier = PoseClassifier(config)
    camera = config['camera']
    while not stopping:
        started = time.monotonic()
        state, reason, detail = UNKNOWN, 'request_failed', {}
        try:
            content, headers = fetch(base, f'/{camera}/latest.jpg?store=0&quality=95')
            stats = json.loads(fetch(base, '/stats')[0])
            wall = time.time()
            healthy = headers.get('X-Frigate-Offline', '').lower() != 'true' and camera_healthy(stats, camera, wall)
            candidate, clock, detail = classifier.read(content) if healthy else (UNKNOWN, None, {})
            state, reason = gate.observe(candidate, clock, time.monotonic(), wall, healthy)
        except Exception as exc:
            gate.reset()
            classifier.previous = None
            detail = {'error_type': type(exc).__name__}
        publish(directory, state, reason, gate, time.time(), detail)
        remaining = 1 - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
    gate.reset()
    publish(directory, UNKNOWN, 'stopped', gate, time.time(), {})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--base', default='http://127.0.0.1:15000/api')
    parser.add_argument('--state-dir', type=Path, default=Path('/state'))
    args = parser.parse_args()
    run(json.loads(args.config.read_text()), args.base.rstrip('/'), args.state_dir)


if __name__ == '__main__':
    main()

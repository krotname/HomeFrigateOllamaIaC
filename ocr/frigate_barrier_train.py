"""Train a fixed-camera HOG model from independently labelled archive crops.

Manifest: {origin:[x,y], samples:[{path,label,role,period,group}]}.
Labels are 0 unknown, 1 closed, 2 open. Roles are train/validation/test.
Test samples are never read by the training command.
"""
import argparse
import collections
import json
from pathlib import Path

from ocr.barrier_learning import FEATURE_LENGTH, FEATURE_VERSION, LearnedPose, crop_region, features


def dataset(manifest_path, roles):
    manifest = json.loads(manifest_path.read_text())
    groups = collections.defaultdict(set)
    for row in manifest['samples']:
        groups[row['group']].add(row['role'])
    if any(len(value) > 1 for value in groups.values()):
        raise ValueError('episode_in_multiple_splits')
    if not all(row['label'] in (0, 1, 2) for row in manifest['samples']):
        raise ValueError('invalid_label')
    if any(row['role'] not in ('train', 'validation', 'test') or row['period'] not in ('day', 'night') for row in manifest['samples']):
        raise ValueError('invalid_split')
    return manifest, [r for r in manifest['samples'] if r['role'] in roles]


def vectors(manifest_path, config, rows, origin, augment=False):
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    result = {name: [] for name in config['regions']}
    visible = {name: [] for name in config['regions']}
    labels = []
    for row in rows:
        image = cv2.imread(str(manifest_path.parent / row['path']), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError('missing_sample: ' + row['path'])
        variants = [(image, (0, 0), row['label'])]
        if augment:
            # Misaligned and hidden regions must never become a known position.
            variants += [(image, shift, 0) for shift in [(10, 0), (-10, 0), (0, 10), (0, -10)]]
        for source, shift, label in variants:
            try:
                crops = {name: crop_region(source, region, origin, shift)
                         for name, region in config['regions'].items()}
            except ValueError:
                if shift != (0, 0):
                    continue
                raise
            for name, crop in crops.items():
                vector = features(crop)
                visible[name].append(vector is not None)
                if vector is None:
                    vector = np.zeros(FEATURE_LENGTH, dtype=np.float32)
                result[name].append(vector)
            labels.append(label)
    return ({name: np.asarray(values, dtype=np.float32) for name, values in result.items()},
            np.asarray(labels), {name: np.asarray(values, dtype=bool) for name, values in visible.items()})


def predict(model, data, visible):
    import numpy as np
    predictions = []
    for name, value in model['classifiers'].items():
        scores = data[name] @ np.asarray(value['weights']).T + np.asarray(value['bias'])
        order = scores.argsort(axis=1)
        candidate = order[:, -1]
        margin = scores[np.arange(len(scores)), order[:, -1]] - scores[np.arange(len(scores)), order[:, -2]]
        threshold = np.asarray(value['thresholds'])[candidate]
        predictions.append(np.where(visible[name] & (candidate != 0) & (margin >= threshold), candidate, 0))
    return np.where(np.logical_and.reduce([p == predictions[0] for p in predictions]), predictions[0], 0)


def tune_consensus(model, data, labels, visible):
    """Choose margins for the two-region decision using validation only."""
    import numpy as np
    names = list(model['classifiers'])
    decisions = []
    for name in names:
        value = model['classifiers'][name]
        scores = data[name] @ np.asarray(value['weights']).T + np.asarray(value['bias'])
        order = scores.argsort(axis=1)
        best = order[:, -1]
        margin = scores[np.arange(len(scores)), best] - scores[np.arange(len(scores)), order[:, -2]]
        decisions.append((np.where(visible[name], best, 0), margin))
    for state in (1, 2):
        agreed = (decisions[0][0] == state) & (decisions[1][0] == state)
        wrong = agreed & (labels != state)
        correct = agreed & (labels == state)
        first, second = decisions[0][1], decisions[1][1]
        candidates = [.15] + [float(value) + .05 for value in first[wrong]]
        options = []
        for lower in candidates:
            remaining = second[wrong & (first >= lower)]
            upper = max(.15, float(remaining.max()) + .05) if len(remaining) else .15
            accepted = correct & (first >= lower) & (second >= upper)
            options.append((-int(accepted.sum()), lower + upper, lower, upper))
        _, _, first_threshold, second_threshold = min(options)
        model['classifiers'][names[0]]['thresholds'][state] = first_threshold
        model['classifiers'][names[1]]['thresholds'][state] = second_threshold


def train(args):
    import numpy as np
    from sklearn.svm import LinearSVC
    manifest, rows = dataset(args.manifest, {'train', 'validation'})
    config = json.loads(args.config.read_text())
    training = [r for r in rows if r['role'] == 'train']
    validation = [r for r in rows if r['role'] == 'validation']
    if not training or not validation or any(sum(r['label'] == k for r in training) < 5 for k in (1, 2)):
        raise ValueError('insufficient_independent_labels')
    x, y, _ = vectors(args.manifest, config, training, manifest['origin'], augment=True)
    vx, vy, visible = vectors(args.manifest, config, validation, manifest['origin'])
    candidates = []
    for strength in (.01, .1, 1.):
        model = {'schema': 1, 'feature_version': FEATURE_VERSION, 'version': args.version,
                 'camera': config['camera'], 'size': config['size'], 'regions': config['regions'],
                 'classifiers': {}, 'training': {'C': strength, 'train': len(training), 'validation': len(validation)}}
        for name, values in x.items():
            svm = LinearSVC(C=strength, class_weight='balanced', max_iter=20000, random_state=42, dual='auto')
            svm.fit(values, y)
            if list(svm.classes_) != [0, 1, 2]:
                raise ValueError('missing_training_class')
            model['classifiers'][name] = {'weights': svm.coef_.tolist(), 'bias': svm.intercept_.tolist(), 'thresholds': [0., .15, .15]}
        tune_consensus(model, vx, vy, visible)
        result = predict(model, vx, visible)
        wrong = int(((result != 0) & (result != vy)).sum())
        known = int(((result != 0) & (vy != 0)).sum())
        candidates.append((wrong, -known, strength, model))
    wrong, coverage, _, best_model = min(candidates, key=lambda v: v[:3])
    if wrong:
        raise ValueError('validation_false_known')
    args.output.write_text(json.dumps(best_model, separators=(',', ':')), encoding='utf-8')
    LearnedPose(args.output, config)
    print(json.dumps({'model': str(args.output), 'version': args.version, 'train': len(training),
                      'validation': len(validation), 'validation_correct_known': -coverage,
                      'validation_false_known': wrong, 'C': best_model['training']['C']}))


def evaluate(args):
    """Report a frozen model against the held-out split, without tuning it."""
    import cv2
    import numpy as np
    manifest, rows = dataset(args.manifest, {'test'})
    config = json.loads(args.config.read_text())
    learned = LearnedPose(args.model, config)
    baseline, results = [], []
    for row in rows:
        image = cv2.imread(str(args.manifest.parent / row['path']), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError('missing_sample: ' + row['path'])
        old, new = [], []
        for name, region in config['regions'].items():
            crop = crop_region(image, region, manifest['origin'])
            new.append(learned.classify(name, crop)[0])
            scores = {state: max(float(cv2.matchTemplate(crop.astype(np.float32), np.asarray(t, np.float32), cv2.TM_CCOEFF_NORMED)[0, 0]) for t in bank)
                      for state, bank in config['templates'][name].items()}
            order = sorted(scores, key=scores.get, reverse=True)
            best, other = order[:2]
            minimum = config.get('minimum_score', {}).get(name, .90)
            margin = config.get('minimum_margin', {}).get(name, .06)
            old.append({'closed': 1, 'open': 2}[best] if float(crop.std()) >= 10 and scores[best] >= minimum and scores[best] - scores[other] >= margin else 0)
        baseline.append(old[0] if all(v == old[0] for v in old) else 0)
        results.append(new[0] if all(v == new[0] for v in new) else 0)
    report = {'model_version': learned.version, 'samples': len(rows), 'periods': {}}
    for period in ('day', 'night'):
        ids = [i for i, row in enumerate(rows) if row['period'] == period]
        known = [i for i in ids if rows[i]['label'] != 0]
        old_unknown = sum(baseline[i] == 0 for i in known)
        new_unknown = sum(results[i] == 0 for i in known)
        wrong = sum(results[i] != 0 and results[i] != rows[i]['label'] for i in ids)
        swapped = sum(results[i] != 0 and results[i] != rows[i]['label'] for i in known)
        report['periods'][period] = {'samples': len(ids), 'discernible': len(known),
                                    'baseline_unknown': old_unknown, 'learned_unknown': new_unknown,
                                    'false_known': wrong, 'wrong_open_closed': swapped,
                                    'passed': bool(known) and old_unknown > 0 and new_unknown * 2 <= old_unknown and wrong == 0}
    report['wrong_examples'] = [{'path': row['path'], 'label': row['label'], 'prediction': results[i]}
                                for i, row in enumerate(rows) if results[i] and results[i] != row['label']]
    report['passed'] = len(rows) >= 500 and all(v['passed'] for v in report['periods'].values())
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    if not report['passed']:
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', default='1.0.0')
    parser.add_argument('--model', type=Path, help='Evaluate this frozen model on test samples instead of training.')
    args = parser.parse_args()
    (evaluate if args.model else train)(args)


if __name__ == '__main__':
    main()

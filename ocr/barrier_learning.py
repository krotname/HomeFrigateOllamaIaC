"""Small, data-only HOG/linear classifiers for a fixed barrier camera."""
import json
import re
from pathlib import Path


FEATURE_VERSION = 'hog-gray-1'
MODEL_LIMIT = 2 * 1024 * 1024
FEATURE_LENGTH = 884


def features(crop):
    import cv2
    import numpy as np
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if crop.size == 0 or not np.isfinite(crop).all() or float(crop.std()) < 3:
        return None
    normalized = cv2.createCLAHE(clipLimit=2, tileGridSize=(4, 4)).apply(crop)
    resized = cv2.resize(normalized, (64, 32), interpolation=cv2.INTER_AREA)
    hog = cv2.HOGDescriptor((64, 32), (16, 16), (8, 8), (8, 8), 9)
    gradient = hog.compute(resized).reshape(-1)
    thumbnail = cv2.resize(normalized, (16, 8), interpolation=cv2.INTER_AREA).astype(np.float32)
    thumbnail = (thumbnail - thumbnail.mean()) / max(float(thumbnail.std()), 8)
    return np.concatenate([gradient, thumbnail.reshape(-1) * .25]).astype(np.float32)


class LearnedPose:
    """No pickle, code loading, or additional inference runtime."""
    def __init__(self, path, config):
        import numpy as np
        with Path(path).open('rb') as source:
            raw = source.read(MODEL_LIMIT + 1)
        if len(raw) > MODEL_LIMIT:
            raise ValueError('model_too_large')
        model = json.loads(raw)
        if not isinstance(model, dict) or model.get('schema') != 1 or model.get('feature_version') != FEATURE_VERSION:
            raise ValueError('unsupported_model')
        version = model.get('version', '')
        if not isinstance(version, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
            raise ValueError('invalid_model_version')
        if model.get('camera') != config['camera'] or model.get('size') != config['size'] or model.get('regions') != config['regions']:
            raise ValueError('model_camera_mismatch')
        if set(config['regions']) != {'pose', 'arm'}:
            raise ValueError('both_barrier_regions_required')
        self.version, self.regions = version, {}
        if not isinstance(model.get('classifiers'), dict) or set(model['classifiers']) != set(config['regions']):
            raise ValueError('missing_classifier')
        for name, value in model['classifiers'].items():
            if not isinstance(value, dict):
                raise ValueError('invalid_classifier')
            weights, bias = np.array(value['weights'], dtype=np.float32), np.array(value['bias'], dtype=np.float32)
            thresholds = np.array(value['thresholds'], dtype=np.float32)
            if (weights.shape != (3, FEATURE_LENGTH) or bias.shape != (3,) or thresholds.shape != (3,)
                    or not np.isfinite(weights).all() or not np.isfinite(bias).all()
                    or not np.isfinite(thresholds).all() or (thresholds < 0).any()
                    or (np.abs(weights) > 1e6).any() or (np.abs(bias) > 1e6).any()):
                raise ValueError('invalid_classifier')
            self.regions[name] = weights, bias, thresholds

    def classify(self, name, crop):
        import numpy as np
        vector = features(crop)
        if vector is None:
            return 0, {'reason': 'poor_visibility', 'margin': 0.0}
        weights, bias, thresholds = self.regions[name]
        scores = weights @ vector + bias
        order = np.argsort(scores)
        state = int(order[-1])
        margin = float(scores[order[-1]] - scores[order[-2]])
        threshold = float(thresholds[state])
        accepted = state if state != 0 and margin >= threshold else 0
        return accepted, {'candidate': state, 'margin': round(margin, 5),
                          'threshold': round(threshold, 5), 'reason': 'accepted' if accepted else 'low_confidence'}


def crop_region(image, region, origin=(0, 0), shift=(0, 0)):
    x1, y1, x2, y2 = region
    x1, x2 = x1 - origin[0] + shift[0], x2 - origin[0] + shift[0]
    y1, y2 = y1 - origin[1] + shift[1], y2 - origin[1] + shift[1]
    if min(x1, y1) < 0 or x2 > image.shape[1] or y2 > image.shape[0]:
        raise ValueError('region_outside_image')
    return image[y1:y2, x1:x2]

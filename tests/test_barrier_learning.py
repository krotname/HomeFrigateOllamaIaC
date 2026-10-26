import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from ocr.frigate_barrier_train import dataset


class SplitTests(unittest.TestCase):
    def test_episode_cannot_leak_between_days(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            rows = [{'group': 'cross-midnight-car', 'role': role, 'label': 1, 'period': 'night'} for role in ('train', 'test')]
            path.write_text(json.dumps({'samples': rows}))
            with self.assertRaisesRegex(ValueError, 'episode_in_multiple_splits'):
                dataset(path, {'train'})

    def test_test_files_are_excluded_from_training(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            rows = [{'group': role, 'role': role, 'label': 1, 'period': 'day'} for role in ('train', 'validation', 'test')]
            path.write_text(json.dumps({'samples': rows}))
            self.assertEqual([r['role'] for r in dataset(path, {'train', 'validation'})[1]], ['train', 'validation'])


@unittest.skipUnless(importlib.util.find_spec('cv2') and importlib.util.find_spec('numpy'), 'OpenCV runtime checks run in the inference image')
class ModelTests(unittest.TestCase):
    def test_thresholds_reject_joint_errors_without_penalizing_disagreement(self):
        import numpy as np
        from ocr.frigate_barrier_train import predict, tune_consensus
        model = {'classifiers': {name: {'weights': np.eye(3).tolist(), 'bias': [0, 0, 0],
                                       'thresholds': [0., .15, .15]} for name in ('pose', 'arm')}}
        values = {'pose': np.asarray([[0., 3, 0], [0, 20, 0], [0, 1.5, 0]]),
                  'arm': np.asarray([[0., 3, 0], [0, 0, 3], [0, 1, 0]])}
        tune_consensus(model, values, np.asarray([1, 0, 0]))
        self.assertEqual(predict(model, values).tolist(), [1, 0, 0])

    def setUp(self):
        import numpy as np
        from ocr.barrier_learning import FEATURE_LENGTH, FEATURE_VERSION
        self.config = {'camera': 'test', 'size': [1920, 1080], 'regions': {'pose': [1, 1, 20, 33], 'arm': [20, 1, 153, 26]}}
        self.model = {'schema': 1, 'feature_version': FEATURE_VERSION, 'version': '1.0.0', **copy.deepcopy(self.config),
                      'classifiers': {name: {'weights': np.zeros((3, FEATURE_LENGTH)).tolist(), 'bias': [0, 1, 0], 'thresholds': [0, .5, .5]} for name in ('pose', 'arm')}}
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'model.json'

    def load(self, model=None):
        from ocr.barrier_learning import LearnedPose
        self.path.write_text(json.dumps(self.model if model is None else model))
        return LearnedPose(self.path, self.config)

    def test_blank_or_hidden_region_is_unknown_even_with_a_confident_bias(self):
        import numpy as np
        learned = self.load()
        self.assertEqual(learned.classify('pose', np.zeros((32, 19), np.uint8))[0], 0)

    def test_corrupt_models_and_wrong_camera_are_rejected(self):
        for key, value in [('schema', 999), ('version', 'date-20261026'), ('camera', 'other'), ('size', [640, 480]), ('classifiers', None)]:
            with self.subTest(key=key):
                model = {**self.model, key: value}
                with self.assertRaises(ValueError):
                    self.load(model)
        with self.assertRaises(ValueError):
            self.load([])

    def test_nonfinite_weights_and_negative_thresholds_are_rejected(self):
        for field, value in [('bias', [0, float('nan'), 0]), ('thresholds', [0, -1, .5])]:
            with self.subTest(field=field):
                model = copy.deepcopy(self.model)
                model['classifiers']['pose'][field] = value
                with self.assertRaises(ValueError):
                    self.load(model)

    def test_low_margin_never_becomes_a_known_state(self):
        import numpy as np
        self.model['classifiers']['pose']['bias'] = [0, .1, 0]
        learned = self.load()
        crop = np.arange(32 * 19, dtype=np.uint8).reshape(32, 19)
        self.assertEqual(learned.classify('pose', crop)[0], 0)

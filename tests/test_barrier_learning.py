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
        visible = {name: np.ones(3, dtype=bool) for name in values}
        tune_consensus(model, values, np.asarray([1, 0, 0]), visible)
        self.assertEqual(predict(model, values, visible).tolist(), [1, 0, 0])

    def test_edge_crops_skip_only_invalid_augmentation_variants(self):
        import cv2
        import numpy as np
        from ocr.frigate_barrier_train import vectors
        image = np.tile(np.arange(40, dtype=np.uint8), (50, 1))
        cv2.imwrite(str(self.path.parent / 'edge.png'), image)
        config = {'regions': {'pose': [100, 200, 119, 232], 'arm': [119, 200, 140, 232]}}
        rows = [{'path': 'edge.png', 'label': 1}]
        data, labels, visible = vectors(self.path, config, rows, (100, 200), augment=True)
        self.assertEqual(labels.tolist(), [1, 0])
        for name in ('pose', 'arm'):
            self.assertEqual(len(data[name]), len(labels))
            self.assertTrue(visible[name].all())
        config['regions']['arm'][2] += 1
        with self.assertRaisesRegex(ValueError, 'region_outside_image'):
            vectors(self.path, config, rows, (100, 200), augment=True)

    def test_validation_visibility_matches_runtime_despite_confident_bias(self):
        import cv2
        import numpy as np
        from ocr.frigate_barrier_train import predict, tune_consensus, vectors
        image = np.tile(np.arange(40, dtype=np.uint8), (50, 1))
        hidden = image.copy()
        hidden[:, :19] = 0
        for filename, source in [('hidden.png', hidden), ('visible.png', image)]:
            cv2.imwrite(str(self.path.parent / filename), source)
        config = {'regions': {'pose': [0, 0, 19, 32], 'arm': [19, 0, 40, 32]}}
        rows = [{'path': 'hidden.png', 'label': 0}, {'path': 'visible.png', 'label': 1}]
        data, labels, visible = vectors(self.path, config, rows, (0, 0))
        self.assertEqual(visible['pose'].tolist(), [False, True])
        self.assertEqual(visible['arm'].tolist(), [True, True])
        self.assertEqual(predict(self.model, data, visible).tolist(), [0, 1])
        tune_consensus(self.model, data, labels, visible)
        self.assertEqual(predict(self.model, data, visible).tolist(), [0, 1])
        learned = self.load()
        self.assertEqual(learned.classify('pose', hidden[:32, :19])[0], 0)
        self.assertEqual(learned.classify('pose', image[:32, :19])[0], 1)

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

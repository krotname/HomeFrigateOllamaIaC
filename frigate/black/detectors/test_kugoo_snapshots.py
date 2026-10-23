import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("snapshots", Path(__file__).with_name("kugoo_snapshots.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SnapshotRefreshTest(unittest.TestCase):
    def setUp(self):
        self.original = patch.object(module, "original_is_better_thumbnail", return_value=False)
        self.original.start()
        self.addCleanup(self.original.stop)
        self.current = {"frame_time": 100, "score": .9, "area": 10000, "box": (100, 100, 200, 200)}
        self.new = {**self.current, "frame_time": 120, "score": .8, "label": "bicycle"}

    def refresh(self, **values):
        return module.is_better_thumbnail([], self.current, {**self.new, **values}, (720, 1280))

    def test_fresh_confident_native_track_can_supply_another_snapshot(self):
        self.assertTrue(self.refresh())
        self.assertTrue(self.refresh(label="motorcycle"))

    def test_other_native_labels_keep_original_policy(self):
        for label in ("person", "car", "cat", "dog"):
            self.assertFalse(self.refresh(label=label))

    def test_low_quality_stale_and_nonfinite_frames_do_not_refresh(self):
        for values in ({"frame_time": 100}, {"frame_time": 99}, {"frame_time": 119.9},
                       {"score": .69}, {"score": float("nan")}, {"area": 8999},
                       {"box": (0, 100, 200, 200)}):
            self.assertFalse(self.refresh(**values), values)

    def test_original_better_thumbnail_remains_authoritative(self):
        with patch.object(module, "original_is_better_thumbnail", return_value=True):
            self.assertTrue(self.refresh(label="cat"))


class DetectorContextTest(unittest.TestCase):
    def test_exact_queue_camera_reaches_detector_before_inference(self):
        spec = importlib.util.spec_from_file_location("adapter", Path(__file__).with_name("prepare_snapshot_patch.py"))
        adapter = importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
        source = ('class ObjectDetectProcess:\n'
                  '    def run(self, object_detector, input_frame):\n'
                  '        if True:\n'
                  '            if True:\n'
                  '                connection_id = self.detection_queue.get(timeout=1)\n'
                  '            detections = object_detector.detect_raw(input_frame)\n'
                  '            return detections\n'
                  'class RemoteObjectDetector:\n'
                  '    def send(self):\n'
                  '        self.detection_queue.put(self.name)\n')
        patched = adapter.patch_detector_context(source)
        self.assertEqual(patched, adapter.patch_detector_context(patched))
        namespace = {};exec(patched, namespace)
        from unittest.mock import Mock, call
        context=(100,((10,20,330,340),),((30,40,60,70),))
        detector = Mock();detector.detect_raw.side_effect = lambda frame: detector.detect_api.set_camera.assert_called_once_with('camera03',(10,20,330,340),context) or frame
        remote=namespace['RemoteObjectDetector']();remote.name='camera03';remote.detection_queue=__import__('queue').Queue()
        remote.set_region((10,20,330,340));remote.set_frame(100,[(10,20,330,340)],[(30,40,60,70)]);remote.send()
        process=namespace['ObjectDetectProcess']();process.detection_queue=remote.detection_queue
        self.assertEqual(process.run(detector, 'frame'), 'frame')
        self.assertEqual(detector.mock_calls[0], call.detect_api.set_camera('camera03',(10,20,330,340),context))
        process.detection_queue.put('other')
        detector.reset_mock();detector.detect_raw.side_effect=lambda frame:frame
        process.run(detector,'frame')
        detector.detect_api.set_camera.assert_called_once_with('other',None,None)
        with self.assertRaises(ValueError):
            adapter.patch_detector_context(source.replace('input_frame)', 'other_frame)'))

    def test_video_supplies_actual_region_and_rejects_contract_drift(self):
        spec=importlib.util.spec_from_file_location('adapter',Path(__file__).with_name('prepare_snapshot_patch.py'))
        adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
        source='def run(object_detector,tensor_input,region):\n    region_detections = object_detector.detect(tensor_input)\n    return region_detections\n'
        source+=('def process(object_detector,frame_time,regions,object_tracker):\n'
                 '    detections=[]\n'
                 '    if True:\n'
                 '        if True:\n'
                 '            for region in regions:\n'
                 '                detections.extend([])\n')
        result=adapter.patch_video_context(source)
        self.assertEqual(result,adapter.patch_video_context(result))
        namespace={};exec(result,namespace)
        from unittest.mock import Mock,call
        detector=Mock();namespace['run'](detector,'frame',(1,2,3,4))
        self.assertEqual(detector.mock_calls,[call.set_region((1,2,3,4)),call.detect('frame')])
        from types import SimpleNamespace
        tracker=SimpleNamespace(untracked_object_boxes=[(3,4,5,6)],tracked_objects={'bike':{'label':'bicycle','box':(2,3,4,5)},'car':{'label':'car','box':(1,1,2,2)}})
        namespace['process'](detector,123,[(1,2,3,4)],tracker)
        detector.set_frame.assert_called_once_with(123,[(1,2,3,4)],[(3,4,5,6),(2,3,4,5)])
        with self.assertRaises(ValueError):adapter.patch_video_context('changed')


if __name__ == "__main__":
    unittest.main()

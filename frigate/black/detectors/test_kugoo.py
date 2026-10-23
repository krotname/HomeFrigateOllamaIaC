import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np

spec=importlib.util.spec_from_file_location('supplement',Path(__file__).with_name('kugoo.py'))
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.base=np.zeros((20,6),dtype=np.float32)
        self.base[:3]=[[0,.95,.1,.1,.9,.9],[2,.92,.1,.1,.9,.9],[15,.91,.2,.2,.3,.3]]
        self.output=np.array([[[160],[160],[256],[256],[.99]]],dtype=np.float32)

    def test_base_classes_survive_overlapping_proposal(self):
        result=module.append_proposals(self.base,self.output,.7)
        np.testing.assert_array_equal(result[:3],self.base[:3])
        self.assertEqual(int(result[3,0]),1)
        self.assertEqual(np.count_nonzero(self.base[:,1]),3)

    def test_no_room_preserves_all_base_rows(self):
        self.base[:,1]=.8
        np.testing.assert_array_equal(module.append_proposals(self.base,self.output,.7),self.base)

    def test_existing_two_wheeler_is_not_duplicated(self):
        self.base[0,0]=1
        np.testing.assert_array_equal(module.append_proposals(self.base,self.output,.7),self.base)

    def test_coco_bicycle_channel_and_640_coordinates(self):
        output=np.zeros((1,84,2),dtype=np.float32)
        output[0,:4,:]=np.array([[320,100],[320,100],[512,80],[512,80]])
        output[0,5,0]=.99
        output[0,4,1]=.99
        result=module.append_proposals(self.base,output,.7,640)
        np.testing.assert_allclose(result[3],[1,.99,.1,.1,.9,.9],atol=1e-6)
        self.assertEqual(np.count_nonzero(result[:,1]),4)

    def detector(self):
        result=module.KugooDetector.__new__(module.KugooDetector)
        result.base=Mock();result.base.detect_raw.return_value=self.base
        result.config=SimpleNamespace(supplement_interval=.55,supplement_threshold=.7)
        result.session=None;result.last_attempt=0;result.retry_after=0;result.last_status=0
        result.counts={'inferences':0,'proposals':0,'busy':0,'errors':0}
        result.status=Mock();result.load=Mock(side_effect=RuntimeError('load failure'))
        return result

    def test_busy_or_low_memory_skips_supplement(self):
        for state in ({'free_mib':3583,'util_percent':0},{'free_mib':5000,'util_percent':25}):
            det=self.detector()
            with patch.object(module,'gpu_state',return_value=state):
                self.assertIs(det.detect_raw(np.zeros((1,3,320,320),dtype=np.float32)),self.base)
            det.load.assert_not_called()
            self.assertEqual(det.counts['busy'],1)

    def test_supplement_error_retains_base_and_backs_off(self):
        det=self.detector()
        with patch.object(module,'gpu_state',return_value={'free_mib':5000,'util_percent':0}):
            self.assertIs(det.detect_raw(np.zeros((1,3,320,320),dtype=np.float32)),self.base)
        self.assertEqual(det.counts['errors'],1)
        self.assertGreater(det.retry_after,det.last_attempt)

    def test_640_region_keeps_base_native_input(self):
        det=self.detector()
        tensor=np.full((1,3,640,640),.25,dtype=np.float32)
        with patch.object(module,'gpu_state',return_value={'free_mib':5000,'util_percent':25}):
            self.assertIs(det.detect_raw(tensor),self.base)
        original=det.base.detect_raw.call_args.args[0]
        self.assertEqual(original.shape,(1,3,320,320))
        np.testing.assert_array_equal(original,np.full((1,3,320,320),.25,dtype=np.float32))

    def test_warmup_waits_before_rechecking_gpu_capacity(self):
        det=self.detector()
        det.last_attempt=.8
        det.warmup_until=10
        with patch.object(module.time,'monotonic',return_value=1), \
                patch.object(module.time,'sleep') as sleep, \
                patch.object(module,'gpu_state',return_value={'free_mib':5000,'util_percent':25}):
            self.assertIs(det.detect_raw(np.zeros((1,3,320,320),dtype=np.float32)),self.base)
        self.assertAlmostEqual(sleep.call_args.args[0],.35)
        det.load.assert_not_called()

    def test_finished_warmup_does_not_delay_the_base_detector(self):
        det=self.detector()
        det.last_attempt=.8
        det.warmup_until=.9
        with patch.object(module.time,'monotonic',return_value=1), \
                patch.object(module.time,'sleep') as sleep, \
                patch.object(module,'gpu_state') as state:
            self.assertIs(det.detect_raw(np.zeros((1,3,320,320),dtype=np.float32)),self.base)
        sleep.assert_not_called()
        state.assert_not_called()

if __name__=='__main__': unittest.main()

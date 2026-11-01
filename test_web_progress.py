import json
from pathlib import Path
import tempfile
import unittest

from web_progress import JobProgress


class WebProgressTests(unittest.TestCase):
    def test_same_stage_keeps_timer_and_parallel_branches_share_one_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            now=[100];p=JobProgress(folder,clock=lambda:now[0]);now[0]=107;p.update('research')
            self.assertEqual(p.value['stage_elapsed_seconds'],7)
            now[0]=110;p.update('voiceover_and_visuals');now[0]=130;p.update('visuals')
            self.assertEqual(p.value['stage_id'],'assets');self.assertEqual(p.value['stage_elapsed_seconds'],20)
            self.assertEqual(json.loads((Path(folder)/'progress.json').read_text()),p.value)

    def test_stage_transition_and_failure_keep_true_stage_and_stop_time(self):
        with tempfile.TemporaryDirectory() as folder:
            now=[100];p=JobProgress(folder,clock=lambda:now[0]);now[0]=115;p.update('script');now[0]=122;p.fail()
            self.assertEqual(p.value['stage_id'],'script');self.assertEqual(p.value['stage_elapsed_seconds'],7)
            self.assertEqual(p.value['status'],'failed');self.assertEqual(p.value['stopped_at_ms'],122000)
            self.assertLess(p.value['stage_index'],p.value['stage_count'])

    def test_delivery_is_not_completion_and_unknown_stage_is_not_invented(self):
        with tempfile.TemporaryDirectory() as folder:
            p=JobProgress(folder);p.update('finished');before=dict(p.value);p.update('unobserved')
            self.assertEqual(p.value,before);self.assertEqual(p.value['status'],'running')
            p.update('delivery',status='completed');self.assertEqual(p.value['status'],'completed')

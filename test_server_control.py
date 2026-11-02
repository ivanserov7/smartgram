"""Deployment coordination tests: no real services, providers or SSH."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server_infra import control


class DrainTests(unittest.TestCase):
    def test_marker_is_acknowledged_before_inspecting_running_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            panel=Path(folder)
            (panel/'server').mkdir()
            (panel/'server/app.mjs').write_text('// SMARTGRAM_DRAIN_PROTOCOL_V1')
            events=[]
            def active():
                self.assertEqual(events[:2],['marker','acknowledged'])
                events.append('jobs')
                return [{'status':'running'}] if events.count('jobs')==1 else []
            with patch.object(control,'PANEL',panel), \
                 patch.object(control,'marker',return_value=panel/'maintenance.json'), \
                 patch.object(control,'atomic',side_effect=lambda *args:events.append('marker')), \
                 patch.object(control,'health',side_effect=lambda:events.append('acknowledged')), \
                 patch.object(control,'active_jobs',side_effect=active), \
                 patch.object(control.time,'sleep'):
                control.drain()
            self.assertEqual(events,['marker','acknowledged','jobs','jobs'])

    def test_failed_acknowledgement_never_proceeds_to_idle_assumption(self):
        with tempfile.TemporaryDirectory() as folder:
            panel=Path(folder)
            (panel/'server').mkdir()
            (panel/'server/app.mjs').write_text('// SMARTGRAM_DRAIN_PROTOCOL_V1')
            with patch.object(control,'PANEL',panel), \
                 patch.object(control,'marker',return_value=panel/'maintenance.json'), \
                 patch.object(control,'atomic'), \
                 patch.object(control,'health',side_effect=OSError('panel unavailable')), \
                 patch.object(control,'active_jobs') as active:
                with self.assertRaises(OSError):control.drain()
                active.assert_not_called()


if __name__=='__main__':unittest.main()

"""Offline tests for the LINEUP scheduling bridge (no SAP/Windows required)."""
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'LINEUP' / 'app' / 'scheduled_datahub_refresh.py'


class ScheduleBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.names = {
            'coois_production_lineup': 'coois_production_lineup_latest.csv',
            'coois_capacities_lineup': 'coois_capacities_lineup_latest.csv',
            'lx02': 'lx02_latest.csv',
            'lrf2': 'lrf2_latest.csv',
        }
        fake = types.ModuleType('datahub_adapter')
        fake.SOURCE_NAMES = self.names
        fake.is_enabled = lambda: True
        fake.source_path = lambda job: self.root / 'Live' / job / self.names[job]
        fake.validate = lambda: ([], [], [])
        self.adapter_patch = patch.dict(sys.modules, {'datahub_adapter': fake})
        self.adapter_patch.start()
        self.addCleanup(self.adapter_patch.stop)
        spec = importlib.util.spec_from_file_location('schedule_bridge_under_test', MODULE)
        self.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bridge)
        self.bridge.RUNTIME = self.root / 'runtime'
        self.bridge.STATE = self.bridge.RUNTIME / 'schedule.json'
        self.bridge.LOG = self.bridge.RUNTIME / 'schedule.log'
        self.bridge.LOCK = self.bridge.RUNTIME / 'schedule.lock'
        for jid, name in self.names.items():
            folder = self.root / 'Live' / jid
            folder.mkdir(parents=True)
            (folder / name).write_text('field\nvalue\n', encoding='utf8')
            time.sleep(0.003)
            (folder / f'{jid}_schema.json').write_text('{"columns":[]}', encoding='utf8')

    def test_four_reports_have_publication_markers(self):
        paths, signatures = self.bridge.source_status()
        self.assertEqual(len(signatures), 4)
        self.assertEqual(len(paths), 4)

    def test_missing_schema_fails_closed(self):
        (self.root / 'Live' / 'lrf2' / 'lrf2_schema.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Missing completed'):
            self.bridge.source_status()

    def test_schema_older_than_csv_fails_closed(self):
        (self.root / 'Live' / 'lx02' / 'lx02_latest.csv').touch()
        with self.assertRaisesRegex(RuntimeError, 'publish is not complete'):
            self.bridge.source_status()

    def test_all_four_must_advance(self):
        _, sig = self.bridge.source_status()
        self.assertTrue(self.bridge.is_new_cycle(sig, {}))
        self.assertFalse(self.bridge.is_new_cycle(sig, {'sources': sig}))
        later = {k: dict(v) for k, v in sig.items()}
        later['lrf2']['mtime_ns'] += 1
        self.assertFalse(self.bridge.is_new_cycle(later, {'sources': sig}))
        for row in later.values():
            row['mtime_ns'] += 10
        self.assertTrue(self.bridge.is_new_cycle(later, {'sources': sig}))

    def test_prime_and_wait(self):
        self.assertEqual(self.bridge.run('prime'), 0)
        self.assertTrue(self.bridge.STATE.exists())
        self.assertEqual(self.bridge.run('check'), 0)
        saved = self.bridge.load_state()
        self.assertFalse(self.bridge.is_new_cycle(saved['sources'], saved))

    def test_snapshot_is_consistent_and_preserves_mtime(self):
        paths, sig = self.bridge.source_status()
        with self.bridge.staged_inputs(paths, sig) as folder:
            for jid, (src, _) in paths.items():
                copy = folder / 'Live' / jid / src.name
                self.assertEqual(copy.read_bytes(), src.read_bytes())
                self.assertEqual(copy.stat().st_mtime_ns, src.stat().st_mtime_ns)
        self.assertFalse(folder.exists())

    def test_failed_lineup_keeps_last_schedule_baseline(self):
        with patch.object(self.bridge, 'execute_lineup', return_value=1), patch.object(self.bridge.adapter, 'validate'):
            self.assertEqual(self.bridge.run('run'), 1)
        self.assertFalse(self.bridge.STATE.exists())

    def test_successful_lineup_advances_baseline(self):
        with patch.object(self.bridge, 'execute_lineup', return_value=0):
            self.assertEqual(self.bridge.run('run'), 0)
        self.assertTrue(self.bridge.STATE.exists())
        self.assertEqual(self.bridge.run('check'), 0)
        saved = self.bridge.load_state()
        self.assertIn('last_success', saved)
        self.assertFalse(self.bridge.is_new_cycle(saved['sources'], saved))


if __name__ == '__main__':
    unittest.main()

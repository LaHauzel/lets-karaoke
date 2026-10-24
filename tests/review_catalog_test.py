"""Catalog overlay robustness: bad manifests must not take the review page down."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from repair_alignment import atomic
from review_catalog import build_catalog


class CatalogRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'exports'
        self.root.mkdir(parents=True)
        for name in ('v1.mkv', 'v2.mkv', 'h1.mkv', 'current-report.json', 'old-report.json'):
            (self.root / name).write_bytes(b'x')
        row = dict(project='demo', track='01_Sample',
                   quality=dict(total_rows=1, lower_confidence_rows=[], higher_confidence_rows=[1],
                                translation_low_rows=[], alignment_median_model_score=.9),
                   acceptance=dict(status='ok', label='ok', issues=[]), health=dict(ok=True),
                   variants=[dict(file='v1.mkv', variant=1, duration=2)])
        atomic(self.root / 'track-manifest.json', [row])

    def tearDown(self):
        self.temp.cleanup()

    def test_malformed_multitrack_manifest_is_a_diagnostic(self):
        (self.root / 'multitrack-manifest.json').write_text('{not json', encoding='utf8')
        catalog, _ = build_catalog(self.root)
        self.assertTrue(any('多轨清单无法读取' in item for item in catalog['diagnostics']))
        self.assertEqual(len(catalog['tracks']), 1)

    def test_unsupported_multitrack_schema_is_a_diagnostic(self):
        atomic(self.root / 'multitrack-manifest.json', dict(schema_version=2, archives=[]))
        catalog, _ = build_catalog(self.root)
        self.assertTrue(any('多轨清单版本不受支持' in item for item in catalog['diagnostics']))

    def test_null_revision_report_path_keeps_default(self):
        atomic(self.root / 'library-metadata.json', dict(revision_report=None))
        catalog, _ = build_catalog(self.root)
        self.assertIsNone(catalog['revision_report'])

    def test_history_envelope_and_bare_list_are_both_read(self):
        entry = dict(project='demo', track='01_Sample', revision='r1', supporting=[], evidence=[],
                     files=[dict(file='h1.mkv', variant=2, duration=2)])
        atomic(self.root / 'revisions/history.json', dict(schema_version=1, revisions=[entry]))
        catalog, _ = build_catalog(self.root)
        track = catalog['tracks'][0]
        self.assertEqual([item['revision'] for item in track['revision_history']], ['r1'])
        self.assertIn('r1', {f.get('revision') for f in track['files']})
        atomic(self.root / 'revisions/history.json', [entry])
        catalog, _ = build_catalog(self.root)
        self.assertEqual([item['revision'] for item in catalog['tracks'][0]['revision_history']], ['r1'])

    def test_malformed_history_is_a_diagnostic(self):
        (self.root / 'revisions').mkdir()
        (self.root / 'revisions/history.json').write_text('[] trailing', encoding='utf8')
        catalog, _ = build_catalog(self.root)
        self.assertTrue(any('历史修订清单无法读取' in item for item in catalog['diagnostics']))

    def test_repair_report_is_previous_only_when_not_part_of_current_revision(self):
        revision = dict(project='demo', track='01_Sample', revision='r2', supporting=['current-report.json'],
                        evidence=[], files=[dict(file='v2.mkv', variant=1, duration=2)])
        atomic(self.root / 'revisions/manifest.json', [revision])
        atomic(self.root / 'repair-runs/manifest.json',
               [dict(project='demo', track='01_Sample', report='current-report.json', changed=1)])
        catalog, _ = build_catalog(self.root)
        self.assertFalse(catalog['tracks'][0]['repair']['previous_revision'])
        atomic(self.root / 'repair-runs/manifest.json',
               [dict(project='demo', track='01_Sample', report='old-report.json', changed=1)])
        catalog, _ = build_catalog(self.root)
        self.assertTrue(catalog['tracks'][0]['repair']['previous_revision'])


if __name__ == '__main__':
    unittest.main()

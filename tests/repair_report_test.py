import json
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from repair_report import build_report, workspace_base


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.hrefs = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.hrefs.append(dict(attrs).get('href', ''))


class RepairReportTests(unittest.TestCase):
    def render(self, root, destination, **options):
        job = root / '01_Sample & test'
        job.mkdir(parents=True, exist_ok=True)
        row = dict(row=1, text='Synthetic <test> phrase', start=1, end=3,
                   score=.8, reasons=[])
        audit = dict(total=1, rows=[row], blocking_rows=[], duration_rows=[])
        report = dict(source=str(root / 'demo/jobs' / job.name), changed_rows=[],
                      before=audit, after=audit, render_gate=True, anchor_rows=[1])
        (job / 'repair-report.json').write_text(json.dumps(report), encoding='utf8')
        (job / 'align.json').write_text('{}', encoding='utf8')
        build_report(root, destination, project='demo project', **options)
        return (destination / 'index.html').read_text(encoding='utf8')

    def test_report_links_follow_the_serving_port_and_encode_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = self.render(root / 'input', root / 'report')
            links = Links(text).hrefs
            self.assertIn('/#review', links)
            track_link = next(h for h in links if '?track=' in h)
            for port in (8000, 9123):
                target = urlsplit(urljoin(f'http://localhost:{port}/review-exports/r/index.html', track_link))
                self.assertEqual(target.port, port)
                self.assertEqual(target.path, '/')
                self.assertEqual(parse_qs(target.query)['track'], ['demo project/01_Sample & test'])
                self.assertEqual(target.fragment, 'review')
            self.assertIn('Synthetic &lt;test&gt; phrase', text)
            self.assertNotIn('127.0.0.1:7870', text)

    def test_offline_report_can_target_an_explicit_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = self.render(root / 'input', root / 'report', workspace_url='http://localhost:9123/studio')
            links = Links(text).hrefs
            self.assertIn('http://localhost:9123/studio/#review', links)
            self.assertTrue(next(h for h in links if '?track=' in h).startswith('http://localhost:9123/studio/?'))

    def test_invalid_workspace_addresses_are_rejected_before_writing(self):
        for value in ('javascript:alert(1)', '//other.example/', 'file:///tmp/',
                      'http://user:secret@localhost/', 'http://localhost:bad/',
                      '/?token=secret', '/#review', '/\\other.example', '/\nother'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                workspace_base(value)
        self.assertEqual(workspace_base('/studio'), '/studio/')


if __name__ == '__main__':
    unittest.main()

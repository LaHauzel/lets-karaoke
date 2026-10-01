import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from local_history import delete, identify, scan

class HistoryDeleteTest(unittest.TestCase):
    def test_alignment_audit_is_not_a_history_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root/'webui'/'song'
            audit = root/'alignment-repair-audit'/'rebuilt'/'song'
            for directory in (original, audit):
                directory.mkdir(parents=True)
                (directory/'align.json').write_text('{"lines": []}', encoding='utf-8')
                (directory/'job.json').write_text('{"media": "song.mp4"}', encoding='utf-8')
            records = scan(root)['records']
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['path'], 'webui/song')

    def test_selected_scope_and_active_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); a=root/'webui'/'a'; b=root/'nested'/'b'; a.mkdir(parents=True); b.mkdir(parents=True)
            (a/'job.json').write_text('{}'); (b/'job.json').write_text('{}')
            ia,ib=identify(a,root),identify(b,root)
            self.assertEqual(delete(root,[ia],root/'webui'),[ia]); self.assertFalse(a.exists()); self.assertTrue(b.exists())
            with self.assertRaises(ValueError): delete(root,[ib],root/'webui',[ib])
            with self.assertRaises(ValueError): delete(root,['../../'],root/'webui')

    def test_alias_container_and_selection_are_protected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); web=root/'webui'; active=web/'active'; other=web/'other'
            for directory in (active,other):
                directory.mkdir(parents=True); (directory/'job.json').write_text('{}')
            for identifier in (identify(active,root),identify(web,root)):
                with self.assertRaises(ValueError): delete(root,[identifier],web,['active'])
            with self.assertRaises(ValueError): delete(root,[identify(other,root),identify(web,root)],web)
            self.assertTrue(active.exists()); self.assertTrue(other.exists())

    def test_history_restores_auxiliary_files_and_version_counts(self):
        import json
        from local_history import describe
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); task=root/'webui'/'task'; task.mkdir(parents=True)
            (task/'job.json').write_text('{"media":"song.mp4","lang":"zh"}')
            (task/'align.json').write_text('{"lines":[]}')
            (task/'align_v1.json').write_text(json.dumps({'lines':[{'raw':'a','start':1,'end':2,
                'tokens':[{'text':'a','disp':'a','start':1,'end':2}]}],'health':{'ok':True}}))
            (task/'song_karaoke_v1.mp4').touch(); (task/'asr_lyrics.lrc').touch()
            (task/'history_status.json').write_text('{"state":"done","result":{"stats":{"lines":99}}}')
            result=describe(task,root)['result']
            self.assertEqual(result['stats']['lines'],1)
            self.assertEqual(result['stats']['tokens'],1)
            self.assertIn('asr_lyrics.lrc',result['asr_files'])

if __name__=='__main__': unittest.main()

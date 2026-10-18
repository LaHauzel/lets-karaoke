import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from review_store import ReviewStore, ReviewConflict


class DurableReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'human-reviews.json'
        self.store=ReviewStore(self.path)
        self.track=dict(id='bz/07_日本語',total=20,duration=120,files=[dict(id='video',exists=True,
                       path='C:/video/日本語.mp4',signature='100:1234',duration=120)])
        self.payload=dict(status='needs_changes',notes='中文\n日本語 <b>literal</b>',revision=0,file_id='video',file_signature='100:1234',
                          issues=[dict(row=3,time=12.345,note='提前约 1 秒',resolved=False,file_id='video')])

    def tearDown(self):
        self.temp.cleanup()

    def test_unicode_restart_and_backup(self):
        first=self.store.save(self.track,self.payload)
        self.assertEqual(ReviewStore(self.path).read()['reviews'][self.track['id']],first)
        second=copy.deepcopy(self.payload);second.update(revision=1,status='passed');second['issues'][0]['resolved']=True
        self.store.save(self.track,second)
        self.assertEqual(self.store.read()['history'][self.track['id']],[first])
        backup=json.loads(Path(str(self.path)+'.bak').read_text(encoding='utf-8'))
        self.assertEqual(backup['reviews'][self.track['id']],first)

    def test_same_song_stale_revision_rejected_without_overwrite(self):
        first=self.store.save(self.track,self.payload)
        with self.assertRaises(ReviewConflict):self.store.save(self.track,{**self.payload,'notes':'do not overwrite'})
        self.assertEqual(self.store.read()['reviews'][self.track['id']],first)

    def test_concurrent_different_songs_both_survive(self):
        tracks=[{**self.track,'id':'bz/a'},{**self.track,'id':'jam-disc1/b'}]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda t:self.store.save(t,self.payload),tracks))
        self.assertEqual(set(self.store.read()['reviews']),{'bz/a','jam-disc1/b'})

    def test_independent_processes_do_not_lose_records(self):
        import subprocess
        program = '''import sys
sys.path.insert(0, sys.argv[1])
from review_store import ReviewStore
s=ReviewStore(sys.argv[2])
for i in range(8):
 t=dict(id=sys.argv[3]+str(i),total=1,duration=10,files=[])
 s.save(t,dict(status='unchecked',revision=0,notes='independent process'))
'''
        processes=[subprocess.Popen([sys.executable,'-c',program,
            str(Path(__file__).resolve().parents[1]/'src'),str(self.path),prefix],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE) for prefix in ('a','b','c')]
        for process in processes:
            stdout,stderr=process.communicate(timeout=15)
            self.assertEqual(process.returncode,0,stderr.decode())
        self.assertEqual(len(self.store.read()['reviews']),24)

    def test_atomic_failure_retains_original(self):
        self.store.save(self.track,self.payload);original=self.path.read_bytes()
        import os
        replace=os.replace
        def fail_main(source,dest):
            if Path(dest)==self.path:raise OSError('simulated disk failure')
            return replace(source,dest)
        with patch('review_store.os.replace',side_effect=fail_main):
            with self.assertRaises(OSError):self.store.save(self.track,{**self.payload,'revision':1,'notes':'new'})
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(list(self.path.parent.glob('*.tmp')),[])

    def test_corrupt_file_never_reset(self):
        self.path.write_text('{broken',encoding='utf-8')
        with self.assertRaises(ValueError):self.store.read()
        with self.assertRaises(ValueError):self.store.save(self.track,self.payload)
        self.assertEqual(self.path.read_text(encoding='utf-8'),'{broken')

    def test_validation_does_not_write(self):
        cases=[{'status':'other'},{'revision':True},{'file_id':'other-song'},
               {'notes':'x'*10001},{'issues':[dict(row=True,time=1,note='bad')]},
               {'issues':[dict(row=21,time=1,note='bad')]},{'issues':[dict(row=1,time=float('nan'),note='bad')]},
               {'issues':[dict(row=1,time=121,note='bad')]},{'issues':[dict(row=None,time=None,note='bad')]},
               {'issues':[dict(row=1,time=1,note='')]},{'status':'passed'},
               {'status':'passed','issues':[],'file_id':None},
               {'status':'passed','issues':[],'file_signature':'previous-video'}]
        for override in cases:
            with self.subTest(override=override),self.assertRaises(ValueError):
                self.store.save(self.track,{**self.payload,**override})
        self.assertFalse(self.path.exists())

    def test_changed_approved_video_needs_recheck(self):
        self.store.save(self.track,{**self.payload,'status':'passed','issues':[]})
        catalog={'tracks':[copy.deepcopy(self.track)]};self.store.attach(catalog)
        self.assertFalse(catalog['tracks'][0]['human_review']['stale'])
        catalog['tracks'][0]['files'][0]['signature']='100:changed'
        self.store.attach(catalog)
        self.assertTrue(catalog['tracks'][0]['human_review']['stale'])
        self.assertEqual(catalog['tracks'][0]['human_review']['status'],'passed')


if __name__=='__main__':unittest.main(verbosity=2)

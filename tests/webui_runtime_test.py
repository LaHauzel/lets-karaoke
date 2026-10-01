"""Real HTTP upload/file boundaries and durable, serialized task execution."""
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import requests

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import webui
from http_support import receive_multipart
from local_history import identify


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        webui.TASK_QUEUE.join()
        self.temp=tempfile.TemporaryDirectory()
        self.old_root,self.old_out=webui.ROOT,webui.OUT_ROOT
        webui.ROOT=Path(self.temp.name)
        webui.OUT_ROOT=webui.ROOT/'out/webui'
        webui.OUT_ROOT.mkdir(parents=True)
        webui.JOBS.clear(); webui.EDIT_DRAFTS.clear()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),webui.Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        webui.TASK_QUEUE.join()
        self.server.shutdown(); self.server.server_close()
        webui.JOBS.clear(); webui.EDIT_DRAFTS.clear()
        webui.ROOT,webui.OUT_ROOT=self.old_root,self.old_out
        self.temp.cleanup()

    def fixture(self,name='task'):
        directory=webui.OUT_ROOT/name
        directory.mkdir(exist_ok=True)
        (directory/'job.json').write_text('{}')
        return directory

    def test_origin_rejection_preserves_task(self):
        job=webui.Job('task',self.fixture())
        webui.JOBS[job.id]=job
        response=requests.post(self.base+'/api/cancel',json={'job':job.id},
            headers={'Origin':'https://unrelated.invalid'},timeout=5)
        self.assertEqual(response.status_code,403)
        self.assertFalse(job.cancel_req)
        response=requests.post(self.base+'/api/cancel',data=json.dumps({'job':job.id}),
            headers={'Content-Type':'text/plain'},timeout=5)
        self.assertEqual(response.status_code,415)
        self.assertFalse(job.cancel_req)
        alias=f'http://localhost:{self.server.server_port}'
        self.assertEqual(requests.post(self.base+'/api/cancel',json={'job':job.id},
            headers={'Origin':alias},timeout=5).status_code,200)
        self.assertTrue(job.cancel_req)

    def test_streamed_same_name_inputs_are_isolated(self):
        configs=[]
        def worker(job,config):
            configs.append(config)
            job.state='done'; webui.persist_job(job)
        with patch.object(webui,'_run_job',side_effect=worker):
            response=requests.post(self.base+'/api/run',data={'lyrics_text':'a'},files={
                'media':('same.wav',b'media'),'audio_track':('same.wav',b'track'),
                'lyrics_file':('same.wav',b'lyric')},timeout=5)
            self.assertEqual(response.status_code,200,response.text)
            webui.TASK_QUEUE.join()
        config=configs[0]
        self.assertEqual(len({config[k] for k in ('media','audio_track','lyrics_path')}),3)
        self.assertEqual(Path(config['media']).read_bytes(),b'media')
        self.assertEqual(Path(config['audio_track']).read_bytes(),b'track')
        self.assertEqual(Path(config['lyrics_path']).read_bytes(),b'lyric')

    def test_binary_boundary_like_bytes_survive_streaming(self):
        payload=b'abc\r\n--unit-not-a-boundary\x00xyz'
        body=b'--unit\r\nContent-Disposition: form-data; name="media"; filename="a.bin"\r\n\r\n'+payload+b'\r\n--unit--\r\n'
        fields,files=receive_multipart(io.BytesIO(body),len(body),b'unit',Path(self.temp.name))
        self.assertEqual(files['media'][1].read_bytes(),payload)
        with self.assertRaises(ValueError):
            receive_multipart(io.BytesIO(body[:-5]),len(body),b'unit',Path(self.temp.name))

    def test_suffix_range_invalid_range_and_unicode_download(self):
        directory=self.fixture(); name='中文.mp4'; (directory/name).write_bytes(b'0123456789')
        url=self.base+'/files/task/'+requests.utils.quote(name)
        response=requests.get(url,headers={'Range':'bytes=-3'},timeout=5)
        self.assertEqual(response.status_code,206); self.assertEqual(response.content,b'789')
        self.assertEqual(response.headers['Content-Range'],'bytes 7-9/10')
        for value in ('bytes=99-100','bytes=-0','bytes=4-1','bytes=0-1,3-4'):
            self.assertEqual(requests.get(url,headers={'Range':value},timeout=5).status_code,416)
        response=requests.get(url+'?download=1',timeout=5)
        self.assertEqual(response.content,b'0123456789')
        self.assertIn("filename*=UTF-8''",response.headers['Content-Disposition'])

    def test_queue_cancel_and_durable_recovery(self):
        started=threading.Event(); release=threading.Event(); order=[]
        def worker(job,config):
            job.state='running'; webui.persist_job(job); order.append(job.id)
            if job.id=='first':
                started.set(); release.wait(5)
            job.state='done'; webui.persist_job(job)
        with patch.object(webui,'_run_job',side_effect=worker):
            first=webui.Job('first',self.fixture('first')); second=webui.Job('second',self.fixture('second'))
            webui.enqueue_job(first,{'media':'first.wav'})
            self.assertTrue(started.wait(5))
            webui.enqueue_job(second,{'media':'second.wav'})
            self.assertEqual(second.state,'queued')
            alias=identify(second.dir,webui.ROOT/'out')
            response=requests.post(self.base+'/api/history/delete',json={'jobs':[alias]},timeout=5)
            self.assertEqual(response.status_code,400); self.assertTrue(second.dir.exists())
            requests.post(self.base+'/api/cancel',json={'job':'second'},timeout=5)
            release.set(); webui.TASK_QUEUE.join()
        self.assertEqual(order,['first']); self.assertEqual(second.state,'cancelled')
        first.state='running'; webui.persist_job(first); webui.JOBS.clear()
        webui.restore_jobs(start_queued=False)
        self.assertEqual(webui.JOBS['first'].state,'interrupted')
        self.assertEqual(webui.JOBS['second'].state,'cancelled')

    def test_cancelled_queue_retry_runs_generation_and_edit_once(self):
        for kind in ('generate', 'edit'):
            with self.subTest(kind=kind):
                started=threading.Event(); release=threading.Event(); order=[]
                def worker(job,config):
                    job.state='running'; order.append(job.id)
                    if job.id==kind+'-first':
                        started.set(); release.wait(5)
                    job.state='done'; webui.persist_job(job)
                def edit(data,operation):
                    order.append(operation.id)
                    return {'ok':True,'version':1}
                first=webui.Job(kind+'-first',self.fixture(kind+'-first'))
                with patch.object(webui,'_run_job',side_effect=worker), \
                        patch.object(webui,'perform_edit',side_effect=edit):
                    try:
                        webui.enqueue_job(first,{})
                        self.assertTrue(started.wait(5))
                        if kind=='edit':
                            self.fixture('edit-target')
                            response=requests.post(self.base+'/api/rerender',
                                json={'job':'edit-target','async':True},timeout=5)
                            self.assertEqual(response.status_code,202,response.text)
                            identifier=response.json()['operation_job']
                        else:
                            second=webui.Job(kind+'-second',self.fixture(kind+'-second'))
                            webui.enqueue_job(second,{'media':'synthetic.wav'})
                            identifier=second.id
                        original_attempt=webui.JOBS[identifier].attempt_id
                        self.assertEqual(requests.post(self.base+'/api/cancel',
                            json={'job':identifier},timeout=5).status_code,200)
                        response=requests.post(self.base+'/api/resume',json={'job':identifier},timeout=5)
                        self.assertEqual(response.status_code,202,response.text)
                        self.assertNotEqual(original_attempt,webui.JOBS[identifier].attempt_id)
                    finally:
                        release.set(); webui.TASK_QUEUE.join()
                self.assertEqual(order,[first.id,identifier])
                self.assertEqual(webui.JOBS[identifier].state,'done')

    def test_cancelled_queue_item_does_not_recreate_deleted_record(self):
        started=threading.Event(); release=threading.Event()
        def worker(job,config):
            job.state='running'; started.set(); release.wait(5)
            job.state='done'; webui.persist_job(job)
        first=webui.Job('first',self.fixture('first'))
        second=webui.Job('second',self.fixture('second'))
        with patch.object(webui,'_run_job',side_effect=worker):
            try:
                webui.enqueue_job(first,{})
                self.assertTrue(started.wait(5))
                webui.enqueue_job(second,{})
                requests.post(self.base+'/api/cancel',json={'job':second.id},timeout=5).raise_for_status()
                response=requests.post(self.base+'/api/history/delete',json={'jobs':[second.id]},timeout=5)
                self.assertEqual(response.status_code,200,response.text)
                self.assertFalse(second.dir.exists())
            finally:
                release.set(); webui.TASK_QUEUE.join()
        self.assertFalse(second.dir.exists())

    def test_upload_directory_failure_releases_both_slots(self):
        with patch.object(webui,'UPLOAD_SLOTS',threading.BoundedSemaphore(2)), \
                patch.object(webui.traceback,'print_exc'):
            with patch.object(Path,'mkdir',side_effect=OSError('upload directory unavailable')):
                for _ in range(2):
                    response=requests.post(self.base+'/api/run',data={'lyrics_text':'a'},
                        files={'media':('a.wav',b'media')},timeout=5)
                    self.assertEqual(response.status_code,500,response.text)
            def worker(job,config):
                job.state='done'; webui.persist_job(job)
            with patch.object(webui,'_run_job',side_effect=worker):
                response=requests.post(self.base+'/api/run',data={'lyrics_text':'a'},
                    files={'media':('a.wav',b'media')},timeout=5)
                self.assertEqual(response.status_code,200,response.text)
                webui.TASK_QUEUE.join()

    def test_edit_queue_is_async_and_cancelable(self):
        directory=self.fixture(); started=threading.Event()
        def edit(data,operation):
            started.set()
            while not operation.cancel_req:
                threading.Event().wait(.01)
            raise RuntimeError('cancelled')
        with patch.object(webui,'perform_edit',side_effect=edit):
            response=requests.post(self.base+'/api/rerender',json={'job':'task','async':True},timeout=5)
            self.assertEqual(response.status_code,202,response.text)
            identifier=response.json()['operation_job']; self.assertTrue(started.wait(5))
            status=requests.get(self.base+'/api/job',params={'job':identifier},timeout=5).json()
            self.assertEqual(status['state'],'running')
            requests.post(self.base+'/api/cancel',json={'job':identifier},timeout=5)
            webui.TASK_QUEUE.join()
        self.assertEqual(webui.JOBS[identifier].state,'cancelled')
        self.assertFalse((directory/'history_status.json').exists())

    def test_zero_style_and_grouping_are_respected(self):
        from ass_builder import AssOptions
        options=AssOptions(**webui._ass_kwargs({'lead_ms':0,'tail_ms':0,'outline':0,'min_gap_ms':0,'group_same_unit':True}))
        self.assertEqual((options.lead_ms,options.tail_ms,options.outline,options.min_gap_ms),(0,0,0,0))
        self.assertTrue(options.group_same_unit)

    def test_draft_roundtrip_uses_json(self):
        from ass_builder import KaraokeLine,KaraokeToken
        identifier='a'*32
        draft={'job_id':'task','job_dir':str(self.fixture()),'base_version':0,'updated':1,'insertions':[],
            'lines':[KaraokeLine('a',[KaraokeToken('a','a',1,2)],start=1,end=2)]}
        from dataclasses import asdict
        webui.atomic_json(webui.OUT_ROOT/'.drafts'/f'{identifier}.json',{**draft,'lines':[asdict(row) for row in draft['lines']]})
        restored=webui.load_draft(identifier)
        self.assertEqual(restored['lines'][0].tokens[0].end,2)
        response=requests.get(self.base+'/api/draft',params={'draft_id':identifier,
            'job':identify(Path(draft['job_dir']),webui.ROOT/'out'),'base_version':0},timeout=5)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['lines'][0]['tokens'][0]['end'],2)
        self.assertEqual(requests.get(self.base+'/api/draft',params={'draft_id':identifier,
            'job':'task','base_version':1},timeout=5).status_code,404)
        webui.discard_draft(identifier)
        self.assertIsNone(webui.load_draft(identifier))

    def test_edit_draft_restores_server_derived_acceptance_reference(self):
        from ass_builder import KaraokeLine,KaraokeToken
        self.fixture()
        original=[KaraokeLine('hello',[KaraokeToken('hello','hello',1,2)],start=1,end=2)]
        calls=[]
        def restyle(directory,request,**kwargs):
            calls.append(kwargs.get('initial_reference'))
            return {'preview':True,'_draft_lines':original,
                    'acceptance_reference':{'source':'user_edits','lines':['hello','retained missing line']}}
        with patch('pipeline.restyle',side_effect=restyle):
            response=requests.post(self.base+'/api/rerender',
                json={'job':'task','async':True,'defer_render':True,'line_texts':{'0':'hello'}},timeout=5)
            self.assertEqual(response.status_code,202,response.text)
            webui.TASK_QUEUE.join()
            draft_id=webui.JOBS[response.json()['operation_job']].result['draft_id']
            webui.EDIT_DRAFTS.clear()
            response=requests.post(self.base+'/api/rerender',json={'job':'task','async':True,
                'defer_render':True,'draft_id':draft_id,'acceptance_reference':['client-forged-reference']},timeout=5)
            self.assertEqual(response.status_code,202,response.text)
            webui.TASK_QUEUE.join()
        self.assertEqual(calls,[None,{'source':'user_edits','lines':['hello','retained missing line']}])

    def test_evicted_result_and_retry_state_contract(self):
        directory=self.fixture()
        job=webui.Job('task',directory,state='done',result={'video':'complete.mp4'})
        webui.atomic_json(directory/'task_request.json',{'job':job.id,'directory':str(directory),
            'kind':'generate','config':{'media':'original.wav'}})
        webui.persist_job(job)
        response=requests.get(self.base+'/api/job',params={'job':'task'},timeout=5)
        self.assertEqual(response.json()['result'],job.result)
        self.assertEqual(requests.post(self.base+'/api/resume',json={'job':'task'},timeout=5).status_code,409)
        self.assertEqual(requests.post(self.base+'/api/cancel',json={'job':'task'},timeout=5).status_code,200)
        self.assertFalse(webui.JOBS['task'].cancel_req)
        self.assertEqual(requests.post(self.base+'/api/cancel',json={'job':'unknown'},timeout=5).status_code,404)

    def test_edit_request_can_resume_without_overwriting_generation(self):
        directory=self.fixture()
        identifier='edit_'+'a'*16
        operation=webui.Job(identifier,directory,state='interrupted',kind='edit',
            storage_dir=webui.OUT_ROOT/'.tasks'/identifier,target_job='task')
        webui.atomic_json(operation.storage_dir/'task_request.json',{'job':identifier,
            'directory':str(directory),'kind':'edit','target_job':'task','config':{'job':'task'}})
        webui.persist_job(operation)
        with patch.object(webui,'perform_edit',return_value={'ok':True,'version':1}) as edit:
            response=requests.post(self.base+'/api/resume',json={'job':identifier},timeout=5)
            self.assertEqual(response.status_code,202,response.text)
            self.assertEqual(response.json()['target_job'],'task')
            webui.TASK_QUEUE.join()
        self.assertEqual(edit.call_count,1)
        self.assertEqual(webui.JOBS[identifier].state,'done')
        self.assertFalse((directory/'history_status.json').exists())

    def test_disk_failure_does_not_terminate_queue_worker(self):
        first=webui.Job('first',self.fixture('first'))
        second=webui.Job('second',self.fixture('second'))
        def worker(job,config):
            if job.id=='first':
                raise RuntimeError('model failure')
            job.state='done'
        real_persist=webui.persist_job
        def persist(job):
            if job.id=='first' and job.state=='error':
                raise OSError('disk full')
            real_persist(job)
        with patch.object(webui,'_run_job',side_effect=worker), patch.object(webui,'persist_job',side_effect=persist), patch.object(webui.traceback,'print_exc'):
            webui.enqueue_job(first,{})
            webui.enqueue_job(second,{})
            webui.TASK_QUEUE.join()
        self.assertEqual(first.state,'error')
        self.assertEqual(second.state,'done')
        self.assertTrue(webui.WORKER.is_alive())


if __name__=='__main__':
    unittest.main()

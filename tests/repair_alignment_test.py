import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from repair_alignment import repair_job,Engine,fingerprint,align_block,transcribe,transcribe_window,cast_encoder_input


class RepairIntegrationTests(unittest.TestCase):
    def test_timestamp_encoder_input_uses_weight_precision(self):
        import torch
        from types import SimpleNamespace
        encoder=SimpleNamespace(conv1=SimpleNamespace(weight=torch.zeros(1,dtype=torch.float16)))
        mel=torch.ones(2,3,dtype=torch.float32)
        result=cast_encoder_input(encoder,(mel,))
        self.assertEqual(result[0].dtype,torch.float16)
        self.assertEqual(mel.dtype,torch.float32)

    def test_repair_isolated_cached_and_preserves_input(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);job=root/'input';job.mkdir();(job/'in').mkdir();(job/'in/audio_44k.wav').write_bytes(b'audio stub')
            old=dict(raw='hello world',start=1,end=1.1,tokens=[dict(text='hello world',disp='hello world',start=1,end=1.1)])
            align=dict(lines=[old],audio_duration=10,lang='en',options={})
            (job/'align.json').write_text(json.dumps(align));before=(job/'align.json').read_bytes()
            (job/'alignment_evidence.json').write_text(json.dumps(dict(lines=[dict(row=1,text='hello world',confidence=.1,start=1,end=1.1)])))
            (job/'input_lyrics.json').write_text(json.dumps(['hello world']))
            fixed=dict(raw='hello world',start=2,end=4,tokens=[dict(text='hello world',disp='hello world',start=2,end=4)])
            option=dict(row=1,line=fixed,confidence=.8,model='large-v3',audio='mix')
            with patch('repair_alignment.transcribe',return_value=[dict(text='hello world',start=2,end=4)]),patch('repair_alignment.align_block',return_value=[option])as infer:
                report=repair_job(job,root/'output',['large-v3'],Engine('cpu'),lambda *a,**k:None)
                self.assertEqual(report['changed_rows'],[1]);self.assertTrue(report['render_gate']);self.assertFalse(report['human_approved'])
                count=infer.call_count
                again=repair_job(job,root/'output',['large-v3'],Engine('cpu'),lambda *a,**k:None)
                self.assertEqual(again,report);self.assertEqual(infer.call_count,count)
            self.assertEqual((job/'align.json').read_bytes(),before)
            self.assertTrue((root/'output/normal.ass').is_file())
            with self.assertRaises(ValueError):repair_job(job,job,['large-v3'],Engine('cpu'),lambda *a,**k:None)

    def test_cache_changes_when_lyrics_or_settings_change(self):
        with tempfile.TemporaryDirectory()as temp:
            path=Path(temp)/'lyrics.json';path.write_text('["one"]')
            key=fingerprint([path],{'model':'medium'})
            self.assertNotEqual(key,fingerprint([path],{'model':'large-v3'}))
            path.write_text('["two"]')
            self.assertNotEqual(key,fingerprint([path],{'model':'medium'}))

    def test_real_token_mapping_preserves_english_spaces(self):
        import wave
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory()as temp:
            folder=Path(temp);audio=folder/'audio.wav'
            with wave.open(str(audio),'wb')as stream:
                stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(b'\0\0'*16000*5)
            result=SimpleNamespace(segments=[SimpleNamespace(words=[
                SimpleNamespace(word='Hello',start=1.,end=1.5,probability=.9),
                SimpleNamespace(word=' world',start=1.5,end=2.,probability=.9)])])
            engine=Engine('cpu')
            with patch.object(engine,'load',return_value=None),patch('stable_whisper.alignment.align',return_value=result):
                items=align_block(audio,folder,dict(first=1,last=1,left=0,right=5,language='en'),['Hello world'],'medium','mix',engine)
            self.assertEqual(items[0]['line']['raw'],'Hello world')
            self.assertEqual(''.join(t['disp']for t in items[0]['line']['tokens']),'Hello world')

    def test_unprompted_window_decodes_once_then_reuses_cache(self):
        import numpy as np
        from types import SimpleNamespace
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory()as temp:
            folder=Path(temp);source=folder/'audio.wav';source.write_bytes(b'audio identity')
            model=Mock(device='cpu',dims=SimpleNamespace(n_mels=80),is_multilingual=True,num_languages=99)
            model.decode.return_value=SimpleNamespace(text='hello world',tokens=[1,2],no_speech_prob=.1,avg_logprob=-.2)
            engine=Engine('cpu')
            words=[SimpleNamespace(word='hello',start=1.,end=2.,probability=.9),SimpleNamespace(word=' world',start=2.,end=3.,probability=.9)]
            with patch.object(engine,'load',return_value=model),patch('whisper.timing.find_alignment',return_value=words):
                args=(source,folder,'medium','en',engine,lambda *a,**k:None,np.zeros(16000*30,dtype=np.float32),10,20,10,30)
                first=transcribe_window(*args);second=transcribe_window(*args)
            self.assertEqual(model.decode.call_count,1)
            self.assertEqual(first,second);self.assertEqual((first[0]['start'],first[0]['end']),(11,13))
            self.assertFalse(model.transcribe.called)

    def test_wrong_old_language_timing_does_not_exclude_second_language(self):
        import numpy as np
        with tempfile.TemporaryDirectory()as temp:
            folder=Path(temp)
            old=[dict(raw='English words',start=0,end=2),dict(raw='日本語の歌詞',start=10,end=12)]
            with patch('whisper.load_audio',return_value=np.zeros(32000,dtype=np.float32)),patch('repair_alignment.transcribe_window',return_value=[])as window:
                transcribe(folder/'audio.wav',folder,'medium',None,Engine('cpu'),lambda *a,**k:None,old)
            self.assertEqual({call.args[3]for call in window.call_args_list},{'en','ja'})

    def test_cache_only_window_never_loads_a_model(self):
        with tempfile.TemporaryDirectory()as temp:
            folder=Path(temp);source=folder/'audio.wav';source.write_bytes(b'audio identity')
            engine=Engine('cpu')
            with patch.object(engine,'load')as load:
                self.assertEqual(transcribe_window(source,folder,'medium','en',engine,lambda *a,**k:None,None,0,2,0,2,True),[])
                load.assert_not_called()


if __name__=='__main__':unittest.main()

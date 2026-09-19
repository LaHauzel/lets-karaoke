"""Isolated short-audio fixture for interactive editor verification."""
import json
import sys
import tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import webui
from pipeline import restyle

with tempfile.TemporaryDirectory(prefix='karaoke_editor_') as temp:
    webui.ROOT=Path(temp)
    webui.OUT_ROOT=Path(temp)/'out'/'webui'
    job=webui.OUT_ROOT/'editor_test';job.mkdir(parents=True)
    source=ROOT/'data/synth/zh/zh_gapped_mix.wav'
    metadata={'job':'editor_test','media':str(source),'lang':'zh','out_audio':None,
              'media_info':{'duration':7.642,'width':640,'height':360,'has_video':False,'has_audio':True,'acodec':'pcm_s16le'},'options':{}}
    rows=[{'raw':text,'start':start,'end':end,'tokens':[{'text':text,'disp':text,'start':start,'end':end}]} for text,start,end in [('东风送来清凉',.3,2.5),('小船划过池塘',2.4,4.5),('月色照在桥上',5,7)]]
    (job/'job.json').write_text(json.dumps(metadata),encoding='utf-8')
    (job/'align.json').write_text(json.dumps({'lines':rows}),encoding='utf-8')
    restyle(job)
    server=ThreadingHTTPServer(('127.0.0.1',18766),webui.Handler)
    print('READY http://127.0.0.1:18766',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

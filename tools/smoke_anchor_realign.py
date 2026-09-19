"""Exercise real cropped-audio alignment and encoding on the short smoke fixture."""
import json
import shutil
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from pipeline import restyle,RestyleRequest,load_job

directory=Path(tempfile.mkdtemp(prefix='anchor_smoke_',dir=ROOT/'out'))
for name in ('job.json','align.json'):
    shutil.copy2(ROOT/'out/system_smoke/automatic'/name,directory/name)
before=load_job(directory)[1]
result=restyle(directory,RestyleRequest(anchor_row=0,line_offsets_ms={0:20}))
after=load_job(directory,result['version'])[1]
assert len(after)==len(before)
assert abs(after[0].start-before[0].start-.02)<.0001
assert abs(after[0].end-before[0].end-.02)<.0001
assert all(line.start>=after[0].end for line in after[1:])
assert Path(result['video']).is_file()
print(json.dumps({'directory':str(directory),'result':result,'lines':len(after)},ensure_ascii=False))

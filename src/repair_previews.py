"""Create short before/after previews without promoting a blocked candidate."""
import argparse
import copy
import hashlib
import shutil
import subprocess
from pathlib import Path
from filelock import FileLock
from repair_alignment import read,atomic,fingerprint
from repair_delivery import probe,write_caption
from repair_triage import review_queue
from ass_builder import AssOptions,KaraokeLine,KaraokeToken,build_ass
import model_paths


def preview_job(candidate,destination,max_clips=1,encoder='auto'):
    report=read(candidate/'repair-report.json',{})
    changed=report.get('changed_rows',[])
    remaining=list(dict.fromkeys(report.get('after',{}).get('blocking_rows',[])+report.get('after',{}).get('duration_rows',[])))
    if not changed and not remaining:return []
    destination.mkdir(parents=True,exist_ok=True)
    source=Path(report['source'])/'in/source.mp4';info=probe(source)
    video=next(s for s in info['streams']if s['codec_type']=='video')
    baseline=read(candidate/'baseline.json');current=read(candidate/'align.json')
    signature=hashlib.sha256((candidate/'align.json').read_bytes()).hexdigest()
    severe=set(report['before']['blocking_rows']+report['before']['duration_rows'])
    repairs=sorted(changed,key=lambda row:(row not in severe,row))
    queue=review_queue(report)
    priority=(repairs[:1]+[q['rows'][0]for q in queue]+repairs[1:])if repairs else [q['rows'][0]for q in queue]
    windows=[]
    for row in priority:
        a,b=baseline['lines'][row-1],current['lines'][row-1]
        left=max(0,min(a['start'],b['start'])-4);right=min(current['audio_duration'],max(a['end'],b['end'])+4,left+45)
        issue=next((q for q in queue if row in q['rows'] and q['kind']=='reference_structure'),None)
        if issue:
            left=max(0,min(left,issue['start']-6));right=min(current['audio_duration'],max(right,issue['end']+6),left+45)
        if any(l<=b['start']<=r for l,r,_ in windows):continue
        windows.append((left,right,row))
        if len(windows)>=max_clips:break
    results=[]
    for label,align in [('before',baseline),('after',current)]:
        lines=[KaraokeLine(raw=l['raw'],head=l.get('head',''),start=l['start'],end=l['end'],tokens=[KaraokeToken(**t)for t in l['tokens']])for l in align['lines']]
        ass=destination/(label+'.ass')
        write_caption(ass,build_ass(copy.deepcopy(lines),AssOptions(**align['options']),video['width'],video['height']))
        for i,(left,right,row)in enumerate(windows,1):
            dest=destination/f'preview-{i}-{label}.mp4';key=fingerprint([source,ass],dict(left=left,right=right,scale=1280,encoder=encoder))
            cache=dest.with_suffix('.json')
            if not dest.is_file()or read(cache,{}).get('fingerprint')!=key:
                pending=dest.with_name(dest.stem+'.pending.mp4')
                before=destination/f'preview-{i}-before.mp4'
                identical=label=='after'and before.is_file()and ass.read_bytes()==(destination/'before.ass').read_bytes()
                # Add the song offset for libass, then restore clip timestamps.
                vf=f'setpts=PTS-STARTPTS+{left:.6f}/TB,ass={ass.name},scale=1280:-2,setpts=PTS-STARTPTS'
                encoders=[]if identical else(['nvenc','x264']if encoder=='auto'else[encoder])
                if identical:
                    shutil.copyfile(before,pending)
                    completed=subprocess.CompletedProcess([],0,b'',b'')
                for selected in encoders:
                    codec=['-c:v','h264_nvenc','-preset','p4','-cq','23']if selected=='nvenc'else['-c:v','libx264','-preset','fast','-crf','23']
                    cmd=['ffmpeg','-v','error','-nostdin','-y','-ss',str(left),'-i',str(source),'-t',str(right-left),'-vf',vf,
                         *codec,'-threads','4','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',str(pending)]
                    if selected=='nvenc':
                        with FileLock(str(model_paths.ROOT/'out/gpu-processing.lock')):
                            completed=subprocess.run(cmd,cwd=destination,capture_output=True)
                    else:completed=subprocess.run(cmd,cwd=destination,capture_output=True)
                    if not completed.returncode:break
                if completed.returncode:raise RuntimeError(completed.stderr.decode('utf8',errors='replace')[-3000:])
                pending.replace(dest)
                duration=float(probe(dest)['format']['duration'])
                if abs(duration-(right-left))>.2:raise ValueError('预览时间轴时长不正确')
                atomic(cache,dict(fingerprint=key,start=left,end=right,row=row))
            results.append(dict(file=dest.name,label=label,start=left,end=right,row=row,text=current['lines'][row-1]['raw'],candidate_signature=signature,
                                purpose='repair'if row in changed else'unresolved'))
    atomic(destination/'previews.json',results)
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);p.add_argument('destination',type=Path);p.add_argument('--max-clips',type=int,default=2)
    p.add_argument('--encoder',choices=['auto','nvenc','x264'],default='auto')
    p.add_argument('--tracks',nargs='+',help='仅生成指定歌曲目录的预览；未指定时处理全部候选')
    args=p.parse_args()
    failures=[]
    for candidate in sorted(args.root.iterdir()):
        if args.tracks and candidate.name not in args.tracks:continue
        if (candidate/'repair-report.json').is_file():
            print('PREVIEW',candidate.name,flush=True)
            try:preview_job(candidate.resolve(),(args.destination/candidate.name).resolve(),args.max_clips,args.encoder)
            except Exception as error:
                failures.append(dict(track=candidate.name,error=str(error)))
                print('FAILED',candidate.name,str(error),flush=True)
    atomic(args.destination/'preview-failures.json',failures)

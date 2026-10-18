"""Export accepted repair candidates, reusing source pictures and cached stems.

This command never changes a human review. Failed gates receive reports only.
"""
from __future__ import annotations
import argparse
import copy
import os
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from filelock import FileLock
from repair_alignment import read, atomic, fingerprint
from repair_policy import audit
from repair_validation import validate_render
from alignment_review import assess, normalized
from ass_builder import AssOptions, KaraokeLine, KaraokeToken, build_ass, build_srt, display_chunks, esc_text, _fmt_time
import pipeline
import model_paths


def probe(path):
    import json
    return json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)]))


def run(args):
    result=subprocess.run(['ffmpeg','-v','error','-nostdin','-y',*map(str,args)],capture_output=True)
    if result.returncode:raise RuntimeError(result.stderr.decode('utf8',errors='replace')[-4000:])


def japanese(text):return bool(re.search(r'[\u3040-\u30ff\u3400-\u9fff]',text))


def write_caption(path,text):
    encoded=text.encode('utf-8-sig')
    if not path.is_file()or path.read_bytes()!=encoded:path.write_bytes(encoded)


def refresh_gate(candidate):
    """Revalidate cached reports against the current caption rendering rules."""
    report=read(candidate/'repair-report.json',{});align=read(candidate/'align.json')
    if not report:return report
    recorded=report.get('after',{}).get('rows',[])
    if len(recorded)!=len(align['lines'])or any(normalized(a['text'])!=normalized(b['raw'])or
        any(abs(a[k]-b[k])>.0001 for k in ('start','end'))for a,b in zip(recorded,align['lines'])):
        raise ValueError('候选与修复报告不一致，请重新检查')
    evidence=read(candidate/'alignment_evidence.json',{}).get('lines',[])
    report['rendered']=validate_render(align,evidence,report.get('anchor_rows',[]))
    report['render_gate']=report['rendered']['gate']
    report['status']='ready_for_review'if report['render_gate']else'needs_repair'
    lines=[KaraokeLine(raw=l['raw'],head=l.get('head',''),start=l['start'],end=l['end'],
                      tokens=[KaraokeToken(**t)for t in l['tokens']])for l in align['lines']]
    write_caption(candidate/'normal.srt',build_srt(display_chunks(lines,AssOptions(**align['options']))))
    atomic(candidate/'repair-report.json',report)
    return report


def bilingual_ass(lines,options,translations,width,height):
    import unicodedata
    norm=lambda s:''.join(unicodedata.normalize('NFKC',s).split())
    translations={norm(k):v for k,v in translations.items()}
    if not any(japanese(l.raw)for l in lines):return build_ass(lines,options,width,height)
    options=replace(options,next_line=False,line_count=1,position_y=81)
    result=build_ass(lines,options,width,height)
    style=f'Style: ZH,{options.font},{round(38*height/1080)},&H00FFFFFF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,{round(3*height/1080,1)},0,2,60,60,40,1\n'
    result=result.replace('[Events]',style+'\n[Events]')
    chunks=display_chunks(lines,options)
    for line in lines:
        if not japanese(line.raw):continue
        value=translations.get(norm(line.raw))
        if not value:raise ValueError('缺少已缓存译文：'+line.raw)
        text=value['zh'] if isinstance(value,dict) else value
        if len(text)>43:text=text[:len(text)//2]+'\n'+text[len(text)//2:]
        for c in chunks:
            if c.start>=line.start-.001 and c.end<=line.end+.001:
                result+=f'Dialogue: 0,{_fmt_time(c.ev_start)},{_fmt_time(c.ev_end)},ZH,,0,0,0,,{{\\an2\\pos({round(width*.5)},{round(height*.92)})}}{esc_text(text)}\n'
    return result


def export_job(candidate,destination,translation_cache=None,baseline=None,formats=('soft','hard')):
    candidate=Path(candidate).resolve();destination=Path(destination).resolve()
    report=read(candidate/'repair-report.json',{});align=read(candidate/'align.json')
    evidence=read(candidate/'alignment_evidence.json',{}).get('lines',[])
    expected=read(candidate/'input_lyrics.json')
    acceptance=assess(align,expected,align['audio_duration'])
    # The report belongs to these exact rows, not an edited candidate left
    # beside an older successful gate.
    recorded=report.get('after',{}).get('rows',[])
    if len(recorded)!=len(align['lines']) or any(normalized(a['text'])!=normalized(b['raw']) or
            any(abs(a[k]-b[k])>.0001 for k in ('start','end'))for a,b in zip(recorded,align['lines'])):
        raise ValueError('候选与修复报告不一致，请重新检查')
    rendered=validate_render(align,evidence,report.get('anchor_rows',[]))
    if not report.get('render_gate') or not rendered['gate'] or acceptance.get('status')=='needs_fix':
        raise ValueError('时间结构尚未通过，不能导出整曲修订版')
    if not report.get('changed_rows'):return None
    source=Path(report['source'])/'in/source.mp4'
    if destination==candidate or destination.is_relative_to(Path(report['source']).resolve()):
        raise ValueError('成片必须使用独立输出目录')
    info=probe(source);video=next(s for s in info['streams']if s['codec_type']=='video');duration=float(info['format']['duration'])
    if abs(duration-align['audio_duration'])>.6:raise ValueError('媒体与候选时间轴时长不一致')
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'.history-ignore').touch()
    lines=[KaraokeLine(raw=l['raw'],head=l.get('head',''),tokens=[KaraokeToken(**t)for t in l['tokens']],start=l['start'],end=l['end'])for l in align['lines']]
    options=AssOptions(**align['options']);width,height=video['width'],video['height']
    subtitles={1:build_ass(copy.deepcopy(lines),options,width,height)}
    if translation_cache:subtitles[3]=bilingual_ass(copy.deepcopy(lines),options,read(translation_cache,{}),width,height)
    for number,text in subtitles.items():write_caption(destination/('normal.ass'if number==1 else'bilingual.ass'),text)
    write_caption(destination/'normal.srt',build_srt(display_chunks(copy.deepcopy(lines),options)))
    for name in ['align.json','alignment_evidence.json','acceptance.json','repair-report.json','whisper_lyrics.lrc']:
        shutil.copyfile(candidate/name,destination/name)
    accompaniment=Path(baseline)/'accompaniment.m4a' if baseline else None
    if accompaniment is None or not accompaniment.is_file():accompaniment=Path(report['source'])/'in/vg/accompaniment.wav'
    if not accompaniment.is_file():raise FileNotFoundError(accompaniment)
    encoded=destination/'accompaniment.m4a'
    audio_id=fingerprint([accompaniment],dict(duration=duration,aac=256000))
    if not encoded.is_file()or read(destination/'audio-cache.json',{}).get('fingerprint')!=audio_id:
        pending=destination/'accompaniment.pending.m4a'
        if accompaniment.suffix=='.m4a':
            if abs(float(probe(accompaniment)['format']['duration'])-duration)>.6:raise ValueError('缓存伴奏时长不匹配')
            try:os.link(accompaniment,pending)
            except OSError:shutil.copyfile(accompaniment,pending)
        else:run(['-i',accompaniment,'-af','aresample=async=1:first_pts=0','-t',duration,'-c:a','aac','-b:a','256k',pending])
        pending.replace(encoded);atomic(destination/'audio-cache.json',dict(fingerprint=audio_id))
    records=[]
    for number in subtitles:
        ass=destination/('normal.ass'if number==1 else'bilingual.ass')
        for mode in formats:
            extension='.mp4'if mode=='hard'else'.mkv'
            dest=destination/f'{candidate.name}__v{number}{extension}'
            identity=fingerprint([source,ass],dict(mode=mode,quality=20,encoder='auto'))
            checkpoint=dest.with_suffix(extension+'.json')
            if not dest.is_file()or read(checkpoint,{}).get('fingerprint')!=identity:
                pending=dest.with_name(dest.stem+'.pending'+extension)
                first=destination/f'{candidate.name}__v1{extension}'
                if number==3 and subtitles[3]==subtitles[1] and first.is_file():
                    try:os.link(first,pending)
                    except OSError:shutil.copyfile(first,pending)
                elif mode=='soft':run(['-i',source,'-i',ass,'-map','0:v:0','-map','0:a:0','-map','1:0','-c','copy','-c:s','ass','-disposition:s:0','default',pending])
                else:
                    with FileLock(str(model_paths.ROOT/'out/gpu-processing.lock')):
                        pipeline.render_video(source,ass.name,destination,pending,None,{'has_video':True},'auto',20,True)
                pending.replace(dest);atomic(checkpoint,dict(fingerprint=identity))
            alternate=destination/f'{candidate.name}__v{number+1}{extension}'
            alt_key=fingerprint([dest,encoded],dict(mode=mode,variant=number+1))
            alt_checkpoint=alternate.with_suffix(extension+'.json')
            if not alternate.is_file()or read(alt_checkpoint,{}).get('fingerprint')!=alt_key:
                pending=alternate.with_name(alternate.stem+'.pending'+extension)
                cmd=['-i',dest,'-i',encoded,'-map','0:v:0','-map','1:a:0']
                if mode=='soft':cmd+=['-map','0:s:0','-disposition:s:0','default']
                run([*cmd,'-c','copy',*(['-movflags','+faststart']if mode=='hard'else[]),pending])
                pending.replace(alternate);atomic(alt_checkpoint,dict(fingerprint=alt_key))
            for variant,path in [(number,dest),(number+1,alternate)]:
                check=probe(path);streams=check['streams'];expected=['audio','video']+(['subtitle']if mode=='soft'else[])
                if sorted(s['codec_type']for s in streams)!=sorted(expected)or abs(float(check['format']['duration'])-duration)>.6:
                    raise ValueError('导出媒体验证失败：'+str(path))
                records.append(dict(file=str(path),variant=variant,duration=float(check['format']['duration']),size=path.stat().st_size,verified=True))
    result=dict(track=candidate.name,files=records,acceptance=acceptance,health=align['health'],timeline_offset=0,
        evidence=[dict(row=e['row'],text=e['text'],start=e['start'],end=e['end'],score=e.get('confidence'),
                       disagreement=e.get('dual_disagreement_s'),priority=e.get('priority','建议复核'),reasons='；'.join(e.get('reasons',[])))for e in evidence],
        supporting=[str(destination/n)for n in ['align.json','alignment_evidence.json','acceptance.json','repair-report.json','normal.ass','normal.srt']]+([str(destination/'bilingual.ass')]if 3 in subtitles else[]),
        summary=f'自动局部修复 {len(report["changed_rows"])} 行；时间结构门槛通过，仍需人工听看',human_approved=False)
    atomic(destination/'delivery.json',result);return result


def register(result,catalog,project,revision,source):
    catalog=Path(catalog).resolve();source=Path(source).resolve()
    if any(not Path(f['file']).resolve().is_relative_to(catalog)for f in result['files']):raise ValueError('成片必须位于成品库内')
    manifest=catalog/'revisions/manifest.json';manifest.parent.mkdir(parents=True,exist_ok=True)
    with FileLock(str(manifest)+'.lock'):
        rows=read(manifest,[]);old=[r for r in rows if(r['project'],r['track'])==(project,result['track'])]
        if old and old[0].get('revision')!=revision:
            archive=catalog/'revisions/history.json';atomic(archive,read(archive,[])+old)
        rows=[r for r in rows if(r['project'],r['track'])!=(project,result['track'])]
        atomic(manifest,rows+[dict(**result,project=project,revision=revision)])
    linksfile=catalog/'workflow-links.json'
    with FileLock(str(linksfile)+'.lock'):
        links=read(linksfile,{});link=links.setdefault(project+'/'+result['track'],{});out=model_paths.ROOT/'out'
        link.setdefault('versions',{})[revision]=dict(align=Path(result['supporting'][0]).relative_to(out).as_posix(),media=(source/'in/source.mp4').relative_to(out).as_posix())
        atomic(linksfile,links)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('repair_root',type=Path);p.add_argument('destination',type=Path)
    p.add_argument('--translation-cache',type=Path);p.add_argument('--baseline-exports',type=Path)
    p.add_argument('--formats',nargs='+',choices=['soft','hard'],default=['soft','hard'])
    p.add_argument('--catalog-root',type=Path);p.add_argument('--project');p.add_argument('--revision')
    p.add_argument('--tracks',nargs='+',help='仅导出指定歌曲目录；未指定时处理全部候选')
    args=p.parse_args()
    if args.catalog_root and not(args.project and args.revision):p.error('注册成品需要 --project 与 --revision')
    failures=[]
    for candidate in sorted(args.repair_root.iterdir()):
        if args.tracks and candidate.name not in args.tracks:continue
        report=read(candidate/'repair-report.json',{})
        if not report:continue
        try:
            report=refresh_gate(candidate)
            if not report.get('render_gate')or not report.get('changed_rows'):continue
            print('EXPORT',candidate.name,flush=True)
            result=export_job(candidate,args.destination/candidate.name,args.translation_cache,args.baseline_exports/candidate.name if args.baseline_exports else None,args.formats)
            if args.catalog_root and result:register(result,args.catalog_root,args.project,args.revision,report['source'])
        except Exception as error:
            failures.append(dict(track=candidate.name,error=str(error)))
            atomic(args.destination/'export-failures.json',failures)
            print('FAILED',candidate.name,str(error),flush=True);continue
        print('DONE',candidate.name,flush=True)
    atomic(args.destination/'export-failures.json',failures)


if __name__=='__main__':main()

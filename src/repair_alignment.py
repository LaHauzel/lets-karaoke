"""Resumable local repair CLI for an existing karaoke alignment directory.

Creates an isolated candidate and evidence, never overwrites source deliveries.
Example: python src/repair_alignment.py --jobs-root out/concert/jobs \
    --output-root out/concert/repair --models large-v3 medium
"""
from __future__ import annotations

import argparse
import copy
import gc
import hashlib
import json
import math
import statistics
import time
from pathlib import Path

from filelock import FileLock
import model_paths
from alignment_review import assess, normalized
from repair_policy import VERSION, audit, choose_rows, lyric_language, plan_blocks, transcript_anchors

REPORT_VERSION = 8


def atomic(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    temp.replace(path)


def read(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf8')) if Path(path).is_file() else default


def fingerprint(paths, parameters):
    files=[]
    for path in paths:
        p=Path(path).resolve();st=p.stat()
        files.append(dict(path=str(p),size=st.st_size,mtime=st.st_mtime_ns,
                          sha256=hashlib.sha256(p.read_bytes()).hexdigest() if p.suffix=='.json' else None))
    return hashlib.sha256(json.dumps(dict(files=files,parameters=parameters),sort_keys=True).encode()).hexdigest()


class Engine:
    def __init__(self, device='cuda'):
        self.device=device;self.model=None;self.name=None

    def load(self,name):
        if self.name==name:return self.model
        import torch,whisper
        from whisper_compat import patch_whisper_triton
        self.close();patch_whisper_triton()
        checkpoint=model_paths.WHISPER_DIR/(name+'.pt')
        if not checkpoint.is_file():raise FileNotFoundError(f'请先下载本地模型：{checkpoint}')
        self.model=whisper.load_model(name,device='cpu',download_root=str(model_paths.WHISPER_DIR))
        if self.device=='cuda':
            self.model.half()
            for layer in self.model.modules():
                if isinstance(layer,torch.nn.LayerNorm):layer.float()
            # Both Whisper and stable-ts may pass FP32 mel directly to the
            # encoder. Whisper's Conv1d then promotes its weights to FP32.
            # Enforce the loaded model precision at this shared entry point.
            self.model.encoder.register_forward_pre_hook(cast_encoder_input)
        self.model.to(self.device).eval();self.name=name
        return self.model

    def close(self):
        if self.model is not None:
            import torch
            self.model=None;self.name=None;gc.collect()
            if torch.cuda.is_available():torch.cuda.empty_cache()


def cast_encoder_input(module,args):
    return (args[0].to(dtype=module.conv1.weight.dtype),*args[1:])


def transcribe(source, folder, model, language, engine, progress, reference_lines=None, chunk_seconds=20,cache_only=False):
    import whisper
    if cache_only:
        import soundfile as sf
        duration=math.ceil(sf.info(source).duration*16000)/16000;data=None
    else:
        data=whisper.load_audio(str(source));duration=len(data)/16000
    segments=[]
    # Overlapping windows retain words at chunk boundaries. No lyric prompt.
    for offset in range(0,math.ceil(duration),chunk_seconds):
        left=max(0,offset-2);right=min(duration,offset+chunk_seconds+2)
        # Music often triggers spurious language detection (including languages
        # absent from the lyrics). Decode only the reference's language set.
        languages=[language] if language else []
        if not languages and reference_lines:
            weights={}
            for line in reference_lines:
                overlap=max(0,min(right,line['end'])-max(left,line['start']))
                if overlap:
                    lang=lyric_language(line['raw'])
                    weights[lang]=weights.get(lang,0)+overlap
            if weights:
                # The old timing may itself be wrong. For mixed lyrics, try
                # both known languages instead of trusting those boundaries.
                languages=sorted({lyric_language(line['raw'])for line in reference_lines})
            else:
                nearest=min(reference_lines,key=lambda line:min(abs(line['start']-(left+right)/2),abs(line['end']-(left+right)/2)))
                languages=[lyric_language(nearest['raw'])]
        if not languages:raise ValueError('请提供参考歌词以限制转写语言')
        for decode_language in languages:
            segments.extend(transcribe_window(source,folder,model,decode_language,engine,progress,data,left,right,offset,duration,cache_only))
    if not cache_only:
        atomic(folder/'transcript.json',dict(segments=segments,source=str(source),model=model,
                                           language=language,note='按歌词语言限定、无歌词提示的自动转写，不是人工真值'))
    return segments


def transcribe_window(source,folder,model,language,engine,progress,data,left,right,offset,duration,cache_only=False):
    key=fingerprint([source],dict(version=VERSION,model=model,language=language,left=left,right=right,
                                 task='single-decode-asr-v2',word_timestamps=True,sample_len=128))
    cache=folder/'cache'/('asr-'+key+'.json')
    item=read(cache)
    if item is None and cache_only:return []
    if item is None:
        import whisper
        from whisper.tokenizer import get_tokenizer
        from whisper.timing import find_alignment
        from whisper.utils import compression_ratio
        progress('transcribe',position=offset,duration=duration,model=model,language=language)
        loaded=engine.load(model);samples=data[round(left*16000):round(right*16000)]
        mel=whisper.log_mel_spectrogram(whisper.pad_or_trim(samples),n_mels=loaded.dims.n_mels).to(loaded.device)
        # find_alignment() does not cast its input like decode() does. FP32 mel
        # otherwise promotes both encoder and decoder even with FP16 weights,
        # causing shared-memory paging on a 6GB Windows display GPU.
        if engine.device=='cuda':mel=mel.half()
        # transcribe() can repeatedly seek over hallucinated timestamps in music.
        # Exactly one bounded decode per crop prevents that non-progress loop.
        result=loaded.decode(mel,whisper.DecodingOptions(language=language,task='transcribe',
            temperature=0,fp16=engine.device=='cuda',sample_len=128,without_timestamps=True))
        items=[]
        if result.no_speech_prob<=.85 and result.avg_logprob>=-1.5 and compression_ratio(result.text)<=2.4:
            tokenizer=get_tokenizer(loaded.is_multilingual,num_languages=loaded.num_languages,language=language,task='transcribe')
            words=find_alignment(loaded,tokenizer,result.tokens,mel,len(samples)//160)
            group=[]
            for word in words:
                if not word.word.strip():continue
                if group and (word.end-group[0]['start']>9 or len(normalized(''.join(w['word']for w in group)))>35):
                    items.append(dict(text=''.join(w['word']for w in group),start=group[0]['start']+left,
                        end=group[-1]['end']+left,words=[{**w,'start':w['start']+left,'end':w['end']+left}for w in group],language=language))
                    group=[]
                group.append(dict(word=word.word,start=float(word.start),end=float(word.end),probability=float(word.probability)))
            if group:
                items.append(dict(text=''.join(w['word']for w in group),start=group[0]['start']+left,
                    end=group[-1]['end']+left,words=[{**w,'start':w['start']+left,'end':w['end']+left}for w in group],language=language))
        item=dict(segments=items,window=[left,right],model=model,language=language,prompt=None,
                  avg_logprob=result.avg_logprob,no_speech_prob=result.no_speech_prob)
        atomic(cache,item)
        del result,mel
        if engine.device=='cuda':
            import torch
            torch.cuda.empty_cache()
    return [{**segment,'stream':f'{model}:{source}:{left}:{right}:{language}'}for segment in item['segments']]


def align_block(source, folder, block, lyrics, model, audio, engine):
    import soundfile as sf
    import whisper_align as wa
    import pipeline as pp
    from p0_common import TokenSpan,tokenize
    from alignment_policy import resolve_rules
    from ass_builder import KaraokeLine,KaraokeToken
    from dataclasses import asdict
    # Retry implausibly long words within this bounded crop. The outer
    # workflow compares models and audio routes before accepting any result.
    # Cross-attention memory scales with text length. A short token step keeps
    # large-v3 below the Windows 6GB GPU paging threshold for long paragraphs.
    align_options=dict(token_step=48,fast_mode=False,word_dur_factor=2.,max_word_dur=3.,nonspeech_skip=5.,
                       suppress_silence=True,verbose=None,regroup=False,failure_threshold=.4)
    key=fingerprint([source],dict(version=VERSION,model=model,block=block,lyrics=lyrics,options=align_options,
                                 precision='fp16'if engine.device=='cuda'else'fp32'))
    dest=folder/'cache'/('align-'+key+'.json')
    cached=read(dest)
    if cached is not None:return cached
    sr=sf.info(source).samplerate
    samples,sr=sf.read(source,start=round(block['left']*sr),stop=round(block['right']*sr),always_2d=True)
    clip=folder/'cache'/('clip-'+key+'.wav');clip.parent.mkdir(parents=True,exist_ok=True)
    sf.write(clip,samples,sr,subtype='PCM_16')
    from stable_whisper.alignment import align
    measured=align(model=engine.load(model),audio=str(clip),text='\n'.join(lyrics),language=block['language'],**align_options)
    words=[[w.word,float(w.start),float(w.end),float(w.probability or 0)]
           for segment in measured.segments for w in segment.words or [] if w.word.strip()] if measured else []
    lines,mapping=wa.build_lines(words,lyrics,resolve_rules(wa.DEFAULT_RULES,profile='automatic'))
    result=[]
    for local,line in zip(mapping['row_index'],lines):
        tokens=tokenize(line.text,pp.LANGS[block['language']])
        spans,_=pp.map_units_to_tokens([TokenSpan(text=s.text,start=s.start,end=s.end)for s in line.segments],tokens,block['right']-block['left'])
        if any(s is None for s in spans):continue
        spans=pp._fill_and_monotonic(spans,tokens,(0,block['right']-block['left']),repair_collapsed=False)
        head,displays=pp.build_display(line.text,tokens)
        mapped=[KaraokeToken(text=t,disp=d,start=p.start+block['left'],end=p.end+block['left'],unit=p.unit)
                for t,d,p in zip(tokens,displays,spans)]
        if not mapped:continue
        value=KaraokeLine(raw=line.text,head=head,tokens=mapped,start=mapped[0].start,end=mapped[-1].end)
        result.append(dict(row=block['first']+local,line=asdict(value),confidence=float(line.prob),model=model,audio=audio))
    atomic(dest,result)
    return result


def repair_job(job, output, models, engine, progress, anchors_file=None):
    job=Path(job).resolve();output=Path(output).resolve()
    if output==job or output.is_relative_to(job):raise ValueError('修复结果必须放在独立目录')
    sources={'mix':job/'in/audio_44k.wav','vocals':job/'in/vg/vocals.wav'}
    if not sources['mix'].is_file():raise FileNotFoundError(sources['mix'])
    sources={k:v for k,v in sources.items() if v.is_file()}
    paths=[job/'align.json',job/'alignment_evidence.json',*sources.values()]
    if (job/'input_lyrics.json').is_file():paths.append(job/'input_lyrics.json')
    if anchors_file:paths.append(anchors_file)
    identity=fingerprint(paths,dict(version=VERSION,models=models,device=engine.device))
    previous=read(output/'repair-report.json',{})
    if previous.get('report_version')==REPORT_VERSION and previous.get('fingerprint')==identity and (output/'align.json').is_file():
        progress('cached',track=job.name);return previous
    output.mkdir(parents=True,exist_ok=True)
    (output/'.history-ignore').touch()
    baseline=read(job/'align.json');old_evidence=read(job/'alignment_evidence.json',{}).get('lines',[])
    lyrics=read(job/'input_lyrics.json',[l['raw']for l in baseline['lines']])
    if [normalized(s)for s in lyrics]!=[normalized(l['raw'])for l in baseline['lines']]:
        raise ValueError('原始歌词与现有行序不一致，先核准参考歌词，禁止自动丢行')
    atomic(output/'baseline.json',baseline)
    atomic(output/'baseline-evidence.json',old_evidence)
    languages={lyric_language(s,baseline.get('lang','ja'))for s in lyrics}
    language=next(iter(languages)) if len(languages)==1 else None
    # The smaller selected model handles sustained, autoregressive decoding;
    # large-v3 remains in the bounded forced-alignment comparisons. Reuse
    # any previously cached large-model ASR windows without recomputing them.
    asr_model=models[-1]
    segments=transcribe(sources['mix'],output,models[0],language,engine,progress,baseline['lines'],cache_only=True)
    segments.extend(transcribe(sources['mix'],output/('asr-'+asr_model),asr_model,language,engine,progress,baseline['lines']))
    # Release decoder caches between free transcription and forced alignment.
    # On a 6GB display GPU, their combined peak can trigger WDDM paging.
    engine.close()
    anchors,candidates=transcript_anchors(lyrics,segments,baseline['lines'])
    if 'vocals'in sources:
        segments.extend(transcribe(sources['vocals'],output/'vocal-transcript',models[0],language,engine,progress,baseline['lines'],chunk_seconds=12,cache_only=True))
        anchors,candidates=transcript_anchors(lyrics,segments,baseline['lines'])
    if len(anchors)<len(lyrics)*.7 and 'vocals'in sources:
        progress('supplement_vocal_transcript',track=job.name,anchors=len(anchors),rows=len(lyrics))
        supplement=transcribe(sources['vocals'],output/('vocal-asr-'+asr_model),asr_model,language,engine,progress,baseline['lines'],chunk_seconds=12)
        segments.extend(supplement);engine.close()
        anchors,candidates=transcript_anchors(lyrics,segments,baseline['lines'])
    manual=read(anchors_file,[]) if anchors_file else []
    for a in manual:
        row=a['row']
        if not 1<=row<=len(lyrics) or normalized(a['text'])!=normalized(lyrics[row-1]) or not 0<=a['start']<a['end']<=baseline['audio_duration']:
            raise ValueError('人工锚点必须匹配当前歌词与媒体时间')
        anchors=[v for v in anchors if v['row']!=row]
        anchors.append({**a,'source':'provided_anchor','match':1.,'precision':1.})
    anchors.sort(key=lambda a:a['row'])
    if any(a['end']>b['start']+.12 for a,b in zip(anchors,anchors[1:])):
        raise ValueError('人工与自动锚点相互冲突，请核准顺序')
    atomic(output/'anchors.json',dict(selected=anchors,candidates=candidates,provided=manual))
    blocks=plan_blocks(baseline['lines'],old_evidence,anchors,baseline['audio_duration'],baseline.get('lang','ja'))
    atomic(output/'plan.json',blocks)
    options={}
    for model in models:
        for block in blocks:
            if block['skipped']:continue
            for audio,source in sources.items():
                progress('align',track=job.name,rows=[block['first'],block['last']],model=model,audio=audio)
                try:
                    result=align_block(source,output,block,lyrics[block['first']-1:block['last']],model,audio,engine)
                except Exception as error:
                    # Keep a failed candidate diagnostic, never silently approve.
                    atomic(output/'last-candidate-error.json',dict(model=model,block=block,audio=audio,error=str(error)))
                    if 'out of memory' in str(error).lower():engine.close()
                    progress('candidate_error',error=str(error));continue
                for item in result:options.setdefault(item['row'],[]).append(item)
    selected,new_evidence,decisions=choose_rows(baseline['lines'],old_evidence,options,anchors,baseline['audio_duration'])
    repaired=copy.deepcopy(baseline);repaired['lines']=selected
    repaired['diagnostics']=dict(lines=new_evidence,source='automatic_repair')
    from ass_builder import KaraokeLine,KaraokeToken,check_windows,AssOptions,build_ass,build_srt,display_chunks
    from asr_lyrics import AsrLine,Segment,to_enhanced_lrc
    karaoke=[KaraokeLine(raw=l['raw'],head=l.get('head',''),tokens=[KaraokeToken(**t)for t in l['tokens']],start=l['start'],end=l['end'])for l in selected]
    options=AssOptions(**baseline['options'])
    repaired['health']=check_windows(display_chunks(copy.deepcopy(karaoke),options))
    acceptance=assess(repaired,lyrics,baseline['audio_duration'])
    after=audit(selected,new_evidence,baseline['audio_duration'])
    # Repeated/unmatched transcript is a review queue, not an automatic lyric insertion.
    unmatched=[dict(start=s['start'],end=s['end'],text=s['text'])for s in segments if not any(a['start']<=s['end'] and a['end']>=s['start'] for a in anchors)]
    anchor_rows={a['row']for a in anchors}
    unresolved=[dict(row=i+1,text=l['raw'],start=l['start'],end=l['end'])for i,l in enumerate(selected)if i+1 not in anchor_rows]
    from repair_validation import validate_render
    rendered=validate_render(repaired,new_evidence,anchor_rows)
    gate=rendered['gate']
    report=dict(version=VERSION,report_version=REPORT_VERSION,fingerprint=identity,source=str(job),output=str(output),
                before=audit(baseline['lines'],old_evidence,baseline['audio_duration']),after=after,
                changed_rows=[d['row']for d in decisions if d.get('accepted')],decisions=decisions,
                anchor_rows=sorted(anchor_rows),unanchored=unresolved,unmatched_transcript=unmatched,
                rendered=rendered,render_gate=bool(gate),human_approved=False,status='ready_for_review' if gate else 'needs_repair',
                limitation='自动转写锚点与模型概率不是人工真值；未自动增删歌词、改审批或覆写原成品')
    # Guard against inputs changing during a long run.
    if fingerprint(paths,dict(version=VERSION,models=models,device=engine.device))!=identity:
        raise RuntimeError('处理期间输入已变化，候选未发布，请重新运行')
    atomic(output/'align.json',repaired);atomic(output/'alignment_evidence.json',dict(lines=new_evidence))
    atomic(output/'acceptance.json',acceptance);atomic(output/'input_lyrics.json',lyrics)
    options=AssOptions(**baseline['options'])
    (output/'normal.ass').write_text(build_ass(copy.deepcopy(karaoke),options,1920,1080),encoding='utf-8-sig')
    (output/'normal.srt').write_text(build_srt(display_chunks(copy.deepcopy(karaoke),options)),encoding='utf-8-sig')
    asr=[AsrLine(l['raw'],l['start'],l['end'],[Segment(t['disp'],t['start'],t['end'])for t in l['tokens']])for l in selected]
    (output/'whisper_lyrics.lrc').write_text(to_enhanced_lrc(asr),encoding='utf8')
    atomic(output/'repair-report.json',report)
    progress('job_done',track=job.name,changed=len(report['changed_rows']),blocking=len(after['blocking_rows']),duration=len(after['duration_rows']))
    return report


def main():
    import faulthandler
    faulthandler.enable()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs-root',type=Path,required=True)
    parser.add_argument('--output-root',type=Path,required=True)
    parser.add_argument('--models',nargs='+',default=['large-v3','medium'])
    parser.add_argument('--device',choices=['cpu','cuda'],default='cuda')
    parser.add_argument('--only',nargs='*',type=int)
    parser.add_argument('--anchors-root',type=Path)
    args=parser.parse_args()
    root=args.jobs_root.resolve();out=args.output_root.resolve()
    if root==out or out.is_relative_to(root):raise ValueError('输出目录必须独立于输入目录')
    out.mkdir(parents=True,exist_ok=True);(out/'.history-ignore').touch()
    jobs=sorted(p for p in root.iterdir() if p.is_dir() and (p/'align.json').is_file())
    if args.only:jobs=[p for p in jobs if int(p.name.split('_')[0])in args.only]
    # Prioritise blocking songs, then remaining songs in their original order.
    jobs.sort(key=lambda p:(read(p/'acceptance.json',{}).get('status')!='needs_fix',p.name))
    def progress(phase,**details):
        record=dict(phase=phase,time=time.time(),**details)
        atomic(out/'progress.json',record);print(json.dumps(record,ensure_ascii=False),flush=True)
    reports=[];failures=[];engine=Engine(args.device)
    with FileLock(str(out/'.repair.lock')),FileLock(str(model_paths.ROOT/'out/gpu-processing.lock')):
        try:
            for job in jobs:
                progress('job_start',track=job.name)
                anchors=args.anchors_root/(job.name+'.json') if args.anchors_root else None
                try:
                    reports.append(repair_job(job,out/job.name,args.models,engine,progress,anchors if anchors and anchors.is_file() else None))
                except Exception as error:
                    failure=dict(track=job.name,error=str(error),type=type(error).__name__)
                    failures.append(failure);atomic(out/'failures.json',failures)
                    progress('job_error',**failure);engine.close();continue
                atomic(out/'summary.json',[dict(track=Path(r['source']).name,changed=len(r['changed_rows']),
                    before_blocking=len(r['before']['blocking_rows']),after_blocking=len(r['after']['blocking_rows']),
                    before_duration=len(r['before']['duration_rows']),after_duration=len(r['after']['duration_rows']),
                    render_gate=r['render_gate'])for r in reports])
        finally:engine.close()
    atomic(out/'failures.json',failures)
    progress('complete' if not failures else 'complete_with_errors',tracks=len(reports),failed=len(failures))


if __name__=='__main__':main()

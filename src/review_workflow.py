"""Connect approved delivery files to isolated, editable karaoke workspaces."""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from filelock import FileLock
from local_history import identify


def read_links(exports):
    path=Path(exports)/'workflow-links.json'
    return json.loads(path.read_text(encoding='utf8')) if path.is_file() else {}


def attach_links(data, exports, native_root):
    links=read_links(exports)
    for track in data['tracks']:
        if track.get('native'):
            directory=Path(native_root)/track['track']
            metadata=json.loads((directory/'job.json').read_text(encoding='utf8')) if (directory/'job.json').is_file() else {}
            track['workflow']={'editable':bool(metadata),'job':identify(directory,Path(native_root).parent),
                               **metadata.get('review_origin',{})}
        else:
            link=links.get(track['id'],{})
            track['workflow']={'editable':bool(link.get('versions')),
                               **{k:link[k] for k in ('source','source_start','concert_id') if k in link}}
            for file in track['files']:
                version=link.get('versions',{}).get(file.get('revision') or 'baseline',{})
                context={**link,**version}
                file['workflow']={'editable':bool(version),
                                  **{k:context[k] for k in ('source','source_start','concert_id') if k in context}}


def editor_workspace(track, selected, exports, native_root):
    """Create only after a deliberate edit action; never overwrite delivered media."""
    native_root=Path(native_root).resolve()
    out=native_root.parent
    if track.get('native'):
        directory=(native_root/track['track']).resolve()
        if not directory.is_relative_to(native_root) or not (directory/'job.json').is_file():
            raise ValueError('此歌曲没有可编辑的工作区')
        return {'job':identify(directory,out),'version':selected['native_version'],'track_id':track['id']}
    link=read_links(exports).get(track['id'],{})
    version=link.get('versions',{}).get(selected.get('revision') or 'baseline')
    if not version:
        raise ValueError('当前版本没有对应的原文对齐数据，请选择其他完整成品')
    def local_file(relative):
        path=(out/relative).resolve()
        if not path.is_relative_to(out) or not path.is_file():
            raise ValueError('编辑所需的本地素材不存在或超出项目输出目录')
        return path
    alignment=local_file(version['align'])
    media=local_file(version['media'])
    # The editor changes the original lyric timeline; choose its matching vocal
    # preview, regardless of whether the user was previewing a bilingual variant.
    candidates=[f for f in track['files'] if f['exists'] and f.get('variant')==1
                and f.get('revision')==selected.get('revision')]
    if not candidates:
        raise ValueError('缺少相同时间轴的原文带人声预览')
    preview=next((f for f in candidates if f['name'].endswith('.mp4')),candidates[0])
    video=Path(preview['path']).resolve()
    if not video.is_relative_to(Path(exports).resolve()) or not video.is_file():
        raise ValueError('预览视频不在成品目录')
    stamp=f"{track['id']}|{alignment}|{alignment.stat().st_mtime_ns}|{preview['signature']}"
    name='review-'+hashlib.sha256(stamp.encode('utf8')).hexdigest()[:20]
    directory=native_root/name
    native_root.mkdir(parents=True,exist_ok=True)
    with FileLock(str(native_root/('.'+name+'.lock'))):
        if not (directory/'job.json').is_file():
            import pipeline
            align=json.loads(alignment.read_text(encoding='utf8'))
            info=pipeline.probe_media(media)
            if abs(float(info['duration'])-float(align['audio_duration']))>1:
                raise ValueError('素材与对齐时间轴的时长不一致，不能直接进入编辑')
            context={**link,**version}
            origin={'track_id':track['id'],'file_id':selected['id'],
                    **{k:context[k] for k in ('source','source_start','concert_id') if k in context}}
            mode='soft' if video.suffix.lower()=='.mkv' else 'burn'
            metadata=dict(job=name,display_title=track['title']+' · 修改工作区',media=str(media),media_info=info,
                          out_audio=None,align_source=str(media),lang=align['lang'],device='cuda',
                          encoder_choice='auto',quality=20,output_mode=mode,options=align['options'],review_origin=origin)
            align.update(job=name,media=str(media),align_source=str(media),output_mode=mode)
            temp=Path(tempfile.mkdtemp(prefix='.'+name+'-',dir=native_root))
            try:
                # Hard links share immutable delivery bytes; render always writes v1+.
                dest=temp/('review_karaoke'+video.suffix.lower())
                try:os.link(video,dest)
                except OSError:shutil.copy2(video,dest)
                for file_name,value in [('align.json',align),('job.json',metadata),
                                        ('retry_config.json',{'model':'medium','device':'cuda'}),
                                        ('review-origin.json',origin)]:
                    (temp/file_name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
                for source_name,target in [('normal.ass','karaoke.ass'),('normal.srt','lyrics.srt')]:
                    choices=[alignment.parent/source_name,alignment.parent/target,video.parent/source_name]
                    path=next((p for p in choices if p.is_file()),None)
                    if path is not None:shutil.copy2(path,temp/target)
                if directory.exists():
                    raise ValueError('编辑目录不完整，请保留现场检查后再继续')
                temp.replace(directory)
            finally:
                if temp.exists() and temp.is_relative_to(native_root) and temp.name.startswith('.'+name+'-'):
                    shutil.rmtree(temp)
    return {'job':identify(directory,out),'version':0,'track_id':'native/'+name,'origin_track_id':track['id']}

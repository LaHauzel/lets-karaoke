"""Read-only adapter for videos produced by the normal karaoke workflow."""
import hashlib
import json
import re
from pathlib import Path


def append_native(catalog, allowed, root):
    root = Path(root).resolve()
    if not root.is_dir():
        return
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith('.') or not directory.resolve().is_relative_to(root):
            continue
        try:
            track = describe(directory, root, allowed)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            catalog['diagnostics'].append(f'项目成品读取失败：{directory.name}：{exc}')
            continue
        if not track:
            continue
        catalog['tracks'].append(track)
        catalog['projects']['native'] = '项目生成的歌曲'
        summary = catalog['summary']
        summary['tracks'] += 1
        summary['grades']['unknown'] = summary['grades'].get('unknown', 0)+1
        for file in track['files']:
            summary['files'] += 1
            summary['bytes'] += file['bytes']
            summary['soft' if file['kind']=='soft' else 'hard'] += 1


def describe(directory, root, allowed):
    def read(path, default):
        return json.loads(path.read_text(encoding='utf8')) if path.is_file() else default

    def asset(path, kind, **extra):
        path = path.resolve()
        if not path.is_relative_to(root):
            raise ValueError('项目成品路径超出任务目录')
        stat = path.stat()
        identifier = hashlib.sha256(('native/'+path.relative_to(root).as_posix()).encode('utf8')).hexdigest()[:24]
        allowed[identifier] = path
        return dict(id=identifier,path=str(path),url='/review-native/'+identifier,name=path.name,
                    kind=kind,variant=None,exists=True,bytes=stat.st_size,
                    signature=f'{stat.st_size}:{stat.st_mtime_ns}',**extra)

    versions = []
    supporting = []
    for path in directory.glob('align*.json'):
        match = re.fullmatch(r'align(?:_v(\d+))?\.json', path.name)
        if not match or not path.resolve().is_relative_to(root):
            continue
        version = int(match[1] or 0)
        suffix = f'_v{version}' if version else ''
        details = read(path, {})
        extension = '.mkv' if details.get('output_mode')=='soft' else '.mp4'
        videos = sorted(directory.glob(f'*_karaoke{suffix}{extension}'))
        if not videos:
            videos = sorted(directory.glob(f'*_karaoke{suffix}.mkv'))
        if not videos and not version and (directory/'karaoke.mp4').is_file():
            videos = [directory/'karaoke.mp4']
        if not videos:
            continue
        video = videos[0]
        file = asset(video,'soft' if video.suffix=='.mkv' else 'generated',
                     native_version=version,revision=f'native-v{version}',
                     duration=float(details.get('audio_duration',0)),timeline_offset=0)
        versions.append((version,file,details))
        for name,kind in [(path.name,'report'),(f'karaoke{suffix}.ass','subtitle'),(f'lyrics{suffix}.srt','subtitle')]:
            if (directory/name).is_file():
                supporting.append(asset(directory/name,kind))
    if not versions:
        return None
    versions.sort(key=lambda row:row[0])
    version,latest,align = versions[-1]
    base = versions[0][2]

    def evidence(details):
        return [dict(row=i,text=line.get('raw',''),start=line['start'],end=line['end'],score=None,disagreement=None,
                     priority='建议复核',reasons='没有独立的模型概率记录；请按视频听看复核')
                for i,line in enumerate(details.get('lines',[]),1)]

    metadata = read(directory/'job.json',{})
    title = metadata.get('display_title') or Path(metadata.get('media') or align.get('media') or directory.name).stem
    # Each saved video keeps its own row numbering, including intermediate edits.
    files = [{**f, 'evidence':evidence(details)} for _,f,details in reversed(versions)]
    for f in files:
        f['previous_release'] = f['id']!=latest['id']
    quality = dict(grade='unknown',score=None,high_rows=[],low_rows=[],total=len(align.get('lines',[])))
    baseline = {**quality,'total':len(base.get('lines',[]))}
    revision = dict(revision=f'native-v{version}',summary=f'项目生成版本 {version}',
                    evidence=evidence(align),total=quality['total']) if version else None
    return dict(id='native/'+directory.name,project='native',concert='项目生成的歌曲',
                track=directory.name,title=title,number='',native=True,**quality,
                translation_flags=[],acceptance=dict(status='review_required',label='请人工听看复核',issues=[]),
                health=align.get('health',{}),evidence=evidence(base),baseline_quality=baseline,
                files=files,supporting=supporting,reviewed_rows=0,translation_notes={},
                experiment=None,revision=revision,audit=None,default_file=latest['id'],duration=latest['duration'])

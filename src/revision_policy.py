"""Conservative timeline transformations used by delivery revisions.

Human approval belongs to a particular file. Boundary warnings are review
requests, never permission to delete an intro or instrumental outro.
"""
import copy
import hashlib
import json
from pathlib import Path


def revision_fingerprint(files, parameters):
    sources = []
    for raw in files:
        path = Path(raw).resolve()
        stat = path.stat()
        sources.append((str(path), stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps({'sources':sources, 'parameters':parameters},
                                    sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest()


def shift_alignment(align, start, duration):
    """Trim a clip and shift all token/line times; refuse to cut sung tokens."""
    if start < -5 or duration <= 0:
        raise ValueError('Invalid revision bounds')
    result = copy.deepcopy(align)
    for line in result['lines']:
        for token in line['tokens']:
            if token['start'] < start-.1 or token['end'] > start+duration+.1:
                raise ValueError('Requested cut crosses aligned lyrics: '+line['raw'])
            token['start'] = max(0, round(token['start']-start, 4))
            token['end'] = min(duration, round(token['end']-start, 4))
        line['start'] = line['tokens'][0]['start']
        line['end'] = line['tokens'][-1]['end']
    result['audio_duration'] = duration
    return result


def boundary_warnings(align, approved=False):
    tokens = [t for line in align['lines'] for t in line['tokens']]
    if not tokens:
        return ['没有对齐歌词，不能自动判断歌曲边界']
    warnings = []
    if min(t['start'] for t in tokens) > 60:
        warnings.append('首句前超过 60 秒；需区分 MC、前奏和漏词')
    if align['audio_duration']-max(t['end'] for t in tokens) > 45:
        warnings.append('末句后超过 45 秒；需区分尾奏、谢幕和下一首')
    if approved and warnings:
        warnings.append('已有人工通过记录，保留已确认边界')
    return warnings

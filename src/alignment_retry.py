"""Bounded local retries. Reliable neighbours and untouched rows are immutable."""
import copy
import math
from pathlib import Path
import subprocess
import tempfile

from alignment_review import normalized


def valid_candidate(line, text, left, right):
    if normalized(line.text) != normalized(text) or not line.segments:
        return False
    times = [line.start, line.end, line.prob]
    times.extend(v for t in line.segments for v in (t.start, t.end))
    if not all(math.isfinite(v) for v in times):
        return False
    if not left <= line.start < line.end <= right or not .3 <= line.end-line.start <= 15:
        return False
    if normalized(''.join(t.text for t in line.segments)) != normalized(text):
        return False
    prev = line.start
    for token in line.segments:
        if token.start < prev-.001 or not line.start-.001 <= token.start < token.end <= line.end+.001:
            return False
        prev = token.end
    return True


def infer_clip(source, text, left, right, language, model, device, progress):
    from whisper_align import align_words, build_lines, DEFAULT_RULES
    from alignment_policy import resolve_rules
    with tempfile.TemporaryDirectory(prefix='karaoke_retry_') as temp:
        clip = Path(temp)/'clip.wav'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-ss',str(left),'-i',str(source),
                        '-t',str(right-left),'-vn','-ar','16000','-ac','1',str(clip)],
                       check=True, capture_output=True, timeout=90)
        words = align_words(clip, text, language=language, model_size=model,
                            device=device, progress=progress)
        rows, report = build_lines(words, [text], resolve_rules(DEFAULT_RULES, profile='automatic'))
    if len(rows) != 1 or report['row_index'] != [0]:
        return None
    result = rows[0]
    result.start += left
    result.end += left
    for token in result.segments:
        token.start += left
        token.end += left
    return result


def retry_lines(lines, evidence, sources, duration, language, model='large-v3', device='cuda',
                progress=lambda f,m: None, cancel=lambda: False, infer=None, max_lines=3):
    """At most 3 isolated suspect rows x 2 sources x one 20s window per call.

    Adjacent suspect rows are deliberately deferred: they provide no safe anchor.
    """
    infer = infer or infer_clip
    max_lines = min(3, max(0, int(max_lines)))
    result = list(lines)
    updated = copy.deepcopy(evidence)
    records = updated.get('lines', [])
    report = {'attempted': 0, 'accepted': 0, 'items': [], 'limit': max_lines}
    candidates = [i for i, r in enumerate(records) if r.get('reasons')]
    risk = set(candidates)
    sources = list(dict.fromkeys(str(p) for p in sources if p and Path(p).is_file()))[:2]
    for i in candidates:
        item = {'row': records[i]['row'], 'accepted': False}
        report['items'].append(item)
        if report['attempted'] >= max_lines:
            item['reason'] = '达到本轮重试上限'
            continue
        if i >= len(lines) or any(j in risk for j in (i-1, i+1)):
            item['reason'] = '相邻句也有风险，先人工确认锚点'
            continue
        neighbours = [j for j in (i-1,i+1) if 0 <= j < len(records)]
        if any(records[j].get('confidence') is None or records[j]['confidence'] < .6 for j in neighbours):
            item['reason'] = '相邻句缺少可靠声学依据，请先用人工锚点'
            continue
        old = lines[i]
        left = lines[i-1].end if i else 0.0
        right = lines[i+1].start if i+1 < len(lines) else duration
        if not sources or not all(math.isfinite(v) for v in (left,right)) or not 0 <= left < right <= duration or right-left > 20:
            item['reason'] = '缺少声源或安全窗口超过20秒'
            continue
        report['attempted'] += 1
        item['window'] = [left, right]
        item['before'] = {'start': old.start, 'end': old.end, 'confidence': old.prob}
        best = None
        for source in sources:
            if cancel():
                raise RuntimeError('cancelled')
            progress(0, f'局部重试第 {records[i]["row"]} 句，窗口 {left:.2f}–{right:.2f}s')
            try:
                candidate = infer(source, old.text, left, right, language, model, device, progress)
            except Exception as exc:
                if cancel():
                    raise RuntimeError('cancelled') from exc
                item.setdefault('errors', []).append(type(exc).__name__)
                continue
            if candidate and valid_candidate(candidate, old.text, left, right):
                if best is None or candidate.prob > best.prob:
                    best = candidate
        # Do not accept confidence-only gains when a candidate breaks structure,
        # crosses an anchor, drops text, or merely reproduces existing timings.
        changed = best and max(abs(best.start-old.start), abs(best.end-old.end)) >= .02
        gain = best and best.prob >= .6 and best.prob >= old.prob+.1
        structural_gain = best and not valid_candidate(old, old.text, left, right) and best.prob >= max(.6, old.prob)
        if best and changed and (gain or structural_gain):
            result[i] = best
            report['accepted'] += 1
            item.update(accepted=True, reason='文本、窗口、结构检查通过且证据改善',
                        after={'start':best.start,'end':best.end,'confidence':best.prob})
            records[i] = {'row':records[i]['row'], 'text':best.text, 'start':best.start, 'end':best.end,
                          'confidence':best.prob, 'reasons':['局部重试已替换，建议试听确认'],
                          'priority':'建议复核', 'measurement':'local_retry'}
        else:
            item['reason'] = '候选未通过保守替换条件，保留原结果'
    updated['retry'] = report
    return result, updated, report

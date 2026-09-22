"""Version-specific acceptance checks; no score is an accuracy estimate."""
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import unicodedata


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def assess(align, expected=None, duration=None):
    lines = align.get('lines', [])
    diagnostics = align.get('diagnostics', {})
    issues = []
    missing = []
    if expected is not None:
        actual = [normalized(l.get('raw', '')) for l in lines]
        reference = [normalized(s) for s in expected]
        matcher = SequenceMatcher(None, reference, actual, autojunk=False)
        for tag, a, b, c, d in matcher.get_opcodes():
            if tag in ('delete', 'replace'):
                missing.extend({'row': i+1, 'text': expected[i]} for i in range(a, b))
            if tag in ('insert', 'replace'):
                issues.append({'code': 'text_changed', 'severity': 'review',
                               'rows': list(range(c+1, d+1)), 'message': '存在新增或不匹配歌词，请确认文本与顺序'})
    else:
        missing = diagnostics.get('missing_input_rows', [])
    if missing:
        issues.append({'code': 'missing_lyrics', 'severity': 'error',
                       'rows': [r['row'] for r in missing], 'message': f'{len(missing)} 行输入歌词未按原顺序完整输出'})
    if not lines:
        issues.append({'code': 'empty', 'severity': 'error', 'rows': [], 'message': '没有输出歌词'})
    for i, line in enumerate(lines):
        start, end = line.get('start'), line.get('end')
        finite = all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end))
        if not finite or not 0 <= start < end or (duration is not None and end > duration+.05):
            issues.append({'code': 'invalid_boundary', 'severity': 'error', 'rows': [i+1], 'message': '起止无效或超出音频'})
            continue
        if end-start < .3 or end-start > 15:
            issues.append({'code': 'unusual_duration', 'severity': 'review', 'rows': [i+1], 'message': '句子时长异常，请试听确认'})
        if i and isinstance(lines[i-1].get('end'), (int, float)) and lines[i-1]['end'] > start+.001:
            issues.append({'code': 'overlap', 'severity': 'error', 'rows': [i, i+1], 'message': '相邻句时间重叠'})
        tokens = line.get('tokens', [])
        prev = start
        for token in tokens:
            a, b = token.get('start'), token.get('end')
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (a, b)) or not start-.001 <= a < b <= end+.001 or a < prev-.001:
                issues.append({'code': 'invalid_token', 'severity': 'error', 'rows': [i+1], 'message': '字词时间无效、越界或重叠'})
                break
            prev = b
        if not tokens:
            issues.append({'code': 'no_tokens', 'severity': 'error', 'rows': [i+1], 'message': '没有字词时间轴'})
    risk_rows = [r['row'] for r in diagnostics.get('lines', []) if r.get('reasons')]
    if risk_rows:
        issues.append({'code': 'acoustic_risk', 'severity': 'review', 'rows': risk_rows, 'message': '声学或结构依据触发复核提示'})
    measured = sum(r.get('confidence') is not None for r in diagnostics.get('lines', []))
    if measured < len(lines):
        issues.append({'code': 'evidence_missing', 'severity': 'review', 'rows': [], 'message': '部分句子缺少当前版本声学依据'})
    if expected is None:
        issues.append({'code': 'input_unknown', 'severity': 'review', 'rows': [], 'message': '原始输入文本不可用，完整性尚未核实'})
    errors = sum(i['severity'] == 'error' for i in issues)
    return {'status': 'needs_fix' if errors else 'needs_review' if issues else 'ready_for_spot_check',
            'label': '需要处理' if errors else '建议复核' if issues else '可进入抽查',
            'input_lines': len(expected) if expected is not None else None,
            'output_lines': len(lines), 'acoustic_lines': measured,
            'missing_input_rows': missing, 'issues': issues,
            'note': '自动验收只检查已知风险，不等于人工确认或准确率。'}


def attach_review(directory, align, persist=None):
    from alignment_diagnostics import attach
    directory = Path(directory)
    evidence = directory / 'alignment_evidence.json'
    # Edited versions may carry freshly measured retry evidence, never inherit
    # acoustic scores for rows whose timing was changed by hand.
    saved = align.get('review_evidence')
    attach(align, json.loads(evidence.read_text(encoding='utf-8')) if evidence.exists() else {})
    if saved:
        # Retry evidence belongs to exactly this version, including rounding
        # when serializing the alignment. Reject a stale edited snapshot.
        rows = saved.get('lines', [])
        current = align.get('lines', [])
        if len(rows) == len(current) and all(
                normalized(a.get('text', '')) == normalized(b.get('raw', '')) and
                all(abs(a[k]-b[k]) <= .00011 for k in ('start', 'end'))
                for a, b in zip(rows, current)):
            align['diagnostics'] = saved
    inputs = directory / 'input_lyrics.json'
    expected = json.loads(inputs.read_text(encoding='utf-8')) if inputs.exists() else None
    duration = align.get('audio_duration')
    if duration is None and (directory/'job.json').exists():
        duration = json.loads((directory/'job.json').read_text(encoding='utf-8')).get('media_info', {}).get('duration')
    align['acceptance'] = assess(align, expected, duration)
    # Structural failures must also reach the per-line UI and retry selector.
    for issue in align['acceptance']['issues']:
        if issue['code'] not in ('invalid_boundary', 'invalid_token', 'no_tokens', 'unusual_duration', 'overlap'):
            continue
        for row in issue['rows']:
            if 0 < row <= len(align['diagnostics'].get('lines', [])):
                diagnostic = align['diagnostics']['lines'][row-1]
                reasons = diagnostic.setdefault('reasons', [])
                if issue['message'] not in reasons:
                    reasons.append(issue['message'])
                diagnostic['priority'] = '重点复核'
    align['diagnostics']['missing_input_rows'] = align['acceptance']['missing_input_rows']
    if persist:
        Path(persist).write_text(json.dumps(align['acceptance'], ensure_ascii=False, indent=2), encoding='utf-8')
    return align

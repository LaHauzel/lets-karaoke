"""Version-specific acceptance checks; no score is an accuracy estimate."""
from difflib import SequenceMatcher
import copy
import json
import math
from pathlib import Path
import unicodedata


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def lyric_reference(directory, align, _parents=None):
    """Use the saved version's explicit reference, otherwise original input.

    Never derive an expected transcript from the output: that would hide rows
    an aligner omitted. Original input remains immutable for audit/history.
    """
    reference = align.get('acceptance_reference')
    if reference is not None:
        if (not isinstance(reference, dict) or not isinstance(reference.get('lines'), list)
                or any(not isinstance(line, str) for line in reference['lines'])):
            raise ValueError('版本验收歌词基准无效')
        return copy.deepcopy(reference)
    directory = Path(directory)
    inputs = directory / 'input_lyrics.json'
    reference = None
    if inputs.exists():
        lines = json.loads(inputs.read_text(encoding='utf-8'))
        if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
            raise ValueError('原始输入歌词基准无效')
        reference = {'source': 'input_lyrics', 'lines': lines}
    # Older edited versions did not persist references. Reconstruct only from
    # their recorded parent and explicit edit requests, not their output text.
    if (not align.get('version') and not align.get('line_texts')
            and not align.get('line_insertion') and not align.get('line_insertions')):
        return reference
    parent_version = align.get('base_version', 0)
    if isinstance(parent_version, bool) or not isinstance(parent_version, int) or parent_version < 0:
        raise ValueError('验收基准父版本无效')
    parent_path = directory / (f'align_v{parent_version}.json' if parent_version else 'align.json')
    if not parent_path.is_file():
        return reference
    parents = set(_parents or ())
    if parent_path in parents:
        raise ValueError('验收基准版本链存在循环')
    parents.add(parent_path)
    parent = json.loads(parent_path.read_text(encoding='utf-8'))
    reference = lyric_reference(directory, parent, parents)
    actual = [line.get('raw', '') for line in parent.get('lines', [])]
    for insertion in align.get('line_insertions') or []:
        reference = edit_reference(reference, actual, insertion=insertion)
        actual.insert(insertion['after_row'] + 1, insertion['text'].strip())
    reference = edit_reference(reference, actual, align.get('line_texts'), align.get('line_insertion'))
    if reference is not None:
        reference['base_version'] = parent_version
    return reference


def edit_reference(reference, actual, line_texts=None, insertion=None):
    """Apply only explicit user changes to a reference that may contain gaps."""
    if reference is None:
        return None
    result = copy.deepcopy(reference)
    expected = result['lines']
    actual = list(actual)

    def positions():
        matcher = SequenceMatcher(None, [normalized(s) for s in expected],
                                  [normalized(s) for s in actual], autojunk=False)
        return {c + offset: a + offset
                for tag, a, b, c, d in matcher.get_opcodes() if tag == 'equal'
                for offset in range(b - a)}

    mapping = positions()
    for key, text in (line_texts or {}).items():
        row = int(key)
        if row in mapping:
            expected[mapping[row]] = text.strip()
        # Unmatched output is not permission to replace a missing input row.
        # If the edit restores that row's original text, it will naturally
        # match the unchanged reference during acceptance.
        actual[row] = text.strip()
    if insertion:
        after = insertion['after_row']
        text = insertion['text'].strip()
        mapping = positions()
        left = max((mapping[row] + 1 for row in mapping if row <= after), default=0)
        right = min((mapping[row] for row in mapping if row > after), default=len(expected))
        candidate = actual[:after + 1] + [text] + actual[after + 1:]
        remaining = iter(normalized(value) for value in expected)
        restores_occurrence = all(any(value == normalized(lyric) for value in remaining)
                                  for lyric in candidate)
        # Filling a previously omitted input row must not duplicate that row
        # in the reference. Truly additional lyrics enter at the user-selected
        # position while all still-missing original lyrics remain expected.
        if (not restores_occurrence
                and not any(normalized(value) == normalized(text) for value in expected[left:right])):
            expected.insert(left, text)
    if line_texts or insertion:
        result['source'] = 'user_edits'
    return result


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
        # A full lyric line that disappears within a few frames is unusable in
        # the rendered video. Keep short interjections as review items.
        flash = end-start < .35 and len(normalized(line.get('raw', ''))) >= 5
        if flash or end-start < .3 or end-start > 15:
            issues.append({'code': 'unusual_duration',
                           'severity': 'error' if flash else 'review',
                           'rows': [i+1],
                           'message': ('整句歌词一闪而过，需要重新对齐' if flash
                                       else '句子时长异常，请试听确认')})
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
    interpolated = align.get('mapping', {}).get('interpolated_zero_tokens', 0)
    if interpolated:
        issues.append({'code': 'interpolated_tokens', 'severity': 'review',
                       'rows': align.get('mapping', {}).get('interpolated_rows', []),
                       'message': f'{interpolated} 个坍缩字词只做了局部时间插值，请试听确认'})
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
    reference = lyric_reference(directory, align)
    expected = reference['lines'] if reference is not None else None
    duration = align.get('audio_duration')
    if duration is None and (directory/'job.json').exists():
        duration = json.loads((directory/'job.json').read_text(encoding='utf-8')).get('media_info', {}).get('duration')
    align['acceptance'] = assess(align, expected, duration)
    align['acceptance']['reference_source'] = reference.get('source') if reference else None
    # Structural failures must also reach the per-line UI and retry selector.
    for issue in align['acceptance']['issues']:
        if issue['code'] not in ('invalid_boundary', 'invalid_token', 'no_tokens', 'unusual_duration', 'overlap', 'interpolated_tokens'):
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

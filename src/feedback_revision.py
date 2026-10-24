"""Apply explicit, version-bound review decisions without changing approvals.

Times are relative to the reviewed release, never to an earlier source clip.
Reference recording durations are advisory: live arrangements need audio evidence.

The CLI applies one explicit decisions file to one alignment and writes a
separate revised alignment plus its feedback record; it never approves a release.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path

from revision_policy import shift_alignment


def verify_reviewed_file(review):
    path = Path(review['file_path']).resolve()
    stat = path.stat()
    if review.get('file_signature') != f'{stat.st_size}:{stat.st_mtime_ns}':
        raise ValueError('The reviewed file changed; review decisions need rebinding')
    return path


def live_reference(align, order):
    """Expand documented live reprises; never silently drop original lyrics."""
    seen=set();result=copy.deepcopy(align);result['lines']=[]
    for entry in order:
        row=entry['source_row']
        if type(row) is not int or not 1 <= row <= len(align['lines']):
            raise ValueError('Invalid live-reference row')
        if row in seen and not entry.get('reason','').strip():
            raise ValueError('Repeated live lyrics require acoustic evidence')
        result['lines'].append(copy.deepcopy(align['lines'][row-1]));seen.add(row)
    if seen != set(range(1,len(align['lines'])+1)):
        raise ValueError('Use explicit omissions to remove reviewed lyrics')
    return result


def apply_feedback(align, *, start=0, end=None, available_duration=None,
                   omissions=(), replacements=(), quarantine=()):
    """Return an isolated alignment and explicit old/new row mapping.

    Omission entries require row, exact text, and a human evidence reason.
    Replacement entries require row and a measured line of the same text.
    Quarantine retains an unresolved reference row in the report but suppresses
    its untrustworthy display. It always requires further reference review;
    it must never be described as a confirmed omission or a passed release.
    Neither missing ASR text nor a standard song duration authorizes deletion.
    """
    duration = align['audio_duration'] if available_duration is None else available_duration
    end = duration if end is None else end
    if not all(math.isfinite(x) for x in (start, end, duration)) or not 0 <= start < end <= duration + .001:
        raise ValueError('Invalid available media bounds')
    result = copy.deepcopy(align)
    count = len(result['lines'])
    removed, replaced = {}, {}
    for entry in [*omissions,*quarantine]:
        row = entry['row']
        if type(row) is not int or not 1 <= row <= count or row in removed:
            raise ValueError('Invalid or duplicate omission row')
        if entry.get('text') != result['lines'][row-1]['raw'] or not entry.get('reason', '').strip():
            raise ValueError('Suppression requires matching text and documented evidence')
        removed[row] = entry
    quarantined={entry['row'] for entry in quarantine}
    for entry in replacements:
        row = entry['row']
        if type(row) is not int or not 1 <= row <= count or row in removed or row in replaced:
            raise ValueError('Conflicting replacement row')
        line = entry['line']
        if line['raw'] != result['lines'][row-1]['raw']:
            raise ValueError('Timing replacement must preserve the reviewed text')
        result['lines'][row-1] = copy.deepcopy(line)
        replaced[row] = entry
    mapping = []
    result['lines'] = [line for row,line in enumerate(result['lines'], 1) if row not in removed]
    new_row = 0
    for row,line in enumerate(align['lines'], 1):
        if row not in removed:
            new_row += 1
        mapping.append(dict(source_row=row, row=None if row in removed else new_row,
                            text=line['raw'], omitted=row in removed,
                            disposition='unresolved' if row in quarantined else 'confirmed_omission' if row in removed else 'retained',
                            reason=removed[row]['reason'] if row in removed else '',
                            timing_replaced=row in replaced))
    if not result['lines']:
        raise ValueError('Cannot remove every lyric line')
    previous = start
    previous_token = None
    for line in result['lines']:
        if not line['tokens']:
            raise ValueError('A retained line has no measured tokens')
        for token in line['tokens']:
            a,b = token['start'],token['end']
            if not math.isfinite(a) or not math.isfinite(b) or b <= a or a < previous-.03:
                raise ValueError('Non-monotonic or empty measured tokens')
            # Independent crops may overlap by one or two centiseconds. End
            # the preceding highlight at the next measured onset, without
            # moving that onset or stretching either word into silence.
            if previous_token is not None and a < previous:
                if a <= previous_token['start']:
                    raise ValueError('Overlapping tokens have no positive span')
                previous_token['end'] = a
            previous = b
            previous_token = token
    result = shift_alignment(result, start, end-start)
    result['feedback'] = dict(timeline_offset=start, source_duration=align['audio_duration'],
                              available_duration=duration, end=end, rows=mapping,
                              unresolved_rows=[dict(source_row=row,line=copy.deepcopy(align['lines'][row-1]),
                                                    reason=removed[row]['reason'])for row in sorted(quarantined)],
                              requires_reference_review=bool(quarantined),
                              human_approved=False)
    return result


def duration_reference(observed, reference, *, edition, url):
    if not all(math.isfinite(x) and x > 0 for x in (observed, reference)):
        raise ValueError('Invalid reference duration')
    return dict(observed=observed, reference=reference, difference=observed-reference,
                ratio=observed/reference, edition=edition, url=url,
                advisory_only=True, automatic_trim=False)


def _read_json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise ValueError(f'找不到{label}：{path}') from None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'{label} 不是有效的 UTF-8 JSON：{error}') from None


def _write_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                    encoding='utf8')
    temp.replace(path)


def _entries(decisions, key):
    value = decisions.get(key, [])
    if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
        raise ValueError(f'decisions 的 {key} 必须是对象数组')
    return value


def _number(value, name, default=None):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'decisions 的 {name} 必须是数字')
    return value


def _apply(args):
    align_path = Path(args.align).resolve()
    decisions_path = Path(args.decisions).resolve()
    output = Path(args.output).resolve()
    if output == align_path.parent:
        raise ValueError('输出目录必须与 align.json 所在目录不同，请使用独立目录')
    align = _read_json(align_path, 'align.json')
    if not isinstance(align, dict) or not isinstance(align.get('lines'), list) or 'audio_duration' not in align:
        raise ValueError('align.json 缺少 audio_duration 或 lines')
    decisions = _read_json(decisions_path, 'decisions.json')
    if not isinstance(decisions, dict):
        raise ValueError('decisions.json 必须是 JSON 对象')
    review = decisions.get('review')
    if review is not None:
        if not isinstance(review, dict):
            raise ValueError('decisions 的 review 必须是对象')
        verify_reviewed_file(review)
    result = apply_feedback(
        align,
        start=_number(decisions.get('start'), 'start', 0),
        end=_number(decisions.get('end'), 'end'),
        available_duration=_number(decisions.get('available_duration'), 'available_duration'),
        omissions=_entries(decisions, 'omissions'),
        replacements=_entries(decisions, 'replacements'),
        quarantine=_entries(decisions, 'quarantine'),
    )
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / 'align.json', result)
    _write_json(output / 'feedback.json', result['feedback'])
    summary = dict(output=str(output), lines=len(result['lines']),
                   unresolved_rows=[row['source_row'] for row in result['feedback']['unresolved_rows']],
                   requires_reference_review=result['feedback']['requires_reference_review'],
                   human_approved=result['feedback']['human_approved'])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='将人工复核决定应用到一个 align.json，输出独立的修订版与反馈记录（不改变审批）。')
    parser.add_argument('--align', type=Path, required=True, help='已对齐的 align.json')
    parser.add_argument('--decisions', type=Path, required=True,
                        help='人工复核决定 JSON（格式见 docs/feedback-revision.md）')
    parser.add_argument('--output', type=Path, required=True,
                        help='独立输出目录；不能与 align.json 所在目录相同')
    args = parser.parse_args(argv)
    try:
        return _apply(args)
    except (ValueError, OSError) as error:
        print(f'错误：{error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

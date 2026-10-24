"""Validate what libass displays, while retaining raw alignment warnings."""
import copy
from dataclasses import asdict
from ass_builder import AssOptions,KaraokeLine,KaraokeToken,display_chunks,solve_windows,check_windows
from repair_policy import audit,line_issues
from repair_triage import review_queue
from alignment_review import assess


def validate_render(align,evidence,anchor_rows=()):
    duration=align['audio_duration'];raw=audit(align['lines'],evidence,duration)
    options=AssOptions(**align['options'])
    lines=[KaraokeLine(raw=l['raw'],head=l.get('head',''),start=l['start'],end=l['end'],
                       tokens=[KaraokeToken(**t)for t in l['tokens']])for l in align['lines']]
    chunks=display_chunks(copy.deepcopy(lines),options);solve_windows(chunks,options)
    health=check_windows(chunks);blocking=set(raw['blocking_rows']);long=set();details=[];counts={}
    for chunk in chunks:
        row=next((i+1 for i,l in enumerate(lines)if l.start-.002<=chunk.start and chunk.end<=l.end+.002),None)
        reasons=line_issues(asdict(chunk),duration)
        if row is None:
            raise ValueError('显示片段无法映射回歌词行')
        counts[row]=counts.get(row,0)+1
        if any(r not in {'overlong_line','overlong_token','internal_gap'}for r in reasons):blocking.add(row)
        if any(r in {'overlong_line','overlong_token','internal_gap'}for r in reasons):long.add(row)
        if reasons:details.append(dict(row=row,text=chunk.raw,start=chunk.start,end=chunk.end,reasons=reasons))
    reference=sorted({row for item in review_queue(dict(after=raw,anchor_rows=anchor_rows))
                      if item['kind']=='reference_structure'for row in item['rows']})
    acceptance=assess(align,[l['raw']for l in align['lines']],duration)
    unresolved=align.get('feedback',{}).get('unresolved_rows',[])
    gate=not blocking and not long and not reference and not unresolved and health.get('ok') is True and acceptance['status']!='needs_fix'
    return dict(version=1,gate=gate,blocking_rows=sorted(blocking),duration_rows=sorted(long),
                reference_rows=reference,split_rows=sorted(r for r,n in counts.items()if n>1),
                unresolved_source_rows=[r['source_row']for r in unresolved],
                details=details,health=health,
                note='长空档若已拆成正常显示片段并显示圆点，仅保留听看提示；闪句、拖字、重叠和歌词结构疑点仍阻断整曲导出。')

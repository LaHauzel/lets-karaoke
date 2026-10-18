"""Prioritise human checks without treating ASR absence as a missing lyric."""
from statistics import median
from alignment_review import normalized


def review_queue(report):
    rows=report['after']['rows'];anchors=set(report.get('anchor_rows',[]))
    groups=[];group=[]
    for row in rows:
        uncertain=row['row'] not in anchors and row.get('score') is not None and row['score']<.3
        if uncertain:
            group.append(row)
        else:
            if group:groups.append(group)
            group=[]
    if group:groups.append(group)
    result=[];covered=set()
    def add(kind,selected,action):
        indices=[r['row']for r in selected]
        result.append(dict(kind=kind,rows=indices,lyrics=[r['text']for r in selected],
                           start=min(r['start']for r in selected),end=max(r['end']for r in selected),action=action))
        covered.update(indices)
    for group in groups:
        rates=[len(normalized(r['text']))/max(.02,r['end']-r['start'])for r in group]
        if len(group)>=3 and (any('collapsed_line'in r['reasons']for r in group)or median(rates)>12):
            add('reference_structure',group,'先核对现场实际演唱的歌词和顺序，再修时间。连续低分且压缩严重只是疑点，不能据此认定漏唱或删除歌词。')
    for row in rows:
        if row['row'] in report['after']['blocking_rows'] and row['row']not in covered:
            add('timing_blocker',[row],'核对闪现、重叠或字词映射，补充准确的起止时间后局部重对齐。')
    for row in rows:
        if row['row'] in report['after']['duration_rows'] and row['row']not in covered:
            add('duration_check',[row],'听看是否为真实拖长音、观众合唱或误占间奏；确认结束点与下一句起点。')
    return result


LABELS={'reference_structure':'优先核对现场歌词结构','timing_blocker':'修复时间轴阻断','duration_check':'核对长音与空档'}

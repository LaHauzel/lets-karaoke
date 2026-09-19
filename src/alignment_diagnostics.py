"""Explain alignment evidence without treating model scores as accuracy."""
def snapshot(lines, indices):
    return {i: {'start': l.start, 'end': l.end, 'confidence': l.prob} for i, l in zip(indices, lines)}


def explain(lines, primary, alternate, before, indices, intervals, expected_rows=None):
    out=[]
    for line in lines:
        row=indices[id(line)]
        a,b=primary.get(row),alternate.get(row)
        dual=max(abs(a[k]-b[k]) for k in ('start','end')) if a and b else None
        old=before[id(line)]
        shift=max(abs(line.start-old[0]),abs(line.end-old[1]))
        total=sum(max(0,s.end-s.start) for s in line.segments)
        coverage=sum(max(0,min(s.end,y)-max(s.start,x)) for s in line.segments for x,y in intervals)
        outside=max(0,1-coverage/total) if intervals and total else None
        reasons=[]
        if line.prob<.6:reasons.append('模型置信偏低')
        if dual is not None and dual>1:reasons.append('双路边界分歧超过1秒')
        if shift>1:reasons.append('后处理改变边界超过1秒')
        if outside is not None and outside>.2:reasons.append('较多高亮落在低人声能量区')
        out.append(dict(row=row+1,text=line.text,start=line.start,end=line.end,confidence=line.prob,
                        dual_disagreement_s=dual,postprocess_boundary_shift_s=shift,outside_vocal_proxy=outside,
                        reasons=reasons,priority='重点复核' if len(reasons)>=2 else ('建议复核' if reasons else '未触发提示')))
    present={indices[id(l)] for l in lines}
    candidates=range(expected_rows) if expected_rows is not None else sorted(set(primary)|set(alternate))
    return {'lines':out,'source':'initial_alignment','missing_input_rows':[{'row':i+1} for i in candidates if i not in present]}


def attach(align, evidence):
    """Never silently reuse stale acoustic evidence after text/timing edits."""
    old=evidence.get('lines',[]);new=align.get('lines',[])
    norm=lambda s: ''.join(s.split())
    edited=bool(align.get('base_version')) or any(v for v in align.get('offsets',{}).values()) or any(v for v in align.get('token_offsets',{}).values())
    edited = edited or align.get('anchor_row') is not None
    valid=not edited and len(old)==len(new) and all(norm(a.get('text',''))==norm(b.get('raw','')) and
        abs(a['start']-b['start'])<.04 and abs(a['end']-b['end'])<.04 for a,b in zip(old,new))
    edited = edited or bool(align.get('line_bounds'))
    valid = valid and not edited
    if valid and old:
        align['diagnostics'] = evidence
        return align
    # Always provide current per-line structural review. Do not present stale
    # acoustic scores as if the edited version had been measured again.
    rows = []
    for i, line in enumerate(new):
        tokens = line.get('tokens', [])
        reasons = []
        start, end = line['start'], line['end']
        if end <= start or start < 0: reasons.append('句子起止时间无效')
        elif end-start < .3: reasons.append('句子显示时间不足0.3秒')
        if any(t['end'] <= t['start'] for t in tokens): reasons.append('存在非正字词时长')
        if any(a['end'] > b['start']+.001 for a,b in zip(tokens,tokens[1:])): reasons.append('句内字词时间重叠')
        if i and new[i-1]['end'] > start+.001: reasons.append('与上一句时间重叠')
        if any(t['end']-t['start'] > 3 for t in tokens): reasons.append('字词持续超过3秒，请确认拖音')
        if any(t['start'] < start-.001 or t['end'] > end+.001 for t in tokens): reasons.append('字词时间超出句子边界')
        candidate = old[i] if not edited and len(old)==len(new) else None
        usable = candidate is not None and norm(candidate.get('text',''))==norm(line.get('raw','')) and abs(candidate['start']-start)<.04 and abs(candidate['end']-end)<.04
        row = dict(candidate) if usable else {'confidence':None}
        combined = list(dict.fromkeys((row.get('reasons') or []) + reasons))
        row.update(row=i+1,text=line.get('raw',''),start=start,end=end,reasons=combined,
                   priority='重点复核' if len(combined)>=2 else ('建议复核' if combined else '未触发提示'))
        rows.append(row)
    acoustic = sum(r.get('confidence') is not None for r in rows)
    note = (f'{acoustic} 句保留匹配的声学依据；其余句子边界已变化，仅提供结构检查。' if acoustic else
            '当前版本仅提供时间结构检查；人工调整后原始声学依据已失效。' if edited or old else
            '此历史记录未保存声学依据，当前仅提供时间结构检查。')
    align['diagnostics'] = {'lines': rows, 'source':'mixed' if acoustic else 'structural_only',
        'unavailable':note, 'missing_input_rows':evidence.get('missing_input_rows',[]) if not edited else []}
    return align

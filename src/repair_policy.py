"""Model-independent checks and selection for repeatable alignment repair.

Automatic evidence never creates a human approval. Low probability is a reason
to inspect/retry, not permission to delete, stretch or invent lyric occurrences.
"""
from __future__ import annotations

import copy
import math
import re
from difflib import SequenceMatcher

from alignment_review import normalized

VERSION = 1


def lyric_language(text, fallback='ja'):
    if re.search(r'[\u3040-\u30ff\u3400-\u9fff]', text):
        return 'zh' if fallback == 'zh' else 'ja'
    return 'en' if re.search('[A-Za-z]', text) else fallback


def line_issues(line, duration):
    problems = []
    start, end = line.get('start'), line.get('end')
    if not all(isinstance(n, (float, int)) and math.isfinite(n) for n in (start, end)):
        return ['invalid_time']
    if not 0 <= start < end <= duration + .02:
        problems.append('outside_or_invalid_time')
    span = end-start
    if span < .35 and len(normalized(line.get('raw', ''))) >= 5:
        problems.append('collapsed_line')
    if span > 18:
        problems.append('overlong_line')
    tokens = line.get('tokens', [])
    if not tokens or normalized(''.join(t.get('text', t.get('disp', '')) for t in tokens)) != normalized(line.get('raw', '')):
        problems.append('incomplete_tokens')
    previous = start
    for token in tokens:
        a, b = token.get('start'), token.get('end')
        if not all(isinstance(n, (float, int)) and math.isfinite(n) for n in (a, b)):
            problems.append('invalid_tokens'); break
        if not start-.002 <= a < b <= end+.002 or a < previous-.002:
            problems.append('invalid_tokens')
        if b-a > 5:
            problems.append('overlong_token')
        if a-previous > 3:
            problems.append('internal_gap')
        previous = b
    return list(dict.fromkeys(problems))


def audit(lines, evidence, duration):
    records = {r['row']: r for r in evidence}
    rows = []
    for i, line in enumerate(lines):
        reasons = line_issues(line, duration)
        record = records.get(i+1, {})
        score = record.get('confidence', record.get('score'))
        if score is None:
            reasons.append('unmeasured')
        elif score < .6:
            reasons.append('low_score')
        dual = record.get('dual_disagreement_s', record.get('disagreement'))
        if dual is not None and dual > 2:
            reasons.append('route_disagreement')
        if i and lines[i-1]['end'] > line['start']+.002:
            reasons.append('overlap')
        rows.append(dict(row=i+1, text=line['raw'], start=line['start'], end=line['end'],
                         score=score, reasons=reasons))
    hard = {'invalid_time', 'outside_or_invalid_time', 'collapsed_line', 'incomplete_tokens', 'invalid_tokens', 'overlap'}
    return dict(rows=rows, total=len(rows), risk_rows=[r['row'] for r in rows if r['reasons']],
                blocking_rows=[r['row'] for r in rows if hard.intersection(r['reasons'])],
                duration_rows=[r['row'] for r in rows if {'overlong_line','overlong_token','internal_gap'}.intersection(r['reasons'])])


def transcript_anchors(lyrics, segments, baseline):
    """Match unprompted transcript phrases, then choose a monotonic path.

    No lyric text is supplied to ASR. Adjacent short segments may together match
    one line; matches from repeated choruses remain separate occurrences.
    """
    candidates = []
    # Overlapping ASR windows and two audio routes must not interrupt the
    # neighbouring phrases within a single decode. Keep each stream separate.
    streams = {}
    for segment in segments:streams.setdefault(segment.get('stream','legacy'),[]).append(segment)
    phrases=[]
    for stream in streams.values():
        stream=sorted(stream,key=lambda s:(s['start'],s['end']))
        for pos in range(len(stream)):
            group=[]
            for seg in stream[pos:pos+3]:
                if group and seg['start']<group[-1]['end']-.5:break
                group.append(seg)
                if seg['end']-group[0]['start']>25:break
                phrases.append(list(group))
    for row, text in enumerate(lyrics):
        ref = normalized(text)
        if len(ref) < 5:
            continue
        seen = set()
        for group in phrases:
            if group:
                hypothesis = ''.join(normalized(s.get('text', '')) for s in group)
                if not hypothesis or len(hypothesis) > max(120,6*len(ref)):
                    continue
                blocks = SequenceMatcher(None, ref, hypothesis, autojunk=False).get_matching_blocks()
                matched = sum(b.size for b in blocks if b.size >= 2)
                coverage = matched/len(ref)
                indices = [b.b+j for b in blocks if b.size >= 2 for j in range(b.size)]
                # ASR sentence breaks rarely coincide with lyric breaks.
                # Score the matched span, not unrelated words on either side.
                precision = matched/(max(indices)-min(indices)+1) if indices else 0
                if coverage < .72 or precision < .48 or matched < 5:
                    continue
                # Word timestamps narrow a segment containing two lyric lines.
                positions = [];word_sizes=[]
                for s in group:
                    for word in s.get('words', []):
                        size=len(normalized(word.get('word','')))
                        positions.extend([(word['start'], word['end'])]*size);word_sizes.extend([size]*size)
                start, end = group[0]['start'], group[-1]['end']
                partial_start=False
                if len(positions) == len(hypothesis) and indices:
                    # A leading pronoun may absorb several seconds of silence.
                    # Keep the remaining lexical anchor, explicitly partial;
                    # accepting it later also requires cross-model agreement.
                    first_time=positions[min(indices)]
                    if first_time[1]-first_time[0]>5 and word_sizes[min(indices)]<=2:
                        indices=[j for j in indices if positions[j]!=first_time];partial_start=True
                    if not indices:continue
                    start, end = positions[min(indices)][0], positions[max(indices)][1]
                    matched_times=[positions[j]for j in indices]
                    if any(b-a>5 or b<=a for a,b in matched_times):
                        continue
                if not .35 <= end-start <= 22:
                    continue
                identity = (round(start, 1), round(end, 1))
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(dict(row=row+1, start=start, end=end, match=coverage,
                                       precision=precision, text=text, heard=' '.join(s['text'] for s in group),
                                       source='unprompted_asr',partial_start=partial_start))
    candidates.sort(key=lambda c: (c['row'], c['start']))
    scores, predecessors = [], []
    for i, current in enumerate(candidates):
        possible = [j for j, other in enumerate(candidates[:i])
                    if other['row'] < current['row'] and other['end'] <= current['start']+.12]
        previous = max(possible, key=lambda j: scores[j]) if possible else None
        old = baseline[current['row']-1]
        # A small tie-break toward existing timing cannot outweigh textual coverage.
        value = 1+current['match']+.2*current['precision']-.001*min(100, abs(current['start']-old['start']))
        scores.append(value+(scores[previous] if previous is not None else 0))
        predecessors.append(previous)
    anchors = []
    if scores:
        at = max(range(len(scores)), key=scores.__getitem__)
        while at is not None:
            anchors.append(candidates[at]); at = predecessors[at]
    return list(reversed(anchors)), candidates


def plan_blocks(lines, evidence, anchors, duration, fallback='ja', max_rows=4):
    review = audit(lines, evidence, duration)
    risk = set(review['risk_rows'])
    by_row = {a['row']: a for a in anchors}
    reliable={e['row']:lines[e['row']-1]for e in evidence if (e.get('confidence')or 0)>=.75
              and e.get('dual_disagreement_s')is not None and e['dual_disagreement_s']<=1
              and not line_issues(lines[e['row']-1],duration)}
    for row, anchor in by_row.items():
        old = lines[row-1]
        if max(abs(old[k]-anchor[k]) for k in ('start','end')) > 2:
            risk.add(row)
    # Include adjacent context, but preserve its language boundaries.
    targets = {j for i in risk for j in (i-1, i, i+1) if 1 <= j <= len(lines)}
    blocks = []
    at = 1
    while at <= len(lines):
        if at not in targets:
            at += 1; continue
        first = at; lang = lyric_language(lines[at-1]['raw'], fallback)
        while at < len(lines) and at+1 in targets and at-first+1 < max_rows and lyric_language(lines[at]['raw'],fallback) == lang:
            at += 1
        last = at
        own = [a for a in anchors if first <= a['row'] <= last]
        before = [a for a in anchors if a['row'] < first]
        after = [a for a in anchors if a['row'] > last]
        left = min([a['start'] for a in own] or [lines[first-1]['start']])-5
        right = max([a['end'] for a in own] or [lines[last-1]['end']])+5
        if not own:
            prior=[r for r in reliable if first-4<=r<first]
            following=[r for r in reliable if last<r<=last+4]
            if prior:left=min(left,reliable[max(prior)]['end']-.3)
            if following:right=max(right,reliable[min(following)]['start']+.3)
        if own and own[0]['row'] > first:
            left -= 8*(own[0]['row']-first)
        if own and own[-1]['row'] < last:
            right += 8*(last-own[-1]['row'])
        if before and before[-1]['row'] == first-1:
            left = max(left, before[-1]['end']-.1)
        if after and after[0]['row'] == last+1:
            right = min(right, after[0]['start']+.1)
        left, right = max(0,left), min(duration,right)
        blocks.append(dict(first=first,last=last,language=lang,left=round(left,3),right=round(right,3),
                           anchors=[a['row'] for a in own],
                           skipped=None if 1 < right-left <= 95 else '缺少可靠的有限时间范围'))
        at += 1
    return blocks


def choose_rows(baseline, evidence, candidates, anchors, duration):
    """Choose compatible rows; reject structural gains without acoustic support."""
    evidence = {e['row']:e for e in evidence}
    anchor_map = {a['row']:a for a in anchors}
    window_anchors={r:baseline[r-1]for r,e in evidence.items()if (e.get('confidence')or 0)>=.75
                    and e.get('dual_disagreement_s')is not None and e['dual_disagreement_s']<=1
                    and not line_issues(baseline[r-1],duration)}
    window_anchors.update(anchor_map)
    options = []; decisions = []
    for i, old in enumerate(baseline,1):
        ev = evidence.get(i,{})
        old_score = ev.get('confidence',0) or 0
        old_bad = line_issues(old,duration)
        choices = [dict(line=copy.deepcopy(old), evidence=copy.deepcopy(ev), utility=0, changed=False)]
        rejected = []
        fixed=anchor_map.get(i)
        if fixed and fixed.get('source')=='provided_anchor' and max(abs(old[k]-fixed[k])for k in ('start','end'))<=.03:
            options.append(choices);decisions.append(dict(row=i,text=old['raw'],candidates=0,rejected=[],protected=True));continue
        for candidate in candidates.get(i,[]):
            line = candidate['line']; probability = candidate['confidence']; anchor = anchor_map.get(i)
            reasons = line_issues(line,duration)
            if normalized(line['raw']) != normalized(old['raw']): reasons.append('text_changed')
            anchor_error = max(abs(line[k]-anchor[k]) for k in ('start','end')) if anchor else None
            anchor_limit=.12 if anchor and anchor.get('source')=='provided_anchor' else 2.5
            support = bool(anchor and anchor_error <= anchor_limit and probability >= .35)
            # Different models plus two audio routes add corroboration; shared
            # crop bounds alone do not establish an accurate timestamp.
            agreeing = [c for c in candidates.get(i,[]) if c is not candidate
                        and c['model'] != candidate['model'] and c['audio'] != candidate['audio']
                        and not line_issues(c['line'],duration) and c['confidence'] >= .6
                        and max(abs(c['line'][k]-line[k]) for k in ('start','end')) <= 1]
            partial_agreement=[c for c in candidates.get(i,[])if c is not candidate
                and c['model']!=candidate['model'] and c['audio']!=candidate['audio']
                and not line_issues(c['line'],duration)and c['confidence']>=.2
                and max(abs(c['line'][k]-line[k])for k in ('start','end'))<=1]
            if anchor and anchor.get('partial_start'):
                support=bool(partial_agreement and probability>=.25 and anchor_error<=anchor_limit)
            lefts=[r for r in window_anchors if i-4<=r<i]
            rights=[r for r in window_anchors if i<r<=i+4]
            left=window_anchors[max(lefts)]['end'] if lefts else None
            right=window_anchors[min(rights)]['start'] if rights else None
            bounded=left is not None and right is not None and 0<right-left<=60 and left-.15<=line['start']<line['end']<=right+.15
            support = support or bool(not anchor and bounded and agreeing and probability >= .65)
            if not support: reasons.append('insufficient_independent_support')
            if anchor and anchor_error > anchor_limit: reasons.append('asr_disagreement')
            shift = max(abs(line[k]-old[k]) for k in ('start','end'))
            old_anchor_error = max(abs(old[k]-anchor[k]) for k in ('start','end')) if anchor else 0
            improvement = bool(old_bad) or (anchor and old_anchor_error > anchor_error+.5) or probability > old_score+.08
            context_adjustment=support and not reasons and shift<=1.2 and probability>=max(.6,old_score-.15)
            if (not improvement and not context_adjustment) or shift < .02: reasons.append('no_supported_gain')
            if reasons:
                rejected.append(dict(model=candidate['model'],audio=candidate['audio'],reasons=reasons)); continue
            dual = min([max(abs(c['line'][k]-line[k]) for k in ('start','end')) for c in candidates.get(i,[])
                        if c['audio']!=candidate['audio'] and c['model']==candidate['model']] or [float('inf')])
            record = dict(row=i,text=line['raw'],start=line['start'],end=line['end'],confidence=probability,
                          dual_disagreement_s=None if not math.isfinite(dual) else dual,
                          model=candidate['model'],audio=candidate['audio'],asr_boundary_error=anchor_error,
                          measurement='bounded_multilingual_repair',priority='建议复核',
                          reasons=['自动局部修复；转写锚点与模型证据支持，仍需听看确认'])
            utility = (2+2*len(old_bad)+probability-old_score+(min(5,old_anchor_error-anchor_error) if anchor else 0)) if improvement else -.05
            choices.append(dict(line=line,evidence=record,utility=utility,changed=True))
        options.append(choices);decisions.append(dict(row=i,text=old['raw'],candidates=len(choices)-1,rejected=rejected))
    # Global monotonic selection prevents individually good candidates from
    # overwriting neighbouring verses or reusing the same repeated chorus.
    scores=[];prev=[]
    for i, choices in enumerate(options):
        current=[];parents=[]
        for choice in choices:
            available=[j for j,p in enumerate(options[i-1]) if p['line']['end'] <= choice['line']['start']+.002 and math.isfinite(scores[-1][j])] if i else []
            best=max(available,key=lambda j:scores[-1][j]) if available else None
            current.append(choice['utility']+(scores[-1][best] if best is not None else 0) if (not i or best is not None) else -math.inf)
            parents.append(best)
        scores.append(current);prev.append(parents)
    if not scores or not any(math.isfinite(n) for n in scores[-1]):
        return copy.deepcopy(baseline),list(evidence.values()),decisions
    index=max(range(len(scores[-1])),key=scores[-1].__getitem__);chosen=[]
    for i in reversed(range(len(options))):
        chosen.append(options[i][index]);decisions[i]['accepted']=options[i][index]['changed'];index=prev[i][index]
    chosen.reverse()
    return [c['line'] for c in chosen],[c['evidence'] for c in chosen],decisions

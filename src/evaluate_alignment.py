"""Evaluate line boundaries against independent references (stdlib only)."""
import argparse
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path

from alignment_review import normalized


def validate(reference):
    if reference.get('schema_version') != 1:
        raise ValueError('Unsupported schema_version')
    audio = reference['audio']
    if not isinstance(audio['duration_s'], (int,float)) or not math.isfinite(audio['duration_s']) or audio['duration_s'] <= 0:
        raise ValueError('Invalid audio duration')
    ids=set()
    for row in reference['lines']:
        if not row.get('id') or row['id'] in ids:
            raise ValueError('Each occurrence needs a unique line id')
        ids.add(row['id'])
        if not isinstance(row.get('text'),str) or not normalized(row['text']):
            raise ValueError('Missing lyric text')
        if row['status'] not in ('sung','not_sung','uncertain'):
            raise ValueError('Invalid annotation status')
        if row['status']=='not_sung':
            if row.get('start') is not None or row.get('end') is not None:
                raise ValueError('not_sung boundaries must be null')
        else:
            start,end=row['start'],row['end']
            if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (start,end)) or not 0 <= start < end <= audio['duration_s']:
                raise ValueError('Reference boundary outside audio')
        uncertainty=row.get('boundary_uncertainty_ms',0)
        if not isinstance(uncertainty,(int,float)) or not math.isfinite(uncertainty) or uncertainty<0:
            raise ValueError('Invalid boundary uncertainty')


def distribution(values):
    if not values:
        return {'n':0,'mae_ms':None,'p50_ms':None,'p90_ms':None,'hit100_pct':None}
    absolute=sorted(abs(x)*1000 for x in values)
    def quantile(q):
        index=(len(absolute)-1)*q;lo=int(index);hi=min(lo+1,len(absolute)-1)
        return absolute[lo]+(absolute[hi]-absolute[lo])*(index-lo)
    return {'n':len(values),'mae_ms':sum(absolute)/len(absolute),'p50_ms':quantile(.5),
            'p90_ms':quantile(.9),'hit100_pct':100*sum(v<=100+1e-7 for v in absolute)/len(absolute)}


def evaluate(reference, prediction, tolerance_ms=150):
    validate(reference)
    if not math.isfinite(tolerance_ms) or tolerance_ms < 0:
        raise ValueError('Tolerance must be finite and nonnegative')
    truth=reference['lines'];pred=prediction['lines']
    ids=[p.get('line_id') for p in pred]
    use_ids=bool(pred) and all(ids)
    matches={}
    ambiguous=[]
    if any(ids) and not use_ids:
        raise ValueError('Use line_id on every prediction row or none')
    if use_ids:
        if len(ids)!=len(set(ids)):
            raise ValueError('Duplicate prediction line_id')
        lookup={p['line_id']:i for i,p in enumerate(pred)}
        matches={i:lookup[t['id']] for i,t in enumerate(truth) if t['id'] in lookup}
    else:
        a=[normalized(t['text']) for t in truth];b=[normalized(p.get('raw',p.get('text',''))) for p in pred]
        ca,cb=Counter(a),Counter(b)
        ambiguous=[t['id'] for t in truth if ca[normalized(t['text'])]>1 and ca[normalized(t['text'])]!=cb[normalized(t['text'])]]
        for ai,bi,size in SequenceMatcher(None,a,b,autojunk=False).get_matching_blocks():
            for k in range(size):
                if truth[ai+k]['id'] not in ambiguous:
                    matches[ai+k]=bi+k
    starts=[];ends=[];missing=[];false_lines=[];invalid=[];corrections=[];uncertain=[]
    for i,t in enumerate(truth):
        if t['id'] in ambiguous:
            continue
        if t['status']=='uncertain':
            uncertain.append(t['id']);continue
        j=matches.get(i)
        if t['status']=='not_sung':
            if j is not None:false_lines.append(t['id'])
            continue
        if j is None:
            missing.append(t['id']);continue
        p=pred[j];a,b=p.get('start'),p.get('end')
        if normalized(p.get('raw',p.get('text','')))!=normalized(t['text']):
            missing.append(t['id']);invalid.append(t['id']);continue
        if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (a,b)) or not 0<=a<b<=reference['audio']['duration_s']:
            invalid.append(t['id']);continue
        starts.append(a-t['start']);ends.append(b-t['end'])
        # Tolerance includes the annotator's admitted boundary uncertainty.
        threshold=(tolerance_ms+t.get('boundary_uncertainty_ms',0))/1000
        if max(abs(a-t['start']),abs(b-t['end']))>threshold+1e-9:corrections.append(t['id'])
    scored=sum(t['status']=='sung' and t['id'] not in ambiguous for t in truth)
    unmatched=[j+1 for j in range(len(pred)) if j not in matches.values()]
    return {'schema_version':1,'recording_id':reference.get('recording_id'),
            'matching':'line_id' if use_ids else 'ordered_text',
            'reference_sung_lines':sum(t['status']=='sung' for t in truth),
            'unambiguous_sung_lines':scored,'matched_timing_lines':len(starts),
            'timing_coverage':len(starts)/sum(t['status']=='sung' for t in truth) if any(t['status']=='sung' for t in truth) else None,
            'missing_ids':missing,'missing_rate':len(missing)/scored if scored else None,
            'invalid_ids':invalid,'predicted_not_sung_ids':false_lines,
            'ambiguous_repeat_ids':ambiguous,'uncertain_ids':uncertain,
            'unmatched_prediction_rows':unmatched,'timing_review_ids':corrections,
            'timing_review_fraction':len(corrections)/len(starts) if starts else None,
            'start':distribution(starts),'end':distribution(ends),
            'note':'Timing metrics exclude missing, invalid, uncertain and ambiguous rows; always read coverage and missing counts alongside errors. Review fraction is a proxy, not measured human editing time.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference',type=Path,required=True)
    parser.add_argument('--prediction',type=Path,required=True)
    parser.add_argument('--media',type=Path,help='verify SHA-256 of the exact audio/video used')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--tolerance-ms',type=float,default=150)
    args=parser.parse_args()
    ref=json.loads(args.reference.read_text(encoding='utf-8'))
    pred=json.loads(args.prediction.read_text(encoding='utf-8'))
    verified=False
    if args.media:
        digest=hashlib.sha256()
        with args.media.open('rb') as stream:
            for block in iter(lambda:stream.read(1<<20),b''):digest.update(block)
        if digest.hexdigest()!=ref['audio'].get('sha256'):
            parser.error('Audio hash mismatch; do not evaluate a different recording or cut')
        verified=True
    result=evaluate(ref,pred,args.tolerance_ms)
    result['media_verified']=verified
    result['example_only']=ref.get('example_only',False)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

"""Portable, read-only HTML review report for automatic repair candidates."""
import argparse
import html
import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from repair_triage import review_queue,LABELS as QUEUE_LABELS


LABELS={'collapsed_line':'整句过短','overlong_line':'整句跨度过长','overlong_token':'单词高亮过长',
        'internal_gap':'句内长空档','low_score':'模型分数偏低','route_disagreement':'两路时间分歧',
        'overlap':'相邻行重叠','incomplete_tokens':'文字映射不完整','invalid_tokens':'字词时间无效',
        'unmeasured':'缺少模型证据','outside_or_invalid_time':'时间越界','invalid_time':'无效时间'}


def stamp(value):
    return f'{int(value)//60:02d}:{value%60:06.3f}'


def workspace_base(value='/'):
    """Use the serving origin by default; allow an explicit offline report URL."""
    if not value or '\\' in value or any(ord(c) < 32 for c in value):
        raise ValueError('工作台地址必须为 HTTP(S) URL 或以 / 开头的同源路径')
    parts=urlsplit(value)
    absolute=parts.scheme in ('http','https') and bool(parts.hostname)
    relative=not parts.scheme and not parts.netloc and value.startswith('/') and not value.startswith('//')
    if not (absolute or relative) or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError('工作台地址不能包含凭据、查询参数或片段，且只支持 HTTP(S) 或同源路径')
    # Validate the port now, before writing any report files.
    parts.port
    return urlunsplit((parts.scheme,parts.netloc,parts.path.rstrip('/')+'/', '', ''))


def build_report(root, destination, project=None, workspace_url='/'):
    workspace_url=workspace_base(workspace_url)
    root=Path(root).resolve();destination=Path(destination).resolve()
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'.history-ignore').touch()
    sections=[];summary=[]
    for file in sorted(root.glob('*/repair-report.json')):
        report=json.loads(file.read_text(encoding='utf8'));name=file.parent.name
        target=destination/name;target.mkdir(exist_ok=True)
        for filename in ['repair-report.json','align.json','alignment_evidence.json','acceptance.json','anchors.json','normal.ass','normal.srt','review-notes.json']:
            source=file.parent/filename
            if source.is_file() and source.resolve()!=(target/filename).resolve():shutil.copyfile(source,target/filename)
        changed=set(report['changed_rows']);after=report['after'];before=report['before'];rows=[]
        rendered=report.get('rendered',{})
        blocking=sorted(set(rendered.get('blocking_rows',after['blocking_rows'])+rendered.get('reference_rows',[])))
        duration=rendered.get('duration_rows',after['duration_rows'])
        for old,new in zip(before['rows'],after['rows']):
            reasons='；'.join(LABELS.get(r,r)for r in new['reasons']) or '未触发已知异常'
            score=lambda value:f'{value:.3f}'if value is not None else '缺失'
            rows.append(f'<tr class="{"changed" if new["row"] in changed else ""}"><td>{new["row"]}</td><td>{html.escape(new["text"])}</td><td>{stamp(old["start"])}–{stamp(old["end"])}</td><td>{stamp(new["start"])}–{stamp(new["end"])}</td><td>{score(old.get("score"))} → {score(new.get("score"))}</td><td>{"已修订" if new["row"] in changed else "保留"}</td><td>{html.escape(reasons)}</td></tr>')
        state='时间结构门槛通过 · 待听看' if report['render_gate'] else '仍有异常 · 暂缓整曲成片'
        folder=quote(name)
        project_name=project or Path(report['source']).parent.parent.name
        workspace=html.escape(workspace_url+'?track='+quote(project_name+'/'+name,safe='')+'#review',quote=True)
        previews=json.loads((target/'previews.json').read_text(encoding='utf8'))if (target/'previews.json').is_file()else[]
        signature=hashlib.sha256((file.parent/'align.json').read_bytes()).hexdigest()
        previews=[c for c in previews if c.get('candidate_signature')==signature]
        previews.sort(key=lambda c:(c['start'],c['label']!='before'))
        clips=''.join(f'<figure><figcaption>{"待复核段落 · " if c.get("purpose")=="unresolved" else ""}{"修复前" if c["label"]=="before" else "当前候选"} · 歌曲 {stamp(c["start"])} 起 · {html.escape(c["text"])}</figcaption><video controls preload="metadata" src="{folder}/{quote(c["file"])}"></video></figure>'for c in previews)
        queue='；'.join(f'{html.escape(r["text"])}（第 {r["row"]} 行，{stamp(r["start"])}–{stamp(r["end"])})'for r in report.get('unanchored',[]))or'无'
        checks=review_queue(report)
        (target/'review-queue.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf8')
        priorities='<div class="priorities"><h3>优先复核</h3>'+''.join(f'<p><strong>{QUEUE_LABELS[c["kind"]]} · {stamp(c["start"])}–{stamp(c["end"])}</strong><br>{html.escape(" ／ ".join(c["lyrics"]))}<br><span class="note">{c["action"]}</span></p>'for c in checks)+'</div>'if checks else ''
        if rendered:
            priorities=f'<p class="note">实际显示检查：阻断或歌词结构待核对 {len(blocking)} 行，超长高亮 {len(duration)} 行；{len(rendered["split_rows"])} 行的长空档已拆分显示圆点。下列原始时间轴提示仍保留，供试听确认。</p>'+priorities
        notes_path=file.parent/'review-notes.json'
        notes=json.loads(notes_path.read_text(encoding='utf8'))if notes_path.is_file()else[]
        if notes:
            priorities+='<div class="priorities"><h3>成片抽查备注</h3>'+''.join(f'<p>{html.escape(note)}</p>'for note in notes)+'</div>'
        anchor_data=json.loads((file.parent/'anchors.json').read_text(encoding='utf8'))if(file.parent/'anchors.json').is_file()else{}
        ambiguous=[]
        for anchor in anchor_data.get('selected',[]):
            current=after['rows'][anchor['row']-1]
            if anchor['row']not in changed and max(abs(anchor[k]-current[k])for k in ('start','end'))>15:
                ambiguous.append(f'<li>{html.escape(current["text"])}：保留 {stamp(current["start"])}–{stamp(current["end"])}；转写匹配到 {stamp(anchor["start"])}–{stamp(anchor["end"])}。</li>')
        if ambiguous:
            priorities+='<details><summary>转写匹配位置相差较远：检查重复段落与歌词顺序</summary><p class="note">转写可能匹配到另一次重复，也可能提示旧时间错误。该位置未被自动采用，需要结合前后歌词听看。</p><ul>'+''.join(ambiguous)+'</ul></details>'
        priorities+=f'<p><a href="{workspace}" target="_blank" rel="noopener">查看本曲全部版本 / 人工复核 ↗</a></p>'
        sections.append(f'<section id="{html.escape(name,quote=True)}"><h2>{html.escape(name)} <small>{state}</small></h2><p>修改 {len(changed)} / {after["total"]} 行；阻断行 {len(before["blocking_rows"])} → {len(after["blocking_rows"])}；原始行长跨度／空档提示 {len(before["duration_rows"])} → {len(after["duration_rows"])}；自动转写锚点 {len(report["anchor_rows"])} 行。</p><p><a href="{folder}/normal.ass">候选 ASS</a> · <a href="{folder}/normal.srt">候选 SRT</a> · <a href="{folder}/repair-report.json">完整修复证据</a> · <a href="{folder}/review-queue.json">复核队列</a></p><div class="previews">{clips}</div>{priorities}<details><summary>尚无独立转写定位的歌词（不表示漏唱）</summary><p>{queue}</p></details><div class="scroll"><table><thead><tr><th>行</th><th>歌词</th><th>修复前</th><th>当前候选</th><th>模型分数前 → 后</th><th>修改</th><th>剩余提示</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>')
        summary.append(dict(track=name,changed=len(changed),render_gate=report['render_gate'],
                            blocking=blocking,duration=duration,raw_blocking=after['blocking_rows'],raw_duration=after['duration_rows'],
                            report=str(target/'repair-report.json')))
    navigation='<nav>'+''.join(f'<a href="#{quote(r["track"])}">{html.escape(r["track"])} · 修改 {r["changed"]} 行</a>'for r in summary)+'</nav>'
    totals=f'<p><strong>{len(summary)} 首已检查 · {sum(r["changed"]for r in summary)} 行已修订 · {sum(r["render_gate"]for r in summary)} 首通过时间结构门槛</strong></p>'
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>自动修复 · 逐行复核报告</title><style>
body{margin:0;background:#0b131a;color:#dce6e8;font:14px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}main{max-width:1400px;margin:auto;padding:36px 24px}h1{font-size:30px}h2{font-size:20px}small{font-size:12px;color:#e6b56b}a{color:#6bdbc5}section{padding:22px;background:#121c23;border:1px solid #283b45;border-radius:12px;margin:20px 0}.scroll{max-height:560px;overflow:auto}table{width:100%;border-collapse:collapse;min-width:900px}th,td{text-align:left;padding:10px;border-bottom:1px solid #283b45;vertical-align:top}th{position:sticky;top:0;background:#1c2b34}td:nth-child(2){min-width:230px;max-width:400px}td:nth-child(3),td:nth-child(4){white-space:nowrap;font-variant-numeric:tabular-nums}.changed{background:#14352e}.note{color:#a8bcc6;max-width:1000px}
.previews{display:flex;flex-wrap:wrap;gap:16px}.previews figure{flex:1 1 calc(50% - 16px);min-width:280px;margin:0 0 20px}.previews video{width:100%;aspect-ratio:16/9;object-fit:contain;background:#000}figcaption{margin-bottom:8px;color:#a8bcc6}details{margin:16px 0}nav{display:flex;flex-wrap:wrap;gap:10px}nav a{padding:8px 12px;background:#1c2b34;border-radius:6px;text-decoration:none}
</style><main><h1>自动修复 · 逐行复核报告</h1><p class="note">所有修复均为独立候选，原成品及人工审批保持不变。绿色行表示采用了新时间轴。模型分数、无提示转写和时间结构检查均不能替代人工听看；没有自动增删歌词或把歌曲标成“通过”。</p><p><a href="__WORKSPACE_REVIEW__">返回成品与人工复核工作台</a></p>'''.replace('__WORKSPACE_REVIEW__',html.escape(workspace_url+'#review',quote=True))+totals+navigation+''.join(sections)+'</main></html>'
    (destination/'index.html').write_text(page,encoding='utf8')
    (destination/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    return summary


def register_report(summary,destination,catalog_root,project):
    """Attach diagnostics only. This never changes the default video or approval."""
    from filelock import FileLock
    from repair_alignment import atomic,read
    catalog_root=Path(catalog_root).resolve();destination=Path(destination).resolve()
    if not destination.is_relative_to(catalog_root):raise ValueError('复核报告必须在成品目录内')
    manifest=catalog_root/'repair-runs/manifest.json';manifest.parent.mkdir(parents=True,exist_ok=True)
    with FileLock(str(manifest)+'.lock'):
        previous=read(manifest,[]);identities={(project,row['track'])for row in summary}
        previous=[r for r in previous if (r['project'],r['track'])not in identities]
        for row in summary:
            previous.append(dict(project=project,track=row['track'],changed=row['changed'],
                blocking=len(row['blocking']),duration=len(row['duration']),render_gate=row['render_gate'],
                report=str(destination/'index.html'),anchor=quote(row['track']),human_approved=False))
        atomic(manifest,previous)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('root',type=Path);parser.add_argument('destination',type=Path)
    parser.add_argument('--catalog-root',type=Path);parser.add_argument('--project')
    parser.add_argument('--workspace-url',type=workspace_base,default='/',help='工作台根地址；默认沿用报告网页的主机和端口，离线报告可指定 HTTP(S) 地址')
    args=parser.parse_args();summary=build_report(args.root,args.destination,args.project,args.workspace_url)
    if args.catalog_root:
        if not args.project:parser.error('--catalog-root 需要 --project')
        register_report(summary,args.destination,args.catalog_root,args.project)

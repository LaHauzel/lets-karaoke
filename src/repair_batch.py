"""One resumable command from existing jobs to repair reports and gated media."""
import argparse
import subprocess
import sys
from pathlib import Path
from repair_alignment import atomic,read
from repair_runner import run_worker
from repair_report import workspace_base


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs-root',type=Path,required=True)
    parser.add_argument('--repair-root',type=Path,required=True)
    parser.add_argument('--catalog-root',type=Path,required=True)
    parser.add_argument('--project',required=True)
    parser.add_argument('--revision')
    parser.add_argument('--workspace-url',type=workspace_base,default='/')
    parser.add_argument('--translation-cache',type=Path)
    parser.add_argument('--models',nargs='+',default=['large-v3','medium'])
    parser.add_argument('--device',choices=['cpu','cuda'],default='cuda')
    parser.add_argument('--anchors-root',type=Path)
    parser.add_argument('--only',nargs='+',type=int,help='只重新检查指定曲目编号，其余已完成候选和缓存保留')
    parser.add_argument('--idle-timeout',type=float,default=180,help='单次声学计算无进度超时秒数；CPU 慢机器可调高')
    parser.add_argument('--formats',nargs='+',choices=['soft','hard'],default=['soft','hard'])
    args=parser.parse_args()
    revision=args.revision or args.repair_root.name
    for value in (revision,args.project):
        if not value or any(c in value for c in '/\\:')or value in ('.','..'):
            parser.error('project 与 revision 必须是单个目录名')
    root=args.repair_root.resolve();catalog=args.catalog_root.resolve()
    jobs_root=args.jobs_root.resolve()
    if root==jobs_root or root.is_relative_to(jobs_root):
        parser.error('修复输出目录必须独立于输入目录')
    report=catalog/'repair-runs'/revision;delivery=catalog/'revisions'/revision/args.project
    scripts=Path(__file__).resolve().parent
    def stage(name,script,*parameters):
        atomic(root/'batch-progress.json',dict(stage=name,status='running'))
        print('STAGE',name,flush=True)
        subprocess.run([sys.executable,'-X','utf8','-u',str(scripts/script),*map(str,parameters)],check=True)
    try:
        failures=[];retries=[]
        jobs=sorted(p for p in args.jobs_root.iterdir()if p.is_dir()and(p/'align.json').is_file())
        if args.only:jobs=[p for p in jobs if int(p.name.split('_')[0])in args.only]
        if not jobs:
            raise ValueError('没有匹配的歌曲任务，停止处理已有缓存')
        for job in jobs:
            command=[sys.executable,'-X','utf8','-u',str(scripts/'repair_alignment.py'),
                '--jobs-root',str(args.jobs_root),'--output-root',str(root),'--device',args.device,
                '--models',*args.models,'--only',job.name.split('_')[0],
                *(['--anchors-root',str(args.anchors_root)]if args.anchors_root else[])]
            for attempt in range(2):
                atomic(root/'batch-progress.json',dict(stage='alignment',track=job.name,attempt=attempt+1,status='running'))
                print('ALIGNMENT',job.name,'attempt',attempt+1,flush=True)
                try:
                    run_worker(command,root/'progress.json',args.idle_timeout)
                    failures.extend(read(root/'failures.json',[]));break
                except (TimeoutError,subprocess.CalledProcessError)as error:
                    event=dict(track=job.name,attempt=attempt+1,error=str(error));retries.append(event)
                    atomic(root/'worker-retries.json',retries)
                    print('WORKER_RETRY',event,flush=True)
                    if attempt:failures.append(event)
        atomic(root/'failures.json',failures)
        if failures:
            # A failed rerun may leave an older candidate beside changed inputs.
            # Keep all files for recovery, but never publish that stale candidate.
            result=dict(status='failed',stage='alignment',alignment_errors=failures,human_approved=False)
            atomic(root/'batch-summary.json',result);atomic(root/'batch-progress.json',result)
            return 1
        tracks=[p.name for p in jobs]
        stage('previews','repair_previews.py',root,report,'--tracks',*tracks)
        stage('delivery','repair_delivery.py',root,delivery,'--catalog-root',catalog,'--project',args.project,
              '--revision',revision,'--baseline-exports',catalog/args.project,'--formats',*args.formats,'--tracks',*tracks,
              *(['--translation-cache',args.translation_cache]if args.translation_cache else[]))
        stage('report','repair_report.py',root,report,'--catalog-root',catalog,'--project',args.project,'--workspace-url',args.workspace_url)
        rows=read(report/'summary.json',[])
        export_errors=read(delivery/'export-failures.json',[])
        failed_exports={e['track']for e in export_errors}
        eligible={r['track']for r in rows if r['render_gate']and r['changed']and r['track']not in failed_exports}
        deliveries=[read(p,{})for p in delivery.glob('*/delivery.json')if p.parent.name in eligible]
        previews=[clip for p in report.glob('*/previews.json')for clip in read(p,[])]
        result=dict(status='complete',tracks=len(rows),changed_rows=sum(r['changed']for r in rows),
                    ready_for_review=sum(r['render_gate']for r in rows),needs_repair=sum(not r['render_gate']for r in rows),
                    exported_tracks=len(deliveries),exported_files=sum(len(d.get('files',[]))for d in deliveries),preview_files=len(previews),
                    report=str(report/'index.html'),delivery=str(delivery),human_approved=False,
                    alignment_errors=read(root/'failures.json',[]),preview_errors=read(report/'preview-failures.json',[]),
                    export_errors=export_errors)
        if any(result[k]for k in ('alignment_errors','preview_errors','export_errors')):result['status']='complete_with_errors'
        atomic(root/'batch-summary.json',result);atomic(root/'batch-progress.json',result)
        return 0 if result['status']=='complete' else 1
    except Exception as error:
        atomic(root/'batch-progress.json',dict(status='failed',error=str(error)));raise


if __name__=='__main__':raise SystemExit(main())

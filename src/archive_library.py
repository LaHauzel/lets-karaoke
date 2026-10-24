"""Plan or perform lossless multi-track consolidation of a local delivery library.

Creates verified archives and a catalog overlay. Never deletes media or edits
approvals. A separate, reviewable cleanup plan lists only superseded originals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from filelock import FileLock
from multitrack import pack_variants, signature
from repair_alignment import atomic, read


def protected_paths(root):
    """Include current/history/backup approvals and active editing dependencies."""
    root = Path(root).resolve()
    out = root.parent.parent
    paths = set()
    sources = list(root.parent.glob('human-reviews.json*')) + [root / 'workflow-links.json']
    sources += list((out / 'webui').glob('*/job.json'))
    sources += list((out / 'webui').glob('*/review-origin.json'))

    def visit(value):
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and len(value) < 4096:
            try:
                path = Path(value)
                if path.is_absolute():
                    paths.add(path.resolve())
                elif value.endswith(('.mp4', '.mkv', '.wav', '.m4a')):
                    paths.add((out / path).resolve())
            except (OSError, ValueError):
                pass

    for path in sources:
        if path.is_file() and not path.name.endswith('.lock'):
            visit(read(path))  # A malformed approval file must stop migration.
    return paths


def make_plan(root):
    """Return (plan, failures); a non-conforming track is skipped, not fatal."""
    from review_catalog import build_catalog
    root = Path(root).resolve()
    catalog, _ = build_catalog(root)
    if catalog['diagnostics']:
        raise ValueError('先修复成品库诊断：' + '; '.join(catalog['diagnostics']))
    protected = protected_paths(root)
    plan, failures = [], []
    for track in catalog['tracks']:
        files = [f for f in track['files'] if f['kind'] == 'soft' and f['exists']
                 and not f.get('previous_release') and not f.get('multitrack')]
        if not files:
            continue
        by_variant = {f['variant']: f for f in files}
        if len(files) != 4 or set(by_variant) != {1, 2, 3, 4}:
            failures.append(dict(track_id=track['id'], error='没有完整且唯一的四个软字幕版本'))
            continue
        keep = {Path(f['path']).resolve() for f in files if Path(f['path']).resolve() in protected}
        if keep:
            keep.add(Path(by_variant[1]['path']).resolve())
        originals = [dict(path=Path(f['path']).relative_to(root).as_posix(),
                          variant=f['variant'], signature=f['signature'], bytes=f['bytes'],
                          keep=Path(f['path']).resolve() in keep) for f in files]
        identity = hashlib.sha256(json.dumps(originals, sort_keys=True).encode()).hexdigest()[:16]
        destination = root / 'multitrack' / track['project'] / track['track'] / (identity + '.mkv')
        plan.append(dict(track_id=track['id'], revision=files[0].get('revision'), originals=originals,
                         file=destination.relative_to(root).as_posix()))
    return plan, failures


def cleanup_plan(root):
    """Refresh protected references before proposing deletion; no side effects."""
    from review_catalog import build_catalog
    root = Path(root).resolve()
    protected = protected_paths(root)
    catalog, allowed = build_catalog(root)
    if catalog['diagnostics']:
        raise ValueError('清理前成品库仍有诊断错误')
    protected.update(Path(p).resolve() for p in allowed.values())
    result = {}
    archives = read(root / 'multitrack-manifest.json', {}).get('archives', [])
    for archive in archives:
        destination = (root / archive['file']).resolve()
        if not destination.is_relative_to(root) or not destination.is_file() or signature(destination) != archive['signature']:
            raise ValueError('多轨归档验证状态已失效')
        for original in archive['originals']:
            path = (root / original['path']).resolve()
            if not path.is_relative_to(root):
                raise ValueError('待清理文件超出成品库')
            if original.get('keep') or path in protected or not path.exists():
                continue
            if signature(path) != original['signature']:
                raise ValueError('待清理原片发生变化：' + str(path))
            result[str(path)] = dict(path=str(path), signature=original['signature'], bytes=original['bytes'],
                                     replacement=str(destination), replacement_signature=archive['signature'])
    return list(result.values())


def migrate(root, audit_dir, *, apply=False, limit=None):
    root = Path(root).resolve()
    audit_dir = Path(audit_dir).resolve()
    audit_dir.mkdir(parents=True, exist_ok=True)
    plan, failures = make_plan(root)
    for failure in failures:
        print(f"SKIPPED {failure['track_id']} {failure['error']}", flush=True)
    if limit is not None:
        plan = plan[:limit]
    atomic(audit_dir / 'migration-plan.json', plan)
    atomic(audit_dir / 'migration-failures.json', failures)
    if not apply:
        return dict(planned=len(plan), original_bytes=sum(o['bytes'] for p in plan for o in p['originals']),
                    failures=failures)
    manifest = root / 'multitrack-manifest.json'
    for index, item in enumerate(plan, 1):
        print(f"ARCHIVE {index}/{len(plan)} {item['track_id']}", flush=True)
        try:
            variants = {o['variant']: root / o['path'] for o in item['originals']}
            if any(signature(variants[o['variant']]) != o['signature'] for o in item['originals']):
                raise ValueError('计划生成后源文件发生变化')
            checked = pack_variants(variants, root / item['file'])
            # New annotations may arrive during the long-running mux operation.
            protected = protected_paths(root)
            for original in item['originals']:
                original['keep'] |= (root / original['path']).resolve() in protected
            if any(o['keep'] for o in item['originals']):
                next(o for o in item['originals'] if o['variant'] == 1)['keep'] = True
            entry = {**item, **{k: v for k, v in checked.items() if k != 'file'}}
            with FileLock(str(manifest) + '.lock'):
                data = read(manifest, dict(schema_version=1, archives=[]))
                data['archives'] = [r for r in data['archives'] if r['file'] != item['file']] + [entry]
                atomic(manifest, data)
            print('VERIFIED', item['track_id'], checked['bytes'], flush=True)
        except Exception as exc:
            failures.append(dict(track_id=item['track_id'], error=str(exc)))
            print('FAILED', item['track_id'], str(exc), flush=True)
        atomic(audit_dir / 'migration-failures.json', failures)
    candidates = cleanup_plan(root)
    atomic(audit_dir / 'cleanup-candidates.json', candidates)
    return dict(processed=len(plan), failures=failures, cleanup_files=len(candidates),
                cleanup_bytes=sum(c['bytes'] for c in candidates))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--audit-dir', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='生成并验证归档；不删除旧文件')
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    result = migrate(args.root, args.audit_dir, apply=args.apply, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('failures') else 0


if __name__ == '__main__':
    raise SystemExit(main())

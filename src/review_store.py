"""Durable per-song human decisions, kept outside generated exports."""
import copy
import json
import math
import os
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from filelock import FileLock

STATUSES = {'unchecked', 'passed', 'needs_changes'}


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def empty_review():
    return dict(status='unchecked', notes='', issues=[], file_id=None, file_signature=None,
                file_path=None, updated_at=None, revision=0)


class ReviewConflict(ValueError):
    def __init__(self, current):
        super().__init__('这首歌已在另一个页面更新；本次修改尚未覆盖磁盘记录。')
        self.current = current


class ReviewStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _read(self):
        if not self.path.exists():
            return dict(schema_version=1, updated_at=None, reviews={})
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or data.get('schema_version') != 1 or
                    not isinstance(data.get('reviews'), dict) or
                    not isinstance(data.get('history', {}), dict)):
                raise ValueError('不支持的复核文件格式')
            for key, record in data['reviews'].items():
                if (not isinstance(key, str) or not isinstance(record, dict) or
                        not isinstance(record.get('status'), str) or record['status'] not in STATUSES or
                        type(record.get('revision')) is not int or record['revision'] < 1 or
                        not isinstance(record.get('notes'), str) or not isinstance(record.get('issues'), list) or
                        any(not isinstance(i, dict) for i in record['issues'])):
                    raise ValueError('复核记录格式不正确')
            for key, records in data.get('history', {}).items():
                if not isinstance(key, str) or not isinstance(records, list) or any(
                    not isinstance(record, dict) or not isinstance(record.get('status'), str) or
                    record['status'] not in STATUSES or not isinstance(record.get('notes'), str) or
                    not isinstance(record.get('issues'), list) or type(record.get('revision')) is not int
                    for record in records):
                    raise ValueError('历史审批记录格式不正确')
            return data
        except (OSError, ValueError) as exc:
            raise ValueError(f'无法读取人工复核文件，已停止写入以保护原记录：{self.path}；{exc}') from exc

    def read(self):
        with self.lock:
            return self._read()

    def attach(self, catalog):
        data = self.read()
        counts = dict(unchecked=0, passed=0, needs_changes=0)
        for track in catalog['tracks']:
            record = copy.deepcopy(data['reviews'].get(track['id'], empty_review()))
            reviewed_file = next((f for f in track['files'] if f['id'] == record.get('file_id')), None)
            record['stale'] = bool(record.get('file_id') and (
                not reviewed_file or not reviewed_file['exists'] or
                reviewed_file.get('signature') != record.get('file_signature')))
            record['new_version'] = bool(track.get('revision') and (
                not reviewed_file or reviewed_file.get('revision') != track['revision']['revision']))
            track['human_review'] = record
            track['review_history'] = copy.deepcopy(data.get('history', {}).get(track['id'], []))
            counts[record['status']] += 1
        catalog['human_reviews'] = dict(path=str(self.path), backup_path=str(self.path)+'.bak', counts=counts)

    @staticmethod
    def _text(value, label, maximum):
        if not isinstance(value, str) or len(value) > maximum:
            raise ValueError(f'{label}须为不超过 {maximum} 字的文字')
        return value.strip()

    def _validate(self, track, payload):
        status = payload.get('status')
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError('请选择未检查、通过或需要修改')
        revision = payload.get('revision')
        if type(revision) is not int or revision < 0:
            raise ValueError('复核版本号无效，请刷新清单')
        file_id = payload.get('file_id')
        files = {f['id']: f for f in track['files']}
        if file_id is not None and (not isinstance(file_id, str) or file_id not in files):
            raise ValueError('复核版本必须是这首歌的完整视频，不能选择实验片段')
        reviewed_file = files.get(file_id)
        if status == 'passed' and (not reviewed_file or not reviewed_file['exists']):
            raise ValueError('标记通过时，请选择已检查的完整视频版本')
        if status == 'passed' and payload.get('file_signature') != reviewed_file.get('signature'):
            raise ValueError('这个视频在页面打开后发生了变化；请重新加载并检查新视频后再标记通过')
        issues = payload.get('issues', [])
        if not isinstance(issues, list) or len(issues) > 100:
            raise ValueError('每首歌最多保存 100 条问题记录')
        duration = max([track['duration']] + [f.get('duration', 0) for f in track['files']])
        result = []
        for i, item in enumerate(issues, 1):
            if not isinstance(item, dict):
                raise ValueError(f'第 {i} 条问题格式无效')
            row = item.get('row')
            if row is not None and (type(row) is not int or not 1 <= row <= track['total']):
                raise ValueError(f'第 {i} 条问题：歌词行号须在 1–{track["total"]} 之间')
            stamp = item.get('time')
            if stamp is not None and (type(stamp) not in (int, float) or
                    not math.isfinite(stamp) or not 0 <= stamp <= duration):
                raise ValueError(f'第 {i} 条问题：时间点须在本首歌曲的时长范围内')
            note = self._text(item.get('note', ''), f'第 {i} 条问题说明', 2000)
            if not note or (row is None and stamp is None):
                raise ValueError(f'第 {i} 条问题需要说明，以及行号或时间点')
            source_id = item.get('file_id')
            media = track['files'] + ((track.get('experiment') or {}).get('files', []))
            source = next((f for f in media if f['id'] == source_id), None)
            if source_id is not None and not source:
                raise ValueError(f'第 {i} 条问题的视频版本无效')
            resolved = item.get('resolved', False)
            if type(resolved) is not bool:
                raise ValueError('问题解决状态须为布尔值')
            result.append(dict(row=row, time=round(stamp, 3) if stamp is not None else None,
                               note=note, resolved=resolved, file_id=source_id,
                               file_path=source['path'] if source else None))
        if status == 'passed' and any(not item['resolved'] for item in result):
            raise ValueError('还有未解决的问题；请先标为已解决，或将歌曲状态设为“需要修改”')
        return dict(status=status, notes=self._text(payload.get('notes', ''), '歌曲备注', 10000),
                    issues=result, file_id=file_id,
                    file_signature=reviewed_file.get('signature') if reviewed_file else None,
                    file_path=reviewed_file['path'] if reviewed_file else None)

    @staticmethod
    def _atomic_write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name+'.', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def save(self, track, payload):
        clean = self._validate(track, payload)
        # Main UI and the legacy library may run in different processes.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, FileLock(str(self.path)+'.lock', timeout=10):
            data = self._read()
            current = data['reviews'].get(track['id'], empty_review())
            if current['revision'] != payload['revision']:
                raise ReviewConflict(copy.deepcopy(current))
            if current['revision']:
                data.setdefault('history', {}).setdefault(track['id'], []).append(copy.deepcopy(current))
            clean.update(revision=current['revision']+1, updated_at=now())
            data['reviews'][track['id']] = clean
            data['updated_at'] = clean['updated_at']
            content = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
            if self.path.exists():
                self._atomic_write(Path(str(self.path)+'.bak'), self.path.read_bytes())
            self._atomic_write(self.path, content)
            return copy.deepcopy(clean)

"""Shared local-request validation and bounded-memory media HTTP helpers."""
import ipaddress
import mimetypes
import re
from email.message import Message
from pathlib import Path
from urllib.parse import quote, urlparse


def _loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def request_allowed(handler):
    host = urlparse('//'+handler.headers.get('Host', '')).hostname or ''
    bound = str(handler.server.server_address[0])
    if _loopback(bound) and not _loopback(host):
        return False
    origin = handler.headers.get('Origin')
    if origin is None:
        return handler.headers.get('Sec-Fetch-Site', '') != 'cross-site'
    parsed = urlparse(origin)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return port == handler.server.server_port and (
        parsed.hostname == host or (_loopback(parsed.hostname) and _loopback(host)))


def serve_file(handler, path, download=False):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError('文件不存在或已移动')
    size = path.stat().st_size
    start, end, code = 0, size-1, 200
    value = handler.headers.get('Range')
    if value:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
        if not match or not any(match.groups()):
            return handler._send(416, 'text/plain', b'', {'Content-Range': f'bytes */{size}'})
        a, b = match.groups()
        if a:
            start = int(a)
            end = min(int(b), size-1) if b else size-1
        elif int(b) > 0:
            start = max(0, size-int(b))
        else:
            start = size
        if start > end or start >= size:
            return handler._send(416, 'text/plain', b'', {'Content-Range': f'bytes */{size}'})
        code = 206
    content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    if path.suffix.lower() in {'.ass', '.srt', '.lrc', '.txt'}:
        content_type = 'text/plain; charset=utf-8'
    handler.send_response(code)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(max(0, end-start+1)))
    handler.send_header('Accept-Ranges', 'bytes')
    handler.send_header('Cache-Control', 'no-store')
    if code == 206:
        handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    if download:
        handler.send_header('Content-Disposition', 'attachment; filename="download'+path.suffix+'"; filename*=UTF-8\'\''+quote(path.name))
    handler.end_headers()
    try:
        with path.open('rb') as stream:
            stream.seek(start)
            remaining = end-start+1
            while remaining > 0:
                chunk = stream.read(min(1 << 20, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass


def receive_multipart(stream, length, boundary, directory, field_limit=1 << 20):
    """Stream one form into unique files; retain only small fields in memory."""
    if not boundary or len(boundary) > 200 or b'\r' in boundary or b'\n' in boundary:
        raise ValueError('无效上传边界')
    remaining = length
    buffer = bytearray()
    directory = Path(directory)
    fields, files = {}, {}

    def fill():
        nonlocal remaining
        if remaining <= 0:
            raise ValueError('上传正文不完整')
        block = stream.read(min(1 << 16, remaining))
        if not block:
            raise ValueError('上传连接提前关闭')
        remaining -= len(block)
        buffer.extend(block)

    def take(n):
        while len(buffer) < n:
            fill()
        result = bytes(buffer[:n])
        del buffer[:n]
        return result

    def until(marker, write, suffix=False):
        search = 0
        while True:
            position = buffer.find(marker, search)
            if position >= 0:
                if suffix:
                    while len(buffer) < position+len(marker)+2:
                        fill()
                    if buffer[position+len(marker):position+len(marker)+2] not in (b'\r\n', b'--'):
                        search = position+1
                        continue
                write(bytes(buffer[:position]))
                del buffer[:position+len(marker)]
                return
            keep = len(marker)+2
            if len(buffer) > keep:
                write(bytes(buffer[:-keep]))
                del buffer[:-keep]
            search = 0
            fill()

    if take(len(boundary)+4) != b'--'+boundary+b'\r\n':
        raise ValueError('无效 multipart 正文')
    for index in range(20):
        header = bytearray()
        def add_header(block):
            header.extend(block)
            if len(header) > 65536:
                raise ValueError('上传头部过大')
        until(b'\r\n\r\n', add_header)
        message = Message()
        for line in header.decode('utf-8', 'replace').split('\r\n'):
            key, separator, value = line.partition(':')
            if separator:
                message[key] = value.strip()
        name = message.get_param('name', header='content-disposition')
        filename = message.get_filename()
        if not name or name in files or name in fields:
            raise ValueError('上传字段无效或重复')
        if filename:
            target = directory/f'part-{index}'
            with target.open('wb') as output:
                until(b'\r\n--'+boundary, output.write, suffix=True)
            files[name] = (filename, target)
        else:
            value = bytearray()
            def add_field(block):
                value.extend(block)
                if len(value) > field_limit:
                    raise ValueError('表单字段过大')
            until(b'\r\n--'+boundary, add_field, suffix=True)
            fields[name] = value.decode('utf-8', 'replace')
        ending = take(2)
        if ending == b'--':
            # Consume the epilogue so a keep-alive connection stays aligned.
            while remaining:
                fill()
                buffer.clear()
            return fields, files
        if ending != b'\r\n':
            raise ValueError('上传分隔符无效')
    raise ValueError('上传字段过多')

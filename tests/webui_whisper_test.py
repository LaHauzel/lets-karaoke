"""WebUI「whisper 对齐（演唱场景）」链路验证。

覆盖：
1. lyrics + whisper=1 + vocal_guide=1 时，/api/run 受理并跑完；
2. 产出的 mapping 必须是 enhanced-lrc（= 直接采信时间轴，未再做声学对齐）；
3. whisper 四件套齐全、诊断带回 suspect_lines；
4. LRC 正文与输入歌词逐行一致（顺序不丢不换）；
5. 字幕结构自检通过、输出音轨未被擅自替换、文件可下载。

用法（需先启动 WebUI）：
  python tests/webui_whisper_test.py --media "C:\\path\\live.mp4" \
      --lyrics out/example/lyrics.txt [--port 7870]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import mimetypes
from pathlib import Path

FAILS: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  ' + extra) if extra else ''}")
    if not cond:
        FAILS.append(label)


def post_multipart(base: str, path: str, fields: dict, files: dict, timeout=60):
    boundary = "----wb" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += str(v).encode("utf-8") + b"\r\n"
    for k, fp in files.items():
        fp = Path(fp)
        ctype = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{k}"; '
                 f'filename="{fp.name}"\r\n').encode("utf-8")
        body += f"Content-Type: {ctype}\r\n\r\n".encode()
        body += fp.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        base + path, data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw}


def get_json(base: str, path: str, timeout=30):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + path)
    with opener.open(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def head_status(base: str, path: str, timeout=30) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + path, method="GET")
    try:
        with opener.open(req, timeout=timeout) as r:
            r.read(256)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def watch(base: str, job: str, limit_s: float = 900.0) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + "/api/events?job=" + urllib.parse.quote(job))
    last = {"state": "queued", "progress": 0.0}
    t0 = time.time()
    with opener.open(req, timeout=30) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = json.loads(line[5:].strip())
            for msg in d.get("lines", []):
                if "[whisper]" in msg or "[vocal-guide]" in msg:
                    print("      " + msg)
            last = d
            if d.get("state") in ("done", "error", "cancelled"):
                break
            if time.time() - t0 > limit_s:
                last = {"state": "timeout"}
                break
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--media", required=True)
    ap.add_argument("--lyrics", required=True)
    ap.add_argument("--port", type=int, default=7870)
    ap.add_argument("--timeout", type=float, default=1200.0)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    media, lyr = Path(args.media), Path(args.lyrics)
    for p in (media, lyr):
        if not p.exists():
            print(f"!! 文件不存在: {p}")
            return 2
    ref_lines = [l.strip() for l in lyr.read_text(encoding="utf-8").splitlines() if l.strip()]

    print("=== ① 服务可用性 ===")
    try:
        st, meta = get_json(base, "/api/meta")
        check(st == 200 and "fonts" in meta, "GET /api/meta 正常",
              f"status={st} fonts={len(meta.get('fonts', []))}")
    except Exception as e:
        print(f"!! 无法连接 WebUI（{base}）：{e}\n   先启动：python src/webui.py --port {args.port}")
        return 2

    print()
    print("=== ② lyrics + whisper=1 + vocal_guide=1 —— 受理并跑完 ===")
    fields = {
        "lyrics_text": lyr.read_text(encoding="utf-8"),   # 文本框粘贴（用户提供路径）
        "whisper": "1",
        "whisper_model": "large-v3",
        "vocal_guide": "1",
        "vocal_mode": "keep",     # 商业曲目：输出保留原唱
        "separate": "0",
        "lang": "ja",
        "encoder": "auto",
        "quality": "21",
        "options": json.dumps({"font": "Yu Gothic", "font_size": 60,
                               "group_same_unit": True}),
    }
    st, j = post_multipart(base, "/api/run", fields, {"media": media})
    # 歌词用「文本框粘贴」传入 —— 这是实际用户的路径，且能覆盖
    # 「lyrics_text 优先级高于 whisper 产物」的回归（早期测试发现）
    check(st == 200 and j.get("job"), "任务被受理", f"status={st} job={j.get('job')}")
    if not j.get("job"):
        print(f"  !! {j}")
        return 1
    job = j["job"]

    print("  跟踪进度：")
    last = watch(base, job, limit_s=args.timeout)
    check(last.get("state") == "done", "任务完成", f"state={last.get('state')}")
    if last.get("state") != "done":
        print("  !! " + str(last.get("error"))[:600])
        return 1

    print()
    print("=== ③ 结果校验 ===")
    res = last.get("result") or {}
    stats = res.get("stats") or {}
    health = stats.get("health") or {}
    check(bool(res.get("video")), "产出成片", res.get("video"))
    check(bool(res.get("ass")) and bool(res.get("srt")), "产出 ASS + SRT")

    wf = res.get("whisper_files") or []
    check(len(wf) == 4, "whisper 四件套齐全（lrc/plain/srt/词级json）", str(wf))
    wi = res.get("whisper_info") or {}
    check(bool(wi.get("language")), "回传语言", str(wi.get("language")))
    check(wi.get("model") == "large-v3", "回传模型名", str(wi.get("model")))
    check("suspect_lines" in wi, "诊断带回 suspect_lines",
          str((wi.get("suspect_lines") or [])[:6]))
    check(wi.get("vocal_guide") is True, "人声能量引导已启用")

    check(stats.get("mapping", {}).get("mode") == "enhanced-lrc",
          "管线走 Route C（直接采信逐字时间轴）", str(stats.get("mapping")))
    check(health.get("ok") is True, "字幕结构自检通过",
          f"overlap={len(health.get('overlap_pairs') or [])} "
          f"nonmono={len(health.get('nonmonotonic_lines') or [])}")
    check(stats.get("output_audio") == "original", "输出音轨=原唱（未擅自替换）",
          str(stats.get("output_audio")))

    print()
    print("=== ④ LRC 正文与输入歌词逐行一致 ===")
    wl = (res.get("whisper_files") or [])[0] if res.get("whisper_files") else ""
    st2 = head_status(base, "/files/" + job + "/" + urllib.parse.quote(wl)) if wl else 404
    check(st2 == 200, "whisper LRC 可下载", f"status={st2}")
    lrc_text = last.get("whisper_lrc") or ""
    import re
    got = [re.sub(r"<[^>]*>", "", re.sub(r"^\[[^\]]*\]", "", l)).strip()
           for l in lrc_text.splitlines() if l.strip()]
    check(len(got) == len(ref_lines), f"行数一致 {len(got)}/{len(ref_lines)}")
    mismatch = [i for i, (a, b) in enumerate(zip(got, ref_lines)) if a != b]
    check(not mismatch, "逐行文本一致" + (f" 首个差异行 {mismatch[0]}" if mismatch else ""))

    print()
    print(f"通过 {0 if FAILS else 1} 组 / 失败 {len(FAILS)} 项 " + ("✅" if not FAILS else "❌"))
    for f in FAILS:
        print("  FAIL:", f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())

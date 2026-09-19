"""WebUI「无歌词文件（本地 ASR 自动转写）」链路验证。

覆盖：
1. 只给 media + asr=1（不给歌词）时，/api/run 必须受理；
2. 不给歌词也不开 ASR 时必须拒绝（400）且提示可操作；
3. 全链路跑完，结果里必须带 ASR 草稿三件套，且成片存在；
4. 字幕结构自检通过、输出音轨未被擅自替换。

用法（需先启动 WebUI）：
  python tests/webui_asr_test.py --media "C:\\path\\live.mp4" [--port 7870]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
    """只取 HTTP 状态码（视频/字幕是二进制或纯文本，不能按 JSON 解析）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + path, method="GET")
    try:
        with opener.open(req, timeout=timeout) as r:
            r.read(256)          # 读一点点就够确认可访问
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def watch(base: str, job: str, limit_s: float = 900.0) -> dict:
    """跟 SSE 直到终态，返回最后的 payload。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + "/api/events?job=" + urllib.parse.quote(job))
    last = {"state": "queued", "progress": 0.0}
    t0 = time.time()
    seen = 0
    with opener.open(req, timeout=30) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = json.loads(line[5:].strip())
            for msg in d.get("lines", []):
                seen += 1
                if seen <= 6 or "[ASR]" in msg:
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
    ap.add_argument("--port", type=int, default=7870)
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    media = Path(args.media)
    if not media.exists():
        print(f"!! 媒体不存在: {media}")
        return 2

    print("=== ① 服务可用性 ===")
    try:
        st, meta = get_json(base, "/api/meta")
        check(st == 200 and "fonts" in meta, "GET /api/meta 正常",
              f"status={st} fonts={len(meta.get('fonts', []))}")
    except Exception as e:
        print(f"!! 无法连接 WebUI（{base}）：{e}\n   先启动：python src/webui.py --port {args.port}")
        return 2

    print()
    print("=== ② 不给歌词、也不开 ASR —— 必须拒绝 ===")
    st, j = post_multipart(base, "/api/run", {"vocal_mode": "keep", "separate": "0"},
                           {"media": media})
    check(st == 400, "返回 400", f"status={st}")
    check("ASR" in (j.get("error") or ""), "错误信息提示了 ASR 选项",
          repr(j.get("error"))[:80])

    print()
    print("=== ③ 只给 media + asr=1 —— 必须受理并跑完 ===")
    fields = {
        "asr": "1",
        "vocal_mode": "keep",     # 商业曲目：不分离、保留原唱
        "separate": "0",
        "lang": "ja",
        "backend": "qwen",
        "encoder": "auto",
        "quality": "21",
        "options": json.dumps({"font": "Yu Gothic", "font_size": 60,
                               "group_same_unit": True}),
    }
    st, j = post_multipart(base, "/api/run", fields, {"media": media})
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
    print("=== ④ 结果校验 ===")
    res = last.get("result") or {}
    stats = res.get("stats") or {}
    health = stats.get("health") or {}
    check(bool(res.get("video")), "产出成片", res.get("video"))
    check(bool(res.get("ass")) and bool(res.get("srt")), "产出 ASS + SRT")
    names = res.get("asr_files") or []
    check(len(names) == 3, "ASR 草稿三件套齐全", str(names))
    check(any(n.endswith(".lrc") for n in names), "含增强 LRC 草稿")
    check(health.get("ok") is True, "字幕结构自检通过",
          f"overlap={len(health.get('overlap_pairs') or [])} "
          f"nonmono={len(health.get('nonmonotonic_lines') or [])}")
    check(stats.get("output_audio") == "original", "输出音轨=原唱（未擅自替换）",
          str(stats.get("output_audio")))
    check(stats.get("align_source") == "source_audio.wav", "未做人声分离",
          str(stats.get("align_source")))

    asr_info = res.get("asr_info") or {}
    check(bool(asr_info.get("language")), "回传了 ASR 语言判别", str(asr_info.get("language")))
    check(len(last.get("asr_lrc") or "") > 0, "SSE 推送了歌词草稿文本",
          f"{len(last.get('asr_lrc') or '')} 字符")

    print()
    print("=== ⑤ 成片与草稿可下载 ===")
    for n in (res.get("video"), *(names or [])):
        if not n:
            continue
        try:
            st = head_status(base, "/files/" + urllib.parse.quote(job) + "/"
                             + urllib.parse.quote(n))
            check(st == 200, f"可访问 {n}", f"status={st}")
        except Exception as e:
            check(False, f"可访问 {n}", f"{type(e).__name__}: {e}")

    print()
    print("=== ⑥ 语言是否被真正传给 ASR ===")
    # 指定 ja 时 Qwen 会把 language 强制成 Japanese（输出 text-only），
    # 因此回传的 language 必须是 Japanese 而不是自动判别的结果。
    check(asr_info.get("language") == "Japanese",
          "lang=ja 被传给 ASR（回传 language=Japanese）",
          f"实际 {asr_info.get('language')!r}")
    check(asr_info.get("collapse_ratio", 1) < 0.8,
          "零宽单元占比 < 80%（指定语言后不该大面积塌缩）",
          f"实际 {(asr_info.get('collapse_ratio') or 0)*100:.0f}%")

    print()
    if FAILS:
        print(f"!! {len(FAILS)} 项失败：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())

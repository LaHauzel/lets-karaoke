"""WebUI HTTP 全链路回归测试：真起服务、真上传、真跑任务、真拉 SSE、真重渲染。
用法：python tests/webui_http_test.py
"""
import json, mimetypes, sys, threading, time, urllib.request, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BASE = "http://127.0.0.1:7871"
LOG = []


def log(s):
    LOG.append(str(s))


# ---- 起服务（独立线程）----
import webui

webui.OUT_ROOT = ROOT / "out" / "webui_test"
webui.OUT_ROOT.mkdir(parents=True, exist_ok=True)
from http.server import ThreadingHTTPServer

srv = ThreadingHTTPServer(("127.0.0.1", 7871), webui.Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)
log(f"服务已起 {BASE}")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.status, r.read(), dict(r.headers)


def post_json(path, obj):
    req = urllib.request.Request(BASE + path, method="POST",
                                data=json.dumps(obj).encode())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def post_multipart(path, fields, files):
    b = "----kb" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fn, data) in files.items():
        body += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                 f"filename=\"{fn}\"\r\nContent-Type: "
                 f"{mimetypes.guess_type(fn)[0] or 'application/octet-stream'}\r\n\r\n"
                 ).encode()
        body += data + b"\r\n"
    body += f"--{b}--\r\n".encode()
    req = urllib.request.Request(BASE + path, method="POST", data=bytes(body))
    req.add_header("Content-Type", f"multipart/form-data; boundary={b}")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


# ---- ① 静态与元数据 ----
st, body, hdr = get("/")
log(f"① GET /            -> {st} {hdr.get('Content-Type')} {len(body)} bytes")
log(f"   <title> = {'<title>' in body.decode('utf-8', 'replace')}")
meta = json.loads(get("/api/meta")[1])
log(f"② GET /api/meta    -> fonts={meta['fonts']}")
log(f"   defaults = {meta['defaults']}")

# ---- ③ 真上传 + 真跑任务 ----
man = json.loads((ROOT / "data/synth/manifest.json").read_text(encoding="utf-8"))
audio = ROOT / man["langs"]["zh"]["variants"]["gapped_clean"]["audio"]
video = ROOT / "out/p1_e2e/zh_clean/input.mp4"
lyrics = "\n".join(man["langs"]["zh"]["lyrics_lines"])
log(f"③ 上传 media={video.name} ({video.stat().st_size/1024:.0f}KB) + 歌词 {len(lyrics)} 字")

r = post_multipart("/api/run", {
    "lyrics_text": lyrics, "vocal_mode": "keep", "lang": "zh",
    "separate": "0", "encoder": "auto",
    "options": json.dumps({"font": "Microsoft YaHei", "font_size": 60,
                           "next_line": True, "lead_ms": 320, "tail_ms": 320}),
}, {"media": (video.name, video.read_bytes())})
job = r["job"]
log(f"   任务已创建 job={job}")

# ---- ④ 拉 SSE ----
events, t0 = [], time.time()
with urllib.request.urlopen(f"{BASE}/api/events?job={job}", timeout=900) as resp:
    buf = b""
    while True:
        chunk = resp.read(1)
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"\n\n"):
            for line in buf.decode("utf-8").splitlines():
                if line.startswith("data: "):
                    d = json.loads(line[6:])
                    events.append(d)
                    for m in d["lines"]:
                        log(f"   [{d['progress']*100:5.1f}%] {m}")
                    if d["state"] in ("done", "error", "cancelled"):
                        final = d
            buf = b""
            if events and events[-1]["state"] in ("done", "error", "cancelled"):
                break
log(f"④ SSE 收到 {len(events)} 条事件，耗时 {time.time()-t0:.1f}s，终态={final['state']}")
if final.get("error"):
    log(f"   !! error = {final['error']}")
res = final.get("result") or {}
log(f"   result = {json.dumps(res, ensure_ascii=False)[:400]}")

# ---- ⑤ 文件服务（含 Range）----
if res.get("video"):
    u = f"/files/{job}/" + urllib.parse.quote(res["video"])
    st, b, h = get(u)
    log(f"⑤ GET {res['video']} -> {st} {len(b)}B AcceptRanges={h.get('Accept-Ranges')}")
    req = urllib.request.Request(BASE + u, headers={"Range": "bytes=0-99"})
    with urllib.request.urlopen(req, timeout=30) as rr:
        log(f"   Range 请求 -> {rr.status} ({rr.headers.get('Content-Range')}) 实收 {len(rr.read())}B")

# ---- ⑥ 对齐数据 ----
aj = json.loads(get(f"/api/align?job={job}")[1])
log(f"⑥ GET /api/align   -> {len(aj['lines'])} 行，health={aj['health']}")
log(f"   第0行 tokens[0..2] = " + ", ".join(
    f"{t['text']}(unit={t.get('unit')})" for t in aj["lines"][0]["tokens"][:3]))

# ---- ⑦ 微调重渲染 ----
d = post_json("/api/rerender", {"job": job, "line_offsets": {"0": 200},
                                "options": {"group_same_unit": True, "font_size": 72}})
log(f"⑦ POST /api/rerender -> v{d.get('version')} video={d.get('video')} "
    f"health_ok={d['health']['ok']} enc={d.get('encoder')}")
log(f"   logs = {d.get('logs')}")

# ---- ⑧ 404 / 穿越防护 ----
try:
    get("/files/../../etc/passwd")
    log("⑧ 路径穿越：未拦截 !!")
except Exception as e:
    log(f"⑧ 路径穿越被拦截 -> {type(e).__name__}")

srv.shutdown()
(ROOT / "out" / "p1_e2e" / "webui_http_test.txt").write_text("\n".join(LOG), encoding="utf-8")
print("ok")

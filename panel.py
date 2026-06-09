#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小虎视频翻译 · 可视化控制面板
- 纯 Python 标准库，无需额外安装
- 下载 / Whisper 转写 / ffmpeg 烧录 由面板直接执行
- 翻译润色 / 出文档 由面板调度 `claude -p`（用你现有登录，无需 API key）
启动后浏览器访问 http://127.0.0.1:8765
"""
import json, os, re, glob, subprocess, threading, time, shutil, sys, platform
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------- 路径与常量（全部相对自身，可移植） ----------
BASE = Path(__file__).resolve().parent
SCRIPTS = BASE / "scripts"
CONFIG_PATH = BASE / "config.json"
BILINGUAL_ASS = SCRIPTS / "bilingual_ass.py"
DEFAULT_OUTPUT = BASE / "output"
LLM_MODEL = os.environ.get("VIDEO_PANEL_MODEL", "sonnet")   # 翻译/出文档用的模型
PORT = int(os.environ.get("VIDEO_PANEL_PORT", "8765"))


def open_path(target):
    """跨平台打开文件/URL/文件夹。"""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        elif os.name == "nt":
            os.startfile(str(target))  # type: ignore
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception:
        pass

JOBS = {}                      # job_id -> job dict
JOBS_LOCK = threading.Lock()


# ---------- 配置读写 ----------
def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        # 首次运行：默认输出到仓库内 output/，开箱即用
        return {"output_dir": str(DEFAULT_OUTPUT), "settings": {"subtitle_type": "zh"}}


def save_config(output_dir=None, subtitle_type=None):
    cfg = load_config()
    if output_dir is not None:
        cfg["output_dir"] = output_dir
    if subtitle_type is not None:
        cfg.setdefault("settings", {})["subtitle_type"] = subtitle_type
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def output_root():
    cfg = load_config()
    raw = str(cfg.get("output_dir", "")).strip()
    if not raw:
        raise RuntimeError("config.json 里 output_dir 为空，请在面板「配置」里填写绝对路径")
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        raise RuntimeError(f"output_dir 必须是绝对路径：{raw}")
    (p / "tmp").mkdir(parents=True, exist_ok=True)
    (p / "data").mkdir(parents=True, exist_ok=True)
    return p


# ---------- 任务日志 ----------
def new_job(params):
    jid = time.strftime("%Y%m%d-%H%M%S")
    job = {"id": jid, "status": "running", "stage": "排队中",
           "log": [], "outputs": [], "error": "", "params": params,
           "lock": threading.Lock()}
    with JOBS_LOCK:
        JOBS[jid] = job
    return job


def jlog(job, msg):
    with job["lock"]:
        job["log"].append(msg)


def jstage(job, s):
    job["stage"] = s
    jlog(job, f"\n=== {s} ===")


class StepError(Exception):
    pass


def run_cmd(job, cmd, cwd=None, env=None):
    """执行命令并把输出实时写入任务日志，非 0 退出抛 StepError。"""
    jlog(job, "$ " + " ".join(str(c) for c in cmd))
    e = os.environ.copy()
    if env:
        e.update(env)
    p = subprocess.Popen([str(c) for c in cmd], cwd=cwd, env=e,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    for line in p.stdout:
        jlog(job, line.rstrip("\n"))
    p.wait()
    if p.returncode != 0:
        raise StepError(f"命令失败（退出码 {p.returncode}）：{' '.join(str(c) for c in cmd[:3])} ...")
    return p.returncode


def claude_generate(job, prompt):
    """调度 claude -p 生成文本（翻译 / markdown）。"""
    p = subprocess.Popen(["claude", "-p", "--model", LLM_MODEL, "--output-format", "text"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    out, err = p.communicate(prompt)
    if p.returncode != 0:
        raise StepError("claude 调用失败：" + (err or "")[:300])
    return strip_fences(out.strip())


def strip_fences(t):
    t = t.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n", "", t)
        t = re.sub(r"\n```$", "", t)
    return t.strip()


def sanitize(name):
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]", " ", name).strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:60] or "video").strip()


def ffprobe_height(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", str(path)],
            text=True).strip().splitlines()
        return int(out[0])
    except Exception:
        return 1080


def srt_to_text(srt_path):
    """SRT -> 纯文本（去序号与时间戳）。"""
    lines = Path(srt_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    out = []
    for ln in lines:
        s = ln.strip()
        if not s or s.isdigit() or "-->" in s:
            continue
        out.append(s)
    return "\n".join(out)


# ---------- 下载 ----------
def fetch_title(url):
    for extra in ([], ["--cookies-from-browser", "chrome"]):
        try:
            t = subprocess.check_output(
                ["yt-dlp", "--skip-download", "--print", "%(title)s", *extra, url],
                text=True, stderr=subprocess.DEVNULL, timeout=60).strip().splitlines()
            if t:
                return sanitize(t[0])
        except Exception:
            continue
    return None


def ytdlp_download_video(job, url, workdir):
    out_tpl = str(workdir / "video.%(ext)s")
    base = ["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
            "-o", out_tpl, url]
    try:
        run_cmd(job, base)
    except StepError:
        jlog(job, "↻ 首次失败，改用浏览器 cookies 重试…")
        run_cmd(job, base[:-1] + ["--cookies-from-browser", "chrome", url])
    vids = glob.glob(str(workdir / "video.*"))
    if not vids:
        raise StepError("下载完成但找不到视频文件")
    return vids[0]


def ytdlp_download_audio(job, url, workdir):
    out_tpl = str(workdir / "audio.%(ext)s")
    base = ["yt-dlp", "-x", "--audio-format", "mp3", "-o", out_tpl, url]
    try:
        run_cmd(job, base)
    except StepError:
        jlog(job, "↻ 首次失败，改用浏览器 cookies 重试…")
        run_cmd(job, base[:-1] + ["--cookies-from-browser", "chrome", url])
    auds = glob.glob(str(workdir / "audio.*"))
    if not auds:
        raise StepError("下载完成但找不到音频文件")
    return auds[0]


def douyin_download(job, url, workdir, want_video):
    flag = "--video" if want_video else "--audio"
    run_cmd(job, ["python3", str(SCRIPTS / "douyin_download.py"), url, flag,
                  "--outdir", str(output_root())])
    # 抖音脚本写到 <root>/tmp/，取最新文件
    tmp = output_root() / "tmp"
    cands = sorted(tmp.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for c in cands:
        if want_video and c.suffix.lower() in (".mp4", ".mov", ".mkv"):
            return str(c)
        if not want_video and c.suffix.lower() in (".m4a", ".mp3", ".wav", ".aac"):
            return str(c)
    raise StepError("抖音下载完成但未定位到产物文件")


def extract_audio(job, video_path, workdir):
    audio = workdir / "audio.wav"
    run_cmd(job, ["ffmpeg", "-y", "-i", str(video_path), "-vn",
                  "-ac", "1", "-ar", "16000", str(audio)])
    return str(audio)


# ---------- 转写 ----------
def transcribe(job, audio_path, workdir):
    src_srt = workdir / "src.srt"
    run_cmd(job, ["python3", str(SCRIPTS / "transcribe_srt.py"), str(audio_path),
                  "--output", str(src_srt)])
    if not src_srt.exists():
        raise StepError("转写完成但未生成 SRT")
    return str(src_srt)


# ---------- 翻译 ----------
ZH_PROMPT = """你是专业字幕译者。下面是一个 SRT 字幕文件，请把它翻译成自然流畅的简体中文。
严格规则：
1. 每一条字幕的「序号行」和「时间戳行」原样保留，绝对不要改动；
2. 只翻译文本行；一条字幕仍是一条；
3. 输出合法的 SRT，不要任何解释、不要代码围栏。
=== SRT 开始 ===
{content}
=== SRT 结束 ==="""

BI_PROMPT = """你是专业字幕译者。下面是一个 SRT 字幕文件，请生成「中英双语」SRT。
严格规则：
1. 每一条字幕的「序号行」和「时间戳行」原样保留；
2. 每条字幕的文本改成两行：第一行简体中文翻译，第二行英文原文；
3. 输出合法 SRT，不要任何解释、不要代码围栏。
=== SRT 开始 ===
{content}
=== SRT 结束 ==="""

DOC_PROMPT = """下面是一段视频的转写文本。请整理成一篇干净的简体中文 Markdown 文档。
要求：
1. 不要增删改原意，只做标点、合句、按语义分段；若原文非中文，请翻译成通顺的简体中文；
2. 开头加 YAML front matter：title 与 source（来源）；
3. 适当用小标题分段，便于阅读；
4. 只输出 Markdown 正文，不要解释、不要代码围栏。
标题：{title}
来源：{source}
=== 转写文本开始 ===
{content}
=== 转写文本结束 ==="""


def translate_srt(job, src_srt, workdir, bilingual):
    content = Path(src_srt).read_text(encoding="utf-8", errors="ignore")
    tmpl = BI_PROMPT if bilingual else ZH_PROMPT
    jlog(job, f"调度 claude（{LLM_MODEL}）翻译字幕，请稍候…")
    out = claude_generate(job, tmpl.format(content=content))
    dst = workdir / ("bi.srt" if bilingual else "zh.srt")
    dst.write_text(out + "\n", encoding="utf-8")
    jlog(job, f"翻译完成 -> {dst.name}")
    return str(dst)


# ---------- 烧录 ----------
def burn_zh(job, video_path, zh_srt, out_path, workdir):
    style = ("FontName=PingFang SC,FontSize=18,PrimaryColour=&Hffffff,"
             "OutlineColour=&H80000000,BorderStyle=1,Outline=2,Shadow=0,MarginV=28")
    run_cmd(job, ["ffmpeg", "-y", "-i", str(video_path),
                  "-vf", f"subtitles={Path(zh_srt).name}:force_style='{style}'",
                  "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                  "-c:a", "copy", str(out_path)], cwd=str(workdir))


def burn_bilingual(job, video_path, bi_srt, out_path, workdir):
    height = ffprobe_height(video_path)
    ass = workdir / "bi.ass"
    run_cmd(job, ["python3", str(BILINGUAL_ASS), Path(bi_srt).name,
                  "--output", ass.name, "--height", str(height)], cwd=str(workdir))
    run_cmd(job, ["ffmpeg", "-y", "-i", str(video_path),
                  "-vf", f"ass={ass.name}",
                  "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                  "-c:a", "copy", str(out_path)], cwd=str(workdir))


# ---------- 主流程 ----------
def run_job(job):
    try:
        p = job["params"]
        root = output_root()
        workdir = root / "tmp" / job["id"]
        workdir.mkdir(parents=True, exist_ok=True)
        source = p["source"]          # youtube | douyin | local
        mode = p["mode"]              # doc | translate
        sub_type = p.get("subtitle_type", "zh")
        url = p.get("url", "").strip()
        local = p.get("file", "").strip()

        # 1) 确定标题
        jstage(job, "准备")
        if source == "local":
            if not local or not Path(local).exists():
                raise StepError(f"本地文件不存在：{local}")
            title = sanitize(Path(local).stem)
            source_label = local
        else:
            if not url:
                raise StepError("请填写视频链接")
            title = (fetch_title(url) if source == "youtube" else None) or job["id"]
            source_label = url
        jlog(job, f"标题：{title}")

        # 2) 获取视频/音频
        video_path = None
        if mode == "translate":
            jstage(job, "下载视频")
            if source == "youtube":
                video_path = ytdlp_download_video(job, url, workdir)
            elif source == "douyin":
                video_path = douyin_download(job, url, workdir, want_video=True)
            else:
                video_path = local
            jstage(job, "提取音频")
            audio_path = extract_audio(job, video_path, workdir)
        else:  # doc
            jstage(job, "获取音频")
            if source == "youtube":
                audio_path = ytdlp_download_audio(job, url, workdir)
            elif source == "douyin":
                audio_path = douyin_download(job, url, workdir, want_video=False)
            else:
                audio_path = extract_audio(job, local, workdir)

        # 3) 转写
        jstage(job, "Whisper 转写")
        src_srt = transcribe(job, audio_path, workdir)

        # 4) 出文档 or 翻译烧录
        if mode == "doc":
            jstage(job, "生成 Markdown（调度 Claude）")
            text = srt_to_text(src_srt)
            md = claude_generate(job, DOC_PROMPT.format(
                title=title, source=source_label, content=text))
            out_md = root / "data" / f"{title}-中文.md"
            out_md.write_text(md + "\n", encoding="utf-8")
            job["outputs"].append(str(out_md))
            jlog(job, f"已生成：{out_md}")
        else:
            jstage(job, "翻译字幕（调度 Claude）")
            bilingual = (sub_type == "bilingual")
            tr_srt = translate_srt(job, src_srt, workdir, bilingual)
            jstage(job, "烧录字幕")
            suffix = "双语字幕" if bilingual else "中文字幕"
            out_vid = root / "data" / f"{title}-{suffix}.mp4"
            if bilingual:
                burn_bilingual(job, video_path, tr_srt, out_vid, workdir)
            else:
                burn_zh(job, video_path, tr_srt, out_vid, workdir)
            job["outputs"].append(str(out_vid))
            jlog(job, f"已生成：{out_vid}")

        job["stage"] = "完成"
        job["status"] = "done"
        jlog(job, "\n✅ 全部完成")
    except Exception as ex:
        job["status"] = "error"
        job["error"] = str(ex)
        job["stage"] = "出错"
        jlog(job, f"\n❌ 出错：{ex}")


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def log_message(self, *a):
        pass

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, HTML, "text/html")
        elif u.path == "/api/config":
            cfg = load_config()
            self._json({"output_dir": cfg.get("output_dir", ""),
                        "subtitle_type": cfg.get("settings", {}).get("subtitle_type", "zh")})
        elif u.path == "/api/status":
            q = parse_qs(u.query)
            jid = q.get("id", [""])[0]
            since = int(q.get("since", ["0"])[0])
            job = JOBS.get(jid)
            if not job:
                self._json({"error": "no such job"}, 404); return
            with job["lock"]:
                lines = job["log"][since:]
                total = len(job["log"])
            self._json({"status": job["status"], "stage": job["stage"],
                        "error": job["error"], "outputs": job["outputs"],
                        "lines": lines, "next": total})
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(body) if body.strip() else {}
        except Exception:
            data = {}

        if u.path == "/api/config":
            try:
                cfg = save_config(output_dir=data.get("output_dir"),
                                  subtitle_type=data.get("subtitle_type"))
                # 试着创建目录
                if cfg.get("output_dir"):
                    output_root()
                self._json({"ok": True})
            except Exception as ex:
                self._json({"ok": False, "error": str(ex)}, 400)

        elif u.path == "/api/run":
            try:
                output_root()  # 校验配置
            except Exception as ex:
                self._json({"ok": False, "error": str(ex)}, 400); return
            job = new_job(data)
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            self._json({"ok": True, "id": job["id"]})

        elif u.path == "/api/open":
            try:
                root = output_root()
                open_path(root / "data")
                self._json({"ok": True})
            except Exception as ex:
                self._json({"ok": False, "error": str(ex)}, 400)
        else:
            self._send(404, "not found", "text/plain")


HTML = r"""<!doctype html><html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>视频翻译 · 控制面板</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e7eaf0;--mut:#8b93a7;--acc:#6ea8fe;--ok:#3ecf8e;--err:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"PingFang SC",sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 22px;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 18px;margin-bottom:16px}
.card h2{font-size:14px;margin:0 0 14px;color:var(--fg)}
label{display:block;color:var(--mut);font-size:12px;margin:10px 0 5px}
input[type=text],select{width:100%;background:#0d0f14;border:1px solid var(--line);color:var(--fg);
 border-radius:9px;padding:9px 11px;font-size:14px;outline:none}
input:focus,select:focus{border-color:var(--acc)}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1;min-width:180px}
.seg{display:flex;gap:8px;flex-wrap:wrap}
.seg button{flex:1;min-width:110px;background:#0d0f14;border:1px solid var(--line);color:var(--mut);
 padding:9px;border-radius:9px;cursor:pointer;font-size:13px}
.seg button.on{border-color:var(--acc);color:var(--fg);background:#16203a}
.btn{background:var(--acc);color:#06122b;border:none;border-radius:10px;padding:11px 18px;
 font-size:14px;font-weight:600;cursor:pointer}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--fg);font-weight:500}
.hint{color:var(--mut);font-size:12px;margin-top:8px}
.bar{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut)}
.dot.run{background:var(--acc);animation:pulse 1s infinite}.dot.done{background:var(--ok)}.dot.err{background:var(--err)}
@keyframes pulse{50%{opacity:.3}}
pre{background:#0a0c10;border:1px solid var(--line);border-radius:10px;padding:12px;
 max-height:340px;overflow:auto;font:12px/1.55 "SF Mono",Menlo,monospace;color:#c7cedb;white-space:pre-wrap;word-break:break-word}
.out a{color:var(--acc);text-decoration:none}.out div{margin:4px 0}
.muted{color:var(--mut)}.err{color:var(--err)}
.save{font-size:12px;color:var(--ok);margin-left:10px}
</style></head><body><div class="wrap">
<h1>🎬 视频翻译 · 控制面板</h1>
<p class="sub">下载 / 转写 / 烧录由面板执行；翻译与出文档调度 Claude（你的登录，无需 API key）</p>

<div class="card">
  <h2>⚙️ 配置 <span id="saveMsg" class="save"></span></h2>
  <label>成品输出目录（绝对路径）</label>
  <input id="outdir" type="text" placeholder="/Users/you/.../video-out">
  <label>默认字幕类型</label>
  <div class="seg" id="defSub">
    <button data-v="zh">中文字幕</button>
    <button data-v="bilingual">中英双语</button>
  </div>
  <div style="margin-top:14px"><button class="btn ghost" onclick="saveCfg()">保存配置</button>
  <button class="btn ghost" onclick="openFolder()">在 Finder 打开产物目录</button></div>
</div>

<div class="card">
  <h2>➕ 新建任务</h2>
  <label>来源</label>
  <div class="seg" id="source">
    <button data-v="youtube">YouTube 链接</button>
    <button data-v="douyin">抖音链接</button>
    <button data-v="local">本地文件</button>
  </div>
  <div id="urlBox"><label>视频链接</label>
    <input id="url" type="text" placeholder="粘贴 YouTube / 抖音 链接"></div>
  <div id="fileBox" style="display:none"><label>本地视频文件绝对路径</label>
    <input id="file" type="text" placeholder="/Users/you/Movies/xxx.mp4"></div>

  <label>做什么</label>
  <div class="seg" id="mode">
    <button data-v="translate">翻译视频（烧录字幕）</button>
    <button data-v="doc">转写成 Markdown 文档</button>
  </div>
  <div id="subBox"><label>字幕类型</label>
    <div class="seg" id="subType">
      <button data-v="zh">中文字幕</button>
      <button data-v="bilingual">中英双语</button>
    </div></div>

  <div style="margin-top:16px"><button class="btn" id="runBtn" onclick="run()">▶ 开始</button></div>
  <p class="hint">转写引擎：Apple Silicon 走 MLX Whisper（GPU 加速），其它机器自动降级 faster-whisper。首次会下载模型，稍慢。</p>
</div>

<div class="card">
  <h2>📡 运行日志</h2>
  <div class="bar"><span class="dot" id="dot"></span><span id="stage" class="muted">空闲</span></div>
  <pre id="log">还没有任务。填好上面的表单点「开始」即可。</pre>
  <div class="out" id="outputs"></div>
</div>

<script>
let cfg={subtitle_type:"zh"}, sel={source:"youtube",mode:"translate",subType:"zh",defSub:"zh"};
function segInit(id,key,cb){const g=document.getElementById(id);
 g.querySelectorAll("button").forEach(b=>b.onclick=()=>{
  g.querySelectorAll("button").forEach(x=>x.classList.remove("on"));
  b.classList.add("on");sel[key]=b.dataset.v;cb&&cb(b.dataset.v);});}
function setSeg(id,v){const g=document.getElementById(id);
 g.querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.v===v));}

segInit("source","source",v=>{
  document.getElementById("urlBox").style.display=v==="local"?"none":"block";
  document.getElementById("fileBox").style.display=v==="local"?"block":"none";});
segInit("mode","mode",v=>{document.getElementById("subBox").style.display=v==="translate"?"block":"none";});
segInit("subType","subType");
segInit("defSub","defSub");

async function loadCfg(){const r=await fetch("/api/config");cfg=await r.json();
 document.getElementById("outdir").value=cfg.output_dir||"";
 sel.subType=sel.defSub=cfg.subtitle_type||"zh";
 setSeg("defSub",sel.defSub);setSeg("subType",sel.subType);}
setSeg("source","youtube");setSeg("mode","translate");

async function saveCfg(){
 const body={output_dir:document.getElementById("outdir").value.trim(),subtitle_type:sel.defSub};
 const r=await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
 const d=await r.json();const m=document.getElementById("saveMsg");
 if(d.ok){m.textContent="已保存 ✓";m.style.color="var(--ok)";}else{m.textContent=d.error;m.style.color="var(--err)";}
 setTimeout(()=>m.textContent="",4000);}
async function openFolder(){await fetch("/api/open",{method:"POST"});}

let polling=null;
async function run(){
 const body={source:sel.source,mode:sel.mode,subtitle_type:sel.subType,
  url:document.getElementById("url").value.trim(),file:document.getElementById("file").value.trim()};
 const r=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
 const d=await r.json();
 if(!d.ok){alert("无法启动："+d.error);return;}
 document.getElementById("runBtn").disabled=true;
 document.getElementById("log").textContent="";
 document.getElementById("outputs").innerHTML="";
 poll(d.id,0);}

function setDot(s){const dot=document.getElementById("dot");dot.className="dot "+
 (s==="running"?"run":s==="done"?"done":s==="error"?"err":"");}
async function poll(id,since){
 const r=await fetch("/api/status?id="+id+"&since="+since);const d=await r.json();
 const log=document.getElementById("log");
 if(d.lines&&d.lines.length){log.textContent+=(log.textContent?"\n":"")+d.lines.join("\n");log.scrollTop=log.scrollHeight;}
 document.getElementById("stage").textContent=d.stage||"";setDot(d.status);
 if(d.status==="running"){setTimeout(()=>poll(id,d.next),1000);}
 else{
  document.getElementById("runBtn").disabled=false;
  const ob=document.getElementById("outputs");
  if(d.status==="done"&&d.outputs.length){
    ob.innerHTML="<div class='muted' style='margin-top:8px'>产物：</div>"+
     d.outputs.map(p=>"<div>📄 "+p+"</div>").join("");
  }else if(d.status==="error"){ob.innerHTML="<div class='err'>失败："+(d.error||"")+"</div>";}
 }}
loadCfg();
</script></div></body></html>"""


def main():
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  视频翻译 · 控制面板已启动")
    print(f"  → 浏览器打开：{url}")
    print(f"  → 停止：在本窗口按 Ctrl+C\n")
    if os.environ.get("VIDEO_PANEL_NO_BROWSER") != "1":
        open_path(url)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline.py — 视频链接 → 视频 + 转写稿 + 笔记草稿，一条命令跑完。

四步，每步幂等（已有产物自动跳过）：

    S0  前置检查   浏览器 / 模型 / ffmpeg
    S1  解析+抓取  抖音走 CDP；B 站走公开 API
    S2  下载       抖音取最高码率；B 站 DASH 双轨合并
    S3  转写       B 站有字幕就直接用，否则本地 faster-whisper（音频不出机器）
    S4  出草稿     按模板生成 note_draft.md（frontmatter 填好，正文待归纳）

S4 之后只剩一件事：把转写稿归纳成知识点。这一步故意不自动化——
机器分不清「绘画结束」其实是「会话结束」，转写对不对得靠人看一眼原片。

用法：
    python pipeline.py "https://v.douyin.com/XXXX/"
    python pipeline.py "https://www.bilibili.com/video/BV1xxxxxxxxx"
    python pipeline.py "https://v.douyin.com/XXXX/" --prompt "领域术语A、术语B"
    python pipeline.py --queue queue.jsonl        # 消费 clip_watch.py 的产出
    python pipeline.py "<链接>" --skip-asr         # 只要视频
    python pipeline.py --doctor                   # 只打印环境诊断
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

import config  # noqa: E402

PY = config.python_exe()
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def log(stage: str, msg: str) -> None:
    print(f"[{stage}] {msg}", flush=True)


# ---------------------------------------------------------------- S0

def preflight(need_browser: bool = True, model_required: bool = True) -> bool:
    """need_browser=False 时（纯 B 站任务）不强制要求浏览器。

    model_required=False 表示只有 B 站链接：命中字幕就不需要 whisper，
    模型缺失只警告不拦截，由实际是否拿到字幕决定成败。
    """
    ok = True
    if need_browser:
        exe = config.get("browser_exe")
        if not exe or not os.path.isfile(exe):
            log("S0", "✗ 找不到浏览器，设 DY_BROWSER_EXE 或写入 ~/.video-to-obsidian.json")
            ok = False
    model = config.get("model_dir")
    if not model or not os.path.isdir(model):
        msg = "找不到模型目录，设 WHISPER_MODEL（可用 fetch_model.py 下载）"
        if model_required:
            log("S0", "✗ " + msg); ok = False
        else:
            log("S0", "! " + msg + "（B 站有字幕就不需要它）")
    import shutil
    for b in ("ffmpeg", "ffprobe"):
        if not shutil.which(b):
            log("S0", f"✗ 缺少 {b}"); ok = False
    if ok:
        log("S0", "✓ " + ("浏览器 / 模型 / ffmpeg 齐备" if need_browser else "模型 / ffmpeg 齐备"))
    return ok


# ---------------------------------------------------------------- S1

def resolve_aweme_id(url: str) -> str | None:
    """短链 → aweme_id。优先跟 302，其次从 HTML 里捞。不需要浏览器。"""
    m = re.search(r"/video/(\d+)", url) or re.search(r"/share/video/(\d+)", url)
    if m:
        return m.group(1)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            final = r.geturl()
            m = re.search(r"/video/(\d+)", final) or re.search(r"/share/video/(\d+)", final)
            if m:
                return m.group(1)
            body = r.read(400_000).decode("utf-8", "replace")
            for pat in (r'"aweme_id"\s*:\s*"?(\d{15,})', r"/video/(\d{15,})"):
                m = re.search(pat, body)
                if m:
                    return m.group(1)
    except Exception as e:
        log("S1", f"短链解析异常：{type(e).__name__}: {e}")
    return None


def step_collect(aid: str, workdir: str, fresh: bool) -> str | None:
    out = os.path.join(workdir, f"dy-{aid}.api.json")
    if os.path.exists(out) and not fresh:
        log("S1", f"跳过（已存在 {os.path.basename(out)}）")
        return out
    if str(config.get("headless", default=False)).strip().lower() in ("1", "true", "yes", "on"):
        log("S1", "后台抓取（headless，不弹窗口）…")
    else:
        log("S1", "启动隔离浏览器实例抓取（不影响你正在用的浏览器）…")
    r = subprocess.run([PY, os.path.join(HERE, "fetch_douyin.py"), aid, "--outdir", workdir],
                       capture_output=True)
    txt = (r.stdout + r.stderr).decode("utf-8", "replace")
    if r.returncode != 0 or not os.path.exists(out):
        log("S1", "✗ 没抓到 aweme 详情")
        for line in txt.strip().splitlines()[-6:]:
            log("S1", "   " + line.strip())
        return None
    log("S1", f"✓ {os.path.basename(out)}  {os.path.getsize(out) / 1024:.0f}KB")
    return out


# ---------------------------------------------------------------- S2

def step_download(api_json: str, workdir: str) -> str | None:
    dl_root = os.path.join(workdir, "downloads")
    r = subprocess.run([PY, os.path.join(HERE, "douyin_dl.py"), api_json, "--out", dl_root],
                       capture_output=True)
    txt = (r.stdout + r.stderr).decode("utf-8", "replace")
    for line in txt.strip().splitlines():
        log("S2", line.strip())
    if r.returncode != 0:
        return None
    m = re.search(r"douyin-(\d+)", txt)
    return os.path.join(dl_root, f"douyin-{m.group(1)}") if m else None


# ---------------------------------------------------------------- S3

def step_asr(folder: str, prompt: str | None) -> str | None:
    video = os.path.join(folder, "video.mp4")
    if not os.path.exists(video):
        log("S3", f"✗ 找不到 {video}")
        return None
    cmd = [PY, os.path.join(HERE, "asr_local.py"), video]
    if prompt:
        cmd += ["--prompt", prompt]
    r = subprocess.run(cmd, capture_output=True)
    txt = (r.stdout + r.stderr).decode("utf-8", "replace")
    for line in txt.strip().splitlines()[-4:]:
        log("S3", line.strip())
    out = os.path.join(folder, "video.txt")
    return out if os.path.exists(out) else None


# ---------------------------------------------------------------- S4

def make_title(desc: str, fallback: str) -> str:
    """抖音 desc 是整段文案，标题通常在双空格/换行之前。
    顺序：去 hashtag → 按双空格切 → 仍超长按句号切 → 硬截 40。"""
    t = re.sub(r"#\S+", "", desc).strip()
    parts = [p.strip() for p in re.split(r"\s{2,}|\n", t) if p.strip()]
    cand = parts[0] if parts else t
    if len(cand) > 40:
        cand = re.split(r"[。！？]", cand)[0]
    if len(cand) > 40:
        cand = cand[:40].rstrip("，,、 ")
    return cand or fallback


def pick_tags(desc: str) -> list[str]:
    seen, out = set(), []
    for t in re.findall(r"#([^\s#]{1,20})", desc):
        if t not in seen:
            seen.add(t); out.append(t)
    return out[:5] or ["待归类"]


ASR_GATE = {
    "whisper": [
        "转写自本地 faster-whisper。真人口播字错率约 3–4%，",
        "关键结论必须回看原片核对，**未经复核前不要当作定论引用**。",
    ],
    "bili-ai": [
        "转写自 B 站 AI 字幕（平台侧 ASR，不是本地转写）。",
        "对人名、专有名词的识别明显好于本地 whisper，但**同音字与数字仍可能出错**，关键处要回看原片。",
    ],
    "bili-cc": [
        "转写自 UP 主上传的字幕（人工）。",
        "准确度较高，但**仍需核对数字、专有名词与断句**，不要当作定论引用。",
    ],
    "ytdlp-sub": [
        "转写自平台侧字幕（经 yt-dlp 获取）。",
        "自动字幕可能有同音字与断句错误，关键处回看原片核对，**未经复核前不要当作定论引用**。",
    ],
}


def step_draft(folder: str, source_url: str, overlay: list[dict] | None = None,
               asr_source: str = "whisper") -> str | None:
    meta_p = os.path.join(folder, "metadata.json")
    meta = json.load(open(meta_p, encoding="utf-8")) if os.path.exists(meta_p) else {}
    desc = (meta.get("desc") or "").strip()
    author = (meta.get("author") or "").strip()
    dur_s = round((meta.get("duration_ms") or 0) / 1000)
    title = make_title(desc, f"视频-{meta.get('aweme_id')}")
    tags = pick_tags(desc)
    # 本地转写与 B 站字幕格式一致、文件名分开，先找转写再找字幕
    txt_p = next((p for p in (os.path.join(folder, "video.txt"),
                              os.path.join(folder, "subtitle.txt"))
                  if os.path.exists(p)), None)
    body = open(txt_p, encoding="utf-8").read() if txt_p else "（未转写）"

    # 画面文字单独成节，插在原文转写之前
    overlay_sec: list[str] = []
    asr_no = "五"
    if overlay:
        asr_no = "六"
        overlay_sec = [
            "## 五、画面文字（视觉识别）",
            "",
            "> 以下内容来自画面里的文字卡，**不在口播转写中**。归纳配方/参数时优先用它。",
            "",
        ]
        for b in overlay:
            mm, ss = divmod(int(b["second"]), 60)
            overlay_sec += [f"### `{mm:02d}:{ss:02d}`", "", "```text", b["text"], "```", ""]

    lines = [
        "---",
        f"标题: {title}",
        f"来源: {source_url}",
        f"作者: {author}",
        f"创建时间: {time.strftime('%Y-%m-%d')}",
        "状态: 待复核",
        "标签:",
        *[f"  - {t}" for t in tags],
        "---",
        "",
        f"# {title}",
        "",
        "> **一句话结论**",
        "> （待填）",
        "",
        "## 一、待填",
        "",
        "## 二、待填",
        "",
        "## 三、可执行动作",
        "",
        "- [ ] 待填",
        "",
        "## 四、存疑 / 未验证",
        "",
        "- （待填）",
        "",
        *overlay_sec,
        f"## {asr_no}、原文转写",
        "",
        "> [!note]- 带时间戳全文（点击展开）",
        *["> " + ln for ln in body.strip().splitlines()],
        "",
        "---",
        "",
        "> [!warning] 质量闸门",
        *[f"> {ln}" for ln in ASR_GATE.get(asr_source, ASR_GATE["whisper"])],
    ]
    if overlay:
        lines.append("> 画面文字由视觉模型识别，已抽帧目视复核。")
    lines.append("")

    out = os.path.join(folder, "note_draft.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log("S4", f"✓ 草稿 {out}")
    log("S4", f"  标题: {title}")
    log("S4", f"  标签: {', '.join(tags)}（取自 hashtag，需人工确认）")
    log("S4", f"  时长: {dur_s}s  正文待归纳")
    log("S4", f"  转写来源: {asr_source}")
    if overlay:
        log("S4", f"  画面文字: {len(overlay)} 处已并入草稿")
    return out


# ---------------------------------------------------------------- 平台分派

def detect_platform(url: str) -> str:
    """抖音、B 站走自家方案；其余全部交给 yt-dlp 兜底。"""
    u = url.lower()
    if "douyin.com" in u or "iesdouyin.com" in u:
        return "douyin"
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    return "ytdlp"


PLAYLIST_HINTS = re.compile(
    r"(bilibili\.com/list/|space\.bilibili\.com/\d+/(?:video|favlist|channel|uploads)"
    r"|favlist|collectiondetail|medialist|playlist\?|list=)", re.I)


def _bili_cookie() -> str:
    """B 站空间/合集接口必带 buvid3（否则 412），有 SESSDATA 更稳。"""
    import bili_fetch
    import urllib.request
    sess = config.bili_sessdata()
    parts = []
    if sess:
        parts.append(f"SESSDATA={sess}")
    try:
        req = urllib.request.Request(
            "https://api.bilibili.com/x/frontend/finger/spi",
            headers={"User-Agent": bili_fetch.UA, "Referer": "https://www.bilibili.com/"})
        d = json.load(urllib.request.urlopen(req, timeout=10))
        parts.append(f"buvid3={d['data']['b_3']}")
    except Exception:
        pass
    return "; ".join(parts) or "buvid3=0"


def _expand_bilibili(url: str) -> list[str] | None:
    """B 站合集/收藏夹/UP 主投稿 → bvid 列表。识别不了返回 None。"""
    import bili_fetch

    def paged(fetch_page: callable, cap: int) -> list[str]:
        """fetch_page(pn) 返回当页 bvid 列表；空页即停。WBI 签名必须逐页算，
        占位符会把 pn=%7Bpn%7D 签进去导致服务端校验失败。"""
        out, pn = [], 1
        while pn <= 10:  # 每页最多 50-100 条，10 页已够 cap，再超由 cap 决定
            items = fetch_page(pn)
            if not items:
                break
            out.extend(items)
            if len(out) >= cap:
                break
            pn += 1
        return out

    cookie = _bili_cookie()
    mids = re.search(r"space\.bilibili\.com/(\d+)", url)
    fav2 = re.search(r"space\.bilibili\.com/(\d+)/favlist[^?]*\?(?:[^&]*&)*fid=(\d+)", url)
    season = re.search(r"bilibili\.com/list/(\d+)-(\d+)", url) \
        or re.search(r"collectiondetail[^?]*\?(?:[^&]*&)*sid=(\d+)", url)

    # UP 主投稿页（WBI 签名 + 登录态，实测匿名会被 412）
    if mids and re.search(r"/(?:video|uploads/video|channel|uploads)", url):
        mid = mids.group(1)

        def arc(pn):
            qs = bili_fetch._wbi_query(
                {"mid": mid, "pn": pn, "ps": 50, "order": "pubdate"}, cookie)
            d = bili_fetch._get(
                f"https://api.bilibili.com/x/space/wbi/arc/search?{qs}", cookie)
            if d.get("code") != 0:
                raise RuntimeError(f"B 站接口错误 {d.get('code')}：{d.get('message')}")
            return [v["bvid"] for v in ((d.get("data") or {}).get("list") or {})
                    .get("vlist", [])]

        items = paged(arc, 100)
        return [f"https://www.bilibili.com/video/{b}" for b in items]

    # 收藏夹（space.favlist?fid= 形态）
    if fav2:
        fid = fav2.group(2)

        def fav(pn):
            d = bili_fetch._get(
                f"https://api.bilibili.com/x/v3/fav/resource/list?"
                f"media_id={fid}&pn={pn}&ps=50&order=mtime&type=0&tid=0&platform=web",
                cookie)
            if d.get("code") != 0:
                raise RuntimeError(f"B 站接口错误 {d.get('code')}：{d.get('message')}")
            return [m["bvid"] for m in (d.get("data") or {}).get("medias") or []
                    if m.get("bvid")]

        items = paged(fav, 100)
        return [f"https://www.bilibili.com/video/{b}" for b in items]

    # 合集 / 系列（新版 list/<mid>-<sid> 与旧版 collectiondetail?sid=）
    if season:
        mid, sid = season.group(1), season.group(2)

        def season_page(pn):
            d = bili_fetch._get(
                f"https://api.bilibili.com/x/polymer/space/seasons_archives_list?"
                f"mid={mid}&season_id={sid}&page_num={pn}&page_size=100", cookie)
            if d.get("code") != 0:
                raise RuntimeError(f"B 站接口错误 {d.get('code')}：{d.get('message')}")
            return [a["bvid"] for a in (d.get("data") or {}).get("archives") or []]

        items = paged(season_page, 100)
        return [f"https://www.bilibili.com/video/{b}" for b in items]
    return None


def expand_collection(url: str) -> list[str] | None:
    """合集/播放列表/UP 主投稿页 → 单视频链接列表。不像合集返回 None。"""
    if not PLAYLIST_HINTS.search(url):
        return None
    try:
        if detect_platform(url) == "bilibili":
            subs = _expand_bilibili(url)
            if subs is None:
                subs = None  # 识别不了的 B 站形态按单链接处理
        else:
            import ytdlp_fetch
            subs = ytdlp_fetch.expand(url)
    except Exception as e:
        log("S0", f"✗ 展开失败（按单链接处理）：{e}")
        return None
    if not subs:
        log("S0", f"✗ 合集展开为空（按单链接处理）：{url}")
        return None
    log("S0", f"✓ {url} 是合集/播放列表，展开 {len(subs)} 条")
    return subs


def stage_ytdlp(url: str, workdir: str) -> tuple[str | None, str, dict]:
    """yt-dlp 兜底的 S1+S2：元数据 + 下载 + 平台字幕。返回 (folder, stage, info)。"""
    log("S1", f"处理 {url}")
    try:
        import ytdlp_fetch
        folder = ytdlp_fetch.fetch(url, workdir, log=lambda m: log("S1", m))
    except Exception as e:
        log("S1", f"✗ {e}")
        return None, "S1", {}
    return folder, "S2", {"subtitle": None}


def stage_douyin(url: str, workdir: str, fresh: bool) -> tuple[str | None, str, dict]:
    """抖音的 S1+S2：CDP 抓接口 + ffmpeg 下载。返回 (folder, stage, info)。"""
    log("S1", f"处理 {url}")
    aid = resolve_aweme_id(url)
    if not aid:
        log("S1", "✗ 无法解析 aweme_id（链接失效 / 非抖音 / 网络问题）")
        return None, "S1", {}
    log("S1", f"✓ aweme_id = {aid}")

    api = step_collect(aid, workdir, fresh)
    if not api:
        return None, "S1", {}
    folder = step_download(api, workdir)
    if not folder:
        log("S2", "✗ 下载失败")
        return None, "S2", {}
    return folder, "S2", {}


def stage_bilibili(url: str, workdir: str, page: int | None,
                   fresh: bool = False, subtitle: bool = True) -> tuple[str | None, str, dict]:
    """B 站的 S1+S2：公开 API，不需要浏览器。返回 (folder, stage, info)。"""
    log("S1", f"处理 {url}")
    try:
        import bili_fetch
        # bili_fetch 的 log 只收一条消息，这里适配成 pipeline 的两段式
        r = bili_fetch.fetch(url, workdir, config.bili_sessdata(), page,
                             log=lambda m: log("S1", m), fresh=fresh, subtitle=subtitle)
    except Exception as e:
        # BiliError 自带人话说明；其他异常也一并兜住，不让整个队列中断
        log("S1", f"✗ {e}")
        return None, "S1", {}
    return r["folder"], "S2", r


# ---------------------------------------------------------------- 主流程

def run_one(url: str, workdir: str, skip_asr: bool, fresh: bool,
            prompt: str | None, ocr: bool = False, page: int | None = None,
            use_subtitle: bool = True) -> dict:
    res = {"url": url, "status": "failed", "stage": "S1"}

    plat = detect_platform(url)
    res["platform"] = plat

    info: dict = {}
    if plat == "douyin":
        folder, stage, info = stage_douyin(url, workdir, fresh)
    elif plat == "bilibili":
        folder, stage, info = stage_bilibili(url, workdir, page, fresh, use_subtitle)
    else:
        folder, stage, info = stage_ytdlp(url, workdir)

    # 自家方案失败 → yt-dlp 兜底（抖音风控 / 链接失效重试 / B 站 durl 老稿件）
    if not folder and plat in ("douyin", "bilibili"):
        log("S1", f"{plat} 自家方案失败，回落 yt-dlp 兜底…")
        folder, stage, info = stage_ytdlp(url, workdir)
        if folder:
            plat = "ytdlp"
    res["stage"] = stage
    if not folder:
        return res
    res["folder"] = folder
    res["stage"] = "S3"

    # B 站命中字幕就不用跑 whisper：省一次推理，而且错字更少。
    # 字幕是随下载免费拿到的，所以即使 --skip-asr 也照样用上。
    sub = info.get("subtitle") or {}
    asr_source = "whisper"
    if sub and plat == "bilibili":
        asr_source = "bili-ai" if sub.get("ai") else "bili-cc"
        log("S3", f"用 B 站字幕，跳过本地转写（{sub.get('lan_doc')}，"
                  f"{sub.get('cues')} 条 → {sub.get('segments')} 句）")
    elif plat == "ytdlp" and os.path.exists(os.path.join(folder, "subtitle.txt")):
        asr_source = "ytdlp-sub"
        log("S3", "用 yt-dlp 拿到的平台字幕，跳过本地转写")
    elif not skip_asr:
        txt = step_asr(folder, prompt)
        if not txt:
            log("S3", "✗ 转写失败")
            return res
    else:
        log("S3", "已跳过转写（--skip-asr）")
    res["asr_source"] = asr_source

    # 画面文字：口播拿不到的信息（配方克数、参数表）只能从这里来
    overlay = None
    if ocr:
        res["stage"] = "S3b"
        video = next((os.path.join(folder, f) for f in os.listdir(folder)
                      if f.lower().endswith(".mp4")), None)
        if video:
            try:
                import ocr_frames
                overlay = ocr_frames.scan(video, quiet=False)["blocks"]
                res["overlay"] = len(overlay)
            except Exception as e:
                log("S3b", f"✗ 画面文字识别失败（不影响其他环节）：{e}")
        else:
            log("S3b", "✗ 没找到视频文件")

    # 有正文才出草稿。B 站字幕随下载免费拿到，所以 --skip-asr 也可能有正文
    has_text = any(os.path.exists(os.path.join(folder, f))
                   for f in ("video.txt", "subtitle.txt"))
    if not has_text and not ocr:
        res["status"] = "partial"
        return res

    res["stage"] = "S4"
    draft = step_draft(folder, url, overlay, asr_source)
    res["draft"] = draft
    res["status"] = "done" if draft else "partial"
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="视频链接（抖音 / B 站 / 其他站点由 yt-dlp 兜底；合集、播放列表、UP 主投稿页自动展开）")
    ap.add_argument("--queue", help="消费 clip_watch.py 产出的 queue.jsonl")
    ap.add_argument("--workdir", default=os.getcwd())
    ap.add_argument("--skip-asr", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="忽略已有产物强制重抓")
    ap.add_argument("--prompt", default=None, help="ASR 领域词，显著降低术语错字")
    ap.add_argument("--ocr", action="store_true",
                    help="识别画面上的文字卡（配方克数、参数表）。需 DeepSeek key，按 token 计费")
    ap.add_argument("--page", type=int, default=None,
                    help="B 站多 P 视频取第几 P（默认第 1P）")
    ap.add_argument("--no-subtitle", action="store_true",
                    help="不用 B 站字幕，强制本地转写（字幕错得离谱时用）")
    ap.add_argument("--doctor", action="store_true", help="只打印环境诊断")
    args = ap.parse_args()

    if args.doctor:
        print(config.dump())
        return 0
    if not args.url and not args.queue:
        ap.print_help()
        return 2

    urls: list[str] = []
    if args.queue:
        if not os.path.exists(args.queue):
            log("S0", f"✗ 队列文件不存在 {args.queue}")
            return 2
        seen = set()
        for line in open(args.queue, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                u = json.loads(line)["url"]
            except Exception:
                continue
            if u not in seen:
                seen.add(u); urls.append(u)
    if args.url:
        urls.insert(0, args.url)

    # 合集/播放列表/UP 主投稿页 → 展开成单条（失败则按单链接处理）
    expanded: list[str] = []
    seen_u: set[str] = set()
    for u in urls:
        subs = expand_collection(u)
        batch = subs or [u]
        for b in batch:
            if b not in seen_u:
                seen_u.add(b)
                expanded.append(b)
    urls = expanded

    # 抖音要浏览器；yt-dlp 路径要 yt-dlp 在 PATH 里
    need_browser = any(detect_platform(u) == "douyin" for u in urls)
    only_bili = bool(urls) and all(detect_platform(u) == "bilibili" for u in urls)
    if any(detect_platform(u) == "ytdlp" for u in urls):
        try:
            import ytdlp_fetch
            ytdlp_fetch._ytdlp_exe()
        except Exception as e:
            log("S0", f"✗ {e}")
            return 2
    if not preflight(need_browser, model_required=not only_bili):
        log("S0", "前置检查未通过，已停止")
        return 3

    log("S0", f"共 {len(urls)} 条待处理")
    results = [run_one(u, args.workdir, args.skip_asr, args.fresh, args.prompt,
                       args.ocr, args.page, not args.no_subtitle)
               for u in urls]

    print("\n" + "=" * 60)
    ok = sum(1 for r in results if r["status"] == "done")
    for r in results:
        flag = {"done": "✓", "partial": "~", "failed": "✗"}[r["status"]]
        plat = r.get("platform", "?")
        src = r.get("asr_source")
        tail = f", {src}" if src else ""
        print(f"{flag} [{plat}] {r['url']}  [{r['status']}, 停在 {r['stage']}{tail}]")
        if r.get("draft"):
            print(f"    草稿 → {r['draft']}")
    if not args.ocr and ok > 0 and config.deepseek_key():
        print("\n提示：加 --ocr 可识别画面上的文字卡（作者常把配方/参数打在屏幕上而不念出来）。")
    print(f"\n完成 {ok}/{len(results)}。草稿需补正文后移入 Obsidian 收件箱。")
    return 0 if ok == len(results) else 3


if __name__ == "__main__":
    sys.exit(main())

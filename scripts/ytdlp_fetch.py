#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ytdlp_fetch.py — yt-dlp 兜底引擎。

分工：
  抖音 → CDP 隔离实例（元数据最全，aweme_detail 拿作者/时长/20 档码率）
  B 站 → 公开 API + WBI 签名字幕
  其余 → 本模块。YouTube / TikTok / Vimeo / X / 老稿件 durl 等一律回落 yt-dlp，
         抖音、B 站自家方案失败时也回落到这里做第二次尝试。

产出目录结构与其他平台一致，S3（转写）/ S4（草稿）原样复用：
    <outdir>/ytdlp-<extractor>-<id>/
        video.mp4          合成后的 mp4
        metadata.json      desc/author/duration_ms 字段与抖音产物对齐
        subtitle.txt       平台字幕（拿得到才有），`[mm:ss] 文本` 行格式
        subtitle.json      字幕来源信息

依赖：`yt-dlp` 在 PATH 中（或配置文件 ytdlp_exe 指定路径）。
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass


class YtdlpError(Exception):
    """带人话说明的失败，直接展示给用户。"""


def log(m: str) -> None:
    print(f"[ytdlp] {m}", flush=True)


def _ytdlp_exe() -> str:
    import config
    exe = config.get("ytdlp_exe", None)
    if exe and os.path.isfile(exe):
        return exe
    found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if not found:
        raise YtdlpError("找不到 yt-dlp。安装：pip install yt-dlp（需在 PATH 中）")
    return found


def _run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _err_tail(r: subprocess.CompletedProcess) -> str:
    for ln in reversed((r.stderr or "").strip().splitlines()):
        ln = ln.strip()
        if ln and not ln.startswith("[download]"):
            return ln[:200]
    return "未知错误"


def _sanitize(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s)[:80]


def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------- 元数据

def probe(url: str) -> dict | None:
    """`yt-dlp -J` 只拿元数据不下载。失败返回 None。"""
    r = _run([_ytdlp_exe(), "-J", "--no-playlist", "--no-warnings", url], timeout=180)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def _folder_for(url: str, outdir: str) -> tuple[str, dict]:
    meta = probe(url)
    if not meta:
        raise YtdlpError(f"yt-dlp 无法解析该链接（不支持 / 已失效 / 网络不通）：{url}")
    ext = (meta.get("extractor_key") or meta.get("extractor") or "generic").lower()
    vid = _sanitize(str(meta.get("id") or "unknown"))
    folder = os.path.join(outdir, f"ytdlp-{ext}-{vid}")
    return folder, meta


# ---------------------------------------------------------------- 字幕

def _srt_to_lines(text: str) -> list[str]:
    """SRT / VTT 文本 → `[mm:ss] 内容` 行。剥掉序号、时间轴、内嵌标签。"""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.splitlines()]
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.]\d+\s*-->", lines[ti])
        if not m:
            continue
        h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        body = " ".join(
            re.sub(r"<[^>]+>", "", ln).strip()
            for ln in lines[ti + 1:] if ln.strip()
        ).strip()
        if not body or body.startswith(("WEBVTT", "NOTE")):
            continue
        out.append(f"[{(h * 60 + mn):02d}:{s:02d}] {body}")
    return out


def _save_subs(folder: str) -> int:
    """把 yt-dlp 落下的 .srt/.vtt 整理成 subtitle.txt / subtitle.json。返回条数。"""
    sub_files = sorted(glob.glob(os.path.join(folder, "*.vtt")) +
                       glob.glob(os.path.join(folder, "*.srt")))
    if not sub_files:
        return 0
    lines = _srt_to_lines(open(sub_files[0], encoding="utf-8", errors="replace").read())
    if not lines:
        return 0
    with open(os.path.join(folder, "subtitle.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(folder, "subtitle.json"), "w", encoding="utf-8") as f:
        json.dump({"source": "yt-dlp", "file": os.path.basename(sub_files[0]),
                   "lines": len(lines)}, f, ensure_ascii=False, indent=2)
    return len(lines)


# ---------------------------------------------------------------- 主入口

def fetch(url: str, outdir: str, log=log) -> str:
    """下载视频 + 元数据 + 字幕。返回产物目录（与其他平台一致的结构）。"""
    exe = _ytdlp_exe()
    folder, meta = _folder_for(url, outdir)
    os.makedirs(folder, exist_ok=True)

    meta_path = os.path.join(folder, "metadata.json")
    video_path = os.path.join(folder, "video.mp4")
    if os.path.exists(meta_path) and os.path.exists(video_path):
        log(f"复用已有产物 {os.path.basename(folder)}（重下请删目录）")
        return folder

    title = (meta.get("title") or "").strip()
    author = (meta.get("uploader") or meta.get("channel") or "").strip()
    dur = meta.get("duration") or 0
    log(f"{author or '?'} | {title[:60]} | {int(dur)}s | extractor="
        f"{meta.get('extractor_key') or '?'}")

    # 元数据字段与抖音产物对齐，S4 草稿直接复用
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"desc": title, "author": author,
                   "duration_ms": int(dur * 1000),
                   "aweme_id": f"ytdlp-{meta.get('id')}",
                   "source": "ytdlp",
                   "webpage_url": meta.get("webpage_url") or url,
                   "extractor": meta.get("extractor_key"),
                   "original_url": url},
                  f, ensure_ascii=False, indent=1)

    # 视频：mp4 优先，1080p 封顶（4K 源 3 分钟就 200MB+，对转写毫无意义）。
    # 字幕另起一步——字幕下载失败绝不能拖累视频（YouTube 限流 429 实测过）
    log("下载中…")
    r = _run([exe, "-f", "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/"
                      "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
              "--merge-output-format", "mp4",
              "--no-playlist", "--no-warnings", "--newline",
              "-o", video_path, url])
    if r.returncode != 0 or not os.path.exists(video_path):
        raise YtdlpError(f"下载失败：{_err_tail(r)}")
    size_mb = os.path.getsize(video_path) / 1048576
    log(f"✓ video.mp4 {size_mb:.1f}MB")

    # 字幕：精确语言列表（通配符在 YouTube 会展开几十条自动翻译轨道，限流 429）。
    # 失败不阻塞——没字幕就走 whisper。
    try:
        _run([exe, "--skip-download",
              "--write-subs", "--write-auto-subs", "--convert-subs", "srt",
              "--sub-langs", "zh-Hans,zh-CN,zh-HK,zh-TW,zh,en,ai-zh",
              "--no-playlist", "--no-warnings",
              "-o", video_path, url], timeout=300)
    except Exception:
        pass

    n = _save_subs(folder)
    log(f"✓ 字幕 {n} 行" if n else "无平台字幕，后续走本地转写")
    return folder


# ---------------------------------------------------------------- 合集展开

def expand(url: str, cap: int = 100) -> list[str]:
    """合集 / 播放列表 / UP 主投稿页 → 单视频链接列表（保序去重，超出 cap 截断）。"""
    exe = _ytdlp_exe()
    r = _run([exe, "--flat-playlist", "--print", "%(url)s",
              "--no-warnings", url], timeout=300)
    if r.returncode != 0:
        raise YtdlpError(f"展开失败：{_err_tail(r)}")
    out: list[str] = []
    seen: set[str] = set()
    for ln in r.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if not ln.startswith("http"):
            # 部分提取器只吐 id，尽力拼回标准链接
            if re.fullmatch(r"BV[0-9A-Za-z]{10}", ln):
                ln = f"https://www.bilibili.com/video/{ln}"
            elif "youtube" in url.lower():
                ln = f"https://www.youtube.com/watch?v={ln}"
            else:
                continue
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    if len(out) > cap:
        log(f"展开到 {len(out)} 条，超过上限 {cap}，已截断")
        out = out[:cap]
    return out

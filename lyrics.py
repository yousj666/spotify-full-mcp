#!/usr/bin/env python3
"""歌词模块：原文歌词走 LRCLIB，译文预留 Musixmatch 官方 API 接口位。

LRCLIB（https://lrclib.net）：
- 免费、免 key、免登录。GET /api/get 按歌名+艺人+专辑+时长精确取一条，
  返回 plainLyrics（纯文本）与 syncedLyrics（带时间轴的 LRC 同步歌词）。
- 精确匹配失败时退到 GET /api/search 模糊搜，取第一条带歌词的结果。

Musixmatch 译文（接口位）：
- get_translation() 先看凭据文件里有没有 musixmatch_api_key。
- 没配 key 时返回 {"status": "not_configured", ...}，不抛异常、不崩溃。
- 配了 key 时走官方 API（https://api.musixmatch.com/ws/1.1/）：
  matcher.track.get 定位 track_id，再调 track.lyrics.translation.get
  取指定语言译文。返回结构随官方套餐权限而变，解析做了容错。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

LRCLIB_BASE = "https://lrclib.net"
MUSIXMATCH_BASE = "https://api.musixmatch.com/ws/1.1"
UA = "spotify-full/1.0 (personal CLI; lyrics via LRCLIB)"


class LyricsError(Exception):
    """歌词获取错误（网络失败、无匹配结果等）。"""


def _get_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise LyricsError(f"GET {url} 失败 HTTP {err.code}") from err
    except urllib.error.URLError as err:
        raise LyricsError(f"GET {url} 网络错误: {err.reason}") from err


def get_lyrics_lrclib(track_name: str, artist_name: str,
                      album_name: str | None = None,
                      duration: float | None = None) -> dict:
    """从 LRCLIB 取歌词。

    返回 {"track_name", "artist_name", "album_name", "duration",
          "plain_lyrics", "synced_lyrics", "instrumental", "source"}。
    找不到时抛 LyricsError。
    """
    params = {"track_name": track_name, "artist_name": artist_name}
    if album_name:
        params["album_name"] = album_name
    if duration:
        params["duration"] = int(round(duration))
    url = LRCLIB_BASE + "/api/get?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    if data:
        return _normalize_lrclib(data, source="lrclib/get")

    # 精确匹配失败，退到搜索，取第一条带歌词的结果
    search_params = {"track_name": track_name, "artist_name": artist_name}
    search_url = LRCLIB_BASE + "/api/search?" + urllib.parse.urlencode(search_params)
    results = _get_json(search_url) or []
    for item in results:
        if item.get("plainLyrics") or item.get("syncedLyrics"):
            return _normalize_lrclib(item, source="lrclib/search")
    raise LyricsError(f"LRCLIB 没有找到《{track_name}》（{artist_name}）的歌词")


def _normalize_lrclib(item: dict, source: str) -> dict:
    return {
        "track_name": item.get("trackName"),
        "artist_name": item.get("artistName"),
        "album_name": item.get("albumName"),
        "duration": item.get("duration"),
        "plain_lyrics": item.get("plainLyrics"),
        "synced_lyrics": item.get("syncedLyrics"),
        "instrumental": bool(item.get("instrumental")),
        "source": source,
    }


def get_translation(track_name: str, artist_name: str, language: str = "zh",
                    musixmatch_api_key: str = "") -> dict:
    """取译文（Musixmatch 接口位）。

    未配置 key 时返回 {"status": "not_configured", "message": ...}，不抛异常。
    配置后走官方 API，成功返回 {"status": "ok", "language", "lines": [...]}，
    失败返回 {"status": "error", "message": ...}。
    """
    if not musixmatch_api_key:
        return {
            "status": "not_configured",
            "language": language,
            "message": ("未配置 Musixmatch API key（凭据文件 "
                        "~/.config/spotify-full/credentials.json 里的 "
                        "musixmatch_api_key 为空），译文功能未启用。"
                        "原文歌词不受影响，继续走 LRCLIB。"),
        }

    def mxm_get(path: str, extra: dict) -> dict:
        params = {"apikey": musixmatch_api_key, "format": "json"}
        params.update(extra)
        url = MUSIXMATCH_BASE + path + "?" + urllib.parse.urlencode(params)
        data = _get_json(url)
        if not isinstance(data, dict):
            raise LyricsError(f"Musixmatch {path} 返回异常")
        header = data.get("message", {}).get("header", {})
        if header.get("status_code") != 200:
            raise LyricsError(f"Musixmatch {path} 状态码 {header.get('status_code')}")
        return data.get("message", {}).get("body", {})

    try:
        body = mxm_get("/matcher.track.get",
                       {"q_track": track_name, "q_artist": artist_name})
        track = body.get("track", {})
        track_id = track.get("track_id")
        if not track_id:
            return {"status": "error", "language": language,
                    "message": "Musixmatch 目录里没找到这首歌"}
        body = mxm_get("/track.lyrics.translation.get",
                       {"track_id": track_id, "selected_language": language})
        translation = body.get("lyrics_translation", {}) or body.get("lyrics", {})
        text = (translation.get("lyrics_body")
                or translation.get("translation_body") or "")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return {"status": "error", "language": language,
                    "message": "Musixmatch 有这首歌，但没有该语言的译文"}
        return {"status": "ok", "language": language, "lines": lines,
                "source": "musixmatch"}
    except LyricsError as err:
        return {"status": "error", "language": language, "message": str(err)}


def format_side_by_side(plain_lyrics: str, translation: dict) -> str:
    """原文与译文并排输出。译文不可用时只出原文加一行说明。"""
    original_lines = (plain_lyrics or "").splitlines()
    if translation.get("status") != "ok":
        note = translation.get("message", "译文不可用")
        body = "\n".join(original_lines)
        return f"{body}\n\n[译文] {note}"
    translated_lines = translation.get("lines", [])
    width = max((len(ln) for ln in original_lines), default=0)
    out = []
    for idx, original in enumerate(original_lines):
        translated = translated_lines[idx] if idx < len(translated_lines) else ""
        if translated:
            out.append(f"{original:<{width}}  |  {translated}")
        else:
            out.append(original)
    return "\n".join(out)

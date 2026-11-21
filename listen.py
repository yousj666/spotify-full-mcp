#!/usr/bin/env python3
"""AI 的耳朵：把一首歌变成 AI 能「听懂」的数据报告。

原理：AI 没有耳朵，但 Spotify 官方 API 提供两层机器可读的音频数据：
- audio-features：一首歌的整体画像（速度、调式、能量、舞蹈性、情绪效价、
  原声性、器乐性、响度、语音性、拍号等）。
- audio-analysis：逐段拆解（sections 的调式/速度/响度、bars/beats 的数量与
  节拍网格、segments 的音高与音色轮廓）。
把这两层数据聚合，再附上 LRCLIB 的同步歌词（带时间轴），AI 就能像读乐谱
加读歌词一样去理解、评价和欣赏一首歌。这就是本模块生成「聆听报告」的思路。
"""
from __future__ import annotations

from lyrics import LyricsError, get_lyrics_lrclib

PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FEATURE_KEYS = [
    "tempo", "key", "mode", "energy", "danceability", "valence",
    "acousticness", "instrumentalness", "loudness", "speechiness",
    "liveness", "time_signature", "duration_ms",
]


def key_name(key, mode) -> str:
    """把 pitch class + mode 数字转成人类可读调名，如 A 小调。"""
    if key is None or key < 0:
        return "未知"
    name = PITCH_NAMES[key % 12]
    if mode == 1:
        return f"{name} 大调"
    if mode == 0:
        return f"{name} 小调"
    return name


def summarize_analysis(analysis: dict) -> dict:
    """把逐段分析压成概览：段落/小节/节拍计数与各 section 的调速响概览。"""
    sections = analysis.get("sections", []) or []
    bars = analysis.get("bars", []) or []
    beats = analysis.get("beats", []) or []
    segments = analysis.get("segments", []) or []

    section_rows = []
    for idx, sec in enumerate(sections):
        section_rows.append({
            "index": idx,
            "start": round(sec.get("start", 0), 1),
            "duration": round(sec.get("duration", 0), 1),
            "key": key_name(sec.get("key"), sec.get("mode")),
            "tempo": round(sec.get("tempo", 0), 1),
            "loudness": sec.get("loudness"),
            "time_signature": sec.get("time_signature"),
        })

    tempos = [s["tempo"] for s in section_rows if s["tempo"]]
    seg_loud = [s.get("loudness_max", 0) for s in segments]
    summary = {
        "sections_count": len(sections),
        "bars_count": len(bars),
        "beats_count": len(beats),
        "segments_count": len(segments),
        "tempo_range": ([min(tempos), max(tempos)] if tempos else None),
        "segment_loudness_max_avg": (
            round(sum(seg_loud) / len(seg_loud), 2) if seg_loud else None),
        "sections": section_rows,
    }
    track_meta = analysis.get("track", {}) or {}
    if track_meta:
        summary["analysis_track"] = {
            "duration": track_meta.get("duration"),
            "tempo": track_meta.get("tempo"),
            "key": key_name(track_meta.get("key"), track_meta.get("mode")),
            "time_signature": track_meta.get("time_signature"),
            "loudness": track_meta.get("loudness"),
        }
    return summary


def build_report(client, track: dict, with_lyrics: bool = True) -> dict:
    """聚合一首歌的聆听报告。track 为 Spotify track 对象。"""
    track_id = track["id"]
    artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
    duration_s = (track.get("duration_ms") or 0) / 1000.0

    features_raw = client.get_audio_features(track_id)
    features = {k: features_raw.get(k) for k in FEATURE_KEYS}
    features["key_name"] = key_name(features_raw.get("key"), features_raw.get("mode"))

    analysis = client.get_audio_analysis(track_id)
    analysis_summary = summarize_analysis(analysis)

    lyrics_block = None
    if with_lyrics:
        try:
            lyr = get_lyrics_lrclib(track.get("name", ""), artists,
                                    album_name=(track.get("album") or {}).get("name"),
                                    duration=duration_s or None)
            lyrics_block = {
                "plain_lyrics": lyr.get("plain_lyrics"),
                "synced_lyrics": lyr.get("synced_lyrics"),
                "source": lyr.get("source"),
            }
        except LyricsError as err:
            lyrics_block = {"error": str(err)}

    return {
        "track": {
            "id": track_id,
            "name": track.get("name"),
            "artists": artists,
            "album": (track.get("album") or {}).get("name"),
            "duration_s": round(duration_s, 1),
            "uri": track.get("uri"),
        },
        "features": features,
        "analysis": analysis_summary,
        "lyrics": lyrics_block,
    }


def resolve_track(client, title: str | None, artist: str | None,
                  uri_or_id: str | None) -> dict:
    """把「标题+艺人」或 track URI/ID 解析成 Spotify track 对象。"""
    if uri_or_id:
        track_id = uri_or_id.rsplit(":", 1)[-1].split("/")[-1].split("?")[0]
        return client.get_track(track_id)
    query = title or ""
    if artist:
        query = f"track:{title} artist:{artist}" if title else f"artist:{artist}"
    results = client.search(query, types="track", limit=5)
    items = (results.get("tracks") or {}).get("items", [])
    if not items:
        raise ValueError(f"Spotify 目录里没搜到《{title}》（{artist or ''}）")
    return items[0]


def _pct(value) -> str:
    if value is None:
        return "未知"
    return f"{round(value * 100)}%"


def format_report_text(report: dict) -> str:
    """人类可读版聆听报告。"""
    t = report["track"]
    f = report["features"]
    a = report["analysis"]
    lines = []
    lines.append(f"聆听报告：《{t['name']}》 — {t['artists']}")
    lines.append(f"专辑：{t['album'] or '未知'}｜时长：{t['duration_s']} 秒")
    lines.append("")
    lines.append("整体画像（audio-features）：")
    lines.append(f"  速度 {f.get('tempo')} BPM｜调式 {f.get('key_name')}"
                 f"｜拍号 {f.get('time_signature')}/4｜响度 {f.get('loudness')} dB")
    lines.append(f"  能量 {_pct(f.get('energy'))}｜舞蹈性 {_pct(f.get('danceability'))}"
                 f"｜情绪效价 {_pct(f.get('valence'))}")
    lines.append(f"  原声性 {_pct(f.get('acousticness'))}｜器乐性 {_pct(f.get('instrumentalness'))}"
                 f"｜语音性 {_pct(f.get('speechiness'))}｜现场感 {_pct(f.get('liveness'))}")
    lines.append("")
    lines.append("结构拆解（audio-analysis）：")
    lines.append(f"  段落 {a['sections_count']} 段｜小节 {a['bars_count']}"
                 f"｜节拍 {a['beats_count']}｜音色片段 {a['segments_count']}")
    if a.get("tempo_range"):
        lines.append(f"  段落速度范围 {a['tempo_range'][0]}–{a['tempo_range'][1]} BPM")
    for sec in a.get("sections", []):
        lines.append(f"  [{sec['index']}] {sec['start']}s 起，{sec['duration']}s，"
                     f"{sec['key']}，{sec['tempo']} BPM，响度 {sec['loudness']} dB")
    lines.append("")
    lyr = report.get("lyrics")
    if lyr and lyr.get("synced_lyrics"):
        lines.append("同步歌词（LRCLIB，带时间轴）：")
        lines.append(lyr["synced_lyrics"])
    elif lyr and lyr.get("plain_lyrics"):
        lines.append("歌词（LRCLIB）：")
        lines.append(lyr["plain_lyrics"])
    elif lyr and lyr.get("error"):
        lines.append(f"歌词：{lyr['error']}")
    return "\n".join(lines)

#!/usr/bin/env python3
"""全功能 Spotify 包装器 - 让 Muse 直接调用官方 Web API + 歌词。

用法: spotify.py [--json] <子命令> [参数...]

授权:
    auth                      走 OAuth 授权（本地回调 127.0.0.1:8888），令牌落盘
    status                    查看凭据与令牌状态（不调 API）

搜索与目录:
    search <关键词> [--type track,album,artist,playlist] [--limit N]
    track <id>                album <id>            album-tracks <id>
    artist <id>               artist-albums <id>    artist-top <id>

歌单:
    playlists                                   我的歌单列表
    playlist <id>                               歌单详情
    playlist-tracks <id>                        歌单曲目
    playlist-create <名称> [--description 文] [--public]
    playlist-add <歌单id> <uri...> [--position N]
    playlist-remove <歌单id> <uri...>
    playlist-reorder <歌单id> <range_start> <insert_before> [--range-length N]
    playlist-edit <歌单id> [--name 文] [--description 文] [--public|--private]
    playlist-cover <歌单id> <图片.jpg>

曲库:
    library-tracks            已保存歌曲（Liked Songs）
    library-albums            已保存专辑
    save-track <id...>        unsave-track <id...>
    save-album <id...>        unsave-album <id...>

播放:
    devices                   now                     play [--device id] [--uri uri | --context uri]
    pause                     next                    previous
    seek <毫秒>               volume <0-100>          shuffle <on|off>
    repeat <off|track|context>                      queue-add <uri>
    transfer <device_id>

用户数据:
    me                        top-tracks [--range short|medium|long] [--limit N]
    top-artists [--range ...] recent [--limit N]
    following                 follow <艺人id...>      unfollow <艺人id...>

其他:
    audio-features <曲目id>
    lyrics <歌名> <艺人> [--duration 秒] [--album 专辑] [--with-translation] [--lang zh]
    listen [<歌名> [艺人]] [--uri <track uri>] [--no-lyrics]
                        AI 的耳朵：输出聆听报告（音频特征 + 逐段分析 + 同步歌词）
    play-local [--uri uri | --context uri]
                        本机播放（可选组件 spotifyd/librespot）；无参数时只报配置状态

凭据在 ~/.config/spotify-full/credentials.json（结构见 credentials.example.json），
由用户自己的 Spotify 开发者应用 + auth 子命令写入；本包装器不保存其他任何凭证。
"""
import argparse
import json
import sys

sys.path.insert(0, "/home/hatch/workspace/spotify-full")

from listen import build_report, format_report_text, resolve_track
from local_player import local_player_status, play_on_local
from lyrics import LyricsError, format_side_by_side, get_lyrics_lrclib, get_translation
from spotify_client import (CREDENTIALS_PATH, SpotifyClient, SpotifyError,
                            load_credentials, run_auth_flow)

RANGE_MAP = {"short": "short_term", "medium": "medium_term", "long": "long_term"}


def print_result(result, as_json: bool):
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spotify.py", description="全功能 Spotify 包装器")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（歌词默认文本输出）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="OAuth 授权")
    sub.add_parser("status", help="凭据与令牌状态")

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--type", default="track")
    p.add_argument("--limit", type=int, default=20)

    for name in ("track", "album", "album-tracks", "artist", "artist-albums",
                 "artist-top", "audio-features", "playlist", "playlist-tracks"):
        p = sub.add_parser(name)
        p.add_argument("id")

    sub.add_parser("playlists")

    p = sub.add_parser("playlist-create")
    p.add_argument("name")
    p.add_argument("--description", default="")
    p.add_argument("--public", action="store_true")

    p = sub.add_parser("playlist-add")
    p.add_argument("playlist_id")
    p.add_argument("uris", nargs="+")
    p.add_argument("--position", type=int, default=None)

    p = sub.add_parser("playlist-remove")
    p.add_argument("playlist_id")
    p.add_argument("uris", nargs="+")

    p = sub.add_parser("playlist-reorder")
    p.add_argument("playlist_id")
    p.add_argument("range_start", type=int)
    p.add_argument("insert_before", type=int)
    p.add_argument("--range-length", type=int, default=1)

    p = sub.add_parser("playlist-edit")
    p.add_argument("playlist_id")
    p.add_argument("--name", default=None)
    p.add_argument("--description", default=None)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--public", dest="public", action="store_true", default=None)
    group.add_argument("--private", dest="public", action="store_false")

    p = sub.add_parser("playlist-cover")
    p.add_argument("playlist_id")
    p.add_argument("image_path")

    sub.add_parser("library-tracks")
    sub.add_parser("library-albums")
    for name in ("save-track", "unsave-track", "save-album", "unsave-album",
                 "follow", "unfollow"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="+")

    sub.add_parser("devices")
    sub.add_parser("now")
    p = sub.add_parser("play")
    p.add_argument("--device", default=None)
    p.add_argument("--uri", default=None, help="单个曲目 uri")
    p.add_argument("--context", default=None, help="专辑/歌单等上下文 uri")
    for name in ("pause", "next", "previous"):
        p = sub.add_parser(name)
        p.add_argument("--device", default=None)
    p = sub.add_parser("seek")
    p.add_argument("position_ms", type=int)
    p = sub.add_parser("volume")
    p.add_argument("percent", type=int)
    p = sub.add_parser("shuffle")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("repeat")
    p.add_argument("state", choices=["off", "track", "context"])
    p = sub.add_parser("queue-add")
    p.add_argument("uri")
    p = sub.add_parser("transfer")
    p.add_argument("device_id")

    sub.add_parser("me")
    for name in ("top-tracks", "top-artists"):
        p = sub.add_parser(name)
        p.add_argument("--range", default="medium", choices=list(RANGE_MAP))
        p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("recent")
    p.add_argument("--limit", type=int, default=20)
    sub.add_parser("following")

    p = sub.add_parser("lyrics")
    p.add_argument("title")
    p.add_argument("artist")
    p.add_argument("--album", default=None)
    p.add_argument("--duration", type=float, default=None, help="时长（秒），提高精确匹配率")
    p.add_argument("--with-translation", action="store_true")
    p.add_argument("--lang", default="zh")

    p = sub.add_parser("listen")
    p.add_argument("title", nargs="?", default=None)
    p.add_argument("artist", nargs="?", default=None)
    p.add_argument("--uri", default=None, help="track URI 或 ID，与标题/艺人二选一")
    p.add_argument("--no-lyrics", action="store_true", help="不附同步歌词")

    p = sub.add_parser("play-local")
    p.add_argument("--uri", default=None, help="要播放的曲目 uri")
    p.add_argument("--context", default=None, help="要播放的专辑/歌单 uri")
    p.add_argument("--status", action="store_true", help="只报本机播放端配置状态")

    return parser


def run_lyrics(args) -> int:
    result = get_lyrics_lrclib(args.title, args.artist,
                                album_name=args.album, duration=args.duration)
    if args.json:
        out = dict(result)
        if args.with_translation:
            creds = load_credentials()
            out["translation"] = get_translation(
                args.title, args.artist, language=args.lang,
                musixmatch_api_key=creds.get("musixmatch_api_key", ""))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    text = result.get("plain_lyrics") or result.get("synced_lyrics") or ""
    if not text:
        print("（这首歌在 LRCLIB 只有曲目信息，没有歌词文本）")
        return 0
    if args.with_translation:
        creds = load_credentials()
        translation = get_translation(
            args.title, args.artist, language=args.lang,
            musixmatch_api_key=creds.get("musixmatch_api_key", ""))
        print(format_side_by_side(text, translation))
    else:
        print(text)
    return 0


def run_client_command(client: SpotifyClient, args):
    cmd = args.command
    if cmd == "search":
        return client.search(args.query, types=args.type, limit=args.limit)
    if cmd == "track":
        return client.get_track(args.id)
    if cmd == "album":
        return client.get_album(args.id)
    if cmd == "album-tracks":
        return client.get_album_tracks(args.id)
    if cmd == "artist":
        return client.get_artist(args.id)
    if cmd == "artist-albums":
        return client.get_artist_albums(args.id)
    if cmd == "artist-top":
        return client.get_artist_top_tracks(args.id)
    if cmd == "audio-features":
        return client.get_audio_features(args.id)
    if cmd == "playlists":
        return client.list_my_playlists()
    if cmd == "playlist":
        return client.get_playlist(args.id)
    if cmd == "playlist-tracks":
        return client.get_playlist_tracks(args.id)
    if cmd == "playlist-create":
        return client.create_playlist(args.name, description=args.description,
                                      public=args.public)
    if cmd == "playlist-add":
        return client.add_to_playlist(args.playlist_id, args.uris,
                                      position=args.position)
    if cmd == "playlist-remove":
        return client.remove_from_playlist(args.playlist_id, args.uris)
    if cmd == "playlist-reorder":
        return client.reorder_playlist(args.playlist_id, args.range_start,
                                       args.insert_before,
                                       range_length=args.range_length)
    if cmd == "playlist-edit":
        return client.edit_playlist(args.playlist_id, name=args.name,
                                    description=args.description, public=args.public)
    if cmd == "playlist-cover":
        with open(args.image_path, "rb") as fh:
            return client.set_playlist_cover(args.playlist_id, fh.read())
    if cmd == "library-tracks":
        return client.get_saved_tracks()
    if cmd == "library-albums":
        return client.get_saved_albums()
    if cmd == "save-track":
        return client.save_tracks(args.ids)
    if cmd == "unsave-track":
        return client.unsave_tracks(args.ids)
    if cmd == "save-album":
        return client.save_albums(args.ids)
    if cmd == "unsave-album":
        return client.unsave_albums(args.ids)
    if cmd == "devices":
        return client.get_devices()
    if cmd == "now":
        return client.get_now_playing()
    if cmd == "play":
        uris = [args.uri] if args.uri else None
        return client.play(device_id=args.device, context_uri=args.context, uris=uris)
    if cmd == "pause":
        return client.pause(device_id=args.device)
    if cmd == "next":
        return client.next_track(device_id=args.device)
    if cmd == "previous":
        return client.previous_track(device_id=args.device)
    if cmd == "seek":
        return client.seek(args.position_ms)
    if cmd == "volume":
        return client.set_volume(args.percent)
    if cmd == "shuffle":
        return client.set_shuffle(args.state == "on")
    if cmd == "repeat":
        return client.set_repeat(args.state)
    if cmd == "queue-add":
        return client.add_to_queue(args.uri)
    if cmd == "transfer":
        return client.transfer_playback(args.device_id)
    if cmd == "me":
        return client.get_me()
    if cmd == "top-tracks":
        return client.get_top_tracks(limit=args.limit, time_range=RANGE_MAP[args.range])
    if cmd == "top-artists":
        return client.get_top_artists(limit=args.limit, time_range=RANGE_MAP[args.range])
    if cmd == "recent":
        return client.get_recently_played(limit=args.limit)
    if cmd == "following":
        return client.get_followed_artists()
    if cmd == "follow":
        return client.follow_artists(args.ids)
    if cmd == "unfollow":
        return client.unfollow_artists(args.ids)
    raise SpotifyError(f"未知子命令: {cmd}")


def main() -> int:
    args = build_parser().parse_args()

    try:
        if args.command == "status":
            creds = load_credentials()
            info = {
                "credentials_path": CREDENTIALS_PATH,
                "configured": bool(creds.get("client_id") and creds.get("client_secret")),
                "authorized": bool(creds.get("refresh_token")),
                "scope": creds.get("scope", ""),
                "musixmatch_configured": bool(creds.get("musixmatch_api_key")),
            }
            print_result(info, True)
            return 0

        if args.command == "auth":
            run_auth_flow()
            print("授权完成，令牌已写入凭据文件。")
            return 0

        if args.command == "lyrics":
            return run_lyrics(args)

        if args.command == "play-local":
            creds = load_credentials()
            if args.status or (not args.uri and not args.context):
                print_result(local_player_status(creds), True)
                return 0
            client = SpotifyClient(creds)
            uris = [args.uri] if args.uri else None
            print_result(play_on_local(client, creds,
                                       context_uri=args.context, uris=uris), True)
            return 0

        if args.command == "listen":
            client = SpotifyClient()
            track = resolve_track(client, args.title, args.artist, args.uri)
            report = build_report(client, track, with_lyrics=not args.no_lyrics)
            if args.json:
                print_result(report, True)
            else:
                print(format_report_text(report))
            return 0

        client = SpotifyClient()
        result = run_client_command(client, args)
        print_result(result, True)
        return 0
    except (SpotifyError, LyricsError) as err:
        print(f"错误: {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

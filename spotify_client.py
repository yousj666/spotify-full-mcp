#!/usr/bin/env python3
"""Spotify 官方 Web API 客户端（全功能，纯标准库实现）。

接口面（https://api.spotify.com/v1）：
- 搜索与目录：search、单曲/专辑/艺人读取、专辑曲目、艺人作品与热门曲目
- 歌单：列表、读取、新建、加歌（支持 position 插入）、删歌、reorder 排序、
  改名/描述/公开私密、换封面
- 曲库：已保存歌曲/专辑、save/unsave（已保存歌曲即 Liked Songs）
- 播放：设备、play/pause/next/previous/seek/音量/shuffle/repeat/队列/transfer、
  当前播放
- 用户：profile、top tracks/artists、recently played、关注艺人 follow/unfollow
- 音频特征：audio-features

授权：OAuth Authorization Code 流程，本地回调 http://127.0.0.1:8888/callback。
凭据文件 ~/.config/spotify-full/credentials.json（目录 700、文件 600），
字段见 credentials.example.json。client_id / client_secret 来自用户自己在
Spotify Developer Dashboard 创建的开发者应用；本模块不负责创建应用，
也不会向任何人索要真实凭证。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.spotify.com/v1"
ACCOUNTS_BASE = "https://accounts.spotify.com"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8888

CONFIG_DIR = os.environ.get(
    "SPOTIFY_FULL_CONFIG_DIR", os.path.expanduser("~/.config/spotify-full"))
CREDENTIALS_PATH = os.path.join(CONFIG_DIR, "credentials.json")

# 申请的全部 scope（歌单读写含公开/私密两套，缺一不可）
SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-library-read",
    "user-library-modify",
    "user-follow-read",
    "user-follow-modify",
    "user-top-read",
    "user-read-recently-played",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-read-private",
    "user-read-email",
]


class SpotifyError(Exception):
    """客户端错误（未配置凭据、令牌失效、服务端报错等）。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise SpotifyError("服务端返回了重定向，已拒绝跟随，请检查请求地址")


def load_credentials() -> dict:
    """读取凭据文件。不存在时返回空 dict（调用方决定如何报错）。"""
    if not os.path.exists(CREDENTIALS_PATH):
        return {}
    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_credentials(data: dict) -> None:
    """写入凭据文件，目录 700、文件 600。"""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    tmp_path = CREDENTIALS_PATH + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CREDENTIALS_PATH)
    try:
        os.chmod(CREDENTIALS_PATH, 0o600)
    except OSError:
        pass


def build_authorize_url(client_id: str, state: str = "") -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "show_dialog": "true",
    }
    if state:
        params["state"] = state
    return ACCOUNTS_BASE + "/authorize?" + urllib.parse.urlencode(params)


def _token_request(form: dict, client_id: str, client_secret: str) -> dict:
    body = urllib.parse.urlencode(form).encode("utf-8")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        ACCOUNTS_BASE + "/api/token",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        raise SpotifyError(f"令牌请求失败 HTTP {err.code}: {detail}") from err


def exchange_code(code: str, creds: dict) -> dict:
    """用授权 code 换取 access/refresh token 并写回凭据文件。"""
    data = _token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
        creds.get("client_id", ""),
        creds.get("client_secret", ""),
    )
    creds = dict(creds)
    creds["access_token"] = data["access_token"]
    if data.get("refresh_token"):
        creds["refresh_token"] = data["refresh_token"]
    creds["token_expires_at"] = int(time.time()) + int(data.get("expires_in", 3600)) - 60
    creds["scope"] = data.get("scope", " ".join(SCOPES))
    save_credentials(creds)
    return creds


def run_auth_flow() -> dict:
    """在本机 127.0.0.1:8888 起一次性回调服务，等待用户在浏览器完成授权。

    返回写入后的凭据 dict。用户需要先在 credentials.json 里填好
    client_id / client_secret（来自其自己的 Spotify 开发者应用）。
    """
    import http.server
    import secrets
    import threading
    import webbrowser

    creds = load_credentials()
    client_id = creds.get("client_id", "")
    client_secret = creds.get("client_secret", "")
    if not client_id or not client_secret:
        raise SpotifyError(
            "凭据文件缺少 client_id / client_secret。"
            f"请先在 Spotify Developer Dashboard 创建应用，把回调地址设为 {REDIRECT_URI}，"
            f"再把 client_id / client_secret 填入 {CREDENTIALS_PATH}（结构见 credentials.example.json）。"
        )

    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server 命名约定)
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            result["query"] = query
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if query.get("code"):
                self.wfile.write("授权成功，可以关掉这个页面了。".encode("utf-8"))
            else:
                self.wfile.write("授权失败，请回到终端查看错误。".encode("utf-8"))

        def log_message(self, *args):  # 静默
            pass

    server = http.server.HTTPServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    url = build_authorize_url(client_id, state=state)
    print("请在浏览器打开下面的链接完成 Spotify 授权（已登录的账号即要连接的账号）：")
    print(url)
    try:
        webbrowser.open(url)
    except Exception:
        pass

    thread.join(timeout=600)
    server.server_close()
    query = result.get("query")
    if not query:
        raise SpotifyError("等待授权回调超时（10 分钟），未收到 code")
    if query.get("state", [""])[0] != state:
        raise SpotifyError("授权回调 state 不匹配，已中止")
    if query.get("error"):
        raise SpotifyError(f"授权被拒绝或失败: {query['error'][0]}")
    code = query.get("code", [""])[0]
    if not code:
        raise SpotifyError("授权回调里没有 code")
    return exchange_code(code, creds)


class SpotifyClient:
    """带自动续期的 Spotify Web API 客户端。"""

    def __init__(self, creds: dict | None = None):
        self.creds = creds if creds is not None else load_credentials()
        if not self.creds.get("client_id") or not self.creds.get("client_secret"):
            raise SpotifyError(
                f"未配置凭据：{CREDENTIALS_PATH} 不存在或缺少 client_id / client_secret"
                "（结构见 credentials.example.json）"
            )
        if not self.creds.get("refresh_token"):
            raise SpotifyError("尚未完成 OAuth 授权：请先运行 spotify.py auth")

    # ---------- 令牌 ----------

    def _access_token(self) -> str:
        token = self.creds.get("access_token", "")
        expires_at = int(self.creds.get("token_expires_at", 0))
        if token and time.time() < expires_at:
            return token
        data = _token_request(
            {"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"]},
            self.creds["client_id"],
            self.creds["client_secret"],
        )
        self.creds["access_token"] = data["access_token"]
        self.creds["token_expires_at"] = int(time.time()) + int(data.get("expires_in", 3600)) - 60
        if data.get("refresh_token"):
            self.creds["refresh_token"] = data["refresh_token"]
        save_credentials(self.creds)
        return self.creds["access_token"]

    # ---------- 基础请求 ----------

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None, raw_body: bytes | None = None,
                content_type: str = "application/json"):
        """path 可为 /v1 之后的相对路径（如 /me/playlists）或完整 URL（分页 next）。"""
        if path.startswith("http"):
            url = path
        else:
            url = API_BASE + path
            if params:
                clean = {k: v for k, v in params.items() if v is not None}
                if clean:
                    url += "?" + urllib.parse.urlencode(clean)
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._access_token()}")
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as resp:
                payload = resp.read()
                if resp.status == 204 or not payload:
                    return {"ok": True, "status": resp.status}
                return json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")
            raise SpotifyError(f"{method} {path} 失败 HTTP {err.code}: {detail}") from err

    def get_all_pages(self, path: str, params: dict | None = None, max_pages: int = 20) -> dict:
        """把分页接口的 items 全部拉完，返回合并后的 dict。"""
        items: list = []
        page = self.request("GET", path, params=params)
        total = page.get("total")
        for _ in range(max_pages):
            items.extend(page.get("items", []))
            nxt = page.get("next")
            if not nxt:
                break
            page = self.request("GET", nxt)
        return {"items": items, "total": total if total is not None else len(items)}

    # ---------- 搜索与目录 ----------

    def search(self, query: str, types: str = "track", limit: int = 20, offset: int = 0) -> dict:
        return self.request("GET", "/search",
                            params={"q": query, "type": types, "limit": limit, "offset": offset})

    def get_track(self, track_id: str) -> dict:
        return self.request("GET", f"/tracks/{track_id}")

    def get_album(self, album_id: str) -> dict:
        return self.request("GET", f"/albums/{album_id}")

    def get_album_tracks(self, album_id: str, all_pages: bool = True) -> dict:
        if all_pages:
            return self.get_all_pages(f"/albums/{album_id}/tracks", params={"limit": 50})
        return self.request("GET", f"/albums/{album_id}/tracks", params={"limit": 50})

    def get_artist(self, artist_id: str) -> dict:
        return self.request("GET", f"/artists/{artist_id}")

    def get_artist_albums(self, artist_id: str, all_pages: bool = True) -> dict:
        if all_pages:
            return self.get_all_pages(f"/artists/{artist_id}/albums", params={"limit": 50})
        return self.request("GET", f"/artists/{artist_id}/albums", params={"limit": 50})

    def get_artist_top_tracks(self, artist_id: str, market: str = "US") -> dict:
        return self.request("GET", f"/artists/{artist_id}/top-tracks", params={"market": market})

    # ---------- 歌单 ----------

    def list_my_playlists(self, all_pages: bool = True) -> dict:
        if all_pages:
            return self.get_all_pages("/me/playlists", params={"limit": 50})
        return self.request("GET", "/me/playlists", params={"limit": 50})

    def get_playlist(self, playlist_id: str) -> dict:
        return self.request("GET", f"/playlists/{playlist_id}")

    def get_playlist_tracks(self, playlist_id: str, all_pages: bool = True) -> dict:
        if all_pages:
            return self.get_all_pages(f"/playlists/{playlist_id}/tracks", params={"limit": 100})
        return self.request("GET", f"/playlists/{playlist_id}/tracks", params={"limit": 100})

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> dict:
        me = self.request("GET", "/me")
        return self.request("POST", f"/users/{me['id']}/playlists",
                            body={"name": name, "description": description, "public": public})

    def add_to_playlist(self, playlist_id: str, uris: list[str],
                        position: int | None = None) -> dict:
        body: dict = {"uris": uris}
        if position is not None:
            body["position"] = position
        return self.request("POST", f"/playlists/{playlist_id}/tracks", body=body)

    def remove_from_playlist(self, playlist_id: str, uris: list[str]) -> dict:
        return self.request("DELETE", f"/playlists/{playlist_id}/tracks",
                            body={"tracks": [{"uri": u} for u in uris]})

    def reorder_playlist(self, playlist_id: str, range_start: int, insert_before: int,
                         range_length: int = 1, snapshot_id: str | None = None) -> dict:
        body: dict = {"range_start": range_start, "insert_before": insert_before,
                      "range_length": range_length}
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        return self.request("PUT", f"/playlists/{playlist_id}/tracks", body=body)

    def edit_playlist(self, playlist_id: str, name: str | None = None,
                      description: str | None = None, public: bool | None = None) -> dict:
        body: dict = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if public is not None:
            body["public"] = public
        if not body:
            raise SpotifyError("没有要修改的字段")
        return self.request("PUT", f"/playlists/{playlist_id}", body=body)

    def set_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict:
        """换封面：上传 base64 编码的 JPEG。图片建议 640x640 以内、小于 256KB。"""
        raw = base64.b64encode(jpeg_bytes)
        return self.request("PUT", f"/playlists/{playlist_id}/images",
                            raw_body=raw, content_type="image/jpeg")

    # ---------- 曲库 ----------

    def get_saved_tracks(self, all_pages: bool = True) -> dict:
        """已保存歌曲，也就是 Liked Songs。"""
        if all_pages:
            return self.get_all_pages("/me/tracks", params={"limit": 50})
        return self.request("GET", "/me/tracks", params={"limit": 50})

    def get_saved_albums(self, all_pages: bool = True) -> dict:
        if all_pages:
            return self.get_all_pages("/me/albums", params={"limit": 50})
        return self.request("GET", "/me/albums", params={"limit": 50})

    def save_tracks(self, ids: list[str]) -> dict:
        return self.request("PUT", "/me/tracks", params={"ids": ",".join(ids)})

    def unsave_tracks(self, ids: list[str]) -> dict:
        return self.request("DELETE", "/me/tracks", params={"ids": ",".join(ids)})

    def save_albums(self, ids: list[str]) -> dict:
        return self.request("PUT", "/me/albums", params={"ids": ",".join(ids)})

    def unsave_albums(self, ids: list[str]) -> dict:
        return self.request("DELETE", "/me/albums", params={"ids": ",".join(ids)})

    # ---------- 播放控制 ----------

    def get_devices(self) -> dict:
        return self.request("GET", "/me/player/devices")

    def get_now_playing(self) -> dict:
        return self.request("GET", "/me/player")

    def play(self, device_id: str | None = None, context_uri: str | None = None,
             uris: list[str] | None = None, offset: dict | None = None) -> dict:
        body: dict = {}
        if context_uri:
            body["context_uri"] = context_uri
        if uris:
            body["uris"] = uris
        if offset:
            body["offset"] = offset
        return self.request("PUT", "/me/player/play",
                            params={"device_id": device_id} if device_id else None,
                            body=body if body else None)

    def pause(self, device_id: str | None = None) -> dict:
        return self.request("PUT", "/me/player/pause",
                            params={"device_id": device_id} if device_id else None)

    def next_track(self, device_id: str | None = None) -> dict:
        return self.request("POST", "/me/player/next",
                            params={"device_id": device_id} if device_id else None)

    def previous_track(self, device_id: str | None = None) -> dict:
        return self.request("POST", "/me/player/previous",
                            params={"device_id": device_id} if device_id else None)

    def seek(self, position_ms: int, device_id: str | None = None) -> dict:
        params = {"position_ms": position_ms}
        if device_id:
            params["device_id"] = device_id
        return self.request("PUT", "/me/player/seek", params=params)

    def set_volume(self, volume_percent: int, device_id: str | None = None) -> dict:
        params = {"volume_percent": volume_percent}
        if device_id:
            params["device_id"] = device_id
        return self.request("PUT", "/me/player/volume", params=params)

    def set_shuffle(self, state: bool, device_id: str | None = None) -> dict:
        params = {"state": "true" if state else "false"}
        if device_id:
            params["device_id"] = device_id
        return self.request("PUT", "/me/player/shuffle", params=params)

    def set_repeat(self, state: str, device_id: str | None = None) -> dict:
        """state 取 off / track / context。"""
        if state not in ("off", "track", "context"):
            raise SpotifyError("repeat 只能是 off / track / context")
        params = {"state": state}
        if device_id:
            params["device_id"] = device_id
        return self.request("PUT", "/me/player/repeat", params=params)

    def add_to_queue(self, uri: str, device_id: str | None = None) -> dict:
        params = {"uri": uri}
        if device_id:
            params["device_id"] = device_id
        return self.request("POST", "/me/player/queue", params=params)

    def transfer_playback(self, device_id: str, play: bool = False) -> dict:
        return self.request("PUT", "/me/player",
                            body={"device_ids": [device_id], "play": play})

    # ---------- 用户数据 ----------

    def get_me(self) -> dict:
        return self.request("GET", "/me")

    def get_top_tracks(self, limit: int = 20, time_range: str = "medium_term") -> dict:
        return self.request("GET", "/me/top/tracks",
                            params={"limit": limit, "time_range": time_range})

    def get_top_artists(self, limit: int = 20, time_range: str = "medium_term") -> dict:
        return self.request("GET", "/me/top/artists",
                            params={"limit": limit, "time_range": time_range})

    def get_recently_played(self, limit: int = 20) -> dict:
        return self.request("GET", "/me/player/recently-played", params={"limit": limit})

    def get_followed_artists(self, all_pages: bool = True) -> dict:
        page = self.request("GET", "/me/following", params={"type": "artist", "limit": 50})
        artists = page.get("artists", {})
        items = list(artists.get("items", []))
        if all_pages:
            nxt = artists.get("next")
            rounds = 0
            while nxt and rounds < 20:
                page = self.request("GET", nxt)
                artists = page.get("artists", {})
                items.extend(artists.get("items", []))
                nxt = artists.get("next")
                rounds += 1
        return {"items": items, "total": len(items)}

    def follow_artists(self, ids: list[str]) -> dict:
        return self.request("PUT", "/me/following",
                            params={"type": "artist", "ids": ",".join(ids)})

    def unfollow_artists(self, ids: list[str]) -> dict:
        return self.request("DELETE", "/me/following",
                            params={"type": "artist", "ids": ",".join(ids)})

    # ---------- 音频特征与分析（AI 的耳朵） ----------

    def get_audio_features(self, track_id: str) -> dict:
        return self.request("GET", f"/audio-features/{track_id}")

    def get_audio_analysis(self, track_id: str) -> dict:
        """逐段音频分析：sections / bars / beats / segments / tatums。"""
        return self.request("GET", f"/audio-analysis/{track_id}")

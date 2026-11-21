#!/usr/bin/env python3
"""Spotify cookie 模式后端：网页播放器内部接口（第一方客户端通道）。

为什么有这个后端：Spotify 对 Development Mode 开发者应用收紧后，没有
Premium 的应用所有者调官方 Web API 会被 403 拦死（歌单内容接口尤其）。
而免费账号在官方网页播放器里本来就能建歌单、加歌、删歌、排序——走的
是第一方内部接口。本模块用账号自己的登录 cookie（sp_dc）以第一方
网页客户端身份调同一套接口，不经过开发者应用，因此不受那道门限制。

鉴权链（全部逆向自网页播放器，社区有完整实证，见文末出处）：
1. 凭据文件里的 sp_dc（浏览器登录态 cookie，有效期约一年）。
2. GET https://open.spotify.com/api/server-time 取服务器时间。
3. 用网页播放器 JS 内置的共享密钥生成 TOTP（密钥按版本轮换，社区持续
   从官方 bundle 提取维护；本模块运行时优先从社区仓库拉取并本地缓存，
   拉不到时用内置版本兜底）。
4. GET https://open.spotify.com/api/token?reason=transport
   &productType=web-player&totp=…&totpServer=…&totpVer=… 换第一方
   access token（约 1 小时有效，只存内存，过期自动重换）。

接口面（刻意收窄，只做歌单）：
- 读歌单内容：Pathfinder GraphQL fetchPlaylistContents
  （POST https://api-partner.spotify.com/pathfinder/v2/query）
- 加歌 addToPlaylist（支持 BOTTOM/TOP/AFTER_UID 定位，批量按序）
- 删歌 removeFromPlaylist（按条目 uid 删除，先按曲目 uri 解析）
- 重排 moveItemsInPlaylist（按完整目标顺序计算并执行移动）
- 建歌单：spclient POST /playlist/v2/playlist（尽力而为：能建出歌单
  并返回 URI，但归档进曲库 rootlist 需要账号 user id，本后端暂未解析，
  返回值里会明确标注未归档；不要把它当成官方后端的完整替代）

关键坑（实证记录在案）：Pathfinder 写操作即使失败也可能返回 HTTP 200，
失败藏在返回节点的 __typename 里（错误联合类型）。本模块每次写操作都
检查 __typename 与 errors 数组，失败如实抛错，绝不假报成功。

安全与拟人：
- sp_dc 与 access token 绝不打印、绝不进日志与报错文本。
- 写操作之间至少间隔 1 秒；单次进程运行的写调用有上限（默认 50，
  环境变量 SPOTIFY_FULL_COOKIE_MAX_WRITES 可调），超限立即停下报错。
- 本模块从不调用 api.spotify.com（官方 REST 主机与本后端无关）。

持久化查询哈希会随网页播放器发版轮换；哈希失效时服务端返回
PersistedQueryNotFound，本模块会明确报出「哈希可能已轮换」，而不是
笼统失败。当前哈希与 TOTP 版本取自 2026-07 的实证文档（见文末）。

出处：
- songmirror 的 cookie 写后端设计与 Postman 手册（端到端实证）：
  https://github.com/ahnafnafee/songmirror/blob/HEAD/docs/design/2026-07-20-spotify-cookie-write-backend.md
  https://github.com/ahnafnafee/songmirror/blob/HEAD/docs/spotify-web-postman.md
- TOTP 密钥社区维护（secretDict.json，{版本: [字节…]} 格式）：
  https://raw.githubusercontent.com/xyloflake/spot-secrets-go/main/secrets/secretDict.json
  https://raw.githubusercontent.com/Thereallo1026/spotify-secrets/main/secrets/secretDict.json
  算法与 misiektoja/spotify_monitor 的调试工具交叉核对一致。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from spotify_client import CONFIG_DIR, CREDENTIALS_PATH, SpotifyError, load_credentials

TOKEN_URL = "https://open.spotify.com/api/token"
SERVER_TIME_URL = "https://open.spotify.com/api/server-time"
PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
CLIENTTOKEN_URL = "https://clienttoken.spotify.com/v1/clienttoken"
SPCLIENT_PLAYLIST_URL = "https://spclient.wg.spotify.com/playlist/v2/playlist"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/152.0"
APP_VERSION = "1.2.95.312.gda5d7e47"
# 网页播放器第一方 client id（公开在社区文档里，仅 client-token 铸造用）
WEB_PLAYER_CLIENT_ID = "d8a5ed958d274c2e8ee717e6a4b0971d"

# Pathfinder 持久化查询哈希（2026-07 实证值；add/remove/move 共用一个
# per-document 哈希，靠 operationName 区分；fetch 家族共用另一个）
HASH_MUTATION = "47b2a1234b17748d332dd0431534f22450e9ecbb3d5ddcdacbd83368636a0990"
HASH_FETCH = "a65e12194ed5fc443a1cdebed5fabe33ca5b07b987185d63c72483867ad13cb4"
PERSISTED_HASHES = {
    "addToPlaylist": HASH_MUTATION,
    "removeFromPlaylist": HASH_MUTATION,
    "moveItemsInPlaylist": HASH_MUTATION,
    "fetchPlaylistContents": HASH_FETCH,
    "fetchPlaylist": HASH_FETCH,
}

# TOTP 内置兜底密钥（网页播放器共享密钥，非任何人的账号凭证；
# 运行时优先用社区仓库的最新版本，见 load_totp_secrets）
BUILTIN_TOTP_SECRETS = {
    59: "{iOFn;4}<1PFYKPV?5{%u14]M>/V0hDH",
    60: 'OmE{ZA.J^":0FG\\Uz?[@WW',
    61: ',7/*F("rLJ2oxaKL^f+E1xvP@N',
}
TOTP_SECRETS_URLS = [
    "https://raw.githubusercontent.com/xyloflake/spot-secrets-go/main/secrets/secretDict.json",
    "https://raw.githubusercontent.com/Thereallo1026/spotify-secrets/main/secrets/secretDict.json",
]
TOTP_CACHE_PATH = os.path.join(CONFIG_DIR, "totp_secrets_cache.json")
TOTP_CACHE_MAX_AGE = 24 * 3600

WRITE_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_WRITES = 50
PAGE_LIMIT = 100


# ---------------------------------------------------------------------------
# TOTP（网页播放器同款算法：非标准 TOTP，密钥先做逐字节变换）
# ---------------------------------------------------------------------------

def totp_code(secret: str, for_time: float) -> str:
    """按网页播放器的算法生成 6 位 TOTP。

    密钥变换：key_str = 密钥每个字节 b_i 与 ((i % 33) + 9) 异或后转成
    十进制字符串再拼接；HMAC 密钥 = key_str 的 UTF-8 字节。
    计数器 = int(for_time) // 30，8 字节大端；HMAC-SHA1 后做动态截断，
    结果 mod 10^6、补零到 6 位。与社区两份独立实现交叉核对一致。
    """
    raw = secret.encode("utf-8")
    key_str = "".join(str(b ^ ((i % 33) + 9)) for i, b in enumerate(raw))
    counter = int(for_time) // 30
    digest = hmac_mod.new(key_str.encode("utf-8"),
                          counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def _normalize_secret_dict(data) -> dict:
    """把社区 secretDict 格式（{版本: [字节…]} 或 {版本: "字符串"}，
    或 [{version, secret}]）统一成 {int 版本: str 密钥}。解析不了返回 {}。"""
    out: dict = {}
    if isinstance(data, dict):
        items = list(data.items())
    elif isinstance(data, list):
        items = []
        for entry in data:
            if isinstance(entry, dict) and "version" in entry and "secret" in entry:
                items.append((entry["version"], entry["secret"]))
    else:
        return {}
    for ver, val in items:
        try:
            version = int(ver)
        except (TypeError, ValueError):
            continue
        if isinstance(val, list) and all(isinstance(x, int) for x in val):
            out[version] = "".join(chr(x) for x in val)
        elif isinstance(val, str) and val:
            out[version] = val
    return out


def _fetch_remote_secrets() -> dict:
    """从社区仓库拉最新 TOTP 密钥 dict。全部失败返回 {}（不抛错）。"""
    urls = []
    override = os.environ.get("SPOTIFY_FULL_TOTP_SECRETS_URL", "").strip()
    if override:
        urls.append(override)
    urls.extend(TOTP_SECRETS_URLS)
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parsed = _normalize_secret_dict(data)
            if parsed:
                return parsed
        except Exception:
            continue
    return {}


def _read_secrets_cache() -> dict:
    try:
        with open(TOTP_CACHE_PATH, "r", encoding="utf-8") as fh:
            return _normalize_secret_dict(json.load(fh))
    except Exception:
        return {}


def _write_secrets_cache(secrets: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
        tmp = TOTP_CACHE_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in secrets.items()}, fh)
        os.replace(tmp, TOTP_CACHE_PATH)
    except OSError:
        pass


def load_totp_secrets(force_refresh: bool = False) -> dict:
    """合并内置兜底 + 本地缓存 +（缓存过期或强制时）远程拉取的密钥。

    返回 {int 版本: str 密钥}。远程拉取失败不影响用缓存/内置继续。
    环境变量 SPOTIFY_FULL_TOTP_NO_FETCH=1 可完全禁用远程拉取。
    """
    merged = dict(BUILTIN_TOTP_SECRETS)
    merged.update(_read_secrets_cache())
    cache_stale = True
    try:
        cache_stale = (time.time() - os.path.getmtime(TOTP_CACHE_PATH)
                       > TOTP_CACHE_MAX_AGE)
    except OSError:
        cache_stale = True
    if (force_refresh or cache_stale) and \
            os.environ.get("SPOTIFY_FULL_TOTP_NO_FETCH") != "1":
        remote = _fetch_remote_secrets()
        if remote:
            merged.update(remote)
            _write_secrets_cache(remote)
    return merged


# ---------------------------------------------------------------------------
# 默认 HTTP 传输（可注入，测试用 mock 替换）
# ---------------------------------------------------------------------------

def _default_http_json(method: str, url: str, headers: dict | None = None,
                       params: dict | None = None, body: dict | None = None):
    """发一个 JSON HTTP 请求，返回 (status, 解析后的 dict)。

    HTTP 错误状态不抛异常，交给调用方按响应体判断（令牌端点的错误
    信息在 body 里）。网络层失败才抛 SpotifyError。报错文本只带状态码
    与截断后的响应体，绝不带请求头（里面有 Cookie / Bearer）。
    """
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as err:
        payload = err.read().decode("utf-8", "replace")
        try:
            return err.code, json.loads(payload) if payload else {}
        except ValueError:
            return err.code, {"raw": payload[:200]}
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise SpotifyError(
            f"网络请求失败（{method} {urllib.parse.urlparse(url).hostname}）: {err}"
        ) from err


def playlist_uri(playlist_id_or_uri: str) -> str:
    """把歌单 id / URI / open.spotify.com 链接统一成 spotify:playlist: URI。"""
    text = playlist_id_or_uri.strip()
    if text.startswith("spotify:playlist:"):
        return text
    if "open.spotify.com/playlist/" in text:
        tail = text.split("/playlist/", 1)[1]
        text = tail.split("?")[0].split("/")[0]
    return "spotify:playlist:" + text


# ---------------------------------------------------------------------------
# Cookie 模式客户端
# ---------------------------------------------------------------------------

class CookieClient:
    """用 sp_dc 以第一方网页客户端身份操作歌单的客户端。

    构造时没有 sp_dc 直接本地报错，不发任何网络请求（与官方后端
    未配置凭据时的行为一致）。
    """

    def __init__(self, creds: dict | None = None, transport=None,
                 sleep=None, write_interval: float = WRITE_INTERVAL_SECONDS,
                 max_writes: int | None = None):
        self.creds = creds if creds is not None else load_credentials()
        self._sp_dc = (self.creds.get("sp_dc") or "").strip()
        if not self._sp_dc:
            raise SpotifyError(
                "未配置 sp_dc：cookie 模式需要先把 open.spotify.com 的 "
                f"sp_dc cookie 填入 {CREDENTIALS_PATH} 的 sp_dc 字段"
                "（获取方法见 README 的「cookie 模式」一节），或运行 "
                "spotify.py cookie-login 用自带无头浏览器登录获取。"
                "本次未发出任何网络请求。"
            )
        self._http = transport or _default_http_json
        self._sleep = sleep or time.sleep
        self._write_interval = write_interval
        if max_writes is None:
            try:
                max_writes = int(os.environ.get(
                    "SPOTIFY_FULL_COOKIE_MAX_WRITES", DEFAULT_MAX_WRITES))
            except ValueError:
                max_writes = DEFAULT_MAX_WRITES
        self._max_writes = max_writes
        self._write_count = 0
        self._last_write_at = 0.0
        self._token = ""
        self._token_expires_at = 0.0

    # ---------- 令牌 ----------

    def _server_time(self) -> float:
        try:
            _, data = self._http(
                "GET", SERVER_TIME_URL,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                         "Cookie": f"sp_dc={self._sp_dc}"})
            server_time = float(data.get("serverTime"))
            if server_time > 0:
                return server_time
        except Exception:
            pass
        return time.time()

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        server_time = self._server_time()
        secrets = load_totp_secrets()
        last_error = ""
        for attempt in range(2):
            version = max(secrets)
            code = totp_code(secrets[version], server_time)
            status, data = self._http(
                "GET", TOKEN_URL,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                         "Referer": "https://open.spotify.com/",
                         "App-Platform": "WebPlayer",
                         "Cookie": f"sp_dc={self._sp_dc}"},
                params={"reason": "transport", "productType": "web-player",
                        "totp": code, "totpServer": code, "totpVer": version})
            token = data.get("accessToken") or ""
            if token:
                if data.get("isAnonymous"):
                    raise SpotifyError(
                        "sp_dc 换回的是匿名令牌：这个 sp_dc 未登录或已失效，"
                        "请重新从浏览器获取已登录状态的 sp_dc。")
                self._token = token
                expires_ms = data.get("accessTokenExpirationTimestampMs")
                self._token_expires_at = (float(expires_ms) / 1000.0
                                          if expires_ms else time.time() + 3300)
                return self._token
            last_error = str(data.get("error") or f"HTTP {status}")
            if "totpVerExpired" in last_error and attempt == 0:
                refreshed = load_totp_secrets(force_refresh=True)
                if refreshed:
                    secrets = refreshed
                continue
            break
        raise SpotifyError(
            f"用 sp_dc 换取第一方令牌失败: {last_error}。"
            "常见原因：sp_dc 已失效（重新获取）、TOTP 密钥版本已轮换"
            "（等社区仓库更新或稍后重试）。报错中不包含任何凭证内容。")

    # ---------- Pathfinder ----------

    def _write_guard(self) -> None:
        """写操作节流 + 计数上限。在真正发出写请求前调用。"""
        if self._write_count >= self._max_writes:
            raise SpotifyError(
                f"已达到单次运行写操作上限（{self._max_writes} 次），已停下。"
                "如确需更多，先核对歌单现状再分批执行，或调高环境变量 "
                "SPOTIFY_FULL_COOKIE_MAX_WRITES。")
        wait = self._write_interval - (time.time() - self._last_write_at)
        if self._last_write_at and wait > 0:
            self._sleep(wait)
        self._last_write_at = time.time()
        self._write_count += 1

    def _pathfinder(self, operation: str, variables: dict, is_write: bool) -> dict:
        if is_write:
            self._write_guard()
        body = {
            "operationName": operation,
            "variables": variables,
            "extensions": {"persistedQuery": {
                "version": 1, "sha256Hash": PERSISTED_HASHES[operation]}},
        }
        status, payload = self._http(
            "POST", PATHFINDER_URL,
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "Content-Type": "application/json;charset=UTF-8",
                     "app-platform": "WebPlayer",
                     "spotify-app-version": APP_VERSION,
                     "Origin": "https://open.spotify.com",
                     "Referer": "https://open.spotify.com/",
                     "Accept": "application/json",
                     "User-Agent": USER_AGENT},
            body=body)
        if status == 401:
            self._token = ""
            raise SpotifyError("Pathfinder 返回 401：第一方令牌被拒，"
                               "请重试（令牌会重新换取）；若反复出现，"
                               "多半是 sp_dc 已失效。")
        if status != 200:
            raise SpotifyError(f"Pathfinder {operation} 失败 HTTP {status}")
        errors = payload.get("errors") or []
        if errors:
            messages = "; ".join(str(e.get("message", e)) for e in errors)
            if "PersistedQueryNotFound" in messages:
                raise SpotifyError(
                    f"Pathfinder {operation} 失败：PersistedQueryNotFound——"
                    "持久化查询哈希可能已随网页播放器发版轮换，需要更新 "
                    "cookie_client.py 里的 PERSISTED_HASHES 后再试。")
            raise SpotifyError(f"Pathfinder {operation} 失败: {messages}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SpotifyError(f"Pathfinder {operation} 返回缺少 data 字段")
        return data

    @staticmethod
    def _check_mutation(data: dict, operation: str) -> dict:
        """检查写操作的返回节点。HTTP 200 不等于成功：失败时节点是
        错误联合类型（__typename 含 Error），必须在这里拦下如实报错。"""
        node = data.get(operation)
        if not isinstance(node, dict):
            raise SpotifyError(
                f"{operation} 返回缺少结果节点，无法确认是否成功，"
                "已按失败处理；请先读一遍歌单现状再决定是否重试。")
        typename = str(node.get("__typename", ""))
        inner_error = node.get("error")
        if "error" in typename.lower() or inner_error:
            detail = ""
            if isinstance(inner_error, dict):
                detail = str(inner_error.get("message") or inner_error)
            elif inner_error:
                detail = str(inner_error)
            elif node.get("message"):
                detail = str(node["message"])
            raise SpotifyError(
                f"{operation} 被服务端拒绝（__typename={typename}）"
                + (f": {detail}" if detail else "")
                + "。请先读一遍歌单现状再决定是否重试。")
        return node

    # ---------- 读 ----------

    def get_playlist_contents(self, playlist_id_or_uri: str) -> dict:
        """读歌单全部条目：[{position, uid, uri, name, artists, album,
        duration_ms}]，外加歌单 URI 与总数。uid 是删歌/重排的凭据。"""
        uri = playlist_uri(playlist_id_or_uri)
        items: list = []
        total = None
        offset = 0
        for _ in range(100):  # 页数上限保护
            data = self._pathfinder(
                "fetchPlaylistContents",
                {"uri": uri, "offset": offset, "limit": PAGE_LIMIT},
                is_write=False)
            playlist_v2 = data.get("playlistV2") or {}
            content = playlist_v2.get("content") or {}
            if total is None:
                total = content.get("totalCount")
            page_items = content.get("items") or []
            for raw in page_items:
                items.append(self._parse_item(raw, len(items)))
            offset += len(page_items)
            if not page_items or (total is not None and offset >= total):
                break
        return {"uri": uri,
                "total": total if total is not None else len(items),
                "items": items}

    @staticmethod
    def _parse_item(raw: dict, position: int) -> dict:
        data = ((raw.get("itemV2") or {}).get("data")) or {}
        artists = []
        for entry in ((data.get("artists") or {}).get("items") or []):
            name = ((entry or {}).get("profile") or {}).get("name")
            if name:
                artists.append(name)
        duration = data.get("duration") or data.get("trackDuration") or {}
        return {
            "position": position,
            "uid": raw.get("uid", ""),
            "uri": data.get("uri", ""),
            "name": data.get("name", ""),
            "artists": artists,
            "album": (data.get("albumOfTrack") or {}).get("name", ""),
            "duration_ms": duration.get("totalMilliseconds"),
        }

    # ---------- 写 ----------

    def add_tracks(self, playlist_id_or_uri: str, uris: list[str],
                   position: int | None = None) -> dict:
        """按给定顺序批量加歌，随后回读校验。

        position=None 追加到末尾；0 插到最前；N>0 插到第 N 首之后
        （以 AFTER_UID 锚点实现）。返回 {added, verified, typename}。
        """
        if not uris:
            raise SpotifyError("没有要添加的曲目")
        uri = playlist_uri(playlist_id_or_uri)
        before = self.get_playlist_contents(uri)
        if position is None or position >= len(before["items"]):
            new_position = {"moveType": "BOTTOM_OF_PLAYLIST", "fromUid": None}
            insert_at = len(before["items"])
        elif position <= 0:
            new_position = {"moveType": "TOP_OF_PLAYLIST", "fromUid": None}
            insert_at = 0
        else:
            anchor = before["items"][position - 1]
            new_position = {"moveType": "AFTER_UID", "fromUid": anchor["uid"]}
            insert_at = position
        data = self._pathfinder(
            "addToPlaylist",
            {"playlistUri": uri, "playlistItemUris": list(uris),
             "newPosition": new_position},
            is_write=True)
        node = self._check_mutation(data, "addToPlaylist")
        after = self.get_playlist_contents(uri)
        after_uris = [it["uri"] for it in after["items"]]
        block = after_uris[insert_at:insert_at + len(uris)]
        verified = (len(after_uris) == len(before["items"]) + len(uris)
                    and block == list(uris))
        return {"playlist": uri, "added": len(uris), "position": position,
                "verified": verified,
                "typename": node.get("__typename", ""),
                "note": "已回读校验" if verified else
                        "回读结果与预期不一致，请人工核对歌单现状"}

    def remove_tracks(self, playlist_id_or_uri: str, uris: list[str]) -> dict:
        """按曲目 uri 删歌（同 uri 的所有条目一并删除），随后回读校验。"""
        if not uris:
            raise SpotifyError("没有要删除的曲目")
        uri = playlist_uri(playlist_id_or_uri)
        before = self.get_playlist_contents(uri)
        wanted = set(uris)
        uids = [it["uid"] for it in before["items"] if it["uri"] in wanted]
        missing = wanted - {it["uri"] for it in before["items"]}
        if missing:
            raise SpotifyError(
                f"歌单里找不到这些曲目，未做任何删除: {sorted(missing)}")
        if not uids:
            raise SpotifyError("没有匹配到可删除的条目")
        data = self._pathfinder(
            "removeFromPlaylist",
            {"playlistUri": uri, "uids": uids},
            is_write=True)
        node = self._check_mutation(data, "removeFromPlaylist")
        after = self.get_playlist_contents(uri)
        remaining = [it for it in after["items"] if it["uri"] in wanted]
        verified = not remaining and \
            len(after["items"]) == len(before["items"]) - len(uids)
        return {"playlist": uri, "removed_items": len(uids),
                "verified": verified,
                "typename": node.get("__typename", ""),
                "note": "已回读校验" if verified else
                        "回读结果与预期不一致，请人工核对歌单现状"}

    def move_items(self, playlist_id_or_uri: str, uids: list[str],
                   new_position: dict) -> dict:
        """底层移动：把 uids 这批条目移到 new_position 指定的锚点处。
        new_position 与 addToPlaylist 共用同一套 {moveType, fromUid}
        结构（同一个持久化文档，靠 operationName 区分）。"""
        uri = playlist_uri(playlist_id_or_uri)
        data = self._pathfinder(
            "moveItemsInPlaylist",
            {"playlistUri": uri, "uids": list(uids),
             "newPosition": new_position},
            is_write=True)
        node = self._check_mutation(data, "moveItemsInPlaylist")
        return {"playlist": uri, "moved_items": len(uids),
                "typename": node.get("__typename", "")}

    def reorder_to(self, playlist_id_or_uri: str, target_uris: list[str]) -> dict:
        """把歌单重排成 target_uris 给出的完整目标顺序，随后回读校验。

        目标顺序必须与歌单现状是同一批曲目（含重复计数），否则报错
        不动手。算法：按目标位置逐个定位，单条目 BEFORE_UID 移动，
        出错即停并报告。
        """
        uri = playlist_uri(playlist_id_or_uri)
        before = self.get_playlist_contents(uri)
        current_items = before["items"]
        if len(target_uris) != len(current_items):
            raise SpotifyError(
                f"目标顺序有 {len(target_uris)} 首，歌单现状有 "
                f"{len(current_items)} 首，数量不一致，未做任何移动。"
                "重排只调整顺序，不增删曲目。")
        from collections import defaultdict, deque
        by_uri: dict = defaultdict(deque)
        for item in current_items:
            by_uri[item["uri"]].append(item["uid"])
        target_uids: list = []
        for track_uri in target_uris:
            if not by_uri.get(track_uri):
                raise SpotifyError(
                    f"目标顺序里的 {track_uri} 在歌单现状中不存在"
                    "（或重复次数对不上），未做任何移动。")
            target_uids.append(by_uri[track_uri].popleft())
        moves = plan_moves([it["uid"] for it in current_items], target_uids)
        executed = 0
        for uid, new_position in moves:
            self.move_items(uri, [uid], new_position)
            executed += 1
        after = self.get_playlist_contents(uri)
        after_uris = [it["uri"] for it in after["items"]]
        verified = after_uris == list(target_uris)
        if not verified:
            raise SpotifyError(
                f"重排执行了 {executed} 步移动，但回读顺序与目标不一致，"
                "请人工核对歌单现状。")
        return {"playlist": uri, "moves": executed, "verified": True,
                "total": len(target_uris),
                "note": "已回读校验，顺序与目标一致"}

    # ---------- 建歌单（spclient，尽力而为） ----------

    def _client_token(self) -> str:
        status, data = self._http(
            "POST", CLIENTTOKEN_URL,
            headers={"Accept": "application/json",
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
            body={"client_data": {
                "client_version": APP_VERSION,
                "client_id": WEB_PLAYER_CLIENT_ID,
                "js_sdk_data": {"device_brand": "unknown",
                                "device_model": "unknown",
                                "os": "windows", "os_version": "NT 10.0",
                                "device_id": "0" * 32,
                                "device_type": "computer"}}})
        token = ((data.get("granted_token") or {}).get("token")) or ""
        if status != 200 or not token:
            raise SpotifyError(f"铸造 client-token 失败 HTTP {status}")
        return token

    def create_playlist(self, name: str) -> dict:
        """经 spclient 建歌单（尽力而为）。

        能建出歌单并返回 URI，但：①归档进曲库 rootlist 需要账号
        user id，本后端暂未解析，所以新歌单暂时不会出现在曲库列表
        里（用返回的 URI 可以直接加歌/读取）；②这个通道只能设名字，
        描述与公开/私密请用官方后端或网页端改。这两点都会如实写进
        返回值，不假装完整。
        """
        if not name.strip():
            raise SpotifyError("歌单名不能为空")
        self._write_guard()
        body = {"ops": [{"kind": 6, "updateListAttributes": {
            "newAttributes": {"values": {"name": name,
                                         "formatAttributes": [],
                                         "pictureSize": []},
                              "noValue": []}}}]}
        status, data = self._http(
            "POST", SPCLIENT_PLAYLIST_URL,
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "client-token": self._client_token(),
                     "Content-Type": "application/json;charset=UTF-8",
                     "Accept": "application/json",
                     "User-Agent": USER_AGENT},
            body=body)
        new_uri = data.get("uri") or ""
        if status != 200 or not new_uri.startswith("spotify:playlist:"):
            raise SpotifyError(
                f"spclient 建歌单失败 HTTP {status}，未拿到新歌单 URI")
        return {"uri": new_uri, "name": name, "filed_in_library": False,
                "note": "已通过 spclient 创建，但尚未归档进曲库 rootlist"
                        "（需要账号 user id，本后端暂未实现解析）；"
                        "用返回的 URI 可直接加歌/读取。描述与公开/私密"
                        "未在此通道设置。"}


def plan_moves(current_uids: list, target_uids: list) -> list:
    """计算把 current_uids 变成 target_uids 的单条目移动序列。

    返回 [(uid, new_position), ...]，new_position 为
    {"moveType": "BEFORE_UID", "fromUid": 锚点uid}。纯函数，供测试。
    前提：两个列表是同一批 uid 的排列。
    """
    work = list(current_uids)
    moves: list = []
    for index, want in enumerate(target_uids):
        if work[index] == want:
            continue
        found = work.index(want, index + 1)
        moves.append((want, {"moveType": "BEFORE_UID",
                             "fromUid": work[index]}))
        work.pop(found)
        work.insert(index, want)
    return moves

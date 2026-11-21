#!/usr/bin/env python3
"""cookie 模式的离线测试：不碰真实账号、不连 Spotify。

覆盖：
- TOTP：与一份按社区 JS 原文逐字重写的独立实现交叉核对；内置密钥
  字符串与社区发布的字节数组（spotify_monitor 的 SECRET_CIPHER_DICT）
  逐字一致；secretDict 解析。
- plan_moves：对随机排列套用移动序列后必须得到目标顺序。
- CookieClient：用内存假 Pathfinder 服务端跑通读/加/删/重排；
  __typename 错误与 PersistedQueryNotFound 必须如实抛错；
  未配置 sp_dc 时本地报错且传输层零调用；写操作上限生效。
- browser_login：登录状态分类与 Chromium 探测（不启动浏览器）。

运行：python3 tests/test_cookie_client.py
"""
import hashlib
import hmac
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SPOTIFY_FULL_TOTP_NO_FETCH"] = "1"  # 测试禁止远程拉密钥

import browser_login
import cookie_client
from cookie_client import (BUILTIN_TOTP_SECRETS, CookieClient,
                           _normalize_secret_dict, plan_moves, totp_code)
from spotify_client import SpotifyError

# 社区发布的 TOTP 密钥字节数组（网页播放器共享密钥，非账号凭证），
# 出自 misiektoja/spotify_monitor 的 SECRET_CIPHER_DICT
PUBLISHED_SECRET_BYTES = {
    59: [123, 105, 79, 70, 110, 59, 52, 125, 60, 49, 80, 70, 89, 75, 80,
         86, 63, 53, 123, 37, 117, 49, 52, 93, 77, 62, 47, 86, 48, 104,
         68, 72],
    60: [79, 109, 69, 123, 90, 65, 46, 74, 94, 34, 58, 48, 70, 71, 92,
         85, 122, 63, 91, 64, 87, 87],
    61: [44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120, 97, 75,
         76, 94, 102, 43, 69, 49, 120, 118, 80, 64, 78],
}


def reference_totp(secret: str, for_time: float) -> str:
    """按 songmirror Postman 手册里的 JS 预请求脚本逐字重写的实现，
    与 cookie_client.totp_code 是两条独立代码路径，互为校验。"""
    key_str = ""
    for i, ch in enumerate(secret):
        key_str += str(ord(ch) ^ (i % 33 + 9))
    counter = int(for_time) // 30
    counter_hex = "%016x" % counter
    hmac_hex = hmac.new(key_str.encode("utf-8"),
                        bytes.fromhex(counter_hex), hashlib.sha1).hexdigest()
    offset = int(hmac_hex[-1], 16)
    code = int(hmac_hex[offset * 2:offset * 2 + 8], 16) & 0x7FFFFFFF
    return "%06d" % (code % 1000000)


class TotpTests(unittest.TestCase):
    def test_builtin_secrets_match_published_bytes(self):
        for version, byte_list in PUBLISHED_SECRET_BYTES.items():
            decoded = "".join(chr(b) for b in byte_list)
            self.assertEqual(BUILTIN_TOTP_SECRETS[version], decoded,
                             f"v{version} 内置密钥与社区字节数组不一致")

    def test_totp_matches_reference_implementation(self):
        times = [0, 29, 30, 61 * 30, 1_700_000_000, 1_763_700_000]
        for version, secret in BUILTIN_TOTP_SECRETS.items():
            for moment in times:
                self.assertEqual(totp_code(secret, moment),
                                 reference_totp(secret, moment),
                                 f"v{version} @ {moment}")
                self.assertEqual(len(totp_code(secret, moment)), 6)

    def test_normalize_secret_dict(self):
        parsed = _normalize_secret_dict(
            {"61": PUBLISHED_SECRET_BYTES[61], "62": "abcXYZ",
             "bad": [1, 2], "63": ""})
        self.assertEqual(parsed[61], BUILTIN_TOTP_SECRETS[61])
        self.assertEqual(parsed[62], "abcXYZ")
        self.assertNotIn("bad", parsed)
        self.assertEqual(_normalize_secret_dict([{"version": 64,
                                                  "secret": "s3cr3t"}]),
                         {64: "s3cr3t"})
        self.assertEqual(_normalize_secret_dict(None), {})
        self.assertEqual(_normalize_secret_dict(42), {})


class PlanMovesTests(unittest.TestCase):
    @staticmethod
    def apply_moves(current, moves):
        work = list(current)
        for uid, pos in moves:
            assert pos["moveType"] == "BEFORE_UID"
            work.remove(uid)
            work.insert(work.index(pos["fromUid"]), uid)
        return work

    def test_random_permutations(self):
        rng = random.Random(20261121)
        for size in (1, 2, 5, 11, 40):
            base = [f"u{i}" for i in range(size)]
            for _ in range(30):
                target = base[:]
                rng.shuffle(target)
                moves = plan_moves(base, target)
                self.assertEqual(self.apply_moves(base, moves), target)

    def test_already_ordered(self):
        self.assertEqual(plan_moves(["a", "b"], ["a", "b"]), [])


class FakeTransport:
    """内存版假 Spotify 第一方服务端：server-time + token + Pathfinder。
    tracks 是 [(uid, track_uri), ...]，move/add/remove 语义与真实
    newPosition 锚点一致。"""

    def __init__(self, tracks=()):
        self.tracks = list(tracks)
        self.counter = 1000
        self.calls = []
        self.fail_add_typename = False
        self.fail_pqn = False
        self.anonymous = False

    def __call__(self, method, url, headers=None, params=None, body=None):
        self.calls.append((method, url, body))
        if url == cookie_client.SERVER_TIME_URL:
            return 200, {"serverTime": 1_763_700_000}
        if url == cookie_client.TOKEN_URL:
            return 200, {"accessToken": "FAKE-TOKEN",
                         "accessTokenExpirationTimestampMs":
                             1_763_700_000_000 + 3_600_000,
                         "isAnonymous": self.anonymous}
        if url == cookie_client.PATHFINDER_URL:
            return self._pathfinder(body or {})
        raise AssertionError(f"意料之外的请求: {method} {url}")

    def _contents_payload(self, offset, limit):
        page = self.tracks[offset:offset + limit]
        items = [{"uid": uid,
                  "itemV2": {"data": {
                      "uri": track_uri, "name": f"Song {track_uri}",
                      "artists": {"items": [{"profile": {"name": "MJ"}}]},
                      "albumOfTrack": {"name": "Album"},
                      "duration": {"totalMilliseconds": 1234}}}}
                 for uid, track_uri in page]
        return {"data": {"playlistV2": {"content": {
            "totalCount": len(self.tracks), "items": items}}}}

    def _insert_index(self, new_position):
        move_type = new_position.get("moveType")
        if move_type == "TOP_OF_PLAYLIST":
            return 0
        if move_type == "BOTTOM_OF_PLAYLIST":
            return len(self.tracks)
        anchor = new_position.get("fromUid")
        idx = next(i for i, (uid, _) in enumerate(self.tracks)
                   if uid == anchor)
        return idx + 1 if move_type == "AFTER_UID" else idx

    def _pathfinder(self, body):
        if self.fail_pqn:
            return 200, {"errors": [{"message": "PersistedQueryNotFound"}]}
        op = body.get("operationName")
        variables = body.get("variables") or {}
        if op == "fetchPlaylistContents":
            return 200, self._contents_payload(
                variables.get("offset", 0), variables.get("limit", 100))
        if op == "addToPlaylist":
            if self.fail_add_typename:
                return 200, {"data": {"addToPlaylist": {
                    "__typename": "AddToPlaylistError",
                    "message": "mock refusal"}}}
            at = self._insert_index(variables["newPosition"])
            for track_uri in variables["playlistItemUris"]:
                self.counter += 1
                self.tracks.insert(at, (f"uid-{self.counter}", track_uri))
                at += 1
            return 200, {"data": {"addToPlaylist": {
                "__typename": "AddToPlaylistResponse"}}}
        if op == "removeFromPlaylist":
            drop = set(variables["uids"])
            self.tracks = [t for t in self.tracks if t[0] not in drop]
            return 200, {"data": {"removeFromPlaylist": {
                "__typename": "RemoveFromPlaylistResponse"}}}
        if op == "moveItemsInPlaylist":
            moving = [t for t in self.tracks
                      if t[0] in set(variables["uids"])]
            self.tracks = [t for t in self.tracks
                           if t[0] not in set(variables["uids"])]
            at = self._insert_index(variables["newPosition"])
            for entry in moving:
                self.tracks.insert(at, entry)
                at += 1
            return 200, {"data": {"moveItemsInPlaylist": {
                "__typename": "MoveItemsInPlaylistResponse"}}}
        raise AssertionError(f"意料之外的 operation: {op}")


def make_client(fake, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("write_interval", 0)
    return CookieClient({"sp_dc": "dummy-value"}, transport=fake, **kwargs)


class CookieClientTests(unittest.TestCase):
    def test_missing_sp_dc_fails_locally_without_network(self):
        def boom(*a, **k):
            raise AssertionError("不该发网络请求")
        with self.assertRaises(SpotifyError) as ctx:
            CookieClient({}, transport=boom)
        self.assertIn("未配置 sp_dc", str(ctx.exception))
        with self.assertRaises(SpotifyError):
            CookieClient({"sp_dc": "  "}, transport=boom)

    def test_read_contents(self):
        fake = FakeTransport([("u1", "spotify:track:A"),
                              ("u2", "spotify:track:B")])
        client = make_client(fake)
        result = client.get_playlist_contents("PLID")
        self.assertEqual(result["total"], 2)
        self.assertEqual([i["uri"] for i in result["items"]],
                         ["spotify:track:A", "spotify:track:B"])
        self.assertEqual(result["items"][0]["uid"], "u1")
        self.assertEqual(result["items"][0]["artists"], ["MJ"])

    def test_add_in_order_and_verify(self):
        fake = FakeTransport([("u1", "spotify:track:A")])
        client = make_client(fake)
        result = client.add_tracks("PLID", ["spotify:track:X",
                                            "spotify:track:Y"])
        self.assertTrue(result["verified"])
        self.assertEqual([t[1] for t in fake.tracks],
                         ["spotify:track:A", "spotify:track:X",
                          "spotify:track:Y"])
        result = client.add_tracks("PLID", ["spotify:track:TOP"],
                                   position=0)
        self.assertTrue(result["verified"])
        self.assertEqual(fake.tracks[0][1], "spotify:track:TOP")

    def test_add_typename_error_is_reported(self):
        fake = FakeTransport([("u1", "spotify:track:A")])
        fake.fail_add_typename = True
        client = make_client(fake)
        with self.assertRaises(SpotifyError) as ctx:
            client.add_tracks("PLID", ["spotify:track:X"])
        self.assertIn("__typename", str(ctx.exception))

    def test_persisted_query_not_found_is_reported(self):
        fake = FakeTransport([("u1", "spotify:track:A")])
        fake.fail_pqn = True
        client = make_client(fake)
        with self.assertRaises(SpotifyError) as ctx:
            client.get_playlist_contents("PLID")
        self.assertIn("PersistedQueryNotFound", str(ctx.exception))

    def test_remove_by_uri(self):
        fake = FakeTransport([("u1", "spotify:track:A"),
                              ("u2", "spotify:track:B"),
                              ("u3", "spotify:track:A")])
        client = make_client(fake)
        result = client.remove_tracks("PLID", ["spotify:track:A"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["removed_items"], 2)
        self.assertEqual([t[1] for t in fake.tracks], ["spotify:track:B"])
        with self.assertRaises(SpotifyError):
            client.remove_tracks("PLID", ["spotify:track:NOT-THERE"])

    def test_reorder_full_target_with_duplicates(self):
        fake = FakeTransport([("u1", "spotify:track:A"),
                              ("u2", "spotify:track:B"),
                              ("u3", "spotify:track:A"),
                              ("u4", "spotify:track:C")])
        client = make_client(fake)
        target = ["spotify:track:A", "spotify:track:A",
                  "spotify:track:C", "spotify:track:B"]
        result = client.reorder_to("PLID", target)
        self.assertTrue(result["verified"])
        self.assertEqual([t[1] for t in fake.tracks], target)

    def test_reorder_rejects_mismatched_target(self):
        fake = FakeTransport([("u1", "spotify:track:A")])
        client = make_client(fake)
        with self.assertRaises(SpotifyError):
            client.reorder_to("PLID", ["spotify:track:A",
                                       "spotify:track:EXTRA"])

    def test_write_cap_stops_run(self):
        fake = FakeTransport([("u1", "spotify:track:A")])
        client = make_client(fake, max_writes=1)
        client.add_tracks("PLID", ["spotify:track:X"])
        with self.assertRaises(SpotifyError) as ctx:
            client.add_tracks("PLID", ["spotify:track:Y"])
        self.assertIn("上限", str(ctx.exception))

    def test_anonymous_token_is_rejected(self):
        fake = FakeTransport([("u1", "spotify:track:A")])
        fake.anonymous = True
        client = make_client(fake)
        with self.assertRaises(SpotifyError) as ctx:
            client.get_playlist_contents("PLID")
        self.assertIn("匿名", str(ctx.exception))


class BrowserLoginTests(unittest.TestCase):
    def test_classify_states(self):
        classify = browser_login.classify_login_state
        self.assertEqual(classify("https://open.spotify.com/", "", True),
                         "logged_in")
        self.assertEqual(classify("https://accounts.google.com/v3/signin",
                                  "", False), "google")
        self.assertEqual(classify("https://accounts.spotify.com/en/login",
                                  "Log in to Spotify", False), "credentials")
        self.assertEqual(classify("https://accounts.spotify.com/en/login",
                                  "Enter the 6-digit verification code",
                                  False), "verification_code")
        self.assertEqual(classify("https://open.spotify.com/",
                                  "Please solve the recaptcha", False),
                         "captcha")
        self.assertEqual(classify("https://open.spotify.com/", "", False),
                         "unknown")

    def test_probe_without_chromium(self):
        old = os.environ.get("SPOTIFY_FULL_CHROMIUM")
        os.environ["SPOTIFY_FULL_CHROMIUM"] = "/nonexistent/chrome-x"
        try:
            self.assertIsNone(browser_login.find_chromium())
            self.assertFalse(browser_login.probe()["chromium_available"])
        finally:
            if old is None:
                os.environ.pop("SPOTIFY_FULL_CHROMIUM", None)
            else:
                os.environ["SPOTIFY_FULL_CHROMIUM"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)

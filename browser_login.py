#!/usr/bin/env python3
"""自带无头浏览器登录模块（可选组件）：自动取 sp_dc。

路线：Yousj 浏览器项目的 real 后端做法——无头优先、用 Chrome
DevTools Protocol（CDP）驱动一个真实 Chromium 渲染后端。本模块把
这套做法收窄成一个专用登录器：启动无头 Chromium（独立持久 profile），
打开 open.spotify.com，若浏览器会话里已有有效登录态，直接提取
sp_dc cookie 写回凭据文件；若登录卡在需要真人介入的步骤（账号密码
表单、Google 登录、验证码/两步验证、CAPTCHA），立刻停下来结构化
报告「需要什么」，绝不乱试、绝不接收或保存账号密码。

与手动填 sp_dc 的关系：手动填仍然保留且永远可用；本模块只是一个
可选的自动取 cookie 通道。取到的 sp_dc 只写入
~/.config/spotify-full/credentials.json（600），全程不打印 cookie 值。

零依赖：只用 Python 标准库。CDP 走 WebSocket，标准库没有 WebSocket
客户端，这里的最小实现改编自 Yousj 浏览器项目
（~/workspace/yousj-browser/python/yousj/real.py，同一作者的自研
项目）；CDP 只连本机 127.0.0.1 的 ws://，不需要 TLS。

profile 说明：浏览器 profile 固定放在凭据目录下的 browser-profile/
（700）。登录态保存在 profile 里：若无头流程停在需要真人介入的
步骤，可以用同一个 profile 开一次有界面 Chromium 完成登录，之后
无头流程就能直接提取 sp_dc。

本模块在本仓库的 CI/沙箱里只做静态验证，不拿真实账号实测。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.parse
import urllib.request

from spotify_client import (CONFIG_DIR, CREDENTIALS_PATH, SpotifyError,
                            load_credentials, save_credentials)

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOGIN_URL = "https://open.spotify.com/"
PROFILE_DIR = os.path.join(CONFIG_DIR, "browser-profile")
DEFAULT_CHROME = "/opt/meta-chromium/chrome"
CHROME_CANDIDATES = ("chromium", "chromium-browser",
                     "google-chrome", "google-chrome-stable")


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端（改编自 yousj/real.py，仅 ws:// 本机 CDP）
# ---------------------------------------------------------------------------

class _WSClient:
    """够 CDP 用的 WebSocket 客户端：文本消息、任意大小、分片、
    ping/pong。客户端帧按协议要求掩码；仅支持 ws://（CDP 在本机）。"""

    def __init__(self, url, timeout=60):
        parts = urllib.parse.urlparse(url)
        if parts.scheme != "ws":
            raise ValueError("only ws:// URLs are supported")
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or 80
        self._path = parts.path or "/"
        if parts.query:
            self._path += "?" + parts.query
        self._sock = socket.create_connection(
            (self._host, self._port), timeout=timeout)
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n"
               % (self._path, self._host, self._port, key))
        self._sock.sendall(req.encode("ascii"))
        headers = b""
        while b"\r\n\r\n" not in headers:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake: connection closed")
            headers += chunk
        head, rest = headers.split(b"\r\n\r\n", 1)
        self._buf = rest
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0]:
            raise RuntimeError(
                "websocket handshake: unexpected status %r" % (lines[0],))
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if not any(l.lower().startswith("sec-websocket-accept:")
                   and accept in l for l in lines[1:]):
            raise RuntimeError("websocket handshake: bad Sec-WebSocket-Accept")

    def set_timeout(self, timeout):
        self._sock.settimeout(timeout)

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RuntimeError("websocket: connection closed by peer")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv_frame(self):
        h = self._recv_exact(2)
        b0, b1 = h[0], h[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(ln) if ln else b""
        if mask:
            payload = bytes(p ^ mask[i % 4] for i, p in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode, payload):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        mask = os.urandom(4)
        ln = len(payload)
        if ln < 126:
            header = struct.pack(">BB", 0x80 | opcode, 0x80 | ln)
        elif ln < 65536:
            header = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, ln)
        else:
            header = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, ln)
        masked = bytes(p ^ mask[i % 4] for i, p in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text)

    def recv_message(self):
        parts = []
        opcode = None
        while True:
            fin, op, payload = self._recv_frame()
            if op == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if op == 0x8:  # close
                raise RuntimeError("websocket: closed by peer")
            if op in (0x1, 0x2):
                opcode = op
                parts.append(payload)
            elif op == 0x0:  # continuation
                parts.append(payload)
            else:
                continue
            if fin:
                break
        data = b"".join(parts)
        return data.decode("utf-8") if opcode == 0x1 else data

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


class _CDP:
    """同步 CDP 请求/响应（事件消息直接忽略，本模块靠轮询判断状态）。"""

    def __init__(self, ws_url, timeout=60):
        self._ws = _WSClient(ws_url, timeout=timeout)
        self._timeout = timeout
        self._next_id = 0

    def call(self, method, params=None, timeout=None):
        self._ws.set_timeout(timeout or self._timeout)
        self._next_id += 1
        mid = self._next_id
        msg = {"id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self._ws.send_text(json.dumps(msg))
        while True:
            raw = self._ws.recv_message()
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if data.get("id") != mid:
                continue
            if "error" in data:
                err = data["error"]
                raise RuntimeError("CDP %s failed: %s"
                                   % (method, err.get("message", err)))
            return data.get("result", {})

    def close(self):
        self._ws.close()


def _free_port():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _http_no_proxy(method, url, timeout=10):
    """本机 CDP 的 HTTP 端点必须绕开代理环境变量（沙箱出口代理会让
    127.0.0.1 请求挂死），这一点同样来自 Yousj 浏览器 real 后端。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, method=method)
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Chromium 发现与登录状态判断
# ---------------------------------------------------------------------------

def find_chromium() -> str | None:
    """找可用的 Chromium 二进制：环境变量 > Yousj 默认路径 > PATH。"""
    override = os.environ.get("SPOTIFY_FULL_CHROMIUM", "").strip()
    if override:
        return override if (os.path.isfile(override)
                            and os.access(override, os.X_OK)) else None
    if os.path.isfile(DEFAULT_CHROME) and os.access(DEFAULT_CHROME, os.X_OK):
        return DEFAULT_CHROME
    for name in CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def probe() -> dict:
    """只报本机条件（不启动浏览器、不发网络请求）。"""
    chromium = find_chromium()
    return {"component": "browser_login（可选）",
            "chromium_available": chromium is not None,
            "chromium": chromium,
            "profile_dir": PROFILE_DIR,
            "note": "找不到 Chromium 时本模块不可用，手动填 sp_dc 不受影响"}


def classify_login_state(url: str, page_text: str, has_sp_dc: bool) -> str:
    """把当前页面归类成登录流程状态。纯函数，供测试。

    返回：logged_in / google / captcha / verification_code /
    credentials / unknown。
    """
    if has_sp_dc:
        return "logged_in"
    low_url = (url or "").lower()
    low_text = (page_text or "").lower()
    if "accounts.google.com" in low_url or "google.com/signin" in low_url:
        return "google"
    if "recaptcha" in low_text or "captcha" in low_text \
            or "challenge" in low_url:
        return "captcha"
    if "accounts.spotify.com" in low_url or "/login" in low_url:
        if any(k in low_text for k in ("verification code", "6-digit",
                                       "two-factor", "验证码", "两步")):
            return "verification_code"
        return "credentials"
    return "unknown"


_NEEDS_USER_ACTION = {
    "credentials": "无头浏览器已停在 Spotify 账号密码登录页。本模块不接收、"
                   "不保存账号密码：请用同一个浏览器 profile 开一次有界面 "
                   "Chromium 完成登录（profile 在 browser-profile/），或改用"
                   "手动填 sp_dc。",
    "google": "登录跳转到了 Google 登录页，需要真人在 Google 页面完成"
              "登录（可能含谷歌验证码）。无头流程已停下，未做任何尝试。",
    "verification_code": "登录需要验证器/邮箱验证码，需要真人输入。"
                         "无头流程已停下，未做任何尝试。",
    "captcha": "登录页出现了 CAPTCHA/挑战验证，需要真人完成。"
               "无头流程已停下，未做任何尝试。",
}


class _HeadlessBrowser:
    """一个无头 Chromium 页面（CDP 驱动），只保留登录模块要用的能力：
    导航、读 URL/文本、读全部 cookie。用完必须 close()。"""

    def __init__(self, chrome_path: str, profile_dir: str, timeout=60):
        self._port = _free_port()
        self._proc = None
        self._cdp = None
        self._log = None
        os.makedirs(profile_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(profile_dir, 0o700)
        except OSError:
            pass
        args = [chrome_path, "--headless=new", "--no-sandbox",
                "--disable-gpu", "--disable-dev-shm-usage",
                "--remote-debugging-port=%d" % self._port,
                "--window-size=1280,800",
                "--user-data-dir=%s" % profile_dir,
                "--no-proxy-server", "about:blank"]
        log_path = os.path.join(profile_dir, "browser-login.log")
        self._log = open(log_path, "wb")
        try:
            self._proc = subprocess.Popen(
                args, stdout=self._log, stderr=subprocess.STDOUT)
            ws_url = self._wait_for_page(timeout)
            self._cdp = _CDP(ws_url, timeout=timeout)
            self._cdp.call("Page.enable")
            self._cdp.call("Runtime.enable")
            self._cdp.call("Network.enable")
        except Exception:
            self.close()
            raise

    def _wait_for_page(self, timeout):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    "Chromium 启动后即退出（code %s），日志见 profile 目录"
                    % self._proc.returncode)
            try:
                _, body = _http_no_proxy(
                    "GET", "http://127.0.0.1:%d/json/list" % self._port)
                targets = json.loads(body) if body else []
                for target in targets:
                    if target.get("type") == "page" and \
                            target.get("webSocketDebuggerUrl"):
                        return target["webSocketDebuggerUrl"]
            except Exception as err:  # noqa: BLE001 - 启动轮询，继续等
                last_error = err
            time.sleep(0.5)
        raise RuntimeError("Chromium DevTools 端点未在 %ss 内就绪: %s"
                           % (timeout, last_error))

    def goto(self, url, timeout=30):
        self._cdp.call("Page.navigate", {"url": url})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.3)

    def evaluate(self, js):
        res = self._cdp.call("Runtime.evaluate", {
            "expression": js, "returnByValue": True})
        if "exceptionDetails" in res:
            raise RuntimeError("page JS error")
        return (res.get("result") or {}).get("value")

    def url(self) -> str:
        try:
            return self.evaluate("location.href") or ""
        except Exception:
            return ""

    def text(self, limit=4000) -> str:
        try:
            return (self.evaluate(
                "document.body ? document.body.innerText : ''")
                or "")[:limit]
        except Exception:
            return ""

    def get_sp_dc(self) -> str:
        """从浏览器会话取 sp_dc 的值；没有返回空串。值只在本进程内
        流转，调用方负责写入凭据文件，任何地方都不打印。"""
        res = self._cdp.call("Network.getAllCookies")
        for cookie in res.get("cookies", []):
            if cookie.get("name") == "sp_dc" and cookie.get("value"):
                return str(cookie["value"])
        return ""

    def close(self):
        try:
            if self._cdp is not None:
                self._cdp.close()
        except Exception:
            pass
        self._cdp = None
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None
        try:
            if self._log is not None:
                self._log.close()
        except Exception:
            pass
        self._log = None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def fetch_and_store_sp_dc(timeout_seconds: int = 180,
                          poll_interval: float = 2.0) -> dict:
    """启动无头 Chromium 打开 open.spotify.com，尝试提取 sp_dc。

    结果 dict 的 status：
    - stored：sp_dc 已写入凭据文件（值不出现在返回值里）。
    - needs_user_action：停在需要真人介入的步骤，reason/detail 说明
      需要什么；浏览器已关闭，profile 保留，可用同一 profile 有
      界面完成登录后重试。
    - timeout：时限内既没拿到 sp_dc，也没识别出明确的人工步骤。
    找不到 Chromium 等硬失败抛 SpotifyError。
    """
    chromium = find_chromium()
    if not chromium:
        raise SpotifyError(
            "未找到可用的 Chromium，本机用不了自带浏览器登录模块"
            "（可选组件）。可以装一个 Chromium/Chrome，或改用手动填 "
            f"sp_dc 到 {CREDENTIALS_PATH}。本次未启动浏览器、未发请求。")
    browser = _HeadlessBrowser(chromium, PROFILE_DIR)
    try:
        browser.goto(LOGIN_URL)
        deadline = time.monotonic() + timeout_seconds
        last_state = "unknown"
        last_url = ""
        while time.monotonic() < deadline:
            sp_dc = browser.get_sp_dc()
            if sp_dc:
                creds = load_credentials()
                creds["sp_dc"] = sp_dc
                save_credentials(creds)
                return {"status": "stored", "sp_dc_configured": True,
                        "credentials_path": CREDENTIALS_PATH,
                        "note": "sp_dc 已写入凭据文件（600），值未打印、"
                                "未出现在任何输出里"}
            last_url = browser.url()
            last_state = classify_login_state(
                last_url, browser.text(), has_sp_dc=False)
            if last_state in _NEEDS_USER_ACTION:
                return {"status": "needs_user_action",
                        "reason": last_state,
                        "detail": _NEEDS_USER_ACTION[last_state],
                        "url": last_url,
                        "profile_dir": PROFILE_DIR}
            time.sleep(poll_interval)
        return {"status": "timeout",
                "detail": "时限内未拿到 sp_dc，也未识别出明确的人工步骤",
                "last_state": last_state, "url": last_url,
                "profile_dir": PROFILE_DIR}
    finally:
        browser.close()


if __name__ == "__main__":
    import sys
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
    try:
        result = fetch_and_store_sp_dc()
    except SpotifyError as err:
        print(f"错误: {err}")
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))

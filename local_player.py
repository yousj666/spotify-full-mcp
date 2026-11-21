#!/usr/bin/env python3
"""本机播放（可选组件）：让 AI 在自己这台电脑上用 Spotify Connect 放歌。

设计：这是一个可选的播放端，不是主流程的硬依赖。
- 播放端软件二选一（自行安装，本模块不负责安装）：
  * spotifyd（https://github.com/Spotifyd/spotifyd）：Linux/macOS 守护进程，
    以 Spotify Connect 设备身份出现，常用启动方式：
      spotifyd --no-daemon --device-name "Muse 本机" --bitrate 320
  * librespot（https://github.com/librespot-org/librespot）：同类实现，
    示例：librespot -n "Muse 本机" -b 320
- 播放端起来后，它会作为一个设备出现在 Spotify 的设备列表里。
  本模块按名字找到它，transfer 过去再 play，全程走官方 API，
  不碰播放端软件的内部协议。
- 配置写在凭据文件 ~/.config/spotify-full/credentials.json 的
  local_player 段（见 credentials.example.json）：
      "local_player": {"enabled": true, "device_name": "Muse 本机",
                        "backend": "spotifyd"}
- 未启用、找不到后端程序、或设备没上线时，返回 not_configured 状态
  并给出人话说明，绝不抛异常打断主流程。
"""
from __future__ import annotations

import shutil

BACKENDS = ("spotifyd", "librespot")


def local_player_status(creds: dict) -> dict:
    """汇报本机播放端的配置与就绪状态（不调任何 API）。"""
    cfg = (creds or {}).get("local_player") or {}
    enabled = bool(cfg.get("enabled"))
    device_name = cfg.get("device_name", "")
    backend = cfg.get("backend", "")
    found = {name: shutil.which(name) for name in BACKENDS}
    ready_backend = backend if (backend in found and found[backend]) else None
    if not ready_backend:
        ready_backend = next((n for n, p in found.items() if p), None)

    if not enabled:
        state, message = "not_configured", (
            "未配置本机播放端：在凭据文件的 local_player 段把 enabled 设为 true，"
            "并先装好 spotifyd 或 librespot 之一（见本模块顶部说明）。")
    elif not ready_backend:
        state, message = "backend_missing", (
            "已启用本机播放，但 PATH 里找不到 spotifyd / librespot，"
            "请先安装其中之一并让它以 Spotify Connect 设备方式运行。")
    elif not device_name:
        state, message = "device_name_missing", (
            "已启用本机播放，但 local_player.device_name 为空，"
            "填上播放端启动时用的 --device-name 名字。")
    else:
        state, message = "ready", (
            f"本机播放端已就绪（后端 {ready_backend}，设备名「{device_name}」）。"
            "设备是否在线要在播放时查设备列表确认。")
    return {
        "status": state,
        "message": message,
        "enabled": enabled,
        "device_name": device_name,
        "backend_found": found,
    }


def play_on_local(client, creds: dict, context_uri: str | None = None,
                  uris: list[str] | None = None) -> dict:
    """把播放切到本机播放端并开始播放。未配置时返回 not_configured 说明。"""
    status = local_player_status(creds)
    if status["status"] != "ready":
        return status
    device_name = status["device_name"]
    devices = (client.get_devices() or {}).get("devices", [])
    target = next((d for d in devices if d.get("name") == device_name), None)
    if not target:
        return {
            "status": "device_offline",
            "message": (f"设备列表里找不到「{device_name}」，"
                        "本机播放端可能还没启动，或还没连上 Spotify。"),
            "devices_seen": [d.get("name") for d in devices],
        }
    client.transfer_playback(target["id"], play=False)
    result = client.play(device_id=target["id"], context_uri=context_uri, uris=uris)
    return {"status": "playing", "device": device_name, "result": result}

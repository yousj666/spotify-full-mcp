#!/usr/bin/env python3
"""spotify-full 环境初始化：能自动的都自动，真人步骤明确标出。

自动完成：
- 检查 Python 版本（需要 3.10+，本项目零第三方依赖）
- 检查项目文件齐全且能编译
- 创建凭据目录 ~/.config/spotify-full（权限 700，可用环境变量
  SPOTIFY_FULL_CONFIG_DIR 覆盖）
- 若凭据文件不存在，从 credentials.example.json 生成一份空占位
  credentials.json（权限 600，已有文件绝不覆盖）
- 汇报本机播放端后端（spotifyd / librespot）是否在 PATH 中

必须真人完成（脚本只打印清单，不代做）：
1. 在 Spotify Developer Dashboard 创建开发者应用并登记回调地址
2. 把 client_id / client_secret 填入凭据文件（或经安全录入通道写入）
3. 运行 spotify.py auth 时在浏览器里登录 Spotify 并点「同意授权」
"""
import json
import os
import py_compile
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get(
    "SPOTIFY_FULL_CONFIG_DIR", os.path.expanduser("~/.config/spotify-full"))
CREDENTIALS_PATH = os.path.join(CONFIG_DIR, "credentials.json")
PY_FILES = ["spotify.py", "spotify_client.py", "cookie_client.py",
            "browser_login.py", "lyrics.py", "listen.py",
            "local_player.py", "setup.py"]


def check_python() -> bool:
    ok = sys.version_info >= (3, 10)
    print(f"[{'OK' if ok else 'FAIL'}] Python {sys.version.split()[0]}"
          "（需要 3.10+，纯标准库无第三方依赖）")
    return ok


def check_files() -> bool:
    ok = True
    for name in PY_FILES:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            print(f"[FAIL] 缺少文件 {name}")
            ok = False
            continue
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as err:
            print(f"[FAIL] {name} 编译失败: {err}")
            ok = False
    if ok:
        print(f"[OK] 项目文件齐全且全部编译通过（{len(PY_FILES)} 个 .py）")
    return ok


def init_config_dir() -> None:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    print(f"[OK] 凭据目录就绪: {CONFIG_DIR}（700）")

    if os.path.exists(CREDENTIALS_PATH):
        print(f"[OK] 凭据文件已存在，不覆盖: {CREDENTIALS_PATH}")
        return
    example_path = os.path.join(HERE, "credentials.example.json")
    with open(example_path, "r", encoding="utf-8") as fh:
        example = json.load(fh)
    example.pop("_说明", None)
    fd = os.open(CREDENTIALS_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(example, fh, ensure_ascii=False, indent=2)
    print(f"[OK] 已生成空占位凭据文件: {CREDENTIALS_PATH}（600）")


def check_local_player() -> None:
    found = [name for name in ("spotifyd", "librespot") if shutil.which(name)]
    if found:
        print(f"[OK] 本机播放后端可用: {', '.join(found)}（可选组件）")
    else:
        print("[--] 未发现 spotifyd / librespot，本机播放为可选组件，跳过不影响主功能")


def print_human_steps() -> None:
    print()
    print("=== 接下来必须真人完成的步骤 ===")
    print("1. 打开 https://developer.spotify.com/dashboard ，用要连接的 Spotify")
    print("   账号登录，点 Create app 建一个开发者应用（名字随意）。")
    print("2. 在应用的 Settings 里把 Redirect URI 填为：")
    print("   http://127.0.0.1:8888/callback")
    print(f"3. 把应用的 Client ID / Client Secret 填入 {CREDENTIALS_PATH}")
    print("   的 client_id / client_secret 字段（保持文件权限 600）。")
    print("4. 运行: python3 spotify.py auth")
    print("   浏览器会打开授权页，登录并点「同意」后令牌自动落盘。")
    print("5. 自检: python3 spotify.py status  应显示 authorized: true")
    print("        python3 spotify.py me        应返回你的账号资料")


def main() -> int:
    print("=== spotify-full 环境初始化 ===")
    ok = check_python()
    ok = check_files() and ok
    init_config_dir()
    check_local_player()
    print_human_steps()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

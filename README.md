# spotify-full

一个全功能的 Spotify 命令行工具：用**你自己的** Spotify 开发者应用授权，调用官方 Web API 的完整接口面，再加上歌词、译文与「AI 的耳朵」聆听报告。纯 Python 标准库实现，零第三方依赖。

为什么不用现成的连接器？很多托管连接器只申请了部分权限（比如只有「改私密歌单」、没有「改公开歌单」），加歌会被 403 拒绝，也普遍不开放歌单排序。用自己的开发者应用，15 个 scope 一次申请满，能力以官方 API 为上限。

## 功能

- **搜索与目录**：歌曲/专辑/艺人/歌单搜索，专辑曲目，艺人作品与热门曲目
- **歌单全功能**：列表、读取、新建、加歌（支持指定位置插入）、删歌、**拖动排序（reorder）**、改名/描述/公开私密、换封面
- **曲库**：已保存歌曲（Liked Songs）/专辑的读取与 save/unsave
- **播放控制**：设备列表、播放/暂停/上下曲/进度/音量/随机/循环/队列/设备切换、当前播放
- **用户数据**：账号资料、top 歌曲/艺人、最近播放、关注艺人列表与 follow/unfollow
- **歌词**：原文歌词走 [LRCLIB](https://lrclib.net)（免费、免 key，含带时间轴的同步歌词）；译文预留 Musixmatch 官方 API 接口位，配了 key 就能用，没配会明确提示「未配置」而不是报错
- **AI 的耳朵**：`listen` 子命令生成「聆听报告」（见下文）
- **本机播放（可选）**：通过 spotifyd / librespot 在本机起 Spotify Connect 设备播放

## AI 的耳朵是什么

AI 没有耳朵，但 Spotify 官方 API 提供两层机器可读的音频数据：

- **audio-features**：一首歌的整体画像——速度（BPM）、调式（key/mode）、能量、舞蹈性、情绪效价、原声性、器乐性、响度、语音性、拍号
- **audio-analysis**：逐段拆解——每个 section 的调式/速度/响度、小节与节拍网格、成千上万个 segment 的音高与音色轮廓

`listen` 把这两层数据聚合，再附上带时间轴的同步歌词，生成一份结构化报告。AI 读这份报告，就像乐手读总谱加歌词：能说出这首歌是什么调、哪里转段、能量曲线怎么走、歌词在什么时间点唱了什么。这是 AI 理解与欣赏音乐的一条真实通道。

```text
python3 spotify.py listen "Billie Jean" "Michael Jackson"
python3 spotify.py --json listen --uri spotify:track:7J1uxwnxfQLu4APicE5Rnj
```

## 安装

需要 Python 3.10+，不需要 pip 安装任何东西。

```bash
git clone <本仓库地址> spotify-full
cd spotify-full
python3 setup.py
```

`setup.py` 会自动完成：环境与文件检查、创建凭据目录 `~/.config/spotify-full`（700）、生成空占位凭据文件（600），并打印必须真人完成的步骤清单。

## 快速开始

1. 按下面「部署指南」建好开发者应用并填入凭据（约 3 分钟）
2. 授权：`python3 spotify.py auth`，在浏览器里登录并点同意
3. 自检：`python3 spotify.py status` 显示 `authorized: true`；`python3 spotify.py me` 返回你的账号资料
4. 开始用：`python3 spotify.py playlists`、`python3 spotify.py lyrics "Bad" "Michael Jackson"`

## AI Agent 自部署指南

本节写给要替用户部署本工具的 AI Agent。按编号执行，能自动的步骤不要让用户动手；标了的步骤停下来等用户完成，不要代填、不要索要密码或令牌原文。

1. **跑初始化**：执行 `python3 setup.py`。它会自动建凭据目录（`~/.config/spotify-full`，700）与空占位凭据文件（600），并自检环境。期望输出含 `[OK] Python`、`[OK] 项目文件齐全且全部编译通过`。
2. **创建开发者应用**：用户打开 https://developer.spotify.com/dashboard ，用要连接的 Spotify 账号登录，点 **Create app**。App name 与 description 任意填（例如 `spotify-full`）。API 勾选 **Web API**。
3. **登记回调地址**：在应用的 Settings 里，把 **Redirect URI** 精确填为 `http://127.0.0.1:8888/callback` 并保存。多一个斜杠或少一个字符都会让授权失败。
4. **填入应用凭证**：用户在同一 Settings 页能看到 **Client ID** 与 **Client Secret**。把它们填入 `~/.config/spotify-full/credentials.json` 的 `client_id` / `client_secret` 字段，保持文件权限 600。不要在聊天里明文传递 Client Secret，走安全录入。
5. **发起授权**：执行 `python3 spotify.py auth`。它会在本机 127.0.0.1:8888 起一次性回调服务，并打印授权链接（也会尝试自动打开浏览器）。申请的 scope 共 15 个，含 `playlist-modify-public` 与 `playlist-modify-private`，清单在 `spotify_client.py` 的 `SCOPES` 常量里。
6. **登录并同意**：用户在打开的授权页登录 Spotify 并点「同意」。回调成功后令牌自动写入凭据文件，终端打印「授权完成」。
7. **自检（只读，无副作用）**：
- `python3 spotify.py status` → 期望 `"authorized": true`
- `python3 spotify.py me` → 期望返回该账号的 `id`、`display_name`、`email`
- `python3 spotify.py playlists` → 期望列出该账号的歌单
- `python3 spotify.py lyrics "Billie Jean" "Michael Jackson" --duration 294` → 期望输出歌词全文（这条走 LRCLIB，不依赖授权，可先行验证）
8. **首个写操作验证**：先用 `playlist-tracks <id>` 读一遍目标歌单现状，再做一次可核对的小改动（例如给一个测试歌单加一首歌），改完再读一遍确认生效。写操作结果不明时先读现状，不要盲目重试。
9. **可选：本机播放**。若用户想要 AI 在本机放歌，让用户安装 spotifyd 或 librespot 之一，以 `--device-name` 起一个固定名字，再把凭据文件 `local_player` 段的 `enabled` 设为 true 并填 `device_name`。自检：`python3 spotify.py play-local` 应返回 `status: ready`。未配置时它返回 `not_configured` 与说明，这是预期行为，不是故障。
10. **可选：译文**。若用户有 Musixmatch 开发者 key，填入凭据文件的 `musixmatch_api_key`，之后 `lyrics --with-translation` 会并排输出译文。没有 key 时该参数只会多出一行「未配置」说明，原文歌词照常输出。

## 命令一览

| 子命令 | 用途 |
| --- | --- |
| `auth` / `status` | OAuth 授权 / 查看凭据与令牌状态 |
| `search <词> [--type...] [--limit N]` | 搜索歌曲/专辑/艺人/歌单 |
| `track / album / album-tracks / artist / artist-albums / artist-top <id>` | 目录详情 |
| `playlists / playlist / playlist-tracks <id>` | 歌单列表与读取 |
| `playlist-create <名> [--description 文] [--public]` | 新建歌单 |
| `playlist-add <id> <uri...> [--position N]` | 加歌，可指定插入位置 |
| `playlist-remove <id> <uri...>` | 删歌 |
| `playlist-reorder <id> <range_start> <insert_before> [--range-length N]` | 排序 |
| `playlist-edit <id> [--name 文] [--description 文] [--public\|--private]` | 改歌单信息 |
| `playlist-cover <id> <图片.jpg>` | 换封面（JPEG，<256KB） |
| `library-tracks / library-albums` | 已保存歌曲/专辑 |
| `save-track / unsave-track / save-album / unsave-album <id...>` | 收藏与取消 |
| `devices / now / play / pause / next / previous` | 播放控制 |
| `seek <毫秒> / volume <0-100> / shuffle <on\|off> / repeat <off\|track\|context>` | 播放参数 |
| `queue-add <uri> / transfer <device_id>` | 队列与设备切换 |
| `me / top-tracks / top-artists / recent / following / follow / unfollow` | 用户数据 |
| `audio-features <id>` | 单曲音频特征 |
| `lyrics <歌名> <艺人> [--duration 秒] [--with-translation] [--lang zh]` | 原文歌词与译文 |
| `listen [<歌名> [艺人]] [--uri <uri>] [--no-lyrics]` | AI 的耳朵聆听报告 |
| `play-local [--uri <uri> \| --context <uri>]` | 本机播放（可选组件） |

全局参数 `--json` 放在子命令之前时，所有输出为结构化 JSON（歌词与聆听报告默认是人类可读文本）。

## 目录结构

```text
spotify-full/
├── spotify.py CLI 入口（全部子命令）
├── spotify_client.py 官方 Web API 客户端（OAuth + 全接口面）
├── lyrics.py 歌词模块（LRCLIB + Musixmatch 译文接口位）
├── listen.py AI 的耳朵（聆听报告聚合与格式化）
├── local_player.py 本机播放可选组件（spotifyd / librespot）
├── setup.py 环境初始化脚本
├── credentials.example.json 凭据结构示例（只有占位，无真实值）
├──.env.example 可选环境变量示例
├──.gitignore 排除凭据、令牌与缓存
├── LICENSE MIT
└── README.md
```

运行时凭据不在仓库里，存放在 `~/.config/spotify-full/credentials.json`（目录 700、文件 600），可用环境变量 `SPOTIFY_FULL_CONFIG_DIR` 改位置。

## 注意事项

- Spotify 在 2024 年 11 月后收紧了新开发者应用对 audio-features / audio-analysis 的访问，新建应用可能调不通这两个接口，`listen` 会直接报出官方错误。歌单、曲库、播放、歌词等功能不受影响。
- 播放控制需要有在线的 Spotify Connect 设备，且多数播放操作要求 Premium 账号（官方限制）。
- 本工具只调用官方 API 与 LRCLIB 公开接口，不抓取网页、不模拟登录。

## 部署状态与还差什么（给接手者）

已完成：全部代码与文档、语法编译、离线逻辑测试、LRCLIB 真实查询验证、`setup.py` 与 `play-local` 的降级路径实测。

还差（按顺序）：

1. 在 Spotify Developer Dashboard 创建开发者应用，登记回调 `http://127.0.0.1:8888/callback`
2. 把 Client ID / Client Secret 填入 `~/.config/spotify-full/credentials.json`
3. 运行 `python3 spotify.py auth` 完成 OAuth 授权
4. 首测：`status` → `me` → `playlists` 三连只读自检，然后对目标歌单做一次加歌并读回确认；`listen` 与 `audio-features` 在新应用上是否被限一并实测，结果补回连接器文档 `~/workspace/custom-connectors/custom.spotify-full/CONNECTOR.md`

## License

MIT，见 [LICENSE](LICENSE)。


---

## Cookie 模式（网页播放器内部接口后端）

> 本节是后加的第二后端说明。默认后端仍是上面的官方 Web API（OAuth）；
> 本节内容与前文冲突时，cookie 模式以本节为准。

### 为什么有这个模式

Spotify 在 2026 年 2 月起要求 Development Mode 应用的**所有者持有
Premium**，否则官方 Web API 返回 403；而且新建应用在歌单内容接口
（读/加/删歌单曲目）上还有单独一层限制。没有 Premium 时，官方后端
对歌单操作基本堵死。

但免费账号在官方**网页播放器**里本来就能建歌单、加歌、删歌、排序，
走的是 Spotify 第一方内部接口。cookie 模式就是用账号自己的登录
cookie（`sp_dc`）以第一方网页客户端身份调同一套接口：不经过开发者
应用，不受 Premium 门限制。已在多个活跃开源项目中实证（建/读/加/
删/**重排**全通），本工具的实现依据是 songmirror 项目 2026 年 7 月
的端到端实证文档。

### 原理（鉴权链）

1. 凭据文件里的 `sp_dc`（浏览器登录态 cookie，有效期约一年）。
2. 取服务器时间 → 用网页播放器 JS 内置的共享密钥生成 TOTP（密钥按
   版本轮换，社区持续从官方 bundle 提取维护；本工具运行时优先从
   社区仓库 `secretDict.json` 拉取并本地缓存，拉不到用内置版本兜底，
   当前内置最新为 v61）。
3. 用 TOTP 向 `open.spotify.com/api/token` 换第一方 access token
   （约 1 小时有效，只存内存）。
4. 歌单读写走 Pathfinder GraphQL（`api-partner.spotify.com`）的持久化
   查询；建歌单走 spclient 通道（见下方限制）。

### 启用与命令

全局参数 `--backend cookie`，或环境变量 `SPOTIFY_FULL_BACKEND=cookie`。
默认仍是官方后端，两种模式共用同一个凭据文件。

```text
python3 spotify.py --backend cookie playlist-tracks <歌单id>
python3 spotify.py --backend cookie playlist-add <歌单id> <uri...> [--position N]
python3 spotify.py --backend cookie playlist-remove <歌单id> <uri...>
python3 spotify.py --backend cookie playlist-set-order <歌单id> <uri...>   # 按完整目标顺序重排
python3 spotify.py --backend cookie playlist-create <名称>                  # spclient 通道，见限制
python3 spotify.py cookie-login                                             # 自带浏览器登录取 sp_dc
```

- `playlist-add` 按给出的顺序批量加歌：不带 `--position` 追加到末尾，
  `--position 0` 插到最前，`--position N` 插到第 N 首之后。加歌后
  自动回读校验，结果里 `verified` 标明是否与预期一致。
- `playlist-set-order` 必须给出与歌单现状**同一批曲目**的完整目标
  顺序（含重复计数），内部用 `moveItemsInPlaylist` 逐条移动，完成后
  回读校验；数量或曲目对不上会直接报错、不动手。
- `playlist-remove` 按曲目 uri 删除（同 uri 的多个条目一并删除），
  内部先读现状把 uri 解析成条目 uid 再删，完成后回读校验。
- 写操作之间自动间隔 ≥1 秒；单次运行写调用上限默认 50 次
  （`SPOTIFY_FULL_COOKIE_MAX_WRITES` 可调），超限停下报告。
- **关键实现细节**：Pathfinder 写操作失败时 HTTP 仍可能返回 200，
  失败藏在返回节点的 `__typename` 里。本工具每次写操作都检查
  `__typename` 与 `errors` 数组，失败如实报错，绝不假报成功。

### 获取 sp_dc 的两种方式

**方式一：自带无头浏览器登录（`cookie-login`，可选组件）**

本工具自带一个给 AI 用的无头浏览器登录模块 `browser_login.py`，
走的是 Yousj 浏览器项目的路线：无头优先、经 CDP 驱动一个真实
Chromium（标准库最小 WebSocket 实现，无第三方依赖），profile 固定
放在凭据目录的 `browser-profile/` 下。运行
`python3 spotify.py cookie-login` 后：

- 若该 profile 的浏览器会话里已有有效登录态，直接提取 `sp_dc`
  写入凭据文件（600），全程不打印 cookie 值；
- 若停在账号密码表单、Google 登录、验证器验证码或 CAPTCHA 等
  需要真人介入的步骤，立即停下并结构化报告需要什么
  （`status: needs_user_action` + `reason`），绝不乱试；本模块
  不接收、不保存账号密码。可以先用同一个 profile 开一次有界面
  Chromium 完成登录，之后无头流程即可直接取 cookie；
- 本机没有 Chromium 时本模块不可用（`status` 与代码里的 `probe()`
  会报告），手动填的方式不受影响。

**方式二：手动从浏览器复制**

1. 在浏览器登录 https://open.spotify.com（确认是已登录状态）。
2. 打开开发者工具（F12）→ 应用/Application → Cookie →
   `https://open.spotify.com`，找到名为 **`sp_dc`** 的那一行，
   复制它的值。
3. 把值填入 `~/.config/spotify-full/credentials.json` 的 `sp_dc`
   字段（保持文件权限 600）。**绝不要把 sp_dc 发到聊天、issue 或
   任何日志里**——它等同于完整的浏览器登录态，拿到它的人可以
   完全控制这个 Spotify 账号；怀疑泄露时立刻在 Spotify 官网退出
   全部会话/改密码使其失效。

### 能力对照（官方后端 vs cookie 后端）

| 能力 | 官方 OAuth 后端 | cookie 后端 |
| --- | --- | --- |
| 前提 | 自己的开发者应用 + 所有者 Premium（2026 新规） | 账号的 sp_dc，无需开发者应用、无需 Premium |
| 读歌单内容 | ✓（新应用可能被内容接口限制） | ✓ |
| 加歌/删歌 | ✓（同上） | ✓（含回读校验） |
| 排序 | 区间 reorder | 完整目标顺序重排（playlist-set-order） |
| 建歌单 | ✓ 完整 | △ spclient 通道：能建并返回 URI，但暂未归档进曲库列表、不能设描述/公开私密（见下） |
| 改名/封面/播放/曲库/搜索等 | ✓ | ✗（不在本后端范围，用官方后端） |
| 歌词（LRCLIB） | 与后端无关，两种模式都能用 | 同左 |

### 限制与风险（如实说明）

- **建歌单是尽力而为**：spclient 建出的歌单能拿到 URI 并可直接
  加歌/读取，但归档进曲库 rootlist 需要账号 user id，本后端暂未
  实现解析，所以新歌单暂时不会出现在曲库列表里；返回值里
  `filed_in_library: false` 会如实标注。
- **内部接口会漂移**：持久化查询哈希与 TOTP 密钥版本随网页播放器
  发版轮换。哈希失效时服务端返回 `PersistedQueryNotFound`，本工具
  会明确报出需要更新哈希；TOTP 密钥靠社区仓库自动跟进 + 本地缓存。
- **账号级限流**：Spotify 对异常高频调用有账号级惩罚手段，所以本
  工具强制写间隔与写次数上限；请保持低频、拟人地使用。本后端从不
  调用 `api.spotify.com`。
- **条款性质**：使用未公开的内部接口与官方条款存在张力，个人低频
  自用目前无确证封号案例，但官方也没有任何保证。是否使用由账号
  所有者自行决定。
- **部署状态**：cookie 模式代码已实现并通过离线 mock 测试
  （`tests/test_cookie_client.py`，含 TOTP 已知向量交叉核对、
  `__typename` 错误拦截、完整重排演算），但**尚未用真实 sp_dc
  实测**；首次实测后请把结果补记到连接器文档。

### 新增文件

```text
├── cookie_client.py    cookie 模式后端（TOTP + Pathfinder + spclient 建单）
├── browser_login.py    自带无头浏览器登录模块（可选，Yousj 浏览器路线）
└── tests/
    └── test_cookie_client.py  cookie 模式离线测试（mock，不碰真实账号）
```

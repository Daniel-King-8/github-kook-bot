# GitHub -> KOOK 推送机器人

一个轻量的 GitHub 仓库更新监控机器人：**轮询 GitHub API**，发现仓库有新的 Release 或提交时，通过 KOOK 机器人推送到指定频道。同时支持在频道里 **@机器人 + GitHub 地址** 动态添加订阅，并且只有指定 **KOOK 身份组** 的用户才能操作。

- **轮询模式**：不需要公网 IP / 域名，不需要给仓库配 Webhook，可以监控任意公开仓库
- **频道命令**：`@机器人 https://github.com/owner/repo` 即可订阅
- **权限控制**：按 KOOK 身份组 / 用户 ID 白名单限制操作人
- **随机颜色卡片**：所有推送和命令回复都是 KOOK 卡片消息（type=10），每条随机选一个边框色
- **不刷屏**：首次运行只建立基线；每次最多推送 3 个 Release、5 条提交
- **状态持久化**：`data/state.json` 保存检查位置和动态订阅，容器重启不丢失
- 唯一第三方依赖是 `websockets`（用于接收频道消息），其余全部使用 Python 标准库

## 目录结构

```
GitHub-bot/
├─ main.py               # 主程序（单文件）
├─ requirements.txt      # websockets 依赖
├─ config.toml           # 你的配置（含 Token，已被 .gitignore 忽略）
├─ config.example.toml   # 配置模板
├─ Dockerfile
├─ docker-compose.yml
├─ data/                 # 运行后自动生成，保存 state.json
└─ README.md
```

## 一、准备 KOOK 机器人

1. 打开 [KOOK 开发者后台](https://developer.kookapp.cn/app/index)，登录后创建一个应用。
2. 左侧进入 **机器人**，复制 **Token**。
3. **连接方式必须选择 `WebSocket`**（不能是 Webhook）。
   - 本项目要通过 WebSocket 接收频道里的 `@机器人` 消息。选 Webhook 的话机器人收不到消息，只有定时推送能用。
4. 通过 OAuth2 链接把机器人邀请到你的服务器，确保它在目标频道有 **发送消息** 权限。
5. 获取频道 ID：KOOK 客户端 → 设置 → 其他 → 开启 **开发者模式**，右键目标频道 → **复制 ID**（纯数字）。

### 获取身份组 ID（用于命令权限）

有两种方式：

**方式一（最简单）**：先启动机器人，让拥有目标身份组的用户随便 `@机器人 列表` 一次，然后看日志：

```bash
docker compose logs -f | grep "拒绝无权限命令"
```

日志里会显示 `roles=[1234567890, ...]`，把其中的数字填到 `allowed_roles` 即可。

**方式二**：调用 KOOK 接口获取服务器的身份组列表：

```bash
curl -H "Authorization: Bot 你的BotToken" \
  "https://www.kookapp.cn/api/v3/guild-role/list?guild_id=服务器ID"
```

服务器 ID 同样是开启开发者模式后右键服务器复制。

## 二、准备 GitHub Token（强烈建议）

不配置 Token 时，GitHub API 限额只有 **60 次/小时**，只够监控 1~2 个仓库。

配置方法（二选一）：

- 在 `config.toml` 的 `[bot]` 下写 `github_token = "github_pat_xxx"`
- 或在 `docker-compose.yml` 里设置环境变量 `GITHUB_TOKEN`

权限要求：

- 公开仓库：Token 不需要任何 scope（只读公开信息）
- 私有仓库：需要 `repo` 权限（经典 Token）或 Contents/Metadata 只读权限（Fine-grained Token）

## 三、配置

编辑 `config.toml`：

```toml
[kook]
token = "你的 KOOK Bot Token"
channel_id = "目标频道 ID"

[bot]
interval = 300              # 轮询间隔（秒）
max_commits = 5             # 每次最多展示几条提交
include_prerelease = true   # 是否推送预发布版本
notify_on_first_run = false # 首次运行只建基线，不推送历史
log_level = "INFO"
# github_token = "github_pat_xxx"
# ca_bundle = "/path/to/proxy-ca.pem"   # 代理/自签名证书时需要
# insecure_skip_verify = false          # 最后手段：关闭 TLS 校验（不推荐）

[command]
enabled = true
allowed_roles = ["1234567890"]   # 允许操作的身份组 ID
allowed_users = []               # 可选：额外允许的用户 ID
default_events = ["release", "push"]
channel_ids = []                 # 留空 = 只允许上面的 kook.channel_id

# 仓库全部通过频道命令 @机器人 添加/删除，不需要在这里写 [[repos]]
```

通过频道添加的仓库，默认监控 `default_events` 里的事件：

| 值 | 说明 |
|---|---|
| `release` | 新版本发布（GitHub Releases） |
| `push` | 分支有新提交（默认分支） |

`[command]` 配置项：

| 配置项 | 说明 |
|---|---|
| `enabled` | 是否启用频道命令；`false` 时只做定时推送，不连接 WebSocket |
| `allowed_roles` | 允许操作的身份组 ID 列表；留空则所有人都不能操作 |
| `allowed_users` | 额外允许的用户 ID 列表（不受身份组限制） |
| `default_events` | 通过频道添加的仓库默认监控哪些事件 |
| `channel_ids` | 允许执行命令的频道 ID；留空则只允许 `kook.channel_id` |

## 四、频道命令用法

在配置的频道里 @机器人：

```
@机器人 https://github.com/owner/repo           添加订阅（默认 Release + 提交）
@机器人 删除 owner/repo                          取消订阅（也支持完整地址）
@机器人 列表                                    查看当前监控的所有项目
```

说明：

- 所有命令回复也是卡片消息。
- 只有 `allowed_roles` / `allowed_users` 里的用户能操作，其他人会收到「⛔ 没有权限」卡片。
- 所有仓库统一保存在 `data/state.json`，重启容器不会丢。
- 添加后会在后台立即建立基线，并开始按轮询间隔监控。
- 推送统一发到 `kook.channel_id` 指定的频道。
- `@机器人 列表` 只显示仓库名和监控事件，不显示添加人。
- **所有仓库都可以用 `@机器人 删除 <地址>` 删除**，不再区分配置文件。
- 删除 `data/state.json` 会清空所有订阅。
- 从旧版本升级：`config.toml` 里已有的 `[[repos]]` 会在首次启动时自动迁移为频道订阅，之后可以删除这些配置块。

## 五、Docker 部署

```bash
cd /path/to/GitHub-bot
docker compose up -d --build
docker compose logs -f
```

Windows 下在 `X:\Docker\GitHub-bot` 目录执行同样的命令即可。

常用命令：

```bash
docker compose restart          # 修改 config.toml 后重启生效
docker compose down             # 停止并删除容器
docker compose up -d --build    # 重新构建并启动
```

> 修改 `config.toml` 后必须重启容器才会重新读取配置。
>
> 同一个 Bot Token 只能运行一个 WebSocket 实例，不要重复启动多个容器。

## 六、本地测试（不装 Docker 也能跑）

需要 Python 3.11+：

```bash
pip install -r requirements.txt

# 只检查一轮，仅打印要发送的消息，不真正发送、不连接 WebSocket
python main.py --once --dry-run

# 只检查一轮并真正发送
python main.py --once

# 常驻运行（轮询 + 接收频道命令）
python main.py
```

`--dry-run` 非常适合第一次验证配置和消息格式。

> Windows 下如果日志中文乱码，先执行 `set PYTHONUTF8=1`（cmd）或 `export PYTHONUTF8=1`（Git Bash）。

## 七、消息效果

所有机器人消息都是卡片（CardMessage `type=10`），每条随机选一个边框色。

**Release 卡片**

- 标题：`🎉 owner/repo 发布了新版本！`
- 发布用户、仓库、版本
- 描述：Release 说明前 400 字
- 链接、发布时间
- 按钮：`点击查看GitHub项目仓库` / `点击查看Release详细内容`

**提交卡片**

- 标题：`🎉 owner/repo 有更新内容啦！`
- 提交用户、仓库、仓库分支
- 描述：最新提交信息，最多 400 字
- 本次共 N 个新提交
- 链接、提交时间
- 按钮：`点击查看GitHub项目仓库` / `点击查看Commits详细内容`

**命令回复卡片**

`@机器人 列表`、`已添加订阅`、`已在监控中`、`已取消订阅`、`没有找到该订阅`、`没有权限`、`命令用法`、`添加失败` 全部以卡片形式回复。

### 随机颜色

24 色随机池，每条消息选 1 个，避免连续重复。卡片边框使用精确色值；KOOK 按钮只支持 6 种主题色，所以按钮使用最接近的主题：

| 按钮主题 | 按钮实际颜色 | 边框色 |
|---|---|---|
| `danger` | 红 | `#E74C3C` `#FF6B6B` `#FF4D4F` `#E64980` |
| `warning` | 橙 | `#F39C12` `#FFA94D` `#FF922B` `#FFD43B` |
| `success` | 薄荷绿 | `#2ECC71` `#51CF66` `#38D9A9` `#1ABC9C` |
| `primary` | 草绿 | `#7ACC35` `#6CBF00` `#A9E34B` `#82C91E` |
| `info` | 天蓝 | `#3498DB` `#4DABF7` `#5C7CFA` `#9775FA` |
| `secondary` | 灰 | `#95A5A6` `#ADB5BD` `#CED4DA` `#607D8B` |

> 紫色边框会配天蓝色按钮，属于 KOOK 按钮主题的限制，不是 bug。

## 八、常见问题

**Q：启动后没有收到消息？**

- 首次运行默认只建立基线（`notify_on_first_run = false`），之后有**新**更新才会推送。
- 检查 `docker compose logs -f` 是否有 `已发送卡片消息` 或 KOOK API 报错。
- 确认机器人在目标服务器里，且对该频道有发送消息权限。
- 确认 `channel_id` 是频道 ID（纯数字），不是服务器 ID。

**Q：@机器人 没反应？**

按顺序检查：

1. KOOK 开发者后台里机器人的连接方式是不是 **WebSocket**。
2. `[command] enabled` 是否为 `true`。
3. 命令所在频道是否在 `channel_ids` 或 `kook.channel_id` 允许范围内。
4. 日志里有没有 `KOOK WebSocket 已连接`。
5. 是否真的 @ 到了机器人（不是只输入了名字）。
6. 如果 Bot 同时在多个地方运行，同一个 Token 的 WebSocket 会互相挤下线。

**Q：机器人回复「⛔ 没有权限」？**

查看日志里的 `拒绝无权限命令 ... roles=[...]`，把对应用户的角色 ID 加到 `allowed_roles`，重启容器。

**Q：为什么卡片边框和按钮颜色不完全一样？**

KOOK 卡片的边框支持任意 16 进制色，但按钮只支持 6 种预设主题色。项目会把每个边框色映射到最接近的按钮主题，所以紫色边框会配天蓝色按钮、粉色边框会配红色按钮。

**Q：日志提示 403 / 429？**

GitHub API 限流。配置 `GITHUB_TOKEN`，或增大 `interval`。

**Q：日志提示 404？**

仓库名写错，或者这是私有仓库但没配置有权限的 `GITHUB_TOKEN`。

**Q：日志报 `SSL: CERTIFICATE_VERIFY_FAILED`？**

通常是本机或容器走了抓包/代理（如 Clash、公司网关），TLS 证书不是公共 CA 签发的。按顺序尝试：

1. 给容器传入代理：`HTTPS_PROXY=http://host.docker.internal:7890`
2. 把代理的根证书挂进容器，并配置 `ca_bundle = "/certs/proxy-ca.pem"`
3. 临时关闭校验：`insecure_skip_verify = true` 或环境变量 `INSECURE_SKIP_VERIFY=1`（不推荐长期使用）

**Q：想重新建立基线 / 清空状态？**

停止容器，删除 `data/state.json`，再启动。注意这也会清空通过频道命令添加的订阅。

**Q：如何监控更多仓库？**

直接在频道里 `@机器人 https://github.com/owner/repo` 添加，不需要改配置文件，也不用重启。

**Q：会重复推送吗？**

不会。每个仓库的 Release ID / 分支最新 commit SHA 都记录在 `data/state.json`，只有变化时才推送。

**Q：支持 Issue / PR 吗？**

当前版本只支持 Release 和提交推送，保持轻量。如有需要可以在 `main.py` 中按同样的模式扩展 `issues` / `pulls` 事件。

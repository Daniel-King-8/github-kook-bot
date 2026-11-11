#!/usr/bin/env python3
"""GitHub 仓库更新 -> KOOK 频道推送机器人（轮询 + 频道命令）。

用法:
    python main.py                 # 常驻轮询 + 接收 KOOK 命令
    python main.py --once          # 只检查一轮后退出
    python main.py --dry-run       # 只打印消息，不真正发送，也不连接 WebSocket
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import signal
import ssl
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__version__ = "1.2.0"

GITHUB_API = "https://api.github.com"
KOOK_API_DEFAULT = "https://www.kookapp.cn/api/v3"
USER_AGENT = f"github-kook-bot/{__version__}"
MAX_KOOK_CONTENT = 4500

GITHUB_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/([A-Za-z0-9_.-]{1,100})",
    re.IGNORECASE,
)
RESERVED_OWNERS = {
    "about", "collections", "customer-stories", "dashboard", "enterprise",
    "events", "explore", "features", "join", "login", "marketplace", "new",
    "orgs", "pricing", "readme", "security", "settings", "site", "sponsors",
    "topics", "trending",
}
REMOVE_KEYWORDS = ("删除", "取消", "退订", "移除", "remove", "unsubscribe", "unsub", "del")
LIST_KEYWORDS = ("列表", "清单", "list", "订阅列表")

# 1.2.0：所有消息改为 KOOK 卡片消息（type=10）。
# 卡片边框支持任意 16 进制色；KOOK 按钮只支持预设主题色，
# 所以每个颜色都配一个最接近的按钮主题。
COLOR_PALETTE: tuple[tuple[str, str], ...] = (
    # danger 红
    ("#E74C3C", "danger"),
    ("#FF6B6B", "danger"),
    ("#FF4D4F", "danger"),
    ("#E64980", "danger"),
    # warning 橙黄
    ("#F39C12", "warning"),
    ("#FFA94D", "warning"),
    ("#FF922B", "warning"),
    ("#FFD43B", "warning"),
    # success 绿色（薄荷绿按钮）
    ("#2ECC71", "success"),
    ("#51CF66", "success"),
    ("#38D9A9", "success"),
    ("#1ABC9C", "success"),
    # primary 草绿（KOOK 的 primary 按钮是草绿色）
    ("#7ACC35", "primary"),
    ("#6CBF00", "primary"),
    ("#A9E34B", "primary"),
    ("#82C91E", "primary"),
    # info 蓝色（KOOK 的 info 按钮是天蓝色）
    ("#3498DB", "info"),
    ("#4DABF7", "info"),
    ("#5C7CFA", "info"),
    ("#9775FA", "info"),
    # secondary 灰色
    ("#95A5A6", "secondary"),
    ("#ADB5BD", "secondary"),
    ("#CED4DA", "secondary"),
    ("#607D8B", "secondary"),
)

LOG = logging.getLogger("github-kook-bot")
STOP_EVENT = threading.Event()
SSL_CONTEXT: ssl.SSLContext | None = None

_CHECK_GUARD = threading.Lock()
_CHECKING: set[str] = set()

EXIT_OK = 0
EXIT_ERROR = 1


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class RepoConfig:
    name: str
    events: tuple[str, ...]
    branch: str | None = None


@dataclass
class CommandConfig:
    enabled: bool
    allowed_roles: tuple[str, ...]
    allowed_users: tuple[str, ...]
    channel_ids: tuple[str, ...]
    default_events: tuple[str, ...]


@dataclass
class Config:
    kook_token: str
    kook_channel_id: str
    kook_api: str
    github_token: str | None
    interval: int
    max_commits: int
    include_prerelease: bool
    notify_on_first_run: bool
    dry_run: bool
    ca_bundle: str | None
    insecure_skip_verify: bool
    state_path: Path
    repos: list[RepoConfig]
    command: CommandConfig


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str] | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def find_config_path(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if os.getenv("CONFIG_PATH"):
        candidates.append(Path(os.environ["CONFIG_PATH"]).expanduser())
    here = Path(__file__).resolve().parent
    candidates.extend(
        [
            Path("/app/config.toml"),
            here / "config.toml",
            Path.cwd() / "config.toml",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(
        "找不到配置文件。请创建 config.toml，或用 --config / CONFIG_PATH 指定路径。"
    )


def load_config(args: argparse.Namespace) -> Config:
    path = find_config_path(args.config)
    LOG.info("读取配置: %s", path)
    with path.open("rb") as fp:
        raw = tomllib.load(fp)

    kook = raw.get("kook", {})
    bot = raw.get("bot", {})

    token = (os.getenv("KOOK_TOKEN") or str(kook.get("token") or "")).strip()
    channel_id = (os.getenv("KOOK_CHANNEL_ID") or str(kook.get("channel_id") or "")).strip()
    api_base = (
        os.getenv("KOOK_API_BASE") or str(kook.get("api_base") or KOOK_API_DEFAULT)
    ).strip().rstrip("/")
    github_token = (os.getenv("GITHUB_TOKEN") or str(bot.get("github_token") or "")).strip() or None

    if not token or token.startswith("请填写"):
        raise ValueError("未配置 KOOK Bot Token（kook.token 或环境变量 KOOK_TOKEN）")
    if not channel_id or channel_id.startswith("请填写"):
        raise ValueError("未配置 KOOK 频道 ID（kook.channel_id 或环境变量 KOOK_CHANNEL_ID）")
    if not channel_id.isdigit():
        raise ValueError(f"KOOK 频道 ID 应该是纯数字，当前为: {channel_id}")

    interval = int(os.getenv("POLL_INTERVAL") or bot.get("interval") or 300)
    if interval < 30:
        LOG.warning("轮询间隔 %s 秒过小，已调整为 30 秒", interval)
        interval = 30

    max_commits = int(bot.get("max_commits") or 5)
    max_commits = max(1, min(max_commits, 20))

    include_prerelease = _as_bool(bot.get("include_prerelease"), True)
    notify_on_first_run = _as_bool(bot.get("notify_on_first_run"), False)
    dry_run = bool(args.dry_run) or _as_bool(os.getenv("DRY_RUN"), False)
    ca_bundle = (os.getenv("CA_BUNDLE") or str(bot.get("ca_bundle") or "")).strip() or None
    insecure_skip_verify = _as_bool(
        os.getenv("INSECURE_SKIP_VERIFY"),
        _as_bool(bot.get("insecure_skip_verify"), False),
    )

    level_name = str(os.getenv("LOG_LEVEL") or bot.get("log_level") or "INFO").upper()
    logging.getLogger().setLevel(getattr(logging, level_name, logging.INFO))

    repos_raw = raw.get("repos", [])
    if not isinstance(repos_raw, list) or not repos_raw:
        raise ValueError("config.toml 中至少需要配置一个 [[repos]]")

    repos: list[RepoConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(repos_raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个 [[repos]] 配置格式不正确")
        name = str(item.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
            raise ValueError(f"第 {index} 个仓库名不合法: {name!r}，应形如 owner/repo")
        key = name.lower()
        if key in seen:
            LOG.warning("仓库 %s 重复配置，已跳过", name)
            continue
        seen.add(key)

        events_raw = item.get("events", ["release", "push"])
        if isinstance(events_raw, str):
            events_raw = [events_raw]
        events = [str(e).strip().lower() for e in events_raw]
        invalid = set(events) - {"release", "push"}
        if invalid:
            raise ValueError(
                f"仓库 {name} 包含不支持的事件: {', '.join(sorted(invalid))}（仅支持 release / push）"
            )
        if not events:
            raise ValueError(f"仓库 {name} 的 events 不能为空")

        branch_raw = item.get("branch")
        branch = str(branch_raw).strip() if branch_raw else None
        repos.append(
            RepoConfig(name=name, events=tuple(dict.fromkeys(events)), branch=branch or None)
        )

    # ---- 频道命令配置 ----
    cmd_raw = raw.get("command") or {}
    if not isinstance(cmd_raw, dict):
        raise ValueError("[command] 配置格式不正确")
    cmd_enabled = _as_bool(
        os.getenv("COMMAND_ENABLED"),
        _as_bool(cmd_raw.get("enabled"), bool(cmd_raw)),
    )

    roles_env = _env_list("ALLOWED_ROLES")
    roles_raw = roles_env if roles_env is not None else cmd_raw.get("allowed_roles", [])
    allowed_roles = tuple(str(item).strip() for item in roles_raw if str(item).strip())

    users_env = _env_list("ALLOWED_USERS")
    users_raw = users_env if users_env is not None else cmd_raw.get("allowed_users", [])
    allowed_users = tuple(str(item).strip() for item in users_raw if str(item).strip())

    channels_env = _env_list("COMMAND_CHANNELS")
    channels_raw = channels_env if channels_env is not None else cmd_raw.get("channel_ids", [])
    channel_ids = tuple(str(item).strip() for item in channels_raw if str(item).strip())

    default_events_raw = cmd_raw.get("default_events", ["release", "push"])
    if isinstance(default_events_raw, str):
        default_events_raw = [default_events_raw]
    default_events = [str(e).strip().lower() for e in default_events_raw]
    invalid_default = set(default_events) - {"release", "push"}
    if invalid_default:
        raise ValueError(
            f"[command] default_events 包含不支持的事件: {', '.join(sorted(invalid_default))}"
        )
    if not default_events:
        default_events = ["release", "push"]

    if cmd_enabled and not allowed_roles and not allowed_users:
        LOG.warning(
            "频道命令已启用，但 allowed_roles / allowed_users 为空；"
            "所有 @机器人 命令都会被拒绝（可在日志中查看用户的角色 ID 后再配置）"
        )

    # ---- 状态文件路径 ----
    state_env = os.getenv("STATE_PATH")
    if state_env:
        state_path = Path(state_env).expanduser()
    elif bot.get("state_path"):
        state_path = Path(str(bot["state_path"])).expanduser()
    elif Path("/data").is_dir():
        state_path = Path("/data/state.json")
    else:
        state_path = Path(__file__).resolve().parent / "data" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    return Config(
        kook_token=token,
        kook_channel_id=channel_id,
        kook_api=api_base,
        github_token=github_token,
        interval=interval,
        max_commits=max_commits,
        include_prerelease=include_prerelease,
        notify_on_first_run=notify_on_first_run,
        dry_run=dry_run,
        ca_bundle=ca_bundle,
        insecure_skip_verify=insecure_skip_verify,
        state_path=state_path,
        repos=repos,
        command=CommandConfig(
            enabled=cmd_enabled,
            allowed_roles=allowed_roles,
            allowed_users=allowed_users,
            channel_ids=channel_ids,
            default_events=tuple(default_events),
        ),
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status}: {url}\n{body[:500]}")
        self.status = status
        self.url = url
        self.body = body


def configure_ssl(ca_bundle: str | None, insecure: bool) -> None:
    """按需配置 TLS 证书；默认使用系统证书。"""
    global SSL_CONTEXT
    if insecure:
        LOG.warning("已禁用 TLS 证书校验，仅建议在自签名代理环境中临时使用")
        SSL_CONTEXT = ssl._create_unverified_context()
    elif ca_bundle:
        SSL_CONTEXT = ssl.create_default_context(cafile=ca_bundle)
        LOG.info("使用自定义 CA 证书: %s", ca_bundle)
    else:
        SSL_CONTEXT = None


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: Any = None,
    timeout: int = 20,
    retries: int = 3,
) -> Any:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("User-Agent", USER_AGENT)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if data is not None:
            request.add_header("Content-Type", "application/json; charset=utf-8")

        try:
            open_kwargs: dict[str, Any] = {"timeout": timeout}
            if SSL_CONTEXT is not None:
                open_kwargs["context"] = SSL_CONTEXT
            with urllib.request.urlopen(request, **open_kwargs) as response:
                body = response.read().decode("utf-8", "replace")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = 2 ** attempt
                LOG.warning("HTTP %s，%s 秒后重试 (%s/%s): %s", exc.code, wait, attempt, retries, url)
                time.sleep(wait)
                last_error = exc
                continue
            raise HttpError(exc.code, url, body) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                wait = 2 ** attempt
                LOG.warning("网络错误，%s 秒后重试 (%s/%s): %s", wait, attempt, retries, exc)
                time.sleep(wait)
                continue
            raise RuntimeError(f"请求失败: {url}: {exc}") from exc

    raise RuntimeError(f"请求失败: {url}: {last_error}")


# ---------------------------------------------------------------------------
# GitHub / KOOK 客户端
# ---------------------------------------------------------------------------


class GitHubClient:
    def __init__(self, token: str | None):
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = GITHUB_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return request_json("GET", url, headers=self.headers)

    def repo_info(self, repo: str) -> dict[str, Any]:
        return self.get(f"/repos/{repo}")

    def releases(self, repo: str, per_page: int = 10) -> list[dict[str, Any]]:
        return self.get(f"/repos/{repo}/releases", {"per_page": per_page})

    def commits(self, repo: str, branch: str, per_page: int = 20) -> list[dict[str, Any]]:
        return self.get(f"/repos/{repo}/commits", {"sha": branch, "per_page": per_page})


class KookClient:
    def __init__(self, token: str, api_base: str, dry_run: bool = False):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.dry_run = dry_run
        self._color_lock = threading.Lock()
        self._last_color: str | None = None

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.token}"}

    def me(self) -> dict[str, Any]:
        result = request_json("GET", f"{self.api_base}/user/me", headers=self._auth_headers())
        if not isinstance(result, dict) or result.get("code") != 0:
            raise RuntimeError(f"获取机器人信息失败: {result}")
        return result.get("data") or {}

    def gateway(self) -> str:
        result = request_json(
            "GET",
            f"{self.api_base}/gateway/index?compress=0",
            headers=self._auth_headers(),
        )
        if not isinstance(result, dict) or result.get("code") != 0:
            raise RuntimeError(f"获取 KOOK 网关失败: {result}")
        url = (result.get("data") or {}).get("url")
        if not url:
            raise RuntimeError(f"KOOK 网关返回缺少 url: {result}")
        return str(url)

    def _pick_color(self) -> tuple[str, str]:
        """随机选一个颜色，避免与上一条消息完全相同。"""
        with self._color_lock:
            choices = [item for item in COLOR_PALETTE if item[0] != self._last_color]
            if not choices:
                choices = list(COLOR_PALETTE)
            color_hex, theme = secrets.choice(choices)
            self._last_color = color_hex
            return color_hex, theme

    def send_card(
        self,
        channel_id: str,
        title: str,
        lines: list[str],
        buttons: list[dict[str, str]] | None = None,
        quote: str | None = None,
    ) -> None:
        color_hex, theme = self._pick_color()
        title = clean_text(title, 90)
        content = "\n".join(lines).strip() or "—"
        if len(content) > MAX_KOOK_CONTENT:
            content = content[:MAX_KOOK_CONTENT].rstrip() + "\n…（内容过长已截断）"

        modules: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain-text", "content": title},
            },
            {
                "type": "section",
                "text": {"type": "kmarkdown", "content": content},
            },
        ]

        if buttons:
            modules.append({"type": "divider"})
            for button in buttons:
                url = str(button.get("url") or "")
                text = clean_text(button.get("text"), 30)
                if not url.startswith(("http://", "https://")):
                    continue
                modules.append(
                    {
                        "type": "action-group",
                        "elements": [
                            {
                                "type": "button",
                                "theme": theme,
                                "value": url,
                                "click": "link",
                                "text": {"type": "plain-text", "content": text},
                            }
                        ],
                    }
                )

        card = {
            "type": "card",
            "theme": theme,
            "color": color_hex,
            "size": "lg",
            "modules": modules,
        }
        content_json = json.dumps([card], ensure_ascii=False)

        if self.dry_run:
            LOG.info(
                "[DRY-RUN] 卡片发送到频道 %s（color=%s, theme=%s）:\n%s",
                channel_id,
                color_hex,
                theme,
                json.dumps([card], ensure_ascii=False, indent=2),
            )
            return

        payload: dict[str, Any] = {
            "target_id": channel_id,
            "type": 10,
            "content": content_json,
        }
        if quote:
            payload["quote"] = quote

        result = request_json(
            "POST",
            f"{self.api_base}/message/create",
            headers=self._auth_headers(),
            payload=payload,
        )
        if not isinstance(result, dict) or result.get("code") != 0:
            code = result.get("code") if isinstance(result, dict) else "?"
            message = result.get("message") if isinstance(result, dict) else result
            raise RuntimeError(f"KOOK API 返回错误: code={code}, message={message}")
        LOG.info("已发送卡片消息到 KOOK 频道 %s（color=%s, theme=%s）", channel_id, color_hex, theme)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {"version": 1, "repos": {}, "subscriptions": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not isinstance(loaded.get("repos"), dict):
                raise ValueError("state.json 结构不正确")
            loaded.setdefault("subscriptions", [])
            if not isinstance(loaded["subscriptions"], list):
                loaded["subscriptions"] = []
            self.data = loaded
        except Exception:
            backup = self.path.with_suffix(".json.bak")
            LOG.exception("状态文件损坏，备份到 %s 后重建", backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self.data = {"version": 1, "repos": {}, "subscriptions": []}

    def repo(self, name: str) -> dict[str, Any]:
        with self.lock:
            return self.data["repos"].setdefault(name.lower(), {})

    def subscriptions(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                dict(item)
                for item in self.data.get("subscriptions", [])
                if isinstance(item, dict)
            ]

    def has_subscription(self, repo: str) -> bool:
        with self.lock:
            return any(
                str(item.get("repo", "")).lower() == repo.lower()
                for item in self.data.get("subscriptions", [])
            )

    def add_subscription(
        self,
        repo: str,
        events: list[str],
        branch: str | None,
        added_by: str,
        added_by_name: str,
    ) -> bool:
        with self.lock:
            if self.has_subscription(repo):
                return False
            self.data.setdefault("subscriptions", []).append(
                {
                    "repo": repo,
                    "events": list(events),
                    "branch": branch,
                    "added_by": added_by,
                    "added_by_name": added_by_name,
                    "added_at": utc_now(),
                }
            )
            self._save_locked()
            return True

    def remove_subscription(self, repo: str) -> bool:
        with self.lock:
            subscriptions = self.data.setdefault("subscriptions", [])
            remaining = [
                item
                for item in subscriptions
                if str(item.get("repo", "")).lower() != repo.lower()
            ]
            if len(remaining) == len(subscriptions):
                return False
            subscriptions[:] = remaining
            self._save_locked()
            return True

    def save(self) -> None:
        with self.lock:
            self._save_locked()

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# 消息格式化
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(text: Any, limit: int = 100) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = text.replace("`", "'")
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def events_label(events: Any) -> str:
    if isinstance(events, str):
        events = [events]
    labels = []
    for event in events or []:
        labels.append({"release": "Release", "push": "提交"}.get(str(event), str(event)))
    return "、".join(labels) if labels else "未知"


def clean_block(text: Any, limit: int = 400) -> str:
    """清理多行文本，保留换行，防止破坏卡片 KMarkdown。"""
    text = str(text or "").replace("`", "'").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text or "（无描述）"


def format_time(value: Any) -> str:
    if not value:
        return "未知"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y/%m/%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(value)


def build_release_card(
    repo: str, release: dict[str, Any]
) -> tuple[str, list[str], list[dict[str, str]]]:
    tag = clean_text(release.get("tag_name") or release.get("name") or "新版本", 60)
    author = clean_text((release.get("author") or {}).get("login") or "未知", 40)
    url = release.get("html_url") or f"https://github.com/{repo}/releases"
    body = clean_block(release.get("body"), 400)
    published = format_time(release.get("published_at"))

    title = f"🎉 {repo} 发布了新版本！"
    lines = [
        f"**发布用户：** {author}",
        f"**仓库：** [{repo}](https://github.com/{repo})",
        f"**版本：** `{tag}`",
        "",
        "**描述：**",
        body,
        "",
        f"**链接：** {url}",
        f"**发布时间：** {published}",
    ]
    buttons = [
        {"text": "点击查看GitHub项目仓库", "url": f"https://github.com/{repo}"},
        {"text": "点击查看Release详细内容", "url": url},
    ]
    return title, lines, buttons


def build_commit_card(
    repo: str,
    branch: str,
    commit: dict[str, Any],
    total: int,
    compare_url: str | None,
    rewritten: bool = False,
) -> tuple[str, list[str], list[dict[str, str]]]:
    commit_data = commit.get("commit") or {}
    author = clean_text(
        (commit.get("author") or {}).get("login")
        or (commit_data.get("author") or {}).get("name")
        or "未知",
        40,
    )
    branch = clean_text(branch, 60)
    message = clean_block(commit_data.get("message"), 400)
    url = commit.get("html_url") or compare_url or f"https://github.com/{repo}"
    committed_at = (
        (commit_data.get("committer") or {}).get("date")
        or (commit_data.get("author") or {}).get("date")
    )

    title = f"🎉 {repo} 有更新内容啦！"
    lines = [
        f"**提交用户：** {author}",
        f"**仓库：** [{repo}](https://github.com/{repo})",
        f"**仓库分支：** `{branch}`",
        "",
        "**描述：**",
        message,
    ]
    if total > 1:
        lines.extend(["", f"本次共 **{total}** 个新提交。"])
    if rewritten:
        lines.extend(["", "（检测到历史变化，仅显示最新提交）"])
    lines.extend(
        [
            "",
            f"**链接：** {url}",
            f"**提交时间：** {format_time(committed_at)}",
        ]
    )

    buttons = [
        {"text": "点击查看GitHub项目仓库", "url": f"https://github.com/{repo}"},
        {"text": "点击查看Commits详细内容", "url": compare_url or url},
    ]
    return title, lines, buttons


# ---------------------------------------------------------------------------
# 仓库检查
# ---------------------------------------------------------------------------


def check_releases(
    cfg: Config,
    repo: RepoConfig,
    github: GitHubClient,
    kook: KookClient,
    state: StateStore,
) -> None:
    releases = github.releases(repo.name, per_page=10)
    if not isinstance(releases, list):
        LOG.warning("[%s] Releases 接口返回格式异常", repo.name)
        return

    valid = [
        item
        for item in releases
        if isinstance(item, dict)
        and not item.get("draft")
        and (cfg.include_prerelease or not item.get("prerelease"))
    ]
    if not valid:
        LOG.debug("[%s] 没有可用的 Release", repo.name)
        return

    latest_id = max(int(item.get("id") or 0) for item in valid)
    repo_state = state.repo(repo.name)
    with state.lock:
        last_id = repo_state.get("last_release_id")

    if last_id is None:
        if cfg.notify_on_first_run:
            title, lines, buttons = build_release_card(repo.name, valid[0])
            kook.send_card(cfg.kook_channel_id, title, lines, buttons)
        with state.lock:
            repo_state["last_release_id"] = latest_id
        if not cfg.notify_on_first_run:
            LOG.info("[%s] 首次运行，建立 Release 基线: %s", repo.name, valid[0].get("tag_name"))
        return

    new_releases = [item for item in valid if int(item.get("id") or 0) > int(last_id)]
    if not new_releases:
        return

    new_releases.sort(key=lambda item: int(item.get("id") or 0))
    for release in new_releases[-3:]:
        title, lines, buttons = build_release_card(repo.name, release)
        kook.send_card(cfg.kook_channel_id, title, lines, buttons)
        time.sleep(1)
    if len(new_releases) > 3:
        LOG.warning(
            "[%s] 本次发现 %d 个新 Release，仅推送最新 3 个", repo.name, len(new_releases)
        )

    with state.lock:
        repo_state["last_release_id"] = max(
            int(item.get("id") or 0) for item in new_releases
        )


def check_commits(
    cfg: Config,
    repo: RepoConfig,
    github: GitHubClient,
    kook: KookClient,
    state: StateStore,
) -> None:
    repo_state = state.repo(repo.name)
    with state.lock:
        branch = repo.branch or repo_state.get("default_branch") or "main"
        last_sha = (repo_state.get("last_commit_sha") or {}).get(branch)

    per_page = min(max(cfg.max_commits + 10, 20), 100)
    commits = github.commits(repo.name, branch, per_page=per_page)
    if not isinstance(commits, list) or not commits:
        LOG.debug("[%s] 分支 %s 没有提交记录", repo.name, branch)
        return

    latest_sha = commits[0].get("sha")
    if not latest_sha:
        return

    if last_sha is None:
        if cfg.notify_on_first_run:
            title, lines, buttons = build_commit_card(
                repo.name, branch, commits[0], 1, None
            )
            kook.send_card(cfg.kook_channel_id, title, lines, buttons)
        with state.lock:
            repo_state.setdefault("last_commit_sha", {})[branch] = latest_sha
        if not cfg.notify_on_first_run:
            LOG.info(
                "[%s] 首次运行，建立 %s 分支基线: %s", repo.name, branch, latest_sha[:7]
            )
        return

    if latest_sha == last_sha:
        return

    index = next(
        (i for i, commit in enumerate(commits) if commit.get("sha") == last_sha), None
    )
    if index is None:
        new_commits = commits[:1]
        total = 1
        rewritten = True
    else:
        new_commits = commits[:index]
        total = index
        rewritten = False

    compare_url = f"https://github.com/{repo.name}/compare/{last_sha}...{latest_sha}"
    latest_commit = new_commits[0] if new_commits else commits[0]
    title, lines, buttons = build_commit_card(
        repo.name,
        branch,
        latest_commit,
        total,
        compare_url,
        rewritten,
    )
    kook.send_card(cfg.kook_channel_id, title, lines, buttons)
    with state.lock:
        repo_state.setdefault("last_commit_sha", {})[branch] = latest_sha


def process_repo(
    cfg: Config,
    repo: RepoConfig,
    github: GitHubClient,
    kook: KookClient,
    state: StateStore,
) -> None:
    repo_state = state.repo(repo.name)
    with state.lock:
        default_branch = repo_state.get("default_branch")

    if repo.branch is None or not default_branch:
        info = github.repo_info(repo.name)
        with state.lock:
            repo_state["default_branch"] = info.get("default_branch") or "main"
            if info.get("full_name"):
                repo_state["full_name"] = info["full_name"]

    if "release" in repo.events:
        check_releases(cfg, repo, github, kook, state)
    if "push" in repo.events:
        check_commits(cfg, repo, github, kook, state)

    with state.lock:
        repo_state["checked_at"] = utc_now()
        state.save()


def process_repo_guarded(
    cfg: Config,
    repo: RepoConfig,
    github: GitHubClient,
    kook: KookClient,
    state: StateStore,
) -> None:
    key = repo.name.lower()
    with _CHECK_GUARD:
        if key in _CHECKING:
            LOG.debug("[%s] 已在检查中，跳过重复任务", repo.name)
            return
        _CHECKING.add(key)
    try:
        process_repo(cfg, repo, github, kook, state)
    finally:
        with _CHECK_GUARD:
            _CHECKING.discard(key)


def all_repos(cfg: Config, state: StateStore) -> list[RepoConfig]:
    merged: dict[str, RepoConfig] = {item.name.lower(): item for item in cfg.repos}
    for sub in state.subscriptions():
        name = str(sub.get("repo") or "").strip()
        if "/" not in name:
            continue
        key = name.lower()
        if key in merged:
            continue
        events = sub.get("events") or list(cfg.command.default_events)
        if isinstance(events, str):
            events = [events]
        branch = sub.get("branch") or None
        merged[key] = RepoConfig(
            name=name,
            events=tuple(str(event) for event in events),
            branch=branch,
        )
    return list(merged.values())


# ---------------------------------------------------------------------------
# 频道命令
# ---------------------------------------------------------------------------


def reply_card(
    kook: KookClient,
    channel_id: str,
    title: str,
    lines: list[str],
    buttons: list[dict[str, str]] | None = None,
    quote: Any = None,
) -> None:
    try:
        quote_id = str(quote) if quote else None
        kook.send_card(channel_id, title, lines, buttons=buttons, quote=quote_id)
    except Exception:
        LOG.exception("发送命令回复失败")


def extract_repo(text: str) -> str | None:
    match = GITHUB_URL_RE.search(text)
    if not match:
        return None
    owner, name = match.group(1), match.group(2)
    if owner.lower() in RESERVED_OWNERS:
        return None
    if name.lower().endswith(".git"):
        name = name[:-4]
    name = name.strip(".")
    if not owner or not name:
        return None
    return f"{owner}/{name}"


def cmd_add(
    cfg: Config,
    state: StateStore,
    github: GitHubClient,
    kook: KookClient,
    channel_id: str,
    repo: str,
    author: dict[str, Any],
    msg_id: Any,
) -> None:
    try:
        info = github.repo_info(repo)
    except HttpError as exc:
        if exc.status == 404:
            reply_card(
                kook,
                channel_id,
                "⚠️ 添加失败",
                [
                    f"**仓库：** {repo}",
                    "仓库不存在，或这是私有仓库（需要配置有权限的 `GITHUB_TOKEN`）。",
                ],
                quote=msg_id,
            )
        else:
            reply_card(
                kook,
                channel_id,
                "⚠️ 添加失败",
                [
                    f"**仓库：** {repo}",
                    f"查询 GitHub 仓库失败（HTTP {exc.status}），请稍后再试。",
                ],
                quote=msg_id,
            )
        return
    except Exception:
        LOG.exception("查询仓库失败: %s", repo)
        reply_card(
            kook,
            channel_id,
            "⚠️ 添加失败",
            [f"**仓库：** {repo}", "查询 GitHub 仓库失败，请稍后再试。"],
            quote=msg_id,
        )
        return

    full_name = str(info.get("full_name") or repo)
    repo_button = {
        "text": "点击查看GitHub项目仓库",
        "url": f"https://github.com/{full_name}",
    }
    if any(item.name.lower() == full_name.lower() for item in cfg.repos):
        reply_card(
            kook,
            channel_id,
            "ℹ️ 已在监控中",
            [
                f"**仓库：** [{full_name}](https://github.com/{full_name})",
                "该项目已在 `config.toml` 中监控，无需重复添加。",
            ],
            buttons=[repo_button],
            quote=msg_id,
        )
        return

    author_id = str(author.get("id") or "")
    author_name = str(author.get("username") or author_id)
    events = list(cfg.command.default_events)

    if not state.add_subscription(full_name, events, None, author_id, author_name):
        reply_card(
            kook,
            channel_id,
            "ℹ️ 已在监控中",
            [
                f"**仓库：** [{full_name}](https://github.com/{full_name})",
                "该项目已经订阅过了。",
            ],
            buttons=[repo_button],
            quote=msg_id,
        )
        return

    reply_card(
        kook,
        channel_id,
        "✅ 已添加订阅",
        [
            f"**仓库：** [{full_name}](https://github.com/{full_name})",
            f"**推送内容：** {events_label(events)}",
            f"**通知频道：** `{cfg.kook_channel_id}`",
        ],
        buttons=[repo_button],
        quote=msg_id,
    )
    LOG.info("用户 %s(%s) 添加了订阅: %s", author_name, author_id, full_name)

    # 后台立即建立基线，避免等到下一轮
    repo_cfg = RepoConfig(name=full_name, events=tuple(events), branch=None)
    threading.Thread(
        target=process_repo_guarded,
        args=(cfg, repo_cfg, github, kook, state),
        name=f"check-{full_name}",
        daemon=True,
    ).start()


def cmd_remove(
    cfg: Config,
    state: StateStore,
    kook: KookClient,
    channel_id: str,
    repo: str,
    msg_id: Any,
) -> None:
    if any(item.name.lower() == repo.lower() for item in cfg.repos):
        reply_card(
            kook,
            channel_id,
            "⚠️ 无法通过命令删除",
            [
                f"**仓库：** {repo}",
                "该项目由 `config.toml` 管理，请修改配置文件并重启容器。",
            ],
            quote=msg_id,
        )
        return
    if state.remove_subscription(repo):
        reply_card(
            kook,
            channel_id,
            "🗑️ 已取消订阅",
            [f"**仓库：** {repo}"],
            quote=msg_id,
        )
        LOG.info("取消订阅: %s", repo)
    else:
        reply_card(
            kook,
            channel_id,
            "❓ 没有找到该订阅",
            [
                f"**仓库：** {repo}",
                "请确认地址是否正确，可以用 `@机器人 列表` 查看当前项目。",
            ],
            quote=msg_id,
        )


def cmd_list(
    cfg: Config,
    state: StateStore,
    kook: KookClient,
    channel_id: str,
    msg_id: Any,
) -> None:
    lines: list[str] = []
    if cfg.repos:
        lines.append("**配置文件：**")
        for item in cfg.repos:
            lines.append(f"- {item.name}（{events_label(item.events)}）")

    subscriptions = state.subscriptions()
    if subscriptions:
        if lines:
            lines.append("")
        lines.append("**频道添加：**")
        for sub in subscriptions[:20]:
            name = sub.get("repo") or "?"
            by = sub.get("added_by_name") or sub.get("added_by") or ""
            suffix = f" — {by}" if by else ""
            lines.append(f"- {name}（{events_label(sub.get('events'))}）{suffix}")
        if len(subscriptions) > 20:
            lines.append(f"- …另有 {len(subscriptions) - 20} 个")

    if not lines:
        lines.append("暂无监控项目。")
    reply_card(kook, channel_id, "📋 当前监控的项目", lines, quote=msg_id)


def handle_message_event(
    cfg: Config,
    state: StateStore,
    github: GitHubClient,
    kook: KookClient,
    bot_id: str,
    event: dict[str, Any],
) -> None:
    if not cfg.command.enabled or not isinstance(event, dict):
        return

    author_id = str(event.get("author_id") or "")
    if author_id == bot_id:
        return
    if event.get("from_type") == 2:
        return

    extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
    target_id = str(event.get("target_id") or "")
    allowed_channels = cfg.command.channel_ids or (cfg.kook_channel_id,)
    if target_id not in allowed_channels:
        return

    content = str(event.get("content") or "")
    mentions = {str(item) for item in (extra.get("mention") or [])}
    if bot_id not in mentions and f"(met){bot_id}(met)" not in content:
        return

    author = extra.get("author") if isinstance(extra.get("author"), dict) else {}
    if not author_id:
        author_id = str(author.get("id") or "")
    roles = {str(item) for item in (author.get("roles") or [])}
    permitted = bool(roles & set(cfg.command.allowed_roles))
    if author_id in cfg.command.allowed_users:
        permitted = True

    if not permitted:
        LOG.warning(
            "拒绝无权限命令: user=%s(%s) roles=%s content=%s",
            author_id,
            author.get("username"),
            sorted(roles),
            content[:200],
        )
        reply_card(
            kook,
            target_id,
            "⛔ 没有权限",
            [
                "只有指定身份组的用户才能执行该操作。",
                "如需权限，请联系管理员。",
            ],
            quote=event.get("msg_id"),
        )
        return

    text = re.sub(r"\(met\)\d+\(met\)", " ", content)
    text = re.sub(r"\s+", " ", text).strip()
    lower = text.lower()
    repo = extract_repo(text)
    wants_remove = any(keyword in lower for keyword in REMOVE_KEYWORDS)
    wants_list = any(keyword in lower for keyword in LIST_KEYWORDS)

    if repo and wants_remove:
        cmd_remove(cfg, state, kook, target_id, repo, event.get("msg_id"))
    elif repo:
        cmd_add(
            cfg,
            state,
            github,
            kook,
            target_id,
            repo,
            author,
            event.get("msg_id"),
        )
    elif wants_list:
        cmd_list(cfg, state, kook, target_id, event.get("msg_id"))
    else:
        reply_card(
            kook,
            target_id,
            "📖 命令用法",
            [
                "**添加订阅：** `@机器人 https://github.com/owner/repo`",
                "**取消订阅：** `@机器人 删除 https://github.com/owner/repo`",
                "**查看列表：** `@机器人 列表`",
            ],
            quote=event.get("msg_id"),
        )


# ---------------------------------------------------------------------------
# KOOK WebSocket
# ---------------------------------------------------------------------------


async def _sleep_or_stop(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not STOP_EVENT.is_set():
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(remaining, 0.5))


async def _ws_session(
    cfg: Config,
    state: StateStore,
    github: GitHubClient,
    kook: KookClient,
    bot_id: str,
) -> None:
    import websockets

    gateway = await asyncio.to_thread(kook.gateway)
    connect_kwargs: dict[str, Any] = {
        "ping_interval": None,
        "open_timeout": 15,
        "close_timeout": 5,
    }
    if SSL_CONTEXT is not None:
        connect_kwargs["ssl"] = SSL_CONTEXT

    async with websockets.connect(gateway, **connect_kwargs) as ws:
        hello_raw = await asyncio.wait_for(ws.recv(), timeout=15)
        try:
            hello = json.loads(hello_raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"KOOK 网关握手数据无法解析: {hello_raw[:200]}") from exc

        if hello.get("s") != 1 or (hello.get("d") or {}).get("code") != 0:
            raise RuntimeError(f"KOOK 网关握手失败: {str(hello)[:300]}")

        LOG.info("KOOK WebSocket 已连接，开始接收频道命令")
        last_sn = 0
        last_ping = time.monotonic()
        ping_sent_at: float | None = None

        while not STOP_EVENT.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                now = time.monotonic()
                if ping_sent_at is not None and now - ping_sent_at > 6:
                    raise RuntimeError("KOOK 心跳超时（6 秒内未收到 pong）")
                if ping_sent_at is None and now - last_ping >= 25:
                    await ws.send(json.dumps({"s": 2, "sn": last_sn}))
                    last_ping = now
                    ping_sent_at = now
                continue

            try:
                packet = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(packet, dict):
                continue

            sn = packet.get("sn")
            if isinstance(sn, int) and sn > last_sn:
                last_sn = sn

            signal_type = packet.get("s")
            if signal_type == 0:
                event = packet.get("d")
                if isinstance(event, dict):
                    await asyncio.to_thread(
                        handle_message_event,
                        cfg,
                        state,
                        github,
                        kook,
                        bot_id,
                        event,
                    )
            elif signal_type == 3:
                ping_sent_at = None
            elif signal_type == 5:
                LOG.warning("KOOK 要求重连")
                break
            elif signal_type == 6:
                LOG.info("KOOK 要求恢复会话，将重新连接")
                break

        LOG.info("KOOK WebSocket 连接已断开")


async def ws_loop(
    cfg: Config,
    state: StateStore,
    github: GitHubClient,
    kook: KookClient,
) -> None:
    me: dict[str, Any] | None = None
    while not STOP_EVENT.is_set() and me is None:
        try:
            me = await asyncio.to_thread(kook.me)
        except Exception:
            LOG.exception("获取 KOOK 机器人信息失败，10 秒后重试")
            await _sleep_or_stop(10)

    if not me:
        return

    bot_id = str(me.get("id") or "")
    if not bot_id:
        LOG.error("KOOK 返回的机器人信息缺少 id，频道命令功能不可用")
        return
    LOG.info(
        "KOOK 机器人已登录: %s#%s (id=%s)",
        me.get("username"),
        me.get("identify_num"),
        bot_id,
    )

    delay = 5
    while not STOP_EVENT.is_set():
        try:
            await _ws_session(cfg, state, github, kook, bot_id)
            delay = 5
            await _sleep_or_stop(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("KOOK WebSocket 异常，%d 秒后重连", delay)
            await _sleep_or_stop(delay)
            delay = min(delay * 2, 60)


# ---------------------------------------------------------------------------
# 轮询与入口
# ---------------------------------------------------------------------------


def polling_loop(
    cfg: Config,
    state: StateStore,
    github: GitHubClient,
    kook: KookClient,
    once: bool = False,
) -> None:
    while not STOP_EVENT.is_set():
        started = time.monotonic()
        repos = all_repos(cfg, state)
        LOG.debug("本轮检查 %d 个仓库", len(repos))

        for repo in repos:
            if STOP_EVENT.is_set():
                break
            try:
                process_repo_guarded(cfg, repo, github, kook, state)
            except HttpError as exc:
                if exc.status == 404:
                    LOG.error(
                        "[%s] 仓库不存在或无权限（私有仓库需要配置 GITHUB_TOKEN）",
                        repo.name,
                    )
                elif exc.status in (403, 429):
                    LOG.error(
                        "[%s] GitHub API 被限流或拒绝，请配置/检查 GITHUB_TOKEN", repo.name
                    )
                else:
                    LOG.error("[%s] GitHub 请求失败: %s", repo.name, exc)
            except RuntimeError as exc:
                LOG.error("[%s] 检查失败: %s", repo.name, exc)
            except Exception:
                LOG.exception("[%s] 检查失败", repo.name)
            STOP_EVENT.wait(0.3)

        state.save()
        if once:
            break

        elapsed = time.monotonic() - started
        wait_seconds = max(5.0, cfg.interval - elapsed)
        LOG.debug("本轮耗时 %.1f 秒，%.1f 秒后开始下一轮", elapsed, wait_seconds)
        STOP_EVENT.wait(wait_seconds)


def run(cfg: Config, once: bool = False) -> int:
    configure_ssl(cfg.ca_bundle, cfg.insecure_skip_verify)
    github = GitHubClient(cfg.github_token)
    kook = KookClient(cfg.kook_token, cfg.kook_api, cfg.dry_run)
    state = StateStore(cfg.state_path)

    LOG.info("启动 GitHub -> KOOK 推送机器人 v%s", __version__)
    LOG.info(
        "监控 %d 个仓库 | 轮询间隔 %d 秒 | 状态文件 %s",
        len(cfg.repos),
        cfg.interval,
        cfg.state_path,
    )
    if cfg.dry_run:
        LOG.info("当前为 DRY-RUN 模式，不会真正发送消息，也不会连接 WebSocket")
    if not cfg.github_token:
        LOG.warning(
            "未配置 GITHUB_TOKEN，GitHub API 限额仅 60 次/小时；监控多个仓库时请务必配置"
        )

    if once or cfg.dry_run or not cfg.command.enabled:
        if not cfg.command.enabled and not once:
            LOG.info("频道命令未启用，仅运行轮询")
        polling_loop(cfg, state, github, kook, once=once)
        state.save()
        LOG.info("机器人已退出")
        return EXIT_OK

    try:
        import websockets  # noqa: F401
    except ImportError:
        LOG.error(
            "未安装 websockets，无法接收频道命令。请执行 pip install -r requirements.txt"
        )
        return EXIT_ERROR

    poll_thread = threading.Thread(
        target=polling_loop,
        args=(cfg, state, github, kook, False),
        name="kook-poller",
        daemon=True,
    )
    poll_thread.start()

    try:
        asyncio.run(ws_loop(cfg, state, github, kook))
    except KeyboardInterrupt:
        LOG.info("收到中断信号")
    finally:
        STOP_EVENT.set()
        poll_thread.join(timeout=15)
        state.save()

    LOG.info("机器人已退出")
    return EXIT_OK


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GitHub 仓库更新推送到 KOOK 频道（轮询 + 频道命令）"
    )
    parser.add_argument("--config", help="配置文件路径（默认 ./config.toml）")
    parser.add_argument("--once", action="store_true", help="只检查一轮后退出")
    parser.add_argument("--dry-run", action="store_true", help="只打印消息，不真正发送")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()

    try:
        cfg = load_config(args)
    except Exception as exc:
        LOG.error("配置加载失败: %s", exc)
        return EXIT_ERROR

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        return run(cfg, once=args.once)
    except Exception:
        LOG.exception("运行异常退出")
        return EXIT_ERROR


def _handle_signal(signum: int, _frame: Any) -> None:
    LOG.info("收到信号 %s，准备退出…", signum)
    STOP_EVENT.set()


if __name__ == "__main__":
    sys.exit(main())

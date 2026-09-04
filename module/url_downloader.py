"""Interactive URL/media downloader for the bot (yt-dlp -> rclone -> NAS).

Flow (no "please type 下载" confirmation step):
- t.me link / forwarded media / video link:
    bot first shows a folder-card picker (远程下载 is the root; every folder
    is one card). Pick a card to walk into sub-folders, press 存到这里 to
    confirm, or 新建文件夹并存入 to create and save into a new folder.
- after the folder is chosen:
    tg media -> file-name prompt (reply a custom name or use the original
    name; 30s no reply -> original name, auto start);
    other http(s) links -> quality buttons; pressing one starts the download.
- when the NAS already contains a file with the same name, the bot asks the
  user up-front (before downloading, whenever the final name is predictable):
  自动改名 (keep both) / 覆盖保存 / 取消保存. No answer within 60s ->
  自动改名 (default). Names that cannot be predicted are asked again after
  the download finishes.

The user may also reply a full path anytime, e.g. "电影/流浪地球"
(folders under the NAS 远程下载 share are created automatically). Folder
management in chat: 新建文件夹 / 查看文件夹 / 重命名文件夹 / 删除文件夹(空).

Temp files are always removed after success/failure/cancel, so the server
never keeps any downloaded media.
"""

import asyncio
import os
import random
import re
import shutil
import subprocess
import time

from loguru import logger
import pyrogram
from pyrogram import filters
from pyrogram import types
from pyrogram.errors import FloodWait
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from module.pyrogram_extension import parse_link, retry

# pylint: disable = R0902, R0912, R0913, R0914, R0915, C0301

try:
    from yt_dlp import YoutubeDL
except ImportError:  # pragma: no cover
    YoutubeDL = None

_CB = "urldl"
_URL_RE = re.compile(r"https?://[^\s]+")
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f`]')
_ACCEPT_WORDS = {"下载", "开始", "要", "好", "可以", "确认", "yes", "y", "ok", "1"}
_CANCEL_WORDS = {"取消", "不要", "不", "no", "cancel", "0", "/cancel"}
_DEFAULT_WORDS = {"原名", "原文件名", "原名下载", "用原名", "不修改", "默认"}
_OVERWRITE_WORDS = {
    "覆盖", "覆盖保存", "覆盖原文件", "替换", "替换保存", "overwrite", "1",
}
_RENAME_WORDS = {
    "自动改名", "自动改名保存", "改名", "改名保存", "保留两份",
    "保留旧文件", "新名保存", "rename", "2",
}

_QUALITY_EXPIRE_SECS = 15 * 60
_MEDIA_COLLECT_SECS = 2.2
_FOLDER_NAME_WAIT_SECS = 5 * 60
_CONFLICT_WAIT_SECS = 60
# Telegram 对群聊的限流约 20 条消息/分钟（编辑消息同样计入）。
# 固定 4 秒间隔 ≈ 15 条/分钟，给阶段提示/兜底补发留出余量。
_REPORT_MIN_INTERVAL = 4.0
_PICKER_MAX_FOLDERS = 30
_RCLONE_PCT_RE = re.compile(r"Transferred:.*?,\s*(\d{1,3})%")


class _ConflictCancelled(Exception):
    """User chose to abort saving because the NAS file already exists."""


class _TaskCancelled(Exception):
    """A user cancel request aborted the running download task."""

_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v"}
_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}
_ALL_EXTS = _VIDEO_EXTS | _AUDIO_EXTS | _IMAGE_EXTS
_MIME_EXTS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
}


_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_len: int = 180) -> str:
    """Clean a user supplied file name."""
    name = _ILLEGAL_CHARS.sub("_", str(name))
    name = re.sub(r"\s+", " ", name).strip(" .")
    # Windows/SMB 保留设备名（CON、NUL、COM1…）在 NAS 上会出现打不开/同步失败，
    # 统一加下划线前缀
    head = name.rsplit(".", 1)[0] if "." in name else name
    if head.upper() in _WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > max_len:
        # 截断时保留扩展名，避免 NAS 上出现无扩展名文件
        root, ext = os.path.splitext(name)
        keep = max_len - len(ext)
        if keep > 0:
            name = root[:keep].rstrip(" .") + ext
        else:
            name = name[:max_len].rstrip(" .")
    return name or "download"


def _strip_ext(name: str) -> str:
    """Remove a trailing media extension if the user typed one."""
    name = str(name).strip()
    lower = name.lower()
    for ext in sorted(_ALL_EXTS, key=len, reverse=True):
        if lower.endswith(ext):
            return name[: -len(ext)].rstrip(" .")
    return name


def _format_size(size: float) -> str:
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


def _format_eta(sec) -> str:
    sec = int(sec or 0)
    if sec <= 0:
        return "--:--"
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _format_speed(speed: float) -> str:
    return _format_size(speed) + "/s"


def _progress_bar(pct: float, width: int = 12) -> str:
    """A 12-block textual progress bar like ██████░░░░░░."""
    pct = max(0.0, min(100.0, float(pct or 0.0)))
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def _pick(url: str) -> str:
    """Find the first http(s) url inside a message."""
    m = _URL_RE.search(url or "")
    return m.group(0) if m else ""


def _height_options(formats) -> list:
    heights = sorted(
        {int(f.get("height")) for f in formats if f.get("height")},
        reverse=True,
    )
    return heights[:6]


def _format_selector(choice) -> str:
    """Map a quality choice to a yt-dlp format selector."""
    if choice == "audio":
        return "ba/b"
    if choice == "best":
        return "bv*+ba/b"
    height = int(choice)
    return f"bv*[height<={height}]+ba/b[height<={height}]"


def _quality_label(choice) -> str:
    return {
        "best": "原画（最佳）",
        "audio": "仅音频 mp3",
    }.get(choice, choice + "p")


def _fmt_size_of(fmt) -> float:
    raw = fmt.get("filesize") or fmt.get("filesize_approx") or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _estimate_choice_size(formats, choice) -> str:
    """Best-effort file size estimate for a quality choice ('' when unknown)."""
    video_only, audio_only, muxed = [], [], []
    for fmt in formats:
        vcodec = fmt.get("vcodec") or "none"
        acodec = fmt.get("acodec") or "none"
        try:
            height = int(fmt.get("height") or 0)
        except (TypeError, ValueError):
            height = 0
        size = _fmt_size_of(fmt)
        if size <= 0:
            continue
        if vcodec != "none" and acodec != "none":
            muxed.append((size, height))
        elif vcodec != "none":
            video_only.append((size, height))
        elif acodec != "none":
            audio_only.append((size, height))

    best_video = max((s for s, _ in video_only), default=0.0)
    best_audio = max((s for s, _ in audio_only), default=0.0)
    best_muxed = max((s for s, _ in muxed), default=0.0)

    if choice == "audio":
        size = best_audio or best_muxed or best_video
        return _format_size(size) if size else ""

    def _fit_height(limit):
        if limit:
            rows = [s for s, h in video_only if 0 < h <= limit] or [
                s for s, h in muxed if 0 < h <= limit
            ]
            if rows:
                return max(rows)
        return None

    if choice == "best":
        size = (
            max(best_muxed, best_video + best_audio, best_video, best_audio)
            if (best_muxed or best_video or best_audio)
            else 0.0
        )
    else:
        try:
            limit = int(choice)
        except (TypeError, ValueError):
            limit = 0
        fitted = _fit_height(limit)
        size = (fitted or 0.0) + best_audio if fitted else 0.0
    return _format_size(size) if size else ""


def _clean_rel_path(raw) -> str:
    """Normalize a relative folder path (safe, no traversal, no drive/abs)."""
    parts = []
    for seg in str(raw or "").replace("\\", "/").split("/"):
        seg = _ILLEGAL_CHARS.sub("_", seg).strip(" .")
        if not seg or seg in {".", ".."}:
            continue
        parts.append(seg)
    rel = "/".join(parts)
    return rel[:200].rstrip("/")


def _split_dest(raw):
    """Split a user reply into (folder_rel, file_name or None).

    - "名字"            -> ("", "名字")
    - "文件夹/名字"      -> ("文件夹", "名字")   (文件夹自动新建)
    - "a/b/名字"        -> ("a/b", "名字")
    - "文件夹/"         -> ("文件夹", None)      -> use the original name
    """
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return "", None
    if text.endswith("/"):
        return _clean_rel_path(text), None
    if "/" in text:
        folder_raw, name_raw = text.rsplit("/", 1)
    else:
        folder_raw, name_raw = "", text
    name = sanitize_filename(_strip_ext(name_raw)) if name_raw.strip() else None
    return _clean_rel_path(folder_raw), name


_FOLDER_CMDS = {
    "新建文件夹": "create",
    "/新建文件夹": "create",
    "/mkdir": "create",
    "查看文件夹": "list",
    "/查看文件夹": "list",
    "/目录": "list",
    "/文件夹列表": "list",
    "/listdir": "list",
    "重命名文件夹": "rename",
    "/重命名文件夹": "rename",
    "/renamefolder": "rename",
    "删除文件夹": "delete",
    "/删除文件夹": "delete",
    "/rmdir": "delete",
    "帮助": "help",
    "/帮助": "help",
}

_FOLDER_HELP = (
    "📁 文件夹管理（直接发文字即可，不用打斜杠也行）：\n"
    "新建文件夹 综艺          → 在 远程下载 里新建（支持 a/b 多级）\n"
    "查看文件夹               → 列出 远程下载 下的文件夹\n"
    "查看文件夹 综艺          → 查看某个文件夹里的子文件夹\n"
    "重命名文件夹 旧名 新名    → 改文件夹名\n"
    "删除文件夹 名字          → 删除空文件夹\n\n"
    "⬇️ 下载时指定保存位置：\n"
    "发链接/转发视频后，机器人会弹出「文件夹卡片」（一张卡片 = 一个文件夹）：\n"
    "· 点卡片逐层进入，点「📥 就存到这里」确认\n"
    "· 点「➕ 新建文件夹并存入」可自定义新建（再输入名字即可）\n"
    "· 根目录就是 远程下载，也可直接回复：\n"
    "  文件名 → 存到 远程下载 根目录\n"
    "  文件夹/文件名 → 自动新建文件夹再存（例：电影/流浪地球）\n"
    "  文件夹/ → 只指定文件夹，用原文件名\n\n"
    "同名处理：目标文件夹里已有同名文件时，会让你自己选：\n"
    "自动改名（保留两份）/ 覆盖保存 / 取消保存。\n"
    "· 能提前确定文件名 → 下载前就问（不浪费流量）；\n"
    "· 60 秒不选 → 自动改名；也可以直接回复：覆盖 / 自动改名 / 取消保存"
)


class UrlDownloader:
    """Download links / forwarded media to the NAS through the bot."""

    def __init__(self):
        self.app = None
        self.bot = None
        self.client = None
        self.cfg = {}
        self.allowed_user_ids = []
        self.tmp_root = "/app/temp/url_dl"
        self.rclone_bin = os.environ.get("RCLONE", "/app/rclone/rclone")
        self.sessions = {}
        self.pending_name = {}
        self.pending_quality = {}
        self.pending_custom = {}
        self.pending_folder = {}
        self.pending_select = {}
        self.pending_conflict = {}
        self.timers = {}
        self.run_tasks = {}
        self.cancel_events = {}
        self.cancel_watch_tasks = {}
        self.cancel_results = {}
        self.progress_cache = {}
        self.flood_until = {}
        self.status_edit_locks = {}
        self.user_locks = {}
        self.deadlines = {}
        self.sem = None
        self._busy_ts = {}
        self.janitor_task = None

    # ------------------------------------------------------------------ setup

    def register(self, app, bot, client, cfg, allowed_user_ids):
        """Register handlers on the running bot client."""
        self.app = app
        self.bot = bot
        self.client = client
        self.cfg = cfg or {}
        self.allowed_user_ids = allowed_user_ids or []
        self.tmp_root = self.cfg.get("temp_dir") or self.tmp_root
        # 重新注册（主程序 stop/start 循环复用单例）时清空上一个运行期的状态，
        # 避免残留会话让用户一直看到“还有一个任务在进行中”
        if self.janitor_task is not None:
            self.janitor_task.cancel()
        self._reset_runtime_state()
        self.sem = asyncio.Semaphore(1)
        os.makedirs(self.tmp_root, exist_ok=True)
        for entry in os.listdir(self.tmp_root):
            path = os.path.join(self.tmp_root, entry)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass

        users = filters.user(self.allowed_user_ids)
        bot.add_handler(
            MessageHandler(
                self.on_text_message,
                filters.text & users,
            )
        )
        bot.add_handler(
            MessageHandler(
                self.on_media_message,
                filters.media & users,
            )
        )
        # Group -1: run before the legacy callback handler of bot.py, otherwise
        # pyrogram dispatches callback queries only to the first matching
        # handler and our inline buttons would never be answered.
        # Non-"urldl" queries are forwarded with ContinuePropagation.
        bot.add_handler(
            CallbackQueryHandler(self.on_callback, filters=users),
            group=-1,
        )
        self.janitor_task = self.app.loop.create_task(self._janitor_loop())
        logger.info("url_downloader handlers registered")

    # ------------------------------------------------------------- dispatcher

    def _find_user_session(self, user_id):
        for session in self.sessions.values():
            if session.get("user_id") == user_id:
                return session
        return None

    def _busy_notice(self, user_id) -> bool:
        """Rate-limit repeated "you already have a task" messages."""
        now = time.monotonic()
        if now - self._busy_ts.get(user_id, 0.0) < 5:
            return True
        self._busy_ts[user_id] = now
        return False

    def _reset_runtime_state(self):
        """Drop every per-run session/prompt/timer (idempotent re-register)."""
        for task in self.timers.values():
            if task and not task.done():
                task.cancel()
        self.timers.clear()
        for task in self.run_tasks.values():
            if task and not task.done():
                task.cancel()
        self.run_tasks.clear()
        self.sessions.clear()
        self.pending_name.clear()
        self.pending_quality.clear()
        self.pending_custom.clear()
        self.pending_folder.clear()
        self.pending_select.clear()
        self.pending_conflict.clear()
        self.cancel_events.clear()
        for task in self.cancel_watch_tasks.values():
            if task and not task.done():
                task.cancel()
        self.cancel_watch_tasks.clear()
        self.cancel_results.clear()
        self.progress_cache.clear()
        self.flood_until.clear()
        self.status_edit_locks.clear()
        self.deadlines.clear()
        self.user_locks.clear()
        self._busy_ts.clear()

    def _user_lock(self, user_id) -> asyncio.Lock:
        lock = self.user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self.user_locks[user_id] = lock
        return lock

    def _status_edit_lock(self, token) -> asyncio.Lock:
        """Serialize edits to a task's single visible status card."""
        lock = self.status_edit_locks.get(token)
        if lock is None:
            lock = asyncio.Lock()
            self.status_edit_locks[token] = lock
        return lock

    def _flood_left(self, chat_id) -> float:
        """Remaining Telegram flood-wait seconds for this chat (0 = free)."""
        left = self.flood_until.get(chat_id, 0.0) - time.monotonic()
        return max(left, 0.0)

    def _note_flood(self, chat_id, wait_seconds) -> None:
        """Record a FloodWait so edits to this chat pause until it expires."""
        wait = min(max(float(wait_seconds or 0.0), 0.0) + 1.0, 120.0)
        self.flood_until[chat_id] = time.monotonic() + wait
        logger.warning(
            "urldl chat {} flood-waited: status updates paused {:.0f}s",
            chat_id,
            wait,
        )

    def _wait_deadline(self, token, seconds, wait_kind):
        """Remember when a prompt must resolve (backup for the timer tasks)."""
        session = self.sessions.get(token)
        if session is None:
            return
        session["wait_kind"] = wait_kind
        self.deadlines[token] = time.monotonic() + max(1, seconds)

    async def _janitor_loop(self):
        """Safety net: resolve prompts even if a timer task was lost."""
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return
            try:
                now = time.monotonic()
                for token, due in list(self.deadlines.items()):
                    if now < due:
                        continue
                    self.deadlines.pop(token, None)
                    session = self.sessions.get(token)
                    if not session or session.get("started"):
                        continue
                    if session.get("conflict_fut") is not None:
                        continue
                    wait_kind = session.get("wait_kind") or "auto"
                    logger.warning(
                        f"urldl janitor resolves stale wait {token} "
                        f"kind={wait_kind}"
                    )
                    if wait_kind == "auto":
                        # 不再自动下载：只清理停滞会话，等待用户重新发送
                        await self._cancel_session(
                            token, "等待超时，任务已取消，需要时请重新发送"
                        )
                    elif wait_kind == "newname":
                        if session.get("expect_new"):
                            await self._cancel_session(
                                token,
                                "已取消（等待输入新文件夹名超时），请重新发送链接",
                            )
                    else:
                        await self._cancel_session(
                            token, "已取消（等待选择超时），请重新发送链接"
                        )
            except Exception as exc:  # noqa: BLE001
                logger.exception("janitor error: {}", exc)

    # -------------------------------------------------------- rclone helpers

    def _cfg_or_none(self):
        remote = self.cfg.get("rclone_remote", "")
        directory = self.cfg.get("rclone_dir", "")
        if not remote:
            raise RuntimeError("rclone_remote 未配置")
        return remote, directory

    def _remote_dir(self, rel: str = "") -> str:
        remote, directory = self._cfg_or_none()
        base = f"{remote}:{directory}".rstrip("/")
        return f"{base}/{rel}" if rel else base

    async def _rclone_run(self, args, timeout=60):
        cmd = [self.rclone_bin] + list(args)
        config_path = self.cfg.get("rclone_config", "")
        if config_path:
            cmd += ["--config", config_path]
        cmd += ["--log-level", "ERROR"]
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result

    def _nas_display(self, rel: str = "", name: str = "") -> str:
        """Windows-style path shown to the user, e.g. \\\\192.168.5.3\\远程下载\\a\\b.mp4"""
        base = self.cfg.get("nas_unc", "") or "NAS"
        parts = [base.rstrip("\\")]
        if rel:
            parts.append(rel.replace("/", "\\"))
        if name:
            parts.append(name)
        return "\\".join(parts)

    async def _list_dirs(self, rel: str = "", timeout: int = 30):
        """Direct child folders of NAS 远程下载[rel]; None means listing failed."""
        try:
            res = await self._rclone_run(
                ["lsf", self._remote_dir(rel), "--dirs-only"],
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"rclone lsf timeout rel={rel!r}")
            return None
        if res.returncode != 0:
            return None
        return sorted(
            {
                line.rstrip("/")
                for line in (res.stdout or "").splitlines()
                if line.strip() and line.strip() not in {".", ".."}
            }
        )

    async def _list_files(self, rel: str = "", timeout: int = 30):
        """Direct child file names of NAS 远程下载[rel]; None means listing failed."""
        try:
            res = await self._rclone_run(
                ["lsf", self._remote_dir(rel), "--files-only"],
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"rclone lsf files timeout rel={rel!r}")
            return None
        if res.returncode != 0:
            return None
        return {
            line.strip()
            for line in (res.stdout or "").splitlines()
            if line.strip()
        }

    async def _nas_file_exists(self, rel: str, name: str) -> bool:
        """True when a file named `name` already exists under rel (case-insensitive)."""
        names = await self._list_files(rel)
        if names is None:
            return False
        target = name.casefold()
        return any(n.casefold() == target for n in names)

    async def _unique_nas_name(self, rel: str, name: str) -> str:
        """Find a free NAS file name: name -> name_1.ext -> name_2.ext ..."""
        names = await self._list_files(rel)
        existing = {n.casefold() for n in names} if names is not None else set()
        root, ext = os.path.splitext(name)
        candidate = name
        idx = 1
        while candidate.casefold() in existing:
            candidate = f"{root}_{idx}{ext}"
            idx += 1
        return candidate

    def _expected_ext(self, session) -> str:
        """Predict the extension of the final NAS file ('' when unsure)."""
        if session.get("kind") == "yt":
            # yt-dlp always merges into .mp4, or extracts .mp3 for audio.
            return ".mp3" if session.get("quality") == "audio" else ".mp4"
        tg_msg = session.get("tg_msg")
        media = None
        if tg_msg is not None and getattr(tg_msg, "media", None):
            media = getattr(tg_msg, tg_msg.media.value, None)
        candidates = [session.get("title") or ""]
        if media is not None:
            candidates.append(getattr(media, "file_name", "") or "")
            candidates.append(getattr(media, "mime_type", "") or "")
        for raw in candidates:
            ext = os.path.splitext(str(raw or ""))[1].lower()
            if ext in _ALL_EXTS:
                return ext
        if media is not None:
            mime = str(getattr(media, "mime_type", "") or "").lower()
            if mime in _MIME_EXTS:
                return _MIME_EXTS[mime]
        if tg_msg is not None and getattr(tg_msg, "media", None):
            value = tg_msg.media.value
            if value == "photo":
                return ".jpg"
            if value in ("video_note", "animation"):
                return ".mp4"
        return ""

    def _expected_name(self, session) -> str:
        """Predict the final NAS file name before downloading ('' when unsure)."""
        ext = self._expected_ext(session)
        if not ext:
            return ""
        title = session.get("title") or "download"
        final_name = session.get("final_name") or _strip_ext(
            sanitize_filename(str(title))
        )
        return sanitize_filename(final_name) + ext

    async def _preflight_conflict(self, token, session, event):
        """Ask about a same-name file before downloading when predictable.

        Returns False when the user cancelled the task at this prompt.
        """
        name = self._expected_name(session)
        if not name:
            return True
        await self._stage(token, "🔍 正在检查 NAS 上是否已有同名文件…")
        rel = session.get("folder") or ""
        if not await self._nas_file_exists(rel, name):
            return True
        decision = await self._ask_conflict(token, session, event, rel, name)
        if decision == "cancel":
            if await self._edit_status(
                session, "✅ 取消成功：已取消保存（未开始下载）"
            ):
                self._mark_cancel_handled(token)
            return False
        if decision == "rename":
            session["preset_name"] = await self._unique_nas_name(rel, name)
        else:
            session["conflict_decided"] = name
        return True

    async def _run_folder_cmd(self, client, chat_id, action, arg):
        """Folder management: create / list / rename / delete(empty) / help."""
        try:
            if action == "help":
                await client.send_message(chat_id, _FOLDER_HELP)
                return
            remote, directory = self._cfg_or_none()
            root = f"{remote}:{directory}".rstrip("/")

            if action == "create":
                rel = _clean_rel_path(arg)
                if not rel:
                    await client.send_message(
                        chat_id, "用法：新建文件夹 名字（可多级，如 电影/国产）"
                    )
                    return
                res = await self._rclone_run(["mkdir", f"{root}/{rel}"])
                if res.returncode == 0:
                    await client.send_message(
                        chat_id,
                        f"✅ 已新建文件夹：`{directory}/{rel}`\n"
                        "下载时回复 文件夹/文件名 即可存到这里",
                    )
                else:
                    await client.send_message(
                        chat_id,
                        f"❌ 新建失败：{(res.stderr or res.stdout)[:200]}",
                    )
                return

            if action == "list":
                rel = _clean_rel_path(arg)
                dest = f"{root}/{rel}" if rel else root
                res = await self._rclone_run(["lsf", dest, "--dirs-only"])
                if res.returncode != 0:
                    await client.send_message(
                        chat_id,
                        f"❌ 无法查看：{(res.stderr or res.stdout)[:200]}",
                    )
                    return
                names = sorted(
                    {
                        line.rstrip("/")
                        for line in (res.stdout or "").splitlines()
                        if line.strip() and line.strip() not in {".", ".."}
                    }
                )
                shown = f"📂 `{directory}/{rel}`" if rel else f"📂 `{directory}`"
                if not names:
                    await client.send_message(
                        chat_id, f"{shown}\n（暂无子文件夹）"
                    )
                    return
                lines = "\n".join(f"📁 {n}" for n in names[:20])
                more = f"\n…共 {len(names)} 个" if len(names) > 20 else ""
                await client.send_message(chat_id, f"{shown}\n{lines}{more}")
                return

            if action == "rename":
                parts = arg.split(None, 1)
                if len(parts) != 2:
                    await client.send_message(
                        chat_id, "用法：重命名文件夹 旧名 新名"
                    )
                    return
                old = _clean_rel_path(parts[0])
                new = _clean_rel_path(parts[1])
                if not old or not new:
                    await client.send_message(
                        chat_id, "❌ 文件夹名不能为空"
                    )
                    return
                if new == old or new.startswith(old + "/"):
                    await client.send_message(
                        chat_id, "❌ 新名称不能等于或嵌套在旧名称里面"
                    )
                    return
                parent, _, leaf = new.rpartition("/")
                probe = f"{root}/{parent}" if parent else root
                probe_res = await self._rclone_run(["lsf", probe, "--dirs-only"])
                target_exists = False
                if probe_res.returncode == 0:
                    for line in (probe_res.stdout or "").splitlines():
                        if line.strip().rstrip("/") == leaf:
                            target_exists = True
                            break
                if target_exists:
                    await client.send_message(
                        chat_id, "❌ 目标文件夹已存在，请换一个名字"
                    )
                    return
                mk = await self._rclone_run(["mkdir", f"{root}/{new}"])
                if mk.returncode != 0:
                    await client.send_message(
                        chat_id, f"❌ 无法创建目标文件夹：{(mk.stderr or mk.stdout)[:200]}"
                    )
                    return
                mv = await self._rclone_run(
                    [
                        "move",
                        f"{root}/{old}/",
                        f"{root}/{new}/",
                        "--delete-empty-src-dirs",
                    ]
                )
                if mv.returncode != 0:
                    await client.send_message(
                        chat_id, f"❌ 移动内容失败：{(mv.stderr or mv.stdout)[:200]}"
                    )
                    return
                rm_old = await self._rclone_run(["rmdir", f"{root}/{old}"])
                note = (
                    ""
                    if rm_old.returncode == 0
                    else "\n（旧文件夹未能自动清理，请到 NAS 上检查）"
                )
                await client.send_message(
                    chat_id,
                    f"✅ 已重命名：`{directory}/{old}` → `{directory}/{new}`{note}",
                )
                return

            if action == "delete":
                rel = _clean_rel_path(arg)
                if not rel:
                    await client.send_message(chat_id, "用法：删除文件夹 名字")
                    return
                res = await self._rclone_run(["rmdir", f"{root}/{rel}"])
                if res.returncode == 0:
                    await client.send_message(
                        chat_id, f"✅ 已删除空文件夹：`{directory}/{rel}`"
                    )
                else:
                    await client.send_message(
                        chat_id,
                        f"❌ 删除失败：文件夹不存在或非空（只能删空文件夹）\n"
                        f"{(res.stderr or res.stdout)[:200]}",
                    )
                return
        except Exception as exc:  # noqa: BLE001
            logger.exception("folder cmd failed: {}", exc)
            await client.send_message(
                chat_id, f"❌ 操作失败：{type(exc).__name__}: {exc}"
            )

    async def on_text_message(self, client, message):
        """Plain text: a name / quality answer, or a new url task."""
        user = message.from_user
        if user is None:
            return
        text = (message.text or "").strip()
        # 文字“取消”也走快路径：另一个操作卡住时依然能清掉任务
        if await self._cancel_via_text(client, message, user.id, text):
            return
        # 同一个用户的操作串行处理，避免按钮/文字并发导致状态错乱
        async with self._user_lock(user.id):
            await self._text_message_locked(client, message)

    async def _cancel_via_text(self, client, message, user_id, text):
        """Fast text-cancel that does not wait for the per-user lock."""
        low = (text or "").lower().strip()
        if not (low in _CANCEL_WORDS or low.startswith("/cancel")):
            return False
        session = self._find_user_session(user_id)
        if session is None:
            # “不/不要/no/0”这类词可能是误发或给别的场景用的，保持静默；
            # 明确的“取消”类词给一个反馈，避免用户以为没生效
            if low in ("不", "不要", "no", "0"):
                return False
            await client.send_message(
                message.chat.id,
                "当前没有进行中的任务，可以直接发送链接/视频开始下载",
            )
            return True
        if session.get("started"):
            await self._request_cancel(message.chat.id, session)
            return True
        token = session.get("token")
        if token:
            await self._cancel_session(token)
        await client.send_message(
            message.chat.id,
            "✅ 取消成功：任务已清理，可以重新发送链接/视频了",
        )
        return True

    async def _text_message_locked(self, client, message):
        """Plain text: a name / quality answer, or a new url task."""
        text = (message.text or "").strip()
        user = message.from_user
        if not text or user is None:
            return
        user_id = user.id
        logger.info(
            f"urldl text {user_id} chat {message.chat.id}: {text[:60]}"
        )
        if getattr(message, "media", None) is not None:
            # 带媒体的消息：说明文字归媒体流程处理，
            # 不能被当成对上一个任务的“文件名/文件夹名”指令
            return
        is_url = text.lower().startswith(("http://", "https://"))

        # 0) folder management commands (usable anytime, prompts stay open)
        first, _, arg = text.partition(" ")
        cmd_action = _FOLDER_CMDS.get(first.strip())
        if cmd_action:
            await self._run_folder_cmd(
                client, message.chat.id, cmd_action, arg.strip()
            )
            return

        # 1) a file with the same name exists on the NAS -> waiting for choice
        token = self.pending_conflict.get(user_id)
        if token is not None:
            session = self.sessions.get(token)
            if session and message.chat.id != session.get("chat_id"):
                return
            fut = session.get("conflict_fut") if session else None
            low = text.lower().strip()
            if (
                low in _CANCEL_WORDS
                or low.startswith("/cancel")
                or low.startswith("取消")
            ):
                decision = "cancel"
            elif low in _RENAME_WORDS:
                decision = "rename"
            elif low in _OVERWRITE_WORDS:
                decision = "overwrite"
            else:
                await client.send_message(
                    message.chat.id,
                    "⚠️ 请先处理目标文件夹里的同名文件：\n"
                    "点上方按钮，或回复：覆盖 / 自动改名 / 取消保存",
                )
                return
            if fut and not fut.done():
                fut.set_result(decision)
            return

        # 2) folder-card picker / waiting for a new-folder name
        token = self.pending_folder.get(user_id)
        if token is not None:
            session = self.sessions.get(token)
            if session and message.chat.id != session.get("chat_id"):
                return
            if is_url:
                await client.send_message(
                    message.chat.id,
                    "⏳ 当前任务正在选保存位置，先回复 保存位置/取消 处理完它，"
                    "再发送新链接",
                )
                return
            await self._consume_picker_text(client, message, token, text)
            return

        # 2.5) batch file checklist (choose which files to download)
        token = self.pending_select.get(user_id)
        if token is not None:
            session = self.sessions.get(token)
            if session and message.chat.id != session.get("chat_id"):
                return
            if is_url:
                await client.send_message(
                    message.chat.id,
                    "⏳ 当前任务在等你勾选文件，先处理完它或回复“取消”",
                )
                return
            await self._consume_select_text(client, message, token, text)
            return

        # 3) pending "reply a file name" prompt
        token = self.pending_name.get(user_id)
        if token is not None:
            session = self.sessions.get(token)
            if session and message.chat.id != session.get("chat_id"):
                return
            if is_url:
                await client.send_message(
                    message.chat.id,
                    "⏳ 当前有一个任务在等文件名，回复“取消”放弃它之后，再发送新链接",
                )
                return
            await self._consume_name(client, message, token, text)
            return

        # 4) pending "pick a quality" prompt
        token = self.pending_quality.get(user_id)
        if token is not None:
            session = self.sessions.get(token)
            if session and message.chat.id != session.get("chat_id"):
                return
            await self._consume_quality_text(client, message, token, text)
            return

        # 5) 通用取消逃生口：卡在中间状态时也能清掉任务
        low_text = text.lower().strip()
        if low_text in _CANCEL_WORDS or low_text.startswith("/cancel"):
            stuck = self._find_user_session(user_id)
            if stuck is not None:
                token = stuck.get("token")
                if stuck.get("started"):
                    await self._request_cancel(message.chat.id, stuck)
                    return
                if token:
                    await self._cancel_session(token)
                await client.send_message(
                    message.chat.id,
                    "✅ 取消成功：任务已清理，可以重新发送链接/视频了",
                )
            return

        if text.startswith("/"):
            return
        url = _pick(text)
        if not url:
            return

        existing = self._find_user_session(user_id)
        if existing is not None:
            logger.warning(
                f"urldl busy text {user_id}: "
                f"token={existing.get('token')} started={existing.get('started')}"
            )
            if not self._busy_notice(user_id):
                await client.send_message(
                    message.chat.id,
                    "⏳ 你还有一个任务在进行中，先完成或取消它，再发送新链接\n"
                    "（如果没看到进行中的任务，直接回复 取消 即可清理）",
                )
            return

        logger.info(
            f"url message from {user_id} in chat {message.chat.id}: {url[:60]}"
        )
        token = f"{random.randrange(16 ** 8):08x}"
        self.sessions[token] = {
            "token": token,
            "user_id": user_id,
            "chat_id": message.chat.id,
            "url": url,
            "kind": "tg" if url.startswith("https://t.me") else "yt",
            "next_stage": "name"
            if url.startswith("https://t.me")
            else "quality",
        }
        if self.sessions[token]["kind"] == "tg":
            await self._tg_inspect(client, message, token)
        else:
            await self._yt_inspect(client, message, token)

    async def on_callback(self, client, query: types.CallbackQuery):
        """Handle urldl:* buttons."""
        data = query.data or ""
        if isinstance(data, bytes):
            data = data.decode("utf-8", "ignore")
        if not str(data).startswith(_CB + ":"):
            # Not ours: let the legacy handlers of bot.py process it.
            raise pyrogram.ContinuePropagation
        parts = str(data).split(":")
        if len(parts) >= 3 and parts[1] == "cancel":
            # 取消走快路径：即使另一个操作还卡在 NAS/锁里，取消也要立刻生效
            await self._fast_cancel(client, query, parts[2])
            return
        user_id = query.from_user.id if query.from_user else None
        if user_id is not None:
            async with self._user_lock(user_id):
                try:
                    await self._callback_locked(client, query)
                except pyrogram.ContinuePropagation:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception("urldl callback error: {}", exc)
                    try:
                        await query.answer("操作出错了，请重试")
                    except Exception:  # noqa: BLE001
                        pass
            return
        await self._callback_locked(client, query)

    async def _fast_cancel(self, client, query, token):
        """Cancel without waiting for the per-user lock (buttons stay live)."""
        try:
            session = self.sessions.get(token)
            if not session:
                await query.answer("任务已过期，请重新发送链接", show_alert=True)
                return
            if query.from_user is None or query.from_user.id != session["user_id"]:
                await query.answer("这不是你的任务", show_alert=False)
                return
            if not session.get("started"):
                await self._cancel_session(token)
                await query.answer("已取消")
            else:
                chat_id = session.get("chat_id")
                if chat_id is not None:
                    await self._request_cancel(chat_id, session)
                await query.answer("已请求取消，稍后告诉你结果")
        except Exception as exc:  # noqa: BLE001
            logger.exception("urldl fast cancel error: {}", exc)

    async def _callback_locked(self, client, query: types.CallbackQuery):
        """Handle urldl:* buttons."""
        data = query.data or ""
        if isinstance(data, bytes):
            data = data.decode("utf-8", "ignore")
        if not str(data).startswith(_CB + ":"):
            # Not ours: let the legacy handlers of bot.py process it.
            raise pyrogram.ContinuePropagation
        logger.info(f"callback {data} from user {query.from_user.id}")

        parts = str(data).split(":")
        if len(parts) < 3:
            await query.answer()
            return
        action, token = parts[1], parts[2]
        session = self.sessions.get(token)
        if not session:
            await query.answer("任务已过期，请重新发送链接", show_alert=True)
            try:
                await query.message.edit_text(
                    "⏳ 任务已过期（服务重启过），请重新发送链接或视频"
                )
            except Exception:  # noqa: BLE001
                pass
            return
        if query.from_user.id != session["user_id"]:
            await query.answer("这不是你的任务", show_alert=False)
            return

        if action == "cancel":
            if not session.get("started"):
                await query.answer("已取消")
                await self._cancel_session(token)
                return
            chat_id = session.get("chat_id")
            if chat_id is not None:
                await self._request_cancel(chat_id, session)
            await query.answer("已请求取消，稍后告诉你结果")
            return

        if action in ("q", "s"):
            if session.get("started"):
                await query.answer("任务已经开始", show_alert=False)
                return
            user_id = session["user_id"]
            self.pending_quality.pop(user_id, None)
            self.pending_name.pop(user_id, None)
            self.pending_folder.pop(user_id, None)
            custom = self.pending_custom.pop(user_id, None)
            if action == "q":
                session["quality"] = parts[3]
            else:
                session.pop("quality", None)
            if custom:
                self._apply_dest(session, custom)
            elif action == "s":
                session.pop("final_name", None)
            label = _quality_label(parts[3]) if action == "q" else "原文件名"
            await query.answer()
            try:
                await query.message.edit_text(
                    self._target_text(session, label),
                    reply_markup=self._cancel_markup(token),
                )
            except Exception:  # noqa: BLE001
                pass
            await self._begin(token)
            return

        if action in ("fl", "fp", "fn", "fu", "fr", "ff"):
            if session.get("started"):
                await query.answer("任务已开始", show_alert=False)
                return
            await query.answer()
            if action == "fl":
                children = session.get("pick_children") or []
                try:
                    idx = int(parts[3])
                except (IndexError, ValueError):
                    idx = -1
                if idx < 0 or idx >= len(children):
                    return
                nav = session.get("nav_rel") or ""
                child = children[idx]
                nav = f"{nav}/{child}" if nav else child
                self._cancel_timer(token)
                await self._show_folder_picker(self.bot, token, nav)
            elif action == "fp":
                session["folder"] = session.get("nav_rel") or ""
                session.pop("final_name", None)
                await self._after_folder_picked(self.bot, token)
            elif action == "fn":
                if session.get("expect_new"):
                    session["expect_new"] = False
                    self._cancel_timer(token)
                    await self._show_folder_picker(
                        self.bot, token, session.get("nav_rel") or ""
                    )
                else:
                    await self._ask_new_folder(self.bot, token)
            elif action == "fu":
                nav = session.get("nav_rel") or ""
                parent = nav.rsplit("/", 1)[0] if "/" in nav else ""
                self._cancel_timer(token)
                await self._show_folder_picker(self.bot, token, parent)
            elif action == "fr":
                self._cancel_timer(token)
                await self._show_folder_picker(self.bot, token, "")
            elif action == "ff":
                user_id = session["user_id"]
                self.pending_name.pop(user_id, None)
                self.pending_quality.pop(user_id, None)
                self.pending_folder.pop(user_id, None)
                custom = self.pending_custom.pop(user_id, None)
                if custom:
                    self._apply_dest(session, custom)
                self._cancel_timer(token)
                await self._enter_folder_pick(self.bot, token)
            return

        if action in ("sel", "selall", "selnone", "selgo"):
            if session.get("started"):
                await query.answer("任务已开始", show_alert=False)
                return
            user_id = session["user_id"]
            await query.answer()
            msgs = session.get("media_msgs") or []
            if action in ("sel", "selall", "selnone"):
                sel = session.get("selected") or [True] * len(msgs)
                if action == "sel":
                    idx = int(parts[3]) if len(parts) > 3 else -1
                    if 0 <= idx < len(sel):
                        sel[idx] = not sel[idx]
                elif action == "selall":
                    sel = [True] * len(msgs)
                else:
                    sel = [False] * len(msgs)
                session["selected"] = sel
                await self._show_item_select(client, token)
                return
            # selgo: download the checked files only
            sel = session.get("selected") or []
            chosen = [m for i, m in enumerate(msgs) if i < len(sel) and sel[i]]
            if not chosen:
                await query.answer("还没有勾选文件", show_alert=True)
                return
            session["batch_total"] = len(msgs)
            session["media_msgs"] = chosen
            session["size"] = sum(
                getattr(
                    getattr(m, m.media.value, None), "file_size", 0
                ) or 0
                for m in chosen
            )
            session["multi"] = True
            session["at_select"] = False
            session.pop("selected", None)
            self.pending_select.pop(user_id, None)
            self.pending_folder.pop(user_id, None)
            await query.answer(f"开始下载选中的 {len(chosen)} 个")
            await self._begin(token)
            return

        if action == "c":
            fut = session.get("conflict_fut")
            decision = parts[3] if len(parts) > 3 else ""
            if fut is None or fut.done():
                await query.answer("已处理完毕", show_alert=False)
                return
            if decision in ("overwrite", "rename", "cancel"):
                fut.set_result(decision)
                await query.answer("收到，正在处理...")
            else:
                await query.answer()
            return

        await query.answer()

    async def on_media_message(self, client, message):
        """Forwarded media (video/doc/...) -> folder picker + name prompt."""
        user = message.from_user
        if user is None:
            return
        async with self._user_lock(user.id):
            await self._media_message_locked(client, message)

    async def _media_message_locked(self, client, message):
        """Forwarded media (video/doc/...) -> folder picker + name prompt.

        Multiple images/videos forwarded together (album or a quick burst)
        are collected into ONE batch task within a short window, so the user
        picks a folder/name once and everything downloads in order.
        """
        user = message.from_user
        if user is None or not message.media:
            return
        user_id = user.id
        logger.info(f"media message from {user_id} in chat {message.chat.id}")
        media = getattr(message, message.media.value, None)
        if not media:
            return
        original = getattr(media, "file_name", None)
        size = getattr(media, "file_size", 0) or 0
        session = self._find_user_session(user_id)
        if session is not None and session.get("collecting"):
            # 还在收集窗口内：追加，并重置等待计时（等最后一条到了再开始问）
            msgs = session.setdefault("media_msgs", [])
            if message.id not in [m.id for m in msgs]:
                msgs.append(message)
                session["size"] = (session.get("size") or 0) + size
                self._restart_media_collect(client, token=None, session=session)
            return
        if session is not None:
            logger.warning(
                f"urldl busy media {user_id}: "
                f"token={session.get('token')} started={session.get('started')}"
            )
            if not self._busy_notice(user_id):
                if session.get("started"):
                    ci = session.get("cur_i")
                    if ci is not None:
                        cn = session.get("cur_n") or "?"
                        cur = session.get("cur_display")
                        cur_s = f"（`{cur}`）" if cur else ""
                        await client.send_message(
                            message.chat.id,
                            f"⏳ 正在下载上一批：第 {ci}/{cn} 个{cur_s}\n"
                            "新转发的文件这次不会自动加入。\n"
                            "可以等这一批结束再转发；也可以回复「取消」"
                            "停止当前任务后再发。",
                        )
                    else:
                        await client.send_message(
                            message.chat.id,
                            "⏳ 正在下载其他文件（看上方的进度消息），"
                            "请等它结束再转发；或回复「取消」停止当前任务。",
                        )
                else:
                    await client.send_message(
                        message.chat.id,
                        "⏳ 你还有一个任务在等你处理（看上面的卡片），"
                        "先处理完它或回复「取消」，再发送新的媒体",
                    )
            return

        token = f"{random.randrange(16 ** 8):08x}"
        self.sessions[token] = {
            "token": token,
            "user_id": user_id,
            "chat_id": message.chat.id,
            "url": "",
            "kind": "tg",
            "tg_msg": message,
            "title": original or f"telegram_media_{message.id}",
            "size": size,
            "next_stage": "name",
            "collecting": True,
            "media_msgs": [message],
            "media_group_id": getattr(message, "media_group_id", None),
        }
        self._restart_media_collect(client, token=token)

    def _restart_media_collect(self, client, token=None, session=None):
        """(Re)start the short window that gathers forwarded media together."""
        if token is None and session is not None:
            token = session.get("token")
        if not token:
            return
        self._cancel_timer("collect:" + token)
        self.timers["collect:" + token] = self.app.loop.create_task(
            self._media_collect_done(client, token)
        )

    async def _media_collect_done(self, client, token):
        try:
            await asyncio.sleep(_MEDIA_COLLECT_SECS)
        except asyncio.CancelledError:
            return
        self.timers.pop("collect:" + token, None)
        session = self.sessions.get(token)
        if not session or not session.get("collecting"):
            return
        session["collecting"] = False
        msgs = session.get("media_msgs") or []
        msgs.sort(key=lambda m: m.id)
        session["media_msgs"] = msgs
        if len(msgs) == 1:
            session.pop("media_msgs", None)
            session["multi"] = False
            m0 = msgs[0]
            md = getattr(m0, m0.media.value, None)
            session["tg_msg"] = m0
            session["title"] = (
                getattr(md, "file_name", None) or f"telegram_media_{m0.id}"
            )
            session["size"] = getattr(md, "file_size", 0) or 0
        else:
            session["multi"] = True
            session["tg_msg"] = msgs[0]
            session["title"] = "批量媒体"
            session["size"] = sum(
                getattr(
                    getattr(m, m.media.value, None), "file_size", 0
                ) or 0
                for m in msgs
            )
        await self._enter_folder_pick(client, token)

    # ------------------------------------------------------------- questions

    @staticmethod
    def _apply_dest(session, raw):
        """Put a user supplied 'folder/name' reply onto the session.

        A plain name without "/" keeps the folder that was already chosen
        (e.g. picked through the folder cards).
        """
        folder, name = _split_dest(raw) if raw else ("", None)
        if raw:
            text = str(raw).replace("\\", "/").strip()
            if "/" not in text:
                folder = session.get("folder") or ""
        session["folder"] = folder
        if name:
            session["final_name"] = name
        else:
            session.pop("final_name", None)
        return folder, name

    def _target_text(self, session, label=None):
        head = f"▶️ 开始下载（{label}）" if label else "▶️ 开始下载"
        folder = session.get("folder") or ""
        name = session.get("final_name") or session.get("title") or "原名"
        path = self._nas_display(folder, sanitize_filename(name))
        return f"{head}\n保存到：`{path}`"

    def _download_plan_text(self, session, head="⬇️ 下载中"):
        """Download-time header: where the file will be saved (+known size)."""
        folder = session.get("folder") or ""
        if session.get("multi"):
            n = len(session.get("media_msgs") or [])
            size_s = (
                f"　共 {n} 个文件"
                + (f"（合计 {_format_size(session['size'])}）" if session.get("size") else "")
            )
            cur = session.get("cur_display")
            cur_line = ""
            if cur:
                cur_line = f"\n📄 当前：`{str(cur)[:70]}`"
            return (
                f"{head}\n📁 保存到：`{self._nas_display(folder)}`{size_s}"
                f"{cur_line}"
            )
        name = (
            session.get("preset_name")
            or session.get("final_name")
            or session.get("title")
            or "下载文件"
        )
        path = self._nas_display(folder, sanitize_filename(str(name)))
        note = ""
        if session.get("kind") == "tg" and session.get("size"):
            note = f"　（共 {_format_size(session['size'])}）"
        return f"{head}\n📁 保存到：`{path}`{note}"

    def _item_display_name(self, msg) -> str:
        """A friendly file name for one forwarded media message."""
        media = getattr(msg, msg.media.value, None) if getattr(msg, "media", None) else None
        if media is not None:
            fn = getattr(media, "file_name", None)
            if fn:
                return str(fn)
        value = getattr(getattr(msg, "media", None), "value", "")
        title = getattr(msg, "caption", None) or ""
        base = sanitize_filename(str(title).strip()) if str(title).strip() else None
        if value == "photo":
            return (base or "图片") + ".jpg"
        if value in ("video_note", "animation"):
            return (base or "视频") + ".mp4"
        if value == "video":
            return (base or "视频") + ".mp4"
        return (base or "文件") + ".bin"

    def _file_card(self, session):
        """One-line media info shown above the folder cards."""
        lines = []
        if session.get("kind") == "yt":
            title = session.get("title")
            if title:
                lines.append(f"🎬 {str(title)[:80]}")
            meta = []
            if session.get("uploader"):
                meta.append(str(session["uploader"])[:30])
            dur = session.get("duration")
            if dur:
                meta.append(
                    f"{dur // 3600}h{dur % 3600 // 60:02d}m{dur % 60:02d}s"
                )
            if meta:
                lines.append("　".join(meta))
        else:
            if session.get("multi"):
                msgs = session.get("media_msgs") or []
                n = len(msgs)
                total = session.get("size") or 0
                head = f"📎 共 {n} 个文件"
                if total:
                    head += f"（合计 {_format_size(total)}）"
                lines.append(head)
                for m in msgs[:6]:
                    md = getattr(m, m.media.value, None) if m.media else None
                    one = (
                        f"　{_format_size(getattr(md, 'file_size', 0) or 0)}"
                        if md is not None
                        else ""
                    )
                    lines.append(f"　· `{self._item_display_name(m)[:60]}`{one}")
                if n > 6:
                    lines.append(f"　…还有 {n - 6} 个")
            else:
                title = session.get("title")
                if title:
                    line = f"📎 文件：`{sanitize_filename(str(title))[:80]}`"
                    if session.get("size"):
                        line += f"　{_format_size(session['size'])}"
                    lines.append(line)
        return "\n".join(lines)

    async def _edit_with_markup(self, client, session, text, markup):
        """Edit the status message, or send a new one when editing fails."""
        status = session.get("status_msg")
        if status is not None:
            try:
                await status.edit_text(text, reply_markup=markup)
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            status = await client.send_message(
                session["chat_id"], text, reply_markup=markup
            )
            session["status_msg"] = status
        except Exception:  # noqa: BLE001
            pass

    async def _enter_folder_pick(self, client, token):
        """Start the folder-card stage (save location = 远程下载/...)."""
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        session["at_picker"] = True
        session["nav_rel"] = session.get("folder") or ""
        await self._show_folder_picker(client, token, session["nav_rel"])

    async def _after_folder_picked(self, client, token):
        """Folder is fixed now: continue with the name/quality question."""
        session = self.sessions.get(token)
        if not session:
            return
        session["at_picker"] = False
        user_id = session["user_id"]
        self.pending_folder.pop(user_id, None)
        self.pending_custom.pop(user_id, None)
        if session.get("multi"):
            await self._show_item_select(client, token)
        elif (session.get("next_stage") or "name") == "quality":
            await self._show_quality(client, token)
        else:
            await self._prompt_name(client, token)

    # --------------------------------------------------- batch item select

    def _init_selection(self, session):
        if session.get("selected") is None:
            session["selected"] = [True] * len(
                session.get("media_msgs") or []
            )

    def _select_text(self, session):
        msgs = session.get("media_msgs") or []
        self._init_selection(session)
        selected = session.get("selected") or []
        lines = []
        total = session.get("size") or 0
        lines.append(f"📎 共 {len(msgs)} 个文件（合计 {_format_size(total)}）")
        for i, m in enumerate(msgs, 1):
            md = getattr(m, m.media.value, None) if m.media else None
            mark = "✅" if (selected and i - 1 < len(selected) and selected[i - 1]) else "⬜️"
            size_s = (
                f"　{_format_size(getattr(md, 'file_size', 0) or 0)}"
                if md is not None
                else ""
            )
            lines.append(f"{mark} {i}. `{self._item_display_name(m)[:60]}`{size_s}")
        folder = session.get("folder") or ""
        base = session.get("final_name")
        base_note = ""
        if base:
            base_note = f"\n📝 统一命名：`{sanitize_filename(base)}` → 将按顺序存成 `{sanitize_filename(base)}_1`、`_2`…（回复 原名 取消统一命名）"
        lines.append(f"📁 保存到：`{self._nas_display(folder)}`{base_note}")
        k = sum(1 for x in (selected or []) if x)
        lines.append(
            "\n勾选要下载的文件；回复 `旅行` 统一命名、`别的文件夹/` 换位置，"
            "再点下方「⬇️ 下载选中」"
        )
        return "\n".join(lines), k

    def _select_markup(self, session, k):
        msgs = session.get("media_msgs") or []
        selected = session.get("selected") or []
        rows = []
        for i in range(len(msgs)):
            on = bool(selected and i < len(selected) and selected[i])
            label = f"{'✅' if on else '⬜️'} {i + 1}. {self._item_display_name(msgs[i])[:36]}"
            rows.append(
                [
                    types.InlineKeyboardButton(
                        label, callback_data=f"{_CB}:sel:{session['token']}:{i}"
                    )
                ]
            )
        rows.append(
            [
                types.InlineKeyboardButton(
                    "✅ 全选", callback_data=f"{_CB}:selall:{session['token']}"
                ),
                types.InlineKeyboardButton(
                    "⬜️ 全不选", callback_data=f"{_CB}:selnone:{session['token']}"
                ),
            ]
        )
        rows.append(
            [
                types.InlineKeyboardButton(
                    f"⬇️ 下载选中的 {k} 个",
                    callback_data=f"{_CB}:selgo:{session['token']}",
                )
            ]
        )
        rows.append(
            [
                types.InlineKeyboardButton(
                    "📁 改文件夹", callback_data=f"{_CB}:ff:{session['token']}"
                ),
                types.InlineKeyboardButton(
                    "✖️ 取消任务", callback_data=f"{_CB}:cancel:{session['token']}"
                ),
            ]
        )
        return types.InlineKeyboardMarkup(rows)

    async def _show_item_select(self, client, token):
        """Multi-file check-list: choose which files to download."""
        session = self.sessions.get(token)
        if not session or session.get("started") or not session.get("multi"):
            return
        session["at_select"] = True
        self._init_selection(session)
        selected = session.get("selected") or []
        k = sum(1 for x in selected if x)
        text, k2 = self._select_text(session)
        k = k2
        await self._edit_with_markup(
            client, session, text, self._select_markup(session, k)
        )
        self.pending_select[session["user_id"]] = token

    async def _consume_select_text(self, client, message, token, text):
        """Text answers while the multi-file checklist is on screen."""
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        low = text.lower().strip()
        if low in _CANCEL_WORDS or low.startswith("/cancel"):
            await self._cancel_session(token)
            return
        if text.startswith("/"):
            await client.send_message(
                message.chat.id,
                "在这里可以：回复 `旅行` 统一命名、`别的文件夹/` 换位置、"
                "`原名` 取消统一命名、`取消` 放弃任务",
            )
            return
        if low in _ACCEPT_WORDS or low in _DEFAULT_WORDS:
            session.pop("final_name", None)
            await self._show_item_select(client, token)
            return
        folder, name = _split_dest(text)
        if "/" in text.replace("\\", "/"):
            session["folder"] = folder or session.get("folder") or ""
            if name:
                session["final_name"] = name
        else:
            if name:
                session["final_name"] = name
        await self._show_item_select(client, token)

    async def _show_folder_picker(self, client, token, rel):
        """One folder-card screen: 远程下载 root or a sub folder."""
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        rel = _clean_rel_path(rel)
        session["nav_rel"] = rel
        session["at_picker"] = True
        children = await self._list_dirs(rel)
        if children is None:
            # NAS temporarily not listable: keep the task usable via text path
            await self._after_folder_picked(client, token)
            return
        shown = children[:_PICKER_MAX_FOLDERS]
        session["pick_children"] = shown
        more = ""
        if len(children) > _PICKER_MAX_FOLDERS:
            more = (
                f"\n…还有 {len(children) - _PICKER_MAX_FOLDERS} 个文件夹，"
                "可直接回复完整路径保存"
            )
        rows = []
        for idx, child in enumerate(shown):
            label = child if len(child) <= 22 else child[:21] + "…"
            rows.append(
                [
                    types.InlineKeyboardButton(
                        "📁 " + label,
                        callback_data=f"{_CB}:fl:{token}:{idx}",
                    )
                ]
            )
        rows.append(
            [
                types.InlineKeyboardButton(
                    "📥 就存到这里",
                    callback_data=f"{_CB}:fp:{token}",
                )
            ]
        )
        rows.append(
            [
                types.InlineKeyboardButton(
                    "➕ 新建文件夹并存入",
                    callback_data=f"{_CB}:fn:{token}",
                )
            ]
        )
        if rel:
            rows.append(
                [
                    types.InlineKeyboardButton(
                        "⬅️ 返回上级",
                        callback_data=f"{_CB}:fu:{token}",
                    ),
                    types.InlineKeyboardButton(
                        "🏠 远程下载根目录",
                        callback_data=f"{_CB}:fr:{token}",
                    ),
                ]
            )
        rows.append(
            [
                types.InlineKeyboardButton(
                    "✖️ 取消任务",
                    callback_data=f"{_CB}:cancel:{token}",
                )
            ]
        )
        card = self._file_card(session)
        stage = session.get("next_stage") or "name"
        hint = (
            "⏳ 不会自动下载：选好位置、按「📥 就存到这里」后才会开始；"
            "想放弃就回复 取消"
            if stage == "name"
            else "选好文件夹后，下一步再选清晰度"
        )
        text = (
            (card + "\n\n" if card else "")
            + "📁 保存到哪里？一张卡片 = 一个文件夹\n"
            + f"当前：`{self._nas_display(rel)}`\n\n"
            + "点卡片可一层层进入子文件夹；\n"
            + "点「📥 就存到这里」确认，或「➕ 新建文件夹并存入」新建；\n"
            + "也可以直接回复：`文件夹/文件名`、`文件夹/`、`文件名`"
            + more
            + f"\n\n{hint}"
        )
        await self._edit_with_markup(
            client, session, text, types.InlineKeyboardMarkup(rows)
        )
        self.pending_folder[session["user_id"]] = token
        if stage != "name":
            self._schedule_expire(token)

    async def _ask_new_folder(self, client, token):
        """Ask the user to type the name of the new folder (under nav dir)."""
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        session["expect_new"] = True
        where = self._nas_display(session.get("nav_rel") or "")
        text = (
            "✏️ 请输入新文件夹的名字（在当前目录下新建）：\n"
            f"当前：`{where}`\n\n"
            "回复例子：\n"
            "· `电影` → 新建“电影”\n"
            "· `纪录片/自然` → 新建多级文件夹\n\n"
            "回复“取消”返回文件夹卡片"
        )
        markup = types.InlineKeyboardMarkup(
            [
                [
                    types.InlineKeyboardButton(
                        "📁 返回文件夹卡片",
                        callback_data=f"{_CB}:fn:{token}",
                    ),
                    types.InlineKeyboardButton(
                        "✖️ 取消任务",
                        callback_data=f"{_CB}:cancel:{token}",
                    ),
                ]
            ]
        )
        await self._edit_with_markup(client, session, text, markup)
        self.pending_folder[session["user_id"]] = token
        self._schedule_new_folder_expire(token)

    async def _consume_new_folder(self, client, message, token, text):
        """A typed name while "➕ 新建文件夹并存入" is waiting."""
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        low = text.lower().strip()
        if low in _CANCEL_WORDS or low.startswith("/cancel"):
            session["expect_new"] = False
            self._cancel_timer(token)
            await self._show_folder_picker(
                client, token, session.get("nav_rel") or ""
            )
            return
        if text.startswith("/"):
            await client.send_message(
                message.chat.id,
                "请输入文件夹名字（如 `电影`），或回复“取消”返回文件夹卡片",
            )
            return
        name = _clean_rel_path(text)
        if not name:
            await client.send_message(
                message.chat.id,
                "文件夹名字不能为空，请重新输入（如 `电影`），或回复“取消”",
            )
            return
        nav = session.get("nav_rel") or ""
        rel_new = f"{nav}/{name}" if nav else name
        res = await self._rclone_run(
            ["mkdir", self._remote_dir(rel_new)], timeout=30
        )
        if res.returncode != 0:
            logger.warning(
                f"urldl mkdir fail {rel_new}: "
                + (res.stderr or res.stdout or "")[:200]
            )
            session["expect_new"] = False
            await client.send_message(
                message.chat.id,
                f"❌ 新建文件夹失败：{(res.stderr or res.stdout)[:200]}\n"
                "已回到文件夹卡片，也可以直接回复完整路径保存",
            )
            await self._show_folder_picker(
                client, token, session.get("nav_rel") or ""
            )
            return
        session["folder"] = rel_new
        session["nav_rel"] = rel_new
        session["expect_new"] = False
        await self._after_folder_picked(client, token)

    async def _consume_picker_text(self, client, message, token, text):
        """A text answer while the folder cards are on screen."""
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        if session.get("expect_new"):
            await self._consume_new_folder(client, message, token, text)
            return
        low = text.lower().strip()
        if low in _CANCEL_WORDS or low.startswith("/cancel"):
            await self._cancel_session(token)
            return
        if text.startswith("/"):
            await client.send_message(
                message.chat.id,
                "可以直接回复保存位置，例如：`电影/流浪地球`、`电影/`、`文件名`",
            )
            return
        # "下载/原名/确认" -> current folder (root by default) + original name
        if low in _ACCEPT_WORDS or low in _DEFAULT_WORDS:
            session["folder"] = session.get("nav_rel") or ""
            session.pop("final_name", None)
            if session.get("multi"):
                await self._show_item_select(client, token)
            elif (session.get("next_stage") or "name") == "quality":
                await self._after_folder_picked(client, token)
            else:
                await self._edit_prompt(token, self._target_text(session))
                await self._begin(token)
            return
        folder, name = _split_dest(text)
        if "/" in text.replace("\\", "/"):
            session["folder"] = folder
        else:
            # 只回复了文件名 -> 存到当前正在看的文件夹
            session["folder"] = session.get("nav_rel") or ""
        if name:
            session["final_name"] = name
        else:
            session.pop("final_name", None)
        if session.get("multi"):
            await self._show_item_select(client, token)
            return
        if (session.get("next_stage") or "name") == "quality":
            await self._after_folder_picked(client, token)
            return
        if name:
            await self._begin(token)
        else:
            await self._after_folder_picked(client, token)

    async def _prompt_name(self, client, token):
        """Show the file-name prompt (folder already chosen) + 30s auto start."""
        session = self.sessions.get(token)
        if not session:
            return
        session["at_picker"] = False
        start_label = (
            "⏬ 按原名全部下载" if session.get("multi") else "⏬ 立即用原名下载"
        )
        keyboard = types.InlineKeyboardMarkup(
            [
                [
                    types.InlineKeyboardButton(
                        start_label,
                        callback_data=f"{_CB}:s:{token}:best",
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        "📁 改文件夹",
                        callback_data=f"{_CB}:ff:{token}",
                    ),
                    types.InlineKeyboardButton(
                        "✖️ 取消任务",
                        callback_data=f"{_CB}:cancel:{token}",
                    ),
                ],
            ]
        )
        folder = session.get("folder") or ""
        path = self._nas_display(folder)
        if session.get("multi"):
            msgs = session.get("media_msgs") or []
            n = len(msgs)
            text = (
                f"📎 共 {n} 个文件（合计 {_format_size(session.get('size'))}）\n"
                f"📁 保存到：`{path}`\n\n"
                "· 点「⏬ 按原名全部下载」→ 每个文件保留自己的名字；\n"
                "· 回复 `旅行` → 存成 `旅行_1`、`旅行_2`…（自动补序号）；\n"
                "· 回复 `别的文件夹/` → 换文件夹、按原名保存；\n"
                "· NAS 上已有同名文件会自动改名 `_1` 保留两份；\n"
                "· 不会自动下载，需要了就回复“取消”"
            )
        else:
            text = (
                f"📎 文件：`{sanitize_filename(session['title'])}`\n"
                f"大小：{_format_size(session.get('size'))}\n"
                f"📁 保存到：`{path}`\n\n"
                "回复一个新文件名 → 用新名字保存到上面位置；\n"
                "回复 `别的文件夹/文件名` → 换位置保存；\n"
                "不会自动下载：点「⏬ 立即用原名下载」或回复“原名”才开始；\n"
                "不需要了就回复“取消”"
            )
        status = session.get("status_msg")
        if status is not None:
            try:
                await status.edit_text(text, reply_markup=keyboard)
            except Exception:  # noqa: BLE001
                status = None
        if status is None:
            try:
                status = await client.send_message(
                    session["chat_id"], text, reply_markup=keyboard
                )
                session["status_msg"] = status
            except Exception:  # noqa: BLE001
                pass
        self.pending_name[session["user_id"]] = token

    def _cancel_markup(self, token):
        return types.InlineKeyboardMarkup(
            [
                [
                    types.InlineKeyboardButton(
                        "✖️ 取消", callback_data=f"{_CB}:cancel:{token}"
                    )
                ]
            ]
        )

    async def _edit_prompt(self, token, text):
        """Edit the session status message but keep a cancel button."""
        session = self.sessions.get(token)
        if not session:
            return
        status = session.get("status_msg")
        if status is None:
            return
        try:
            await status.edit_text(text, reply_markup=self._cancel_markup(token))
        except Exception:  # noqa: BLE001
            pass

    async def _consume_name(self, client, message, token, text):
        """A text answer to the name prompt."""
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        user_id = session["user_id"]
        low = text.lower().strip()
        if low in _CANCEL_WORDS or low.startswith("/cancel"):
            await self._cancel_session(token)
            return
        if text.startswith("/"):
            await client.send_message(
                message.chat.id,
                "请直接回复文件名（例如 `我的电影`）开始下载，"
                "或回复“取消”结束当前任务",
            )
            return
        self.pending_name.pop(user_id, None)
        if low in _ACCEPT_WORDS or low in _DEFAULT_WORDS:
            # 用原文件名，保存到上面已经选好的文件夹
            session.pop("final_name", None)
        else:
            self._apply_dest(session, text)
        await self._edit_prompt(token, self._target_text(session))
        await self._begin(token)

    async def _consume_quality_text(self, client, message, token, text):
        """A text answer while the quality buttons are shown."""
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        user_id = session["user_id"]
        low = text.lower().strip()
        if low in _CANCEL_WORDS or low.startswith("/cancel"):
            await self._cancel_session(token)
            return

        digits = re.sub(r"\D", "", text)
        heights = [int(h) for h in session.get("heights") or []]
        start_now = False
        if "/" not in text and low in _ACCEPT_WORDS:
            session["quality"] = "best"
            start_now = True
        elif "/" not in text and digits and heights:
            choice = int(digits)
            if choice not in heights:
                choice = min(heights, key=lambda h: abs(h - choice))
            session["quality"] = str(choice)
            start_now = True
        if start_now:
            custom = self.pending_custom.pop(user_id, None)
            if custom:
                self._apply_dest(session, custom)
            await self._edit_prompt(
                token,
                self._target_text(session, _quality_label(session["quality"])),
            )
            await self._begin(token)
            return

        if len(text) > 0 and not text.startswith("/"):
            folder, name = _split_dest(text)
            where = f"{folder or '远程下载根目录'}"
            what = name or "原文件名"
            self.pending_custom[user_id] = text
            await client.send_message(
                message.chat.id,
                f"📝 已记录保存位置：`{where}`，文件名：`{what}`\n"
                "现在点下方清晰度按钮（或直接回复“下载”用原画）开始下载",
            )

    # ------------------------------------------------------------ inspections

    async def _yt_inspect(self, client, message, token):
        session = self.sessions[token]
        status = await client.send_message(
            session["chat_id"],
            "正在获取视频信息...",
            reply_to_message_id=message.id,
        )
        session["status_msg"] = status
        try:
            info = await asyncio.to_thread(self._probe, session["url"])
        except Exception as exc:  # noqa: BLE001
            self.sessions.pop(token, None)
            try:
                await status.edit_text(
                    "无法解析该视频，可能是不支持的网站或需要登录"
                    + f"\n{type(exc).__name__}: {exc}"
                )
            except Exception:  # noqa: BLE001
                pass
            return

        if info.get("_type") == "playlist":
            self.sessions.pop(token, None)
            await status.edit_text("不支持播放列表，请发送单个视频链接")
            return
        if not info.get("formats"):
            self.sessions.pop(token, None)
            await status.edit_text("没有找到可下载的格式")
            return

        title = info.get("title") or session["url"]
        session["info"] = info
        session["title"] = title
        session["duration"] = info.get("duration")
        session["uploader"] = info.get("uploader") or info.get("channel") or ""
        formats = info.get("formats", [])
        session["heights"] = _height_options(formats)
        estimates = {}
        for key in ["best"] + [str(h) for h in session["heights"]] + ["audio"]:
            est = _estimate_choice_size(formats, key)
            if est:
                estimates[key] = est
        session["size_estimates"] = estimates
        logger.info(f"urldl yt estimates {token}: {estimates}")

        await self._enter_folder_pick(client, token)

    async def _show_quality(self, client, token):
        """Quality buttons + chosen folder (the last step before download)."""
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        session["at_picker"] = False
        title = session.get("title") or session.get("url") or "视频"
        duration = session.get("duration")
        dur_str = (
            f"{duration // 3600}h{duration % 3600 // 60:02d}m{duration % 60:02d}s"
            if duration
            else ""
        )
        uploader = session.get("uploader") or ""
        path = self._nas_display(session.get("folder") or "")
        name_note = ""
        if session.get("final_name"):
            name_note = (
                "\n📝 文件名："
                f"`{sanitize_filename(session['final_name'])}`"
            )
        estimates = session.get("size_estimates") or {}
        est_lines = []
        if estimates:
            order = ["best"] + [str(h) for h in (session.get("heights") or [])]
            order += [k for k in ("audio",) if k not in order and k in estimates]
            parts = [
                f"{_quality_label(k)}≈{estimates[k]}"
                for k in order
                if k in estimates
            ]
            if parts:
                est_lines.append("📦 预计大小：" + "、".join(parts))
        text = (
            f"🎬 {str(title)[:80]}\n"
            f"{uploader}  {dur_str}\n"
            f"📁 保存到：`{path}`{name_note}\n\n"
            + ("\n".join(est_lines) + "\n\n" if est_lines else "")
            + "点选清晰度立即开始下载；\n"
            f"默认文件名：`{sanitize_filename(title)}`\n"
            "可先回复 `文件名` 或 `别的文件夹/文件名` 修改，"
            "再点清晰度"
        )
        status = session.get("status_msg")
        if status is not None:
            try:
                await status.edit_text(
                    text, reply_markup=self._quality_markup(token, session)
                )
            except Exception:  # noqa: BLE001
                status = None
        if status is None:
            try:
                status = await client.send_message(
                    session["chat_id"],
                    text,
                    reply_markup=self._quality_markup(token, session),
                )
                session["status_msg"] = status
            except Exception:  # noqa: BLE001
                pass
        self.pending_quality[session["user_id"]] = token
        self._schedule_expire(token)

    def _probe(self, url):
        if YoutubeDL is None:
            raise RuntimeError("yt-dlp not installed")
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 30,
        }
        if self.cfg.get("cookies_file"):
            opts["cookiefile"] = self.cfg["cookies_file"]
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    async def _tg_inspect(self, client, message, token):
        """Resolve a t.me link, then ask for the (optional) file name."""
        session = self.sessions[token]
        status = await client.send_message(
            session["chat_id"],
            "正在获取 Telegram 消息...",
            reply_to_message_id=message.id,
        )
        session["status_msg"] = status
        try:
            chat_id, message_id, _ = await parse_link(self.client, session["url"])
            if not chat_id or not message_id:
                raise ValueError("不是单条消息链接")
            tg_msg = await retry(
                self.client.get_messages, args=(chat_id, message_id)
            )
            if not tg_msg or not getattr(tg_msg, "media", None):
                raise ValueError("这条链接没有媒体内容")
        except Exception as exc:  # noqa: BLE001
            self.sessions.pop(token, None)
            try:
                await status.edit_text(
                    "无法读取这条 Telegram 链接"
                    + f"\n{type(exc).__name__}: {exc}"
                )
            except Exception:  # noqa: BLE001
                pass
            return

        media = getattr(tg_msg, tg_msg.media.value, None)
        original = getattr(media, "file_name", None)
        size = getattr(media, "file_size", 0)
        session["tg_msg"] = tg_msg
        session["title"] = original or f"telegram_media_{message_id}"
        session["size"] = size
        await self._enter_folder_pick(client, token)

    def _quality_markup(self, token, session):
        rows = [
            [
                types.InlineKeyboardButton(
                    "原画（最佳）",
                    callback_data=f"{_CB}:q:{token}:best",
                )
            ]
        ]
        for height in session.get("heights") or []:
            rows.append(
                [
                    types.InlineKeyboardButton(
                        f"{height}p",
                        callback_data=f"{_CB}:q:{token}:{height}",
                    )
                ]
            )
        rows.append(
            [
                types.InlineKeyboardButton(
                    "仅音频 (mp3)",
                    callback_data=f"{_CB}:q:{token}:audio",
                )
            ]
        )
        rows.append(
            [
                types.InlineKeyboardButton(
                    "📁 改文件夹",
                    callback_data=f"{_CB}:ff:{token}",
                )
            ]
        )
        rows.append(
            [
                types.InlineKeyboardButton(
                    "✖️ 取消任务", callback_data=f"{_CB}:cancel:{token}"
                )
            ]
        )
        return types.InlineKeyboardMarkup(rows)

    # ----------------------------------------------------------- state helpers

    async def _begin(self, token):
        """Start the download task for a session (idempotent)."""
        session = self.sessions.get(token)
        if not session or session.get("started") or session.get("start_pending"):
            return
        # 只登记“即将启动”；真正的 started 由 run_task 自行置位，
        # 否则 run_task 入口会被 started=True 挡掉，下载永远不会开始
        session["start_pending"] = True
        self._cancel_timer(token)
        self.deadlines.pop(token, None)
        user_id = session["user_id"]
        self.pending_name.pop(user_id, None)
        self.pending_quality.pop(user_id, None)
        self.pending_custom.pop(user_id, None)
        self.pending_folder.pop(user_id, None)
        self.pending_select.pop(user_id, None)
        task = self.app.loop.create_task(self.run_task(token))
        self.run_tasks[token] = task
        task.add_done_callback(
            lambda _t, tk=token: self.run_tasks.pop(tk, None)
        )

    def _schedule_expire(self, token):
        self._cancel_timer(token)
        self.timers[token] = self.app.loop.create_task(
            self._expire_quality(token)
        )
        self._wait_deadline(token, _QUALITY_EXPIRE_SECS, "expire")

    def _schedule_new_folder_expire(self, token):
        self._cancel_timer(token)
        self.timers[token] = self.app.loop.create_task(
            self._expire_new_folder(token)
        )
        self._wait_deadline(token, _FOLDER_NAME_WAIT_SECS, "newname")

    def _cancel_timer(self, token):
        task = self.timers.pop(token, None)
        if task and not task.done():
            task.cancel()

    async def _expire_quality(self, token):
        try:
            await asyncio.sleep(_QUALITY_EXPIRE_SECS)
        except asyncio.CancelledError:
            return
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        self.timers.pop(token, None)
        await self._cancel_session(
            token, "已取消（超时未选择），请重新发送链接"
        )

    async def _expire_new_folder(self, token):
        try:
            await asyncio.sleep(_FOLDER_NAME_WAIT_SECS)
        except asyncio.CancelledError:
            return
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        self.timers.pop(token, None)
        if not session.get("expect_new"):
            return
        await self._cancel_session(
            token, "已取消（等待输入新文件夹名超时），请重新发送链接"
        )

    def _mark_cancel_handled(self, token):
        """Task card already shows the final claim; watcher stays silent."""
        if token in self.cancel_watch_tasks:
            self.cancel_results[token] = "handled"

    def _record_cancel_saved(self, token):
        """The task saved anyway while a cancel request was pending."""
        if token in self.cancel_watch_tasks:
            self.cancel_results[token] = "saved"

    async def _start_cancel_watch(self, token, chat_id):
        """Guarantee a clear outcome message after a cancel request."""
        if token in self.cancel_watch_tasks:
            return
        self.cancel_watch_tasks[token] = self.app.loop.create_task(
            self._cancel_watch(token, chat_id)
        )

    async def _cancel_watch(self, token, chat_id):
        """Wait for the task to finish cleaning up, then send ONE final word.

        Outcome precedence:
        - "handled": the progress card already shows the final message;
        - "saved"  : cancel arrived too late, the file was saved anyway;
        - None     : normal successful cancel;
        - anything else: send that text verbatim.
        """
        waited = 0
        interim_sent = False
        while token in self.cancel_events:
            await asyncio.sleep(1)
            waited += 1
            if waited >= 8 and not interim_sent:
                interim_sent = True
                try:
                    await self.bot.send_message(
                        chat_id,
                        "⏳ 仍在停止下载线程…（文件较大或正在合并/转码时"
                        "需要几秒到几十秒，请稍候）",
                    )
                except Exception:  # noqa: BLE001
                    pass
            if waited >= 90:
                try:
                    await self.bot.send_message(
                        chat_id,
                        "❌ 取消失败：超过 90 秒下载线程仍未停止。\n"
                        "可能原因：网络卡死、正在合并/转码、或 NAS 连接中断。\n"
                        "可再等一两分钟——如果任务随后自动停止，上方进度消息"
                        "会显示最终结果；否则请重启机器人后重新下载。",
                    )
                except Exception:  # noqa: BLE001
                    pass
                self.cancel_watch_tasks.pop(token, None)
                return
        outcome = self.cancel_results.pop(token, None)
        self.cancel_watch_tasks.pop(token, None)
        if outcome == "handled":
            return
        if outcome == "saved":
            text = (
                "⚠️ 取消请求来得太晚：任务在你取消时其实已下载完成并保存"
                "（见上方保存消息）。如不需要该文件，请自行删除。"
            )
        elif outcome is None:
            text = (
                "✅ 取消成功：任务已停止并清理临时文件，"
                "可以重新发送链接/视频了"
            )
        else:
            text = str(outcome)
        try:
            await self.bot.send_message(chat_id, text)
        except Exception:  # noqa: BLE001
            pass

    async def _request_cancel(self, chat_id, session):
        """One cancel entry point with clear in-progress + final feedback.

        The user always gets one of: 取消成功 / 来得太晚已保存 /
        上传中无法取消 / 任务已结束 / 取消失败(含原因)。
        """
        token = session.get("token")
        if not session.get("started"):
            await self._cancel_session(token)
            try:
                await self.bot.send_message(
                    chat_id,
                    "✅ 取消成功：任务已清理，可以重新发送链接/视频了",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        if session.get("uploading"):
            try:
                await self.bot.send_message(
                    chat_id,
                    "⚠️ 正在上传 NAS，此阶段无法取消（避免 NAS 留下半截文件）。\n"
                    "上传很快完成，结束后会自动提示「已保存」；"
                    "不需要的话之后再删除即可。",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        if token in self.cancel_watch_tasks:
            try:
                await self.bot.send_message(
                    chat_id,
                    "⏳ 取消请求已收到，正在停止下载…请稍等最终结果",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        event = self.cancel_events.get(token)
        if not event:
            try:
                await self.bot.send_message(
                    chat_id,
                    "任务已结束或正在收尾，无法取消（请看上方的最终消息）",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        event.set()
        try:
            await self.bot.send_message(
                chat_id,
                "⏳ 正在取消下载…\n"
                "（文件较大或正在合并/转码时需要几秒到几十秒，"
                "完成后会明确告诉你结果）",
            )
        except Exception:  # noqa: BLE001
            pass
        await self._start_cancel_watch(token, chat_id)

    async def _cancel_session(self, token, text="✅ 取消成功：任务已清理，可以重新发送链接/视频了"):
        """Cancel a session that has not started downloading yet."""
        session = self.sessions.get(token)
        if session:
            logger.info(f"urldl cancel session {token}: {text}")
            self._cancel_timer(token)
            self.deadlines.pop(token, None)
            user_id = session["user_id"]
            self.pending_name.pop(user_id, None)
            self.pending_quality.pop(user_id, None)
            self.pending_custom.pop(user_id, None)
            self.pending_folder.pop(user_id, None)
            self.pending_select.pop(user_id, None)
            self.pending_conflict.pop(user_id, None)
            await self._edit_status(session, text)
            self.sessions.pop(token, None)
            self.status_edit_locks.pop(token, None)
            shutil.rmtree(os.path.join(self.tmp_root, token), ignore_errors=True)

    async def _edit_status(self, session, text):
        if not session:
            return False
        token = session.get("token")
        # Mark the card terminal before waiting for the edit lock. A yt-dlp
        # hook may already have queued an older progress coroutine from its
        # worker thread; that coroutine must not run after this final update.
        session["_status_final"] = True
        lock = self._status_edit_lock(token)
        async with lock:
            status = session.get("status_msg")
            cache = self.progress_cache.get(token)
            if status is None and cache is None:
                return False
            chat_id = session.get("chat_id")
            try:
                # 终态消息不能被“限流静默跳过”：先等冷却结束再发
                left = self._flood_left(chat_id)
                if left:
                    await asyncio.sleep(left)
                if cache is not None:
                    await self.bot.edit_message_text(cache[0], cache[1], text)
                else:
                    await status.edit_text(text)
                return True
            except FloodWait as exc:
                # 仍被限流 → 记录冷却；下面的补发前还会再等一次
                self._note_flood(chat_id, getattr(exc, "value", 0.0))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "status edit failed token={}: {}: {}",
                    token,
                    type(exc).__name__,
                    exc,
                )
            # 编辑失败（消息过旧/被删/仍限流）→ 等冷却结束后补发一条，
            # 确保用户看得到最终结果
            left = self._flood_left(chat_id)
            if left:
                await asyncio.sleep(left)
            try:
                msg = await self.bot.send_message(chat_id, text)
                session["status_msg"] = msg
                if cache is not None:
                    self.progress_cache[token] = (
                        chat_id, msg.id, cache[2], time.monotonic()
                    )
                return True
            except FloodWait as exc:
                self._note_flood(chat_id, getattr(exc, "value", 0.0))
                return False
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "status fallback send failed token={}: {}: {}",
                    token,
                    type(exc).__name__,
                    exc,
                )
            return False

    # -------------------------------------------------------------- download

    async def _stage(self, token, text, cancellable=True):
        """Progress-card update for stage transitions (shares the edit throttle)."""
        lock = self._status_edit_lock(token)
        async with lock:
            cache = self.progress_cache.get(token)
            if not cache:
                return
            chat_id, msg_id, _, last_edit = cache
            now = time.monotonic()
            if self._flood_left(chat_id) or now - last_edit < _REPORT_MIN_INTERVAL:
                return
            self.progress_cache[token] = (chat_id, msg_id, -1.0, now)
            markup = self._cancel_markup(token) if cancellable else None
            try:
                await self.bot.edit_message_text(
                    chat_id, msg_id, text, reply_markup=markup
                )
            except FloodWait as exc:
                # 限流窗口内不要补发新消息，等冷却后的下一次更新即可
                self._note_flood(chat_id, getattr(exc, "value", 0.0))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "stage edit failed token={}: {}: {}",
                    token,
                    type(exc).__name__,
                    exc,
                )
                # 原进度消息不可用（被删/过旧）→ 补发一条，保证反馈不断
                try:
                    msg = await self.bot.send_message(
                        chat_id, text, reply_markup=markup
                    )
                    self.progress_cache[token] = (
                        chat_id, msg.id, -1.0, time.monotonic()
                    )
                    session = self.sessions.get(token)
                    if session is not None:
                        session["status_msg"] = msg
                except FloodWait as send_exc:
                    self._note_flood(chat_id, getattr(send_exc, "value", 0.0))
                except Exception as send_exc:  # noqa: BLE001
                    logger.warning(
                        "stage fallback send failed token={}: {}: {}",
                        token,
                        type(send_exc).__name__,
                        send_exc,
                    )

    async def run_task(self, token):
        """Run the download + move task, one task at a time."""
        session = self.sessions.get(token)
        if not session or session.get("started"):
            return
        session.pop("start_pending", None)
        session["started"] = True
        logger.info(f"run_task start {token} kind={session.get('kind')}")
        user_id = session["user_id"]
        chat_id = session["chat_id"]
        event = asyncio.Event()
        self.cancel_events[token] = event
        # 绑定本次运行持有的信号量：即使重注册换了新信号量，
        # 收尾也只归还自己申请的那一个
        sem = self.sem
        queue_msg = None
        prompt = session.get("status_msg")
        acquired = False
        try:
            if sem.locked():
                try:
                    queue_msg = await self.bot.send_message(
                        chat_id,
                        "⏳ 前面还有下载任务，你已进入排队，完成后自动开始",
                        reply_markup=self._cancel_markup(token),
                    )
                    # From now on this newer queue card is the canonical status
                    # target, including if the user cancels before acquiring.
                    session["status_msg"] = queue_msg
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "queue status send failed token={}: {}: {}",
                        token,
                        type(exc).__name__,
                        exc,
                    )
                    queue_msg = None
            while not acquired:
                try:
                    # 每秒轮询一次，排队中也能及时响应“取消”
                    await asyncio.wait_for(sem.acquire(), timeout=1.0)
                    acquired = True
                except asyncio.TimeoutError:
                    if event.is_set() or token not in self.sessions:
                        break
            if not acquired:
                status_updated = await self._edit_status(
                    session, "✅ 取消成功：任务已清理，可以重新发送链接/视频了"
                )
                if event.is_set() and status_updated:
                    self._mark_cancel_handled(token)
                return
            status = session.get("status_msg")
            if queue_msg is not None:
                # 排队结束：进度消息改用排队消息，原确认消息转为静态说明
                status = queue_msg
                if prompt is not None:
                    try:
                        await prompt.edit_text(
                            self._target_text(session)
                            + "\n\n👇 下方排队消息会实时更新进度"
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "queue prompt edit failed token={}: {}: {}",
                            token,
                            type(exc).__name__,
                            exc,
                        )
            if status is None:
                try:
                    status = await self.bot.send_message(
                        chat_id,
                        self._download_plan_text(session),
                        reply_markup=self._cancel_markup(token),
                    )
                    session["status_msg"] = status
                except Exception:  # noqa: BLE001
                    status = None
            if status is not None:
                self.progress_cache[token] = (
                    chat_id, status.id, 0.0, 0.0
                )
            try:
                if session.get("multi"):
                    await self._download_multi(token, session, event)
                    return
                if not await self._preflight_conflict(token, session, event):
                    return
                size_note = ""
                if session.get("kind") == "yt":
                    est = (session.get("size_estimates") or {}).get(
                        str(session.get("quality") or "best")
                    )
                    if est:
                        size_note = f"（预计 {est}）"
                else:
                    size_hint = session.get("size")
                    if size_hint:
                        size_note = f"（{_format_size(size_hint)}）"
                await self._stage(
                    token,
                    self._download_plan_text(
                        session, head=f"⬇️ 开始下载{size_note}…"
                    ),
                )
                local_path, final_name, size = await self._download(
                    token, session, event, status
                )
                if event.is_set():
                    raise _TaskCancelled()
                local_path = await self._resolve_conflict(
                    token, session, event, local_path
                )
                await self._stage(
                    token,
                    "✅ 本地下载完成，正在上传到 NAS…"
                    "\n（上传过程中无法取消，避免 NAS 残留半截文件）",
                    cancellable=False,
                )
                session["uploading"] = True
                try:
                    nas = await self._move_to_nas(
                        local_path,
                        session.get("folder") or "",
                        token=token,
                    )
                finally:
                    session["uploading"] = False
                text = f"✅ 已保存到 NAS：\n`{nas}`\n大小：{_format_size(size)}"
                await self._edit_status(session, text)
                self._record_cancel_saved(token)
            except asyncio.CancelledError:
                raise
            except _ConflictCancelled:
                if await self._edit_status(
                    session,
                    "✅ 取消成功：已取消保存，服务器临时文件已自动清理",
                ):
                    self._mark_cancel_handled(token)
            except _TaskCancelled:
                done = session.get("multi_done") or 0
                if session.get("multi") and done:
                    text = (
                        f"✅ 已取消：前 {done} 个文件已保存到 NAS"
                        "（见上方进度），剩余已停止并清理。\n"
                        "可以重新发送链接/视频了"
                    )
                else:
                    text = (
                        "✅ 取消成功：已停止下载，临时文件已清理，可以重新发送"
                    )
                if await self._edit_status(session, text):
                    self._mark_cancel_handled(token)
            except Exception as exc:  # noqa: BLE001
                if event.is_set():
                    if await self._edit_status(
                        session,
                        f"❌ 取消过程中任务出错：\n{type(exc).__name__}: {exc}\n"
                        "未能保存，临时文件已清理，可以重新发送链接/视频重试",
                    ):
                        self._mark_cancel_handled(token)
                else:
                    await self._edit_status(
                        session,
                        f"❌ 下载失败：\n{type(exc).__name__}: {exc}\n"
                        "可直接重新发送链接或视频重试",
                    )
                logger.exception("url download failed: {}", exc)
        finally:
            if acquired:
                sem.release()
            logger.info(f"run_task end {token}")
            self.pending_name.pop(user_id, None)
            self.pending_quality.pop(user_id, None)
            self.pending_custom.pop(user_id, None)
            self.pending_folder.pop(user_id, None)
            self.pending_select.pop(user_id, None)
            self.pending_conflict.pop(user_id, None)
            self.cancel_events.pop(token, None)
            if (
                token in self.cancel_results
                and token not in self.cancel_watch_tasks
            ):
                self.cancel_results.pop(token, None)
            self.progress_cache.pop(token, None)
            shutil.rmtree(os.path.join(self.tmp_root, token), ignore_errors=True)
            self.sessions.pop(token, None)
            self.status_edit_locks.pop(token, None)

    async def _resolve_conflict(self, token, session, event, local_path):
        """Ask again (safety net) when the NAS file still collides by name."""
        rel = session.get("folder") or ""
        name = os.path.basename(local_path)
        # 下载前已经就这个名字做过决定（覆盖），不必再问
        if session.get("conflict_decided") == name:
            return local_path
        if not await self._nas_file_exists(rel, name):
            return local_path
        decision = await self._ask_conflict(
            token, session, event, rel, name
        )
        if decision == "cancel":
            raise _ConflictCancelled()
        if decision == "rename":
            new_name = await self._unique_nas_name(rel, name)
            new_local = os.path.join(os.path.dirname(local_path), new_name)
            os.replace(local_path, new_local)
            local_path = new_local
            session["final_name"] = os.path.splitext(new_name)[0]
        return local_path

    async def _ask_conflict(self, token, session, event, rel, name):
        """Show overwrite / auto-rename / cancel and wait for the choice."""
        user_id = session["user_id"]
        fut = self.app.loop.create_future()
        session["conflict_fut"] = fut
        self.pending_conflict[user_id] = token
        target = self._nas_display(rel, name)
        text = (
            f"⚠️ 目标文件夹里已有同名文件：\n`{target}`\n\n"
            "你想怎么处理？\n"
            "✏️ 自动改名 —— 保留旧文件，新文件自动加 _1、_2 存进去\n"
            "♻️ 覆盖保存 —— 用新下载的替换 NAS 上的旧文件\n"
            "🚫 取消保存 —— 本次不保存，自动清理临时文件\n\n"
            f"⏳ {_CONFLICT_WAIT_SECS} 秒内不选择 → 自动改名保存"
        )
        markup = types.InlineKeyboardMarkup(
            [
                [
                    types.InlineKeyboardButton(
                        "✏️ 自动改名",
                        callback_data=f"{_CB}:c:{token}:rename",
                    ),
                    types.InlineKeyboardButton(
                        "♻️ 覆盖保存",
                        callback_data=f"{_CB}:c:{token}:overwrite",
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        "🚫 取消保存",
                        callback_data=f"{_CB}:c:{token}:cancel",
                    )
                ],
            ]
        )
        await self._edit_with_markup(self.bot, session, text, markup)
        timeout = self.app.loop.create_task(asyncio.sleep(_CONFLICT_WAIT_SECS))
        cancel_wait = self.app.loop.create_task(event.wait())
        try:
            await asyncio.wait(
                {fut, timeout, cancel_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            timeout.cancel()
            cancel_wait.cancel()
        if fut.done():
            decision = fut.result()
        elif event.is_set():
            decision = "cancel"
        else:
            decision = "rename"  # 超时：保留 NAS 上的旧文件，自动改名
        self.pending_conflict.pop(user_id, None)
        session.pop("conflict_fut", None)
        return decision

    def _finalize_local(self, local_path, name_with_ext):
        """Rename a finished download to its final name (no-op when equal)."""
        final_path = os.path.join(os.path.dirname(local_path), name_with_ext)
        if os.path.abspath(final_path) == os.path.abspath(local_path):
            return local_path
        if os.path.exists(final_path):
            final_path = self._unique_path(final_path)
        os.replace(local_path, final_path)
        return final_path

    async def _download(self, token, session, event, status):
        if session["kind"] == "yt":
            return await self._download_yt(token, session, event, status)
        return await self._download_tg(token, session, event, status)

    async def _download_yt(self, token, session, event, status):
        tmp_dir = os.path.join(self.tmp_root, token)
        os.makedirs(tmp_dir, exist_ok=True)
        quality = session.get("quality", "best")
        audio = quality == "audio"
        opts = {
            "format": _format_selector(quality),
            "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
        }
        if not audio:
            opts["merge_output_format"] = "mp4"
        else:
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                }
            ]
        if self.cfg.get("cookies_file"):
            opts["cookiefile"] = self.cfg["cookies_file"]

        def hook(d):
            if event.is_set():
                raise _TaskCancelled()
            current_file = d.get("filename") or ""
            if current_file and current_file != stream_seen["file"]:
                # yt-dlp reports 0..100 per stream/file; reset the bar between them.
                stream_seen["file"] = current_file
                self._reset_progress(token)
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                if total > 0:
                    pct = done * 100.0 / total
                    total_s = _format_size(total)
                    left_s = _format_size(max(0.0, total - done))
                else:
                    pct = 0.0
                    total_s = ""
                    left_s = ""
                info = d.get("info_dict") or {}
                vcodec = info.get("vcodec") or "none"
                acodec = info.get("acodec") or "none"
                if vcodec != "none" and acodec == "none":
                    label = "⬇️ 下载视频流"
                elif acodec != "none" and vcodec == "none":
                    label = "⬇️ 下载音频流"
                else:
                    label = "⬇️ 下载中"
                asyncio.run_coroutine_threadsafe(
                    self._report(
                        token,
                        pct,
                        _format_size(done),
                        total_s,
                        _format_speed(d.get("speed") or 0),
                        _format_eta(d.get("eta")),
                        label=label,
                        left_s=left_s,
                    ),
                    self.app.loop,
                )
            elif d.get("status") == "finished":
                asyncio.run_coroutine_threadsafe(
                    self._report(
                        token,
                        100.0,
                        "",
                        "",
                        "",
                        "",
                        label="⚙️ 正在合并/转码…",
                    ),
                    self.app.loop,
                )

        stream_seen = {"file": ""}
        opts["progress_hooks"] = [hook]

        def run():
            if YoutubeDL is None:
                raise RuntimeError("yt-dlp not installed")
            with YoutubeDL(opts) as ydl:
                return ydl.download([session["url"]])

        if event.is_set():
            raise _TaskCancelled()
        await asyncio.to_thread(run)
        if event.is_set():
            raise _TaskCancelled()
        files = [
            os.path.join(tmp_dir, f)
            for f in os.listdir(tmp_dir)
            if os.path.isfile(os.path.join(tmp_dir, f))
        ]
        if not files:
            raise RuntimeError("下载完成但没有找到文件")
        local_path = max(files, key=os.path.getsize)
        ext = os.path.splitext(local_path)[1] or (".mp3" if audio else ".mp4")
        title = session.get("title") or "video"
        final_name = session.get("final_name") or _strip_ext(
            sanitize_filename(title)
        )
        final_name = sanitize_filename(final_name)
        preset = session.get("preset_name") or ""
        if preset and os.path.splitext(preset)[1].lower() == ext.lower():
            final_name = os.path.splitext(preset)[0]
        final_path = self._finalize_local(local_path, final_name + ext)
        session["final_name"] = os.path.splitext(
            os.path.basename(final_path)
        )[0]
        return final_path, os.path.basename(final_path), os.path.getsize(final_path)

    async def _download_tg(
        self, token, session, event, status, tg_msg_override=None
    ):
        tmp_dir = os.path.join(self.tmp_root, token)
        os.makedirs(tmp_dir, exist_ok=True)
        last = {"t": 0.0, "b": 0, "pct": 0.0, "speed": 0.0, "primed": False}
        tg_msg = tg_msg_override or session.get("tg_msg")
        if tg_msg is None:
            raise RuntimeError("无法获取该消息")
        if event.is_set():
            raise _TaskCancelled()
        try:
            tg_msg = await retry(
                self.client.get_messages, args=(tg_msg.chat.id, tg_msg.id)
            )
        except Exception:  # noqa: BLE001
            tg_msg = session.get("tg_msg")
        if not getattr(tg_msg, "media", None):
            raise RuntimeError("无法获取该消息的媒体")
        media = getattr(tg_msg, tg_msg.media.value, None)
        size = getattr(media, "file_size", 0) or 0
        seq = session.get("seq") or ""

        async def progress(current, total):
            if event.is_set():
                raise _TaskCancelled()
            pct = current * 100.0 / total if total else 0.0
            now = asyncio.get_event_loop().time()
            if not last["primed"]:
                # First sample: remember where we are, no speed estimate yet.
                last.update(t=now, b=current, pct=pct, primed=True)
                return
            if (
                now - last["t"] >= 2.0
                or pct - last["pct"] >= 10
                or pct >= 100
            ):
                speed = last["speed"]
                if now - last["t"] >= 1.0:
                    inst = (current - last["b"]) / (now - last["t"])
                    speed = inst if speed <= 0 else speed * 0.6 + inst * 0.4
                last["t"] = now
                last["b"] = current
                last["pct"] = pct
                last["speed"] = speed
                eta = (total - current) / speed if speed > 0 else 0
                left = (total - current) if total > 0 else 0
                await self._report(
                    token,
                    pct,
                    _format_size(current),
                    _format_size(total),
                    _format_speed(speed),
                    _format_eta(eta),
                    label=f"{seq}⬇️ 下载中",
                    left_s=_format_size(max(0, left)),
                )

        await self.client.download_media(
            tg_msg,
            file_name=tmp_dir + "/",
            progress=progress,
        )
        if event.is_set():
            raise _TaskCancelled()
        files = [
            os.path.join(tmp_dir, f)
            for f in os.listdir(tmp_dir)
            if os.path.isfile(os.path.join(tmp_dir, f))
        ]
        if not files:
            raise RuntimeError("下载完成但没有找到文件")
        local_path = files[0]
        ext = os.path.splitext(local_path)[1]
        title = session.get("title") or "telegram_media"
        final_name = session.get("final_name") or _strip_ext(
            sanitize_filename(title)
        )
        final_name = sanitize_filename(final_name)
        preset = session.get("preset_name") or ""
        if preset and os.path.splitext(preset)[1].lower() == ext.lower():
            final_name = os.path.splitext(preset)[0]
        final_path = self._finalize_local(local_path, final_name + ext)
        session["final_name"] = os.path.splitext(
            os.path.basename(final_path)
        )[0]
        return final_path, os.path.basename(final_path), os.path.getsize(final_path)

    def _reset_progress(self, token):
        """Allow a fresh 0..100 cycle (e.g. yt-dlp switches to another stream)."""
        app_loop = getattr(self.app, "loop", None)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if app_loop is not None and running_loop is not app_loop:
            # yt-dlp invokes hooks in the asyncio.to_thread() worker. Keep the
            # compound cache read/write on the owning event-loop thread so it
            # cannot restore an obsolete status-message id during a fallback.
            app_loop.call_soon_threadsafe(self._reset_progress, token)
            return
        cache = self.progress_cache.get(token)
        if cache:
            self.progress_cache[token] = (cache[0], cache[1], -1.0, 0.0)

    async def _report(
        self,
        token,
        pct,
        done_s="",
        total_s="",
        speed_s="",
        eta_s="",
        label="⬇️ 下载中",
        cancellable=True,
        left_s="",
    ):
        """Throttled progress-message update with a visual progress bar."""
        # A yt-dlp hook can reach the loop after run_task() has cleaned up.
        # Do not recreate per-task state for such a late callback.
        if token not in self.progress_cache:
            return
        lock = self._status_edit_lock(token)
        async with lock:
            cache = self.progress_cache.get(token)
            if not cache:
                return
            session = self.sessions.get(token)
            if session is not None and session.get("_status_final"):
                return
            chat_id, msg_id, _, last_edit = cache
            pct = max(0.0, min(100.0, float(pct or 0.0)))
            now = time.monotonic()
            # 编辑消息同样计入群聊 ~20 条/分钟的限流：即使百分比大幅
            # 跳变也遵守固定间隔；触发 FloodWait 后整段跳过，等冷却结束
            # 的下一次回调再更新，避免“失败 → 立刻补发 → 继续失败”
            # 把 Telegram 要求的等待时间越拖越长。
            if self._flood_left(chat_id) or (
                pct < 100.0 and now - last_edit < _REPORT_MIN_INTERVAL
            ):
                return
            self.progress_cache[token] = (chat_id, msg_id, pct, now)
            if total_s or pct >= 100:
                lines = [f"{label}　{pct:.1f}%", f"[{_progress_bar(pct)}]"]
                if done_s or total_s:
                    size_line = f"📦 {done_s} / {total_s}"
                    if left_s and pct < 100:
                        size_line += f"　⏳ 还剩 {left_s}"
                    lines.append(size_line)
            else:
                lines = [label]
                if done_s:
                    lines.append(f"📦 已接收 {done_s}（总大小未知，实时刷新）")
            if speed_s or eta_s:
                lines.append(f"🚀 {speed_s}　⏱ {eta_s}")
            if session is not None:
                plan = self._download_plan_text(session)
                extra = plan.split("\n", 1)
                if len(extra) > 1:
                    lines.extend(extra[1].split("\n"))
            markup = self._cancel_markup(token) if cancellable else None
            body = "\n".join(lines)
            try:
                await self.bot.edit_message_text(
                    chat_id,
                    msg_id,
                    body,
                    reply_markup=markup,
                )
            except FloodWait as exc:
                self._note_flood(chat_id, getattr(exc, "value", 0.0))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "progress edit failed token={}: {}: {}",
                    token,
                    type(exc).__name__,
                    exc,
                )
                # 编辑失败（消息被删/过旧，非限流）→ 补发新进度消息并继续更新
                try:
                    msg = await self.bot.send_message(
                        chat_id, body, reply_markup=markup
                    )
                    self.progress_cache[token] = (
                        chat_id, msg.id, pct, time.monotonic()
                    )
                    if session is not None:
                        session["status_msg"] = msg
                except FloodWait as send_exc:
                    self._note_flood(chat_id, getattr(send_exc, "value", 0.0))
                except Exception as send_exc:  # noqa: BLE001
                    logger.warning(
                        "progress fallback send failed token={}: {}: {}",
                        token,
                        type(send_exc).__name__,
                        send_exc,
                    )

    async def _download_multi(self, token, session, event):
        """Download several forwarded media one by one to the chosen folder.

        Every item is downloaded -> auto-renamed on NAS collision -> uploaded
        immediately, so the temp dir never holds the whole batch at once.
        """
        msgs = sorted(
            (session.get("media_msgs") or []), key=lambda m: m.id
        )
        n = len(msgs)
        rel = session.get("folder") or ""
        base = session.get("final_name")
        results = []
        try:
            for i, m in enumerate(msgs, 1):
                if event.is_set():
                    raise _TaskCancelled()
                seq = f"[{i}/{n}] "
                session["seq"] = seq
                session["cur_i"] = i
                session["cur_n"] = n
                display = self._item_display_name(m)
                session["cur_display"] = os.path.basename(display)
                session["title"] = display
                session["tg_msg"] = m
                if base:
                    # 用户给了一个基名 -> 旅行_1 / 旅行_2 ...
                    session["final_name"] = sanitize_filename(f"{base}_{i}")
                else:
                    # 按原名保存
                    session.pop("final_name", None)
                await self._stage(
                    token,
                    f"{seq}⬇️ 下载中 第 {i}/{n} 个：`{os.path.basename(display)[:70]}`\n"
                    f"📁 保存到：`{self._nas_display(rel)}`",
                )
                try:
                    local_path, fname, size = await self._download_tg(
                        token, session, event, None
                    )
                except _TaskCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"第 {i}/{n} 个下载失败：{type(exc).__name__}: {exc}"
                    ) from exc
                if event.is_set():
                    raise _TaskCancelled()
                # 批量不逐个弹窗：NAS 同名时自动加 _1 保留两份
                if await self._nas_file_exists(rel, fname):
                    new_name = await self._unique_nas_name(rel, fname)
                    new_local = os.path.join(
                        os.path.dirname(local_path), new_name
                    )
                    os.replace(local_path, new_local)
                    local_path = new_local
                    fname = new_name
                session["final_name"] = os.path.splitext(fname)[0]
                session["uploading"] = True
                try:
                    await self._move_to_nas(
                        local_path, rel, token=token, seq=seq
                    )
                finally:
                    session["uploading"] = False
                results.append((fname, size))
                session["multi_done"] = len(results)
        finally:
            session.pop("seq", None)
            session.pop("cur_i", None)
            session.pop("cur_n", None)
            session.pop("cur_display", None)
            session.pop("final_name", None)
        total = sum(s for _, s in results)
        total_orig = session.get("batch_total") or n
        shown = "\n".join(
            f"`{name}`　{_format_size(size)}"
            for name, size in results[:8]
        )
        more = f"\n…还有 {len(results) - 8} 个" if len(results) > 8 else ""
        skip_note = ""
        if n < total_orig:
            skip_note = (
                f"（勾选了 {n} 个下载，其余 {total_orig - n} 个未下载）\n"
            )
        text = (
            f"✅ 已保存 {len(results)}/{total_orig} 个文件到：\n"
            f"`{self._nas_display(rel)}`\n{skip_note}{shown}{more}\n"
            f"合计大小：{_format_size(total)}"
        )
        await self._edit_status(session, text)
        self._record_cancel_saved(token)

    async def _move_to_nas(self, local_path, rel="", token=None, seq=""):
        """rclone move a file into NAS 远程下载[/rel]; live upload progress.

        Not cancellable mid-transfer (an interrupted rclone move could leave a
        partial file on the NAS) - the UI says so before the transfer starts.
        """
        rel = _clean_rel_path(rel)
        dest_dir = self._remote_dir(rel)
        if rel:
            mk = await self._rclone_run(["mkdir", dest_dir])
            if mk.returncode != 0:
                raise RuntimeError(
                    "创建 NAS 文件夹失败: "
                    + (mk.stderr or mk.stdout or "")[:300]
                )
        cmd = [self.rclone_bin, "move", local_path, dest_dir + "/"]
        if self.cfg.get("rclone_config"):
            cmd += ["--config", self.cfg["rclone_config"]]
        cmd += ["--log-level", "NOTICE", "--stats", "1s", "--stats-one-line"]
        total = os.path.getsize(local_path)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        last_pct = -1
        tail = ""
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        proc.stderr.readline(), timeout=60
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError("上传 NAS 超时（60 秒无响应）")
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                tail = line
                m = _RCLONE_PCT_RE.search(line)
                if not m:
                    continue
                pct = int(m.group(1))
                if pct <= last_pct:
                    continue
                last_pct = pct
                if token:
                    done = total * pct / 100.0
                    await self._report(
                        token,
                        pct,
                        _format_size(done),
                        _format_size(total),
                        label=f"{seq}⬆️ 上传 NAS",
                        cancellable=False,
                        left_s=_format_size(max(0.0, total - done)),
                    )
            code = await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if code != 0:
            raise RuntimeError(f"上传 NAS 失败: {(tail or '未知错误')[:300]}")
        if token:
            await self._report(
                token,
                100.0,
                _format_size(total),
                _format_size(total),
                label=f"{seq}⬆️ 上传 NAS",
                cancellable=False,
            )
        return self._nas_display(rel, os.path.basename(local_path))

    @staticmethod
    def _unique_path(path: str) -> str:
        if not os.path.exists(path):
            return path
        root, ext = os.path.splitext(path)
        idx = 1
        while os.path.exists(f"{root}_{idx}{ext}"):
            idx += 1
        return f"{root}_{idx}{ext}"


_url_downloader = UrlDownloader()


def register_url_downloader(app, bot, client, cfg, allowed_user_ids):
    """Register the interactive url downloader on the bot."""
    return _url_downloader.register(app, bot, client, cfg, allowed_user_ids)

"""Regression tests for URL downloader progress reporting."""

import asyncio
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from pyrogram.errors import FloodWait, MessageNotModified

from module import url_downloader as url_mod
from module.url_downloader import UrlDownloader


class _Message:
    def __init__(self, bot, chat_id, message_id):
        self._bot = bot
        self.chat = type("Chat", (), {"id": chat_id})()
        self.id = message_id

    async def edit_text(self, text, **kwargs):
        return await self._bot.edit_message_text(self.chat.id, self.id, text, **kwargs)


class _DelayedBot:
    def __init__(self):
        self.progress_started = asyncio.Event()
        self.release_progress = asyncio.Event()
        self.visible_text = {1: "initial"}

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        del chat_id, kwargs
        if text.startswith("stale progress"):
            self.progress_started.set()
            await self.release_progress.wait()
        self.visible_text[message_id] = text

    async def send_message(self, chat_id, text, **kwargs):
        del kwargs
        message_id = max(self.visible_text, default=0) + 1
        self.visible_text[message_id] = text
        return _Message(self, chat_id, message_id)


class _FallbackBot:
    def __init__(self):
        self.visible_text = {1: "initial"}
        self._failed_original_edit = False

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        del chat_id, kwargs
        if message_id == 1 and not self._failed_original_edit:
            self._failed_original_edit = True
            raise RuntimeError("original message can no longer be edited")
        self.visible_text[message_id] = text

    async def send_message(self, chat_id, text, **kwargs):
        del kwargs
        message_id = max(self.visible_text, default=0) + 1
        self.visible_text[message_id] = text
        return _Message(self, chat_id, message_id)


class _ThreadRecordingCache(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_threads = []

    def __setitem__(self, key, value):
        self.write_threads.append(threading.get_ident())
        return super().__setitem__(key, value)


class _QueueBot:
    def __init__(self, downloader, token):
        self.downloader = downloader
        self.token = token
        self.visible_text = {1: "confirmation"}

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        del chat_id, kwargs
        if message_id == 2:
            raise RuntimeError("queue card can no longer be edited")
        self.visible_text[message_id] = text

    async def send_message(self, chat_id, text, **kwargs):
        del kwargs
        message_id = max(self.visible_text, default=0) + 1
        self.visible_text[message_id] = text
        if message_id == 2:
            self.downloader.cancel_events[self.token].set()
        return _Message(self, chat_id, message_id)


class _BrokenQueueBot(_QueueBot):
    async def send_message(self, chat_id, text, **kwargs):
        if len(self.visible_text) > 1:
            raise RuntimeError("fallback send also failed")
        return await super().send_message(chat_id, text, **kwargs)


class _RecordingBot:
    def __init__(self):
        self.edits = 0
        self.sends = 0
        self.visible_text = {}

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        del chat_id, kwargs
        self.edits += 1
        self.visible_text[message_id] = text

    async def send_message(self, chat_id, text, **kwargs):
        del chat_id, text, kwargs
        self.sends += 1
        raise AssertionError("unexpected fallback send")


class _FloodOnceBot:
    """edit_message_text raises one FloodWait, then records success."""

    def __init__(self, wait):
        self.wait = wait
        self.edits = 0
        self.sends = 0
        self.visible_text = {}

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        del chat_id, kwargs
        self.edits += 1
        if self.edits == 1:
            raise FloodWait(self.wait)
        self.visible_text[message_id] = text

    async def send_message(self, chat_id, text, **kwargs):
        del chat_id, kwargs
        self.sends += 1
        self.visible_text["send:" + str(self.sends)] = text
        return _Message(self, 100, 100 + self.sends)


class _NotModifiedBot:
    """edit_message_text always answers MESSAGE_NOT_MODIFIED."""

    def __init__(self):
        self.edits = 0
        self.sends = 0

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        del chat_id, message_id, text, kwargs
        self.edits += 1
        raise MessageNotModified(
            'Telegram says: [400 MESSAGE_NOT_MODIFIED] - '
            'The message was not modified (caused by "messages.EditMessage")'
        )

    async def send_message(self, chat_id, text, **kwargs):
        del chat_id, text, kwargs
        self.sends += 1
        raise AssertionError("fallback send must not run for MESSAGE_NOT_MODIFIED")


class _UnavailableSemaphore:
    @staticmethod
    def locked():
        return True

    @staticmethod
    async def acquire():
        raise asyncio.TimeoutError

    @staticmethod
    def release():
        raise AssertionError("unavailable semaphore must not be released")


class UrlDownloaderProgressTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _downloader(bot):
        downloader = UrlDownloader()
        downloader.bot = bot
        token = "progress-token"
        session = {
            "token": token,
            "chat_id": 100,
            "status_msg": _Message(bot, 100, 1),
        }
        downloader.sessions[token] = session
        downloader.progress_cache[token] = (100, 1, -1.0, 0.0)
        return downloader, token, session

    async def test_final_status_cannot_be_overwritten_by_inflight_progress(self):
        """Removing status-edit serialization lets stale progress win."""
        bot = _DelayedBot()
        downloader, token, session = self._downloader(bot)

        stale = asyncio.create_task(
            downloader._report(token, 25, label="stale progress")
        )
        await bot.progress_started.wait()
        final = asyncio.create_task(
            downloader._edit_status(session, "saved successfully")
        )
        await asyncio.sleep(0)
        bot.release_progress.set()
        await asyncio.gather(stale, final)

        self.assertEqual("saved successfully", bot.visible_text[1])

    async def test_final_status_updates_the_replacement_progress_message(self):
        """Forgetting the fallback anchor leaves the newest card stale."""
        bot = _FallbackBot()
        downloader, token, session = self._downloader(bot)

        await downloader._report(token, 25, label="current progress")
        self.assertTrue(bot.visible_text[2].startswith("current progress"))

        await downloader._edit_status(session, "saved successfully")

        self.assertEqual("saved successfully", bot.visible_text[2])

    async def test_progress_queued_before_completion_is_ignored_after_final_status(
        self,
    ):
        """Removing the terminal guard lets a queued callback replace success."""
        bot = _DelayedBot()
        bot.release_progress.set()
        downloader, token, session = self._downloader(bot)

        lock = downloader._status_edit_lock(token)
        await lock.acquire()
        stale = asyncio.create_task(
            downloader._report(token, 25, label="stale progress")
        )
        await asyncio.sleep(0)
        final = asyncio.create_task(
            downloader._edit_status(session, "saved successfully")
        )
        await asyncio.sleep(0)
        lock.release()
        await asyncio.gather(stale, final)

        self.assertEqual("saved successfully", bot.visible_text[1])

    async def test_stream_reset_returns_cache_mutation_to_application_loop(self):
        """Direct worker-thread resets can restore an obsolete message id."""
        downloader = UrlDownloader()
        token = "progress-token"
        loop_thread = threading.get_ident()
        downloader.app = SimpleNamespace(loop=asyncio.get_running_loop())
        downloader.progress_cache = _ThreadRecordingCache({token: (100, 2, 50.0, 10.0)})

        await asyncio.to_thread(downloader._reset_progress, token)
        await asyncio.sleep(0)

        self.assertEqual(loop_thread, downloader.progress_cache.write_threads[-1])
        self.assertEqual((100, 2, -1.0, 0.0), downloader.progress_cache[token])

    async def test_queued_cancel_finalizes_the_latest_status_card(self):
        """Editing an older prompt must not hide a failed queue-card update."""
        downloader = UrlDownloader()
        token = "queued-token"
        bot = _QueueBot(downloader, token)
        downloader.bot = bot
        downloader.sem = _UnavailableSemaphore()
        downloader.tmp_root = "/tmp/url-downloader-tests"
        downloader.sessions[token] = {
            "token": token,
            "user_id": 200,
            "chat_id": 100,
            "status_msg": _Message(bot, 100, 1),
        }

        await downloader.run_task(token)

        latest_message_id = max(bot.visible_text)
        self.assertTrue(bot.visible_text[latest_message_id].startswith("✅ 取消成功"))

    async def test_late_progress_after_cleanup_does_not_recreate_task_lock(self):
        """Creating a lock before checking task state leaks completed tokens."""
        downloader = UrlDownloader()
        downloader.bot = _DelayedBot()

        await downloader._report("completed-token", 50)

        self.assertNotIn("completed-token", downloader.status_edit_locks)

    async def test_failed_queued_cancel_status_keeps_watcher_fallback_enabled(self):
        """Silencing the watcher after two send failures loses final feedback."""
        downloader = UrlDownloader()
        token = "queued-token"
        bot = _BrokenQueueBot(downloader, token)
        downloader.bot = bot
        downloader.sem = _UnavailableSemaphore()
        downloader.tmp_root = "/tmp/url-downloader-tests"
        downloader.cancel_watch_tasks[token] = object()
        downloader.sessions[token] = {
            "token": token,
            "user_id": 200,
            "chat_id": 100,
            "status_msg": _Message(bot, 100, 1),
        }

        await downloader.run_task(token)

        self.assertNotEqual("handled", downloader.cancel_results.get(token))

    async def test_report_respects_min_interval_on_large_percent_jumps(self):
        """A big percent jump must not bypass the fixed edit interval."""
        old_interval = url_mod._REPORT_MIN_INTERVAL
        url_mod._REPORT_MIN_INTERVAL = 0.05
        try:
            bot = _RecordingBot()
            downloader, token, _ = self._downloader(bot)

            await downloader._report(
                token, 10, done_s="1MB", total_s="10MB", label="progress"
            )
            self.assertEqual(1, bot.edits)

            await downloader._report(
                token, 90, done_s="9MB", total_s="10MB", label="progress"
            )
            self.assertEqual(1, bot.edits)

            await asyncio.sleep(0.06)
            await downloader._report(
                token, 95, done_s="9.5MB", total_s="10MB", label="progress"
            )
            self.assertEqual(2, bot.edits)
            self.assertIn("95.0%", bot.visible_text[1])
        finally:
            url_mod._REPORT_MIN_INTERVAL = old_interval

    async def test_report_floodwait_skips_fallback_send_and_pauses(self):
        """While flood-waited, no fallback send is attempted and edits pause."""
        bot = _FloodOnceBot(wait=3)
        downloader, token, _ = self._downloader(bot)

        await downloader._report(
            token, 10, done_s="1MB", total_s="10MB", label="progress"
        )
        self.assertEqual(1, bot.edits)
        self.assertEqual(0, bot.sends)
        self.assertGreater(downloader._flood_left(100), 0.0)

        await downloader._report(
            token, 20, done_s="2MB", total_s="10MB", label="progress"
        )
        self.assertEqual(1, bot.edits)
        self.assertEqual(0, bot.sends)

    async def test_report_resumes_after_flood_cooldown(self):
        """Once the flood cooldown expires the next update goes through."""
        bot = _FloodOnceBot(wait=3)
        downloader, token, _ = self._downloader(bot)

        await downloader._report(
            token, 10, done_s="1MB", total_s="10MB", label="progress"
        )
        self.assertEqual(1, bot.edits)
        downloader.flood_until[100] = time.monotonic() - 1.0
        downloader.progress_cache[token] = (100, 1, -1.0, time.monotonic() - 5.0)

        await downloader._report(
            token, 25, done_s="2.5MB", total_s="10MB", label="progress"
        )
        self.assertEqual(2, bot.edits)
        self.assertIn("25.0%", bot.visible_text[1])

    async def test_stage_floodwait_skips_fallback_send(self):
        """A flood-waited stage edit pauses instead of re-sending."""
        bot = _FloodOnceBot(wait=3)
        downloader, token, _ = self._downloader(bot)

        await downloader._stage(token, "stage one")
        self.assertEqual(1, bot.edits)
        self.assertEqual(0, bot.sends)
        self.assertGreater(downloader._flood_left(100), 0.0)

    async def test_edit_status_waits_out_flood_then_sends_final_message(self):
        """Terminal status still reaches the user after a FloodWait."""
        bot = _FloodOnceBot(wait=0.2)
        downloader, token, session = self._downloader(bot)

        await downloader._edit_status(session, "saved successfully")

        self.assertEqual(1, bot.sends)
        self.assertEqual(1, bot.edits)
        self.assertTrue(
            any(t == "saved successfully" for t in bot.visible_text.values())
        )

    async def test_report_unchanged_body_skips_request(self):
        """Repeated reports with identical text do not hit the API again."""
        bot = _RecordingBot()
        downloader, token, _ = self._downloader(bot)
        downloader.progress_cache[token] = (
            100, 1, -1.0, time.monotonic() - 5.0
        )

        await downloader._report(
            token, 30, done_s="3MB", total_s="10MB", label="progress"
        )
        self.assertEqual(1, bot.edits)
        downloader.progress_cache[token] = (
            100, 1, 30.0, time.monotonic() - 5.0
        )
        await downloader._report(
            token, 30, done_s="3MB", total_s="10MB", label="progress"
        )
        self.assertEqual(1, bot.edits)

    async def test_report_message_not_modified_is_benign(self):
        """MESSAGE_NOT_MODIFIED must not trigger the duplicate fallback send."""
        bot = _NotModifiedBot()
        downloader, token, _ = self._downloader(bot)

        await downloader._report(
            token, 30, done_s="3MB", total_s="10MB", label="progress"
        )
        self.assertEqual(1, bot.edits)
        self.assertEqual(0, bot.sends)

    async def test_edit_status_message_not_modified_returns_success(self):
        """A final status already on screen counts as delivered."""
        bot = _NotModifiedBot()
        downloader, token, session = self._downloader(bot)

        self.assertTrue(await downloader._edit_status(session, "saved successfully"))
        self.assertEqual(0, bot.sends)


class _RecordingClient:
    """Minimal chat stand-in that records sent note texts."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        del chat_id, kwargs
        self.sent.append(text)
        return SimpleNamespace(id=len(self.sent))


class UrlDownloaderMediaBatchTest(unittest.IsolatedAsyncioTestCase):
    """Late-arriving media of a forwarded burst must not be dropped.

    Regression: forwarding ~10 photos delivers the last one several seconds
    after the first nine, i.e. after the 2.2s collect window has closed.
    """

    USER = 7487373928
    CHAT = -1004456246045

    @classmethod
    def _photo(cls, message_id):
        media = SimpleNamespace(value="photo", file_name=None, file_size=2048)
        message = SimpleNamespace(
            id=message_id,
            from_user=SimpleNamespace(id=cls.USER),
            chat=SimpleNamespace(id=cls.CHAT),
            media=media,
        )
        message.photo = media
        return message

    def _base_session(self, n, started=False):
        msgs = [self._photo(i) for i in range(1, n + 1)]
        return {
            "token": "batch-token",
            "user_id": self.USER,
            "chat_id": self.CHAT,
            "url": "",
            "kind": "tg",
            "tg_msg": msgs[0],
            "title": "批量媒体",
            "size": 0,
            "next_stage": "name",
            "collecting": False,
            "multi": True,
            "media_msgs": msgs,
            "folder": "旅行",
            "started": started,
        }

    async def test_late_media_after_window_merges_into_visible_batch(self):
        downloader = UrlDownloader()
        client = _RecordingClient()
        session = self._base_session(9)
        token = session["token"]
        downloader.sessions[token] = session
        session["at_picker"] = True
        downloader.pending_folder[self.USER] = token

        await downloader._media_message_locked(client, self._photo(10))

        self.assertEqual(10, len(session["media_msgs"]))
        self.assertTrue(any(m.id == 10 for m in session["media_msgs"]))
        self.assertEqual(1, len(client.sent))
        self.assertIn("自动并入", client.sent[0])
        # 同一条消息重复投递只算一次，不重复提示
        await downloader._media_message_locked(client, self._photo(10))
        self.assertEqual(10, len(session["media_msgs"]))
        self.assertEqual(1, len(client.sent))

    async def test_single_file_session_reopens_to_batch_on_late_media(self):
        downloader = UrlDownloader()
        client = _RecordingClient()
        m0 = self._photo(5)
        session = {
            "token": "single-token",
            "user_id": self.USER,
            "chat_id": self.CHAT,
            "url": "",
            "kind": "tg",
            "tg_msg": m0,
            "title": "图片",
            "size": 2048,
            "next_stage": "name",
            "collecting": False,
            "multi": False,
            "media_msgs": None,  # 单文件窗口结束后列表会被清空
            "folder": "",
        }
        downloader.sessions["single-token"] = session

        await downloader._media_message_locked(client, self._photo(6))

        self.assertTrue(session["multi"])
        self.assertEqual([5, 6], [m.id for m in session["media_msgs"]])

    async def test_media_during_running_download_is_stashed_for_continue(self):
        downloader = UrlDownloader()
        client = _RecordingClient()
        session = self._base_session(9, started=True)
        downloader.sessions[session["token"]] = session

        await downloader._media_message_locked(client, self._photo(10))
        await downloader._media_message_locked(client, self._photo(11))
        await downloader._media_message_locked(client, self._photo(10))

        self.assertEqual([10, 11], [m.id for m in session["strays"]])
        # 第一条附带提示，随后 5 秒内被限流抑制，避免刷屏
        self.assertEqual(1, len(client.sent))
        self.assertIn("自动记录", client.sent[0])

    def _select_session(self, n):
        msgs = []
        for mid in range(1, n + 1):
            media = SimpleNamespace(
                value="photo", file_name=None, file_size=2048
            )
            m = SimpleNamespace(
                id=mid,
                from_user=SimpleNamespace(id=self.USER),
                chat=SimpleNamespace(id=self.CHAT),
                media=media,
            )
            m.photo = media
            msgs.append(m)
        return {
            "token": "select-token",
            "user_id": self.USER,
            "chat_id": self.CHAT,
            "kind": "tg",
            "multi": True,
            "media_msgs": msgs,
            "selected": [True] * n,
            "size": 2048 * n,
            "folder": "旅行",
            "final_name": None,
        }

    async def test_select_card_text_stays_under_limit_for_large_batches(self):
        downloader = UrlDownloader()
        session = self._select_session(80)

        text, k = downloader._select_text(session)

        self.assertEqual(80, k)
        self.assertLess(len(text), 4096)
        self.assertIn("共 80 个文件", text)
        self.assertIn("还有 55 个未列出", text)

    async def test_select_markup_respects_telegram_button_limit(self):
        downloader = UrlDownloader()
        session = self._select_session(120)

        markup = downloader._select_markup(session, 120)
        rows = markup.inline_keyboard
        buttons = sum(len(row) for row in rows)
        self.assertLessEqual(buttons, 100)
        # 前 90 个单项按钮 + 全选/全不选 + 下载 + 改文件夹/取消
        self.assertEqual(90 + 5, buttons)

    async def test_unique_local_name_matches_remote_suffix_scheme(self):
        downloader = UrlDownloader()
        existing = {"a.jpg", "b.PNG", "a_1.jpg", "a_2.jpg"}
        folded = {x.casefold() for x in existing}

        self.assertEqual("a_3.jpg", downloader._unique_local_name(folded, "a.jpg"))
        self.assertEqual("b_1.PNG", downloader._unique_local_name(folded, "b.PNG"))
        # 无冲突时原样返回
        self.assertEqual("c.jpg", downloader._unique_local_name(folded, "c.jpg"))

    async def test_stray_continue_reuses_folder_and_starts_download(self):
        downloader = UrlDownloader()
        started = []
        downloader._begin = lambda tk: started.append(tk)  # type: ignore[method-assign]
        session = self._base_session(9, started=True)

        await downloader._continue_stray_download(session, [self._photo(10)])

        self.assertEqual(1, len(started))
        new_session = downloader.sessions[started[0]]
        self.assertEqual(session["folder"], new_session["folder"])
        self.assertEqual([10], [m.id for m in new_session["media_msgs"]])
        self.assertTrue(new_session["multi"])


class UrlDownloaderPipelineTest(unittest.IsolatedAsyncioTestCase):
    """批量下载：同时最多 2 个下载，上传严格串行、按消息顺序落盘。

    fake 只替换实例方法，统计下载/上传在同一时刻的真实在途数
    （在 await 睡眠期间计数），用于断言并发度与顺序。
    """

    async def _downloader_with_fakes(self, n_items, dl_secs, up_secs):
        downloader = UrlDownloader()
        downloader.tmp_root = tempfile.mkdtemp(prefix="urldl-pipe-")
        self.addCleanup(shutil.rmtree, downloader.tmp_root, True)
        state = {
            "dl": 0,        # 正在下载中的文件数
            "dl_max": 0,    # 下载并发峰值
            "up": 0,        # 正在上传中的文件数
            "up_max": 0,    # 上传并发峰值（必须恒为 1）
            "busy_max": 0,  # 下载+上传同时在途峰值
            "up_order": [],  # 上传文件名（即 NAS 落盘顺序）
            "fail_at": None,      # 指定序号的下载抛错
            "set_cancel_at": None,  # 指定序号的下载一开始就置取消
        }

        async def fake_list_files(rel=""):
            del rel
            return {"a.jpg"}

        async def fake_download_tg(token, session, event, status, sub_dir=""):
            del token, status
            i = session.get("cur_i") or 1
            state["dl"] += 1
            state["dl_max"] = max(state["dl_max"], state["dl"])
            state["busy_max"] = max(
                state["busy_max"], state["dl"] + state["up"]
            )
            item_dir = os.path.join(
                downloader.tmp_root, "pipe", sub_dir or ""
            )
            os.makedirs(item_dir, exist_ok=True)
            path = os.path.join(item_dir, "a.jpg")
            with open(path, "wb") as fh:
                fh.write(b"x" * 8)
            try:
                if state["set_cancel_at"] == i:
                    event.set()
                await asyncio.sleep(dl_secs)
                if state["fail_at"] == i:
                    raise RuntimeError("boom")
            finally:
                state["dl"] -= 1
            return path, "a.jpg", 8

        async def fake_move_to_nas(local_path, rel="", token=None, seq=""):
            del rel, token, seq
            state["up"] += 1
            state["up_max"] = max(state["up_max"], state["up"])
            state["busy_max"] = max(
                state["busy_max"], state["dl"] + state["up"]
            )
            state["up_order"].append(os.path.basename(local_path))
            try:
                await asyncio.sleep(up_secs)
            finally:
                state["up"] -= 1
            # rclone move 成功后会删掉本地源文件
            os.remove(local_path)

        downloader._list_files = fake_list_files
        downloader._download_tg = fake_download_tg
        downloader._move_to_nas = fake_move_to_nas

        msgs = []
        for mid in range(1, n_items + 1):
            media = SimpleNamespace(
                value="photo", file_name=None, file_size=8
            )
            m = SimpleNamespace(
                id=mid,
                from_user=SimpleNamespace(id=777),
                chat=SimpleNamespace(id=-100),
                media=media,
            )
            m.photo = media
            msgs.append(m)
        session = {
            "token": "pipe",
            "user_id": 777,
            "chat_id": -100,
            "kind": "tg",
            "tg_msg": msgs[0],
            "title": "批量媒体",
            "size": 8 * n_items,
            "multi": True,
            "media_msgs": msgs,
            "folder": "旅行",
            "started": True,
        }
        return downloader, session, state

    async def test_two_downloads_at_once_uploads_serial_in_order(self):
        downloader, session, state = await self._downloader_with_fakes(
            6, dl_secs=0.05, up_secs=0.08
        )

        await downloader._download_multi("pipe", session, asyncio.Event())

        # 下载并发达到 2 路；上传从未并发（恒为 1 路）
        self.assertEqual(2, state["dl_max"])
        self.assertEqual(1, state["up_max"])
        # 上传与后续下载确有重叠（下载2 + 上传1 同窗口）
        self.assertGreaterEqual(state["busy_max"], 3)
        # 全部 6 个文件都上传成功
        self.assertEqual(6, session["multi_done"])
        # 目标目录原本有 a.jpg：按消息顺序落盘为 a_1.jpg..a_6.jpg
        self.assertEqual(
            [f"a_{i}.jpg" for i in range(1, 7)], state["up_order"]
        )
        # 本地临时目录不应残留任何已上传文件
        leftovers = []
        for root, _dirs, files in os.walk(
            os.path.join(downloader.tmp_root, "pipe")
        ):
            leftovers.extend(
                os.path.join(root, f) for f in files if f.endswith(".jpg")
            )
        self.assertEqual([], leftovers)

    async def test_summary_shows_all_saved_files_in_order(self):
        downloader, session, state = await self._downloader_with_fakes(
            4, dl_secs=0.02, up_secs=0.02
        )
        results_holder = {}

        async def fake_edit_status(session, text):
            del session
            results_holder["text"] = text

        downloader._edit_status = fake_edit_status

        await downloader._download_multi("pipe", session, asyncio.Event())

        text = results_holder["text"]
        self.assertIn("已保存 4/4 个文件", text)
        self.assertIn("合计大小：", text)
        self.assertEqual(4, len(state["up_order"]))
        pos = [text.index(suffix) for suffix in
               ("a_1.jpg", "a_2.jpg", "a_3.jpg", "a_4.jpg")]
        self.assertEqual(pos, sorted(pos))

    async def test_cancel_stops_rest_but_finishes_inflight_upload(self):
        downloader, session, state = await self._downloader_with_fakes(
            4, dl_secs=0.05, up_secs=0.12
        )
        state["set_cancel_at"] = 3

        with self.assertRaises(url_mod._TaskCancelled):
            await downloader._download_multi("pipe", session, asyncio.Event())

        # 取消前只落盘了第 1 个（当时在途），第 2 个已下载但未开始上传，
        # 之后的不再启动新的上传
        self.assertEqual(1, session["multi_done"])
        self.assertEqual(["a_1.jpg"], state["up_order"])
        self.assertEqual(1, state["up_max"])
        self.assertEqual(2, state["dl_max"])
        # 所有下载协程都已收尾（在途的被取消并在 finally 中减计数）
        self.assertEqual(0, state["dl"])
        self.assertEqual(0, state["up"])

    async def test_failure_uploads_preceding_files_in_order_then_raises(self):
        downloader, session, state = await self._downloader_with_fakes(
            4, dl_secs=0.03, up_secs=0.08
        )
        state["fail_at"] = 3

        with self.assertRaises(RuntimeError) as ctx:
            await downloader._download_multi("pipe", session, asyncio.Event())

        self.assertIn("第 3/4 个下载失败", str(ctx.exception))
        # 失败序号之前的文件按顺序落盘；之后的（第 4 个）不传
        self.assertEqual(2, session["multi_done"])
        self.assertEqual(["a_1.jpg", "a_2.jpg"], state["up_order"])
        self.assertEqual(0, state["dl"])
        self.assertEqual(0, state["up"])


class _ScratchDeleteBot:
    """Records delete_messages calls (the real bot deletes for real)."""

    def __init__(self):
        self.deleted = []

    async def delete_messages(self, chat_id, message_ids):
        self.deleted.append((chat_id, list(message_ids)))


class _ScratchClient:
    """Minimal fake bot client that hands out message ids."""

    def __init__(self):
        self.sent = []
        self._next_id = 500

    async def send_message(self, chat_id, text, **kwargs):
        del kwargs
        self._next_id += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(
            id=self._next_id, chat=SimpleNamespace(id=chat_id)
        )


class UrlDownloaderScratchCleanupTest(unittest.IsolatedAsyncioTestCase):
    """任务期的一次性提示要记录并在收尾时删除，聊天不留垃圾消息。"""

    USER = 777
    CHAT = -100

    def _photo(self, mid):
        media = SimpleNamespace(
            value="photo", file_name=None, file_size=8
        )
        m = SimpleNamespace(
            id=mid,
            from_user=SimpleNamespace(id=self.USER),
            chat=SimpleNamespace(id=self.CHAT),
            media=media,
        )
        m.photo = media
        return m

    async def test_scratch_hint_is_recorded_and_deleted_once(self):
        downloader = UrlDownloader()
        bot = _ScratchDeleteBot()
        downloader.bot = bot
        client = _ScratchClient()
        session = {"token": "t1", "chat_id": self.CHAT}

        await downloader._scratch_send(client, session, "➕ 一次性提示")

        self.assertEqual([501], session["scratch_msgs"])
        self.assertEqual(1, len(client.sent))

        await downloader._scratch_clear(session)

        self.assertEqual([(self.CHAT, [501])], bot.deleted)
        self.assertEqual([], session["scratch_msgs"])
        # 已清空后再清不会重复删除
        await downloader._scratch_clear(session)
        self.assertEqual([(self.CHAT, [501])], bot.deleted)

    async def test_auto_merge_notices_are_deleted_when_task_ends(self):
        downloader = UrlDownloader()
        bot = _ScratchDeleteBot()
        downloader.bot = bot
        client = _ScratchClient()
        m1, m2 = self._photo(1), self._photo(2)
        session = {
            "token": "t2",
            "user_id": self.USER,
            "chat_id": self.CHAT,
            "kind": "tg",
            "multi": True,
            "media_msgs": [m1, m2],
            "size": 16,
            "at_picker": True,
            "folder": "旅行",
            "started": False,
        }
        downloader.sessions["t2"] = session
        downloader.pending_folder[self.USER] = "t2"

        # 文件夹卡已弹出时又并入一个文件 -> 连续提示只留最新一条：
        # 第二条发出前会自动删掉第一条
        session["media_msgs"] = [m1, m2, self._photo(3)]
        await downloader._refresh_question_card(client, session)
        session["media_msgs"] = [m1, m2, self._photo(3), self._photo(4)]
        await downloader._refresh_question_card(client, session)

        self.assertEqual(2, len(client.sent))
        self.assertTrue(
            client.sent[0][1].startswith("➕ 已自动并入新到的文件")
        )
        # 第一条(501)已被第二条发出前的清理删除，记录里只剩最新(502)
        self.assertEqual([(self.CHAT, [501])], bot.deleted)
        self.assertEqual([502], session["scratch_msgs"])

        # 任务结束（无论成功/取消）都会走到统一清理
        await downloader._scratch_clear(session)

        self.assertEqual(
            [(self.CHAT, [501]), (self.CHAT, [502])], bot.deleted
        )
        self.assertEqual([], session["scratch_msgs"])

    async def test_failed_delete_is_retried_by_next_cleanup(self):
        downloader = UrlDownloader()

        class _FlakyBot(_ScratchDeleteBot):
            def __init__(self):
                super().__init__()
                self.fail_once = True

            async def delete_messages(self, chat_id, message_ids):
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("network down")
                await super().delete_messages(chat_id, message_ids)

        bot = _FlakyBot()
        downloader.bot = bot
        client = _ScratchClient()
        session = {"token": "t4", "chat_id": self.CHAT}

        await downloader._scratch_send(client, session, "➕ 提示")
        self.assertEqual([501], session["scratch_msgs"])

        # 第一次删除失败：消息放回记录，等待下次清理重试
        await downloader._scratch_clear(session)
        self.assertEqual([], bot.deleted)
        self.assertEqual([501], session["scratch_msgs"])

        # 任务收尾再次清理：这次成功
        await downloader._scratch_clear(session)
        self.assertEqual([(self.CHAT, [501])], bot.deleted)
        self.assertEqual([], session["scratch_msgs"])

    async def test_stray_received_while_running_is_recorded(self):
        downloader = UrlDownloader()
        bot = _ScratchDeleteBot()
        downloader.bot = bot
        client = _ScratchClient()
        m1 = self._photo(1)
        session = {
            "token": "t3",
            "user_id": self.USER,
            "chat_id": self.CHAT,
            "kind": "tg",
            "tg_msg": m1,
            "title": "批量媒体",
            "multi": True,
            "media_msgs": [m1],
            "started": True,
        }
        downloader.sessions["t3"] = session

        # 下载中又转发来两个文件：第一条会提示并记录，第二条被
        # busy 节流挡住不重复发
        await downloader._media_message_locked(client, self._photo(2))
        await downloader._media_message_locked(client, self._photo(3))

        self.assertEqual(1, len(client.sent))
        self.assertTrue(client.sent[0][1].startswith("📌 收到新文件"))
        self.assertEqual([501], session["scratch_msgs"])
        self.assertEqual(2, len(session["strays"]))

        await downloader._scratch_clear(session)

        self.assertEqual([(self.CHAT, [501])], bot.deleted)


if __name__ == "__main__":
    unittest.main()

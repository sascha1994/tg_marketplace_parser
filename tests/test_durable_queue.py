import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import healthcheck
from tg_marketplace_parser import thresholds
from tg_marketplace_parser.config import Settings
from tg_marketplace_parser.models import Job, ParserState
from tg_marketplace_parser.pipeline import PipelineMixin
from tg_marketplace_parser.storage import DuplicateDB


class TempDbCase(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        self.db = DuplicateDB(self.db_path, cooldown_hours=48, pending_ttl_minutes=20)

    def tearDown(self):
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass


class JobAndCursorStorageTests(TempDbCase):
    def test_jobs_roundtrip_and_deletion(self):
        first = self.db.save_job(10, [3, 1, 2])
        second = self.db.save_job(11, [7], target_channel=None, attempts=2)

        jobs = self.db.load_pending_jobs()
        self.assertEqual([job["job_id"] for job in jobs], [first, second])
        self.assertEqual(jobs[0]["message_ids"], [1, 2, 3])
        self.assertEqual(jobs[1]["attempts"], 2)
        self.assertEqual(self.db.pending_message_ids(10), {1, 2, 3})

        self.db.update_job_attempts(second, 3)
        self.db.delete_job(first)
        remaining = self.db.load_pending_jobs()
        self.assertEqual([job["job_id"] for job in remaining], [second])
        self.assertEqual(remaining[0]["attempts"], 3)

    def test_cursor_never_moves_backwards(self):
        self.assertIsNone(self.db.get_cursor(5))
        self.db.set_cursor(5, 100)
        self.db.set_cursor(5, 50)
        self.assertEqual(self.db.get_cursor(5), 100)
        self.db.set_cursor(5, 120)
        self.assertEqual(self.db.get_cursor(5), 120)

    def test_pending_jobs_survive_reopening_the_database(self):
        job_id = self.db.save_job(1, [9])
        self.db.close()
        self.db = DuplicateDB(self.db_path, cooldown_hours=48, pending_ttl_minutes=20)
        self.assertEqual(self.db.load_pending_jobs()[0]["job_id"], job_id)


class BlocklistAndStatsTests(TempDbCase):
    def test_brand_and_keyword_rules(self):
        self.assertTrue(self.db.add_block("brand", "  NoName  "))
        self.assertFalse(self.db.add_block("brand", "noname"))
        self.assertTrue(self.db.add_block("keyword", "Реплика"))
        self.assertFalse(self.db.add_block("bad", "x"))
        self.assertFalse(self.db.add_block("keyword", "   "))

        self.assertIn("noname", self.db.find_block_match("NoName", "Чайник"))
        self.assertIn("реплика", self.db.find_block_match("Acme", "Сумка РЕПЛИКА бренда"))
        self.assertIsNone(self.db.find_block_match("Acme", "Хороший чайник"))
        self.assertEqual(
            self.db.list_blocks(),
            [("brand", "noname"), ("keyword", "реплика")],
        )

        self.assertTrue(self.db.remove_block("brand", "NONAME"))
        self.assertFalse(self.db.remove_block("brand", "noname"))
        self.assertIsNone(self.db.find_block_match("NoName", "Чайник"))

    def test_brand_rule_does_not_match_partial_brand_names(self):
        self.db.add_block("brand", "acme")
        self.assertIsNone(self.db.find_block_match("Acme Pro", "Чайник"))

    def test_source_stats_accumulate_and_sort_by_published(self):
        self.db.bump_source_stat(1, "received", 3)
        self.db.bump_source_stat(1, "published")
        self.db.bump_source_stat(2, "received", 5)
        self.db.bump_source_stat(2, "published", 4)
        self.db.bump_source_stat(2, "rejected")
        with self.assertRaises(ValueError):
            self.db.bump_source_stat(1, "unknown")

        top = self.db.get_top_sources()
        self.assertEqual([row["chat_id"] for row in top], [2, 1])
        self.assertEqual(top[0]["received"], 5)
        self.assertEqual(top[0]["rejected"], 1)


class ThresholdTests(TempDbCase):
    def test_defaults_without_settings(self):
        owner = SimpleNamespace()
        self.assertEqual(thresholds.ozon_min_discount(owner), 30.0)
        self.assertEqual(thresholds.wb_min_stock(owner), 3)
        self.assertEqual(thresholds.unavailable_mark_discount(owner), 50.0)
        self.assertEqual(thresholds.scheduled_max_attempts(owner), 20)
        self.assertEqual(thresholds.recheck_delay_seconds(owner), 900)

    def test_settings_override_defaults(self):
        owner = SimpleNamespace(settings=SimpleNamespace(ozon_min_discount_percent=40, wb_min_stock=5))
        self.assertEqual(thresholds.ozon_min_discount(owner), 40.0)
        self.assertEqual(thresholds.wb_min_stock(owner), 5)

    def test_database_value_has_priority_over_settings(self):
        owner = SimpleNamespace(
            db=self.db,
            settings=SimpleNamespace(ozon_min_discount_percent=40, wb_min_stock=5),
        )
        self.db.set_setting("ozon_min_discount_percent", "25.5")
        self.db.set_setting("wb_min_stock", "1")
        self.assertEqual(thresholds.ozon_min_discount(owner), 25.5)
        self.assertEqual(thresholds.wb_min_stock(owner), 1)

    def test_invalid_database_value_falls_back_to_settings(self):
        owner = SimpleNamespace(db=self.db, settings=SimpleNamespace(wb_min_stock=4))
        self.db.set_setting("wb_min_stock", "много")
        self.assertEqual(thresholds.wb_min_stock(owner), 4)

    def test_settings_from_env_have_backward_compatible_defaults(self):
        env = {
            "TG_API_ID": "1",
            "TG_API_HASH": "hash",
            "BOT_TOKEN": "token",
            "ADMIN_ID": "2",
            "TG_TARGET_CHANNEL": "main",
        }
        with patch.dict(os.environ, env, clear=True), patch("tg_marketplace_parser.config.load_dotenv"):
            settings = Settings.from_env()
        self.assertEqual(settings.ozon_min_discount_percent, 30.0)
        self.assertEqual(settings.wb_min_stock, 3)
        self.assertEqual(settings.unavailable_mark_discount_percent, 50.0)
        self.assertEqual(settings.scheduled_max_attempts, 20)
        self.assertEqual(settings.recheck_delay_seconds, 900)
        self.assertTrue(settings.heartbeat_path.endswith("heartbeat"))


class HealthcheckTests(unittest.TestCase):
    def test_fresh_heartbeat_is_healthy_and_stale_or_missing_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "heartbeat")
            with patch.object(healthcheck, "HEARTBEAT_PATH", path):
                self.assertFalse(healthcheck.is_healthy())
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("x")
                self.assertTrue(healthcheck.is_healthy())
                old = time.time() - healthcheck.MAX_AGE_SECONDS - 10
                os.utime(path, (old, old))
                self.assertFalse(healthcheck.is_healthy())


def make_message(message_id, *, grouped_id=None, age_seconds=0):
    return SimpleNamespace(
        id=message_id,
        grouped_id=grouped_id,
        date=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


class DurableQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        self.db = DuplicateDB(self.db_path, cooldown_hours=48, pending_ttl_minutes=20)

    async def asyncTearDown(self):
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def make_parser(self, maxsize=5, **settings):
        parser = type("Parser", (PipelineMixin,), {})()
        parser.db = self.db
        parser.queue = asyncio.Queue(maxsize=maxsize)
        parser.state = ParserState()
        parser._inflight_job_ids = set()
        parser._overflow_pending = False
        parser.notify_admin = AsyncMock()
        parser.settings = SimpleNamespace(
            catchup_max_messages=settings.get("catchup_max_messages", 50),
            catchup_max_age_seconds=settings.get("catchup_max_age_seconds", 3600),
        )
        parser.current_sources = set()
        return parser

    async def test_enqueue_persists_source_jobs_and_counts_received(self):
        parser = self.make_parser()
        job = Job(messages=[make_message(5)], source_chat_id=10)

        await parser.enqueue(job)

        self.assertIsNotNone(job.job_id)
        self.assertEqual(parser.queue.qsize(), 1)
        self.assertIn(job.job_id, parser._inflight_job_ids)
        self.assertEqual(self.db.load_pending_jobs()[0]["message_ids"], [5])
        self.assertEqual(self.db.get_top_sources()[0]["received"], 1)

    async def test_admin_test_jobs_are_not_persisted(self):
        parser = self.make_parser()
        job = Job(messages=[make_message(5)], source_chat_id=10, target_channel="test")

        await parser.enqueue(job)

        self.assertIsNone(job.job_id)
        self.assertEqual(parser.queue.qsize(), 1)
        self.assertEqual(self.db.load_pending_jobs(), [])

    async def test_duplicate_enqueue_of_same_job_is_ignored(self):
        parser = self.make_parser()
        job = Job(messages=[make_message(5)], source_chat_id=10)
        await parser.enqueue(job)
        await parser.enqueue(job)
        self.assertEqual(parser.queue.qsize(), 1)

    async def test_overflowing_job_stays_in_database_instead_of_being_dropped(self):
        parser = self.make_parser(maxsize=1)
        await parser.enqueue(Job(messages=[make_message(1)], source_chat_id=10))
        await parser.enqueue(Job(messages=[make_message(2)], source_chat_id=10))
        await parser.enqueue(Job(messages=[make_message(3)], source_chat_id=10))

        self.assertEqual(parser.queue.qsize(), 1)
        self.assertEqual(parser.state.queue_dropped, 0)
        self.assertTrue(parser._overflow_pending)
        self.assertEqual(len(self.db.load_pending_jobs()), 3)
        parser.notify_admin.assert_awaited_once()

    async def test_restore_loads_saved_jobs_after_restart(self):
        job_id = self.db.save_job(10, [4, 5], attempts=1)
        parser = self.make_parser()
        parser.user_client = SimpleNamespace(
            get_messages=AsyncMock(return_value=[make_message(4), None, make_message(5)])
        )

        restored = await parser.restore_pending_jobs()

        self.assertEqual(restored, 1)
        self.assertEqual(parser.state.recovered_jobs, 1)
        job = parser.queue.get_nowait()
        self.assertEqual(job.job_id, job_id)
        self.assertEqual(job.attempts, 1)
        self.assertEqual([message.id for message in job.messages], [4, 5])
        parser.user_client.get_messages.assert_awaited_once_with(10, ids=[4, 5])

    async def test_restore_drops_jobs_whose_messages_no_longer_exist(self):
        self.db.save_job(10, [4])
        parser = self.make_parser()
        parser.user_client = SimpleNamespace(get_messages=AsyncMock(return_value=[None]))

        self.assertEqual(await parser.restore_pending_jobs(), 0)
        self.assertEqual(self.db.load_pending_jobs(), [])

    async def test_restore_skips_jobs_already_in_flight(self):
        job_id = self.db.save_job(10, [4])
        parser = self.make_parser()
        parser._inflight_job_ids.add(job_id)
        parser.user_client = SimpleNamespace(get_messages=AsyncMock(return_value=[make_message(4)]))

        self.assertEqual(await parser.restore_pending_jobs(), 0)
        parser.user_client.get_messages.assert_not_awaited()

    async def test_catch_up_queues_missed_posts_oldest_first_and_moves_cursor(self):
        parser = self.make_parser()
        parser.current_sources = {10}
        self.db.set_cursor(10, 1)
        newest_first = [make_message(4), make_message(3), make_message(2)]

        async def iter_messages(chat_id, min_id, limit):
            self.assertEqual((chat_id, min_id), (10, 1))
            for message in newest_first:
                yield message

        parser.user_client = SimpleNamespace(iter_messages=iter_messages, get_messages=AsyncMock())

        queued = await parser.catch_up_sources()

        self.assertEqual(queued, 3)
        self.assertEqual(parser.state.caught_up_posts, 3)
        self.assertEqual(self.db.get_cursor(10), 4)
        order = [parser.queue.get_nowait().messages[0].id for _ in range(3)]
        self.assertEqual(order, [2, 3, 4])

    async def test_catch_up_groups_albums_into_one_job(self):
        parser = self.make_parser()
        parser.current_sources = {10}
        self.db.set_cursor(10, 1)
        album = [make_message(3, grouped_id=77), make_message(2, grouped_id=77)]

        async def iter_messages(chat_id, min_id, limit):
            for message in album:
                yield message

        parser.user_client = SimpleNamespace(iter_messages=iter_messages, get_messages=AsyncMock())

        self.assertEqual(await parser.catch_up_sources(), 1)
        job = parser.queue.get_nowait()
        self.assertEqual(sorted(message.id for message in job.messages), [2, 3])

    async def test_catch_up_skips_stale_posts_but_advances_cursor(self):
        parser = self.make_parser(catchup_max_age_seconds=3600)
        parser.current_sources = {10}
        self.db.set_cursor(10, 1)

        async def iter_messages(chat_id, min_id, limit):
            yield make_message(9, age_seconds=7200)

        parser.user_client = SimpleNamespace(iter_messages=iter_messages, get_messages=AsyncMock())

        self.assertEqual(await parser.catch_up_sources(), 0)
        self.assertEqual(self.db.get_cursor(10), 9)
        self.assertTrue(parser.queue.empty())

    async def test_catch_up_skips_messages_already_stored_as_pending_jobs(self):
        parser = self.make_parser()
        parser.current_sources = {10}
        self.db.set_cursor(10, 1)
        self.db.save_job(10, [2])

        async def iter_messages(chat_id, min_id, limit):
            yield make_message(2)

        parser.user_client = SimpleNamespace(iter_messages=iter_messages, get_messages=AsyncMock())

        self.assertEqual(await parser.catch_up_sources(), 0)

    async def test_first_run_initializes_cursor_without_publishing_old_posts(self):
        parser = self.make_parser()
        parser.current_sources = {10}
        parser.user_client = SimpleNamespace(
            get_messages=AsyncMock(return_value=[make_message(42)]),
            iter_messages=lambda *args, **kwargs: None,
        )

        self.assertEqual(await parser.catch_up_sources(), 0)
        self.assertEqual(self.db.get_cursor(10), 42)
        self.assertTrue(parser.queue.empty())

    async def test_catch_up_can_be_disabled(self):
        parser = self.make_parser(catchup_max_messages=0)
        parser.current_sources = {10}
        parser.user_client = SimpleNamespace(get_messages=AsyncMock())

        self.assertEqual(await parser.catch_up_sources(), 0)
        parser.user_client.get_messages.assert_not_awaited()

    async def test_incoming_handler_updates_cursor_even_when_paused(self):
        parser = self.make_parser()
        parser.state.is_paused = True
        event = SimpleNamespace(message=make_message(15), chat_id=10)

        await parser.incoming_post_handler(event)

        self.assertEqual(self.db.get_cursor(10), 15)
        self.assertEqual(parser.state.incoming, 0)
        self.assertTrue(parser.queue.empty())

    async def test_requeue_after_flood_wait_keeps_job_id_and_reenqueues(self):
        parser = self.make_parser()
        job = Job(messages=[make_message(5)], source_chat_id=10)
        await parser.enqueue(job)
        parser.queue.get_nowait()
        job.retry_scheduled = True

        await parser.requeue_later(job, 0)

        self.assertFalse(job.retry_scheduled)
        self.assertEqual(parser.queue.qsize(), 1)
        self.assertEqual(len(self.db.load_pending_jobs()), 1)


if __name__ == "__main__":
    unittest.main()

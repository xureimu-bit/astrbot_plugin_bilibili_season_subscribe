import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import test_summarization as base
from bili_plugin_test.dashboard import DashboardError, PluginDashboard


class SavedConfig(dict):
    def save_config(self):
        if getattr(self, 'fail', False):
            raise OSError('private credential')
        self.path.write_text(json.dumps(self), encoding='utf-8')


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = SavedConfig(summary_enabled=True, bilibili_sessdata='private-session-value')
        self.config.path = self.root / 'config.json'
        self.context = base.modern_context()
        with patch.object(base.main.StarTools, 'get_data_dir', return_value=str(self.root)):
            self.plugin = base.main.BilibiliSeasonSubscribePlugin(self.context, self.config)
        await self.plugin.store.load()
        self.dashboard = self.plugin.dashboard
        self.item = base.main.SeasonSubscription('1', '2', '<script>external title</script>', 'up', '',
                    subscribers=[base.main.Subscriber('a', '群聊A', 'now'), base.main.Subscriber('b', '群聊B', 'now')],
                    pending_bvids=[base.BVID], seen_bvids=['older'])
        await self.plugin.store.upsert_subscription(self.item)

    async def asyncTearDown(self):
        await self.dashboard.stop()
        self.temp.cleanup()

    async def test_overview_uses_real_store_and_never_exposes_credentials(self):
        result = await self.dashboard.dispatch('overview', {})
        self.assertEqual(result['metrics'], {'subscriptions': 1, 'sessions': 2, 'pending': 1, 'processed': 1})
        self.assertFalse(result['running'])
        self.assertEqual(result['subscriptions'][0]['title'], self.item.season_title)
        self.assertNotIn('private-session', json.dumps(result))
        settings = self.dashboard.settings()
        self.assertEqual(settings['values']['bilibili_sessdata'], '')
        self.assertTrue(settings['configured']['bilibili_sessdata'])
        self.assertNotIn('private-session', json.dumps(settings))

    async def test_valid_settings_persist_and_apply_without_erasing_secret(self):
        settings = self.dashboard.settings()
        result = await self.dashboard.save_settings({'revision': settings['revision'],
            'values': {'summary_max_output_chars': 150, 'summary_max_segments': 8, 'meme_enabled': True, 'bilibili_sessdata': ''}})
        self.assertNotEqual(settings['revision'], result['revision'])
        self.assertEqual(self.plugin.summarizer.max_output, 150)
        self.assertEqual(self.plugin.summarizer.max_segments, 8)
        self.assertTrue(self.plugin.summarizer.memes.enabled)
        self.assertEqual(self.config['bilibili_sessdata'], 'private-session-value')
        self.assertEqual(json.loads(self.config.path.read_text())['summary_max_output_chars'], 150)

    async def test_invalid_types_bounds_and_unknown_fields_rejected_before_write(self):
        for values in ({'summary_enabled': 'false'}, {'asr_timeout_seconds': True}, {'summary_max_output_chars': 40},
                       {'summary_max_segments': 1}, {'summary_max_segments': 65},
                       {'asr_chunk_seconds': 999}, {'not_a_setting': 'x'}, {'ffmpeg_path': None}):
            with self.subTest(values=values), self.assertRaises(DashboardError):
                await self.dashboard.save_settings({'revision': self.dashboard.settings()['revision'], 'values': values})
        self.assertFalse(self.config.path.exists())
        self.assertTrue(self.plugin.summary_enabled)

    async def test_title_setting_defaults_off_and_applies_both_directions(self):
        self.assertFalse(self.dashboard.settings()['values']['send_title_message'])
        self.assertFalse(self.plugin.send_title_message)
        for enabled in (True, False):
            await self.dashboard.save_settings({'revision': self.dashboard.settings()['revision'],
                'values': {'send_title_message': enabled}})
            self.assertEqual(self.plugin.send_title_message, enabled)
            self.assertEqual(json.loads(self.config.path.read_text())['send_title_message'], enabled)

    async def test_revision_conflict_and_failed_write_preserve_config(self):
        revision = self.dashboard.settings()['revision']
        self.config['summary_enabled'] = False
        with self.assertRaises(DashboardError) as raised:
            await self.dashboard.save_settings({'revision': revision, 'values': {'summary_enabled': True}})
        self.assertEqual(raised.exception.status, 409)
        self.config.fail = True
        with self.assertRaises(DashboardError) as raised:
            await self.dashboard.save_settings({'revision': self.dashboard.settings()['revision'], 'values': {'summary_enabled': True}})
        self.assertNotIn('private', str(raised.exception))
        self.assertFalse(self.config['summary_enabled'])

    async def test_upload_fallback_settings_apply_and_status_is_scoped_to_matching_collection(self):
        self.assertFalse(self.plugin.upload_fallback.enabled)
        self.item.season_title = '合集·神人电台'
        self.item.upload_last_status = '未发现需判定的新正片标题'
        await self.plugin.store.upsert_subscription(self.item)
        await self.dashboard.save_settings({'revision': self.dashboard.settings()['revision'], 'values': {
            'upload_fallback_enabled': True, 'upload_fallback_max_videos': 20}})
        self.assertTrue(self.plugin.upload_fallback.enabled)
        self.assertEqual(self.plugin.upload_fallback.limit, 20)
        overview = await self.dashboard.overview()
        self.assertTrue(overview['subscriptions'][0]['upload_fallback'])
        self.assertEqual(overview['subscriptions'][0]['upload_status'], self.item.upload_last_status)
        for values in ({'upload_fallback_max_videos': 51}, {'upload_fallback_timeout_seconds': 4}):
            with self.assertRaises(DashboardError):
                await self.dashboard.save_settings({'revision': self.dashboard.settings()['revision'], 'values': values})

    async def test_secret_clear_requires_explicit_intent(self):
        revision = self.dashboard.settings()['revision']
        with self.assertRaises(DashboardError):
            await self.dashboard.save_settings({'revision': revision, 'values': {'bilibili_sessdata': 'new'}, 'clear_secrets': ['bilibili_sessdata']})
        await self.dashboard.save_settings({'revision': revision, 'values': {}, 'clear_secrets': ['bilibili_sessdata']})
        self.assertEqual(self.config['bilibili_sessdata'], '')

    async def test_remove_is_scoped_to_exact_collection_and_session(self):
        second = base.main.SeasonSubscription('9', '2', '相同ID另一个UP', 'up', '', subscribers=[base.main.Subscriber('a', 'A', 'now')])
        await self.plugin.store.upsert_subscription(second)
        await self.dashboard.dispatch('subscriptions/remove', {'mid': '1', 'season_id': '2', 'origin': 'a'})
        items = await self.plugin.store.list_subscriptions()
        self.assertEqual([s.unified_msg_origin for s in items[0].subscribers], ['b'])
        self.assertEqual(len(items[1].subscribers), 1)
        with self.assertRaises(DashboardError):
            await self.dashboard.dispatch('subscriptions/remove', {'mid': '1', 'season_id': '2', 'origin': 'a'})

    async def test_add_reuses_existing_session_and_chat_subscription_behavior(self):
        self.plugin._handle_add = AsyncMock(return_value='订阅成功')
        await self.dashboard.dispatch('subscriptions/add', {'origin': 'a', 'url': 'https://space.bilibili.com/1/lists/3?type=season'})
        self.assertEqual(self.plugin._handle_add.call_args.args[0].unified_msg_origin, 'a')
        for payload in ({'origin': 'unknown', 'url': 'https://space.bilibili.com/1/lists/3'},
                        {'origin': 'a', 'url': 'https://space.bilibili.com.evil/1/lists/3'}):
            with self.assertRaises(DashboardError):
                await self.dashboard.dispatch('subscriptions/add', payload)

    async def test_background_check_busy_guard_and_shutdown(self):
        self.plugin._check_once = AsyncMock(side_effect=lambda **kwargs: asyncio.sleep(10))
        # A real coroutine keeps the job alive until explicitly cancelled.
        async def slow(**kwargs):
            await asyncio.sleep(10)
        self.plugin._check_once.side_effect = slow
        self.assertTrue((await self.dashboard.dispatch('check', {}))['started'])
        with self.assertRaises(DashboardError) as raised:
            await self.dashboard.dispatch('check', {})
        self.assertEqual(raised.exception.status, 409)
        await self.dashboard.stop()
        self.assertTrue(self.dashboard.job.done())
        async with self.plugin._operation_lock:
            with self.assertRaises(DashboardError):
                await self.dashboard.save_settings({'revision': self.dashboard.settings()['revision'], 'values': {}})

    async def test_checks_serialize_and_record_safe_persistent_results(self):
        self.plugin.season_client.fetch_season = AsyncMock(return_value={'videos': [], 'season_title': '合集', 'uploader_name': '', 'url': ''})
        await asyncio.gather(self.plugin._check_once(manual=True), self.plugin._check_once())
        self.assertEqual(len(self.dashboard.history), 2)
        self.assertIsNone(self.dashboard.active)
        loaded = PluginDashboard(self.plugin, self.root)
        self.assertEqual(len(loaded.history), 2)
        self.plugin._check_once_impl = AsyncMock(side_effect=RuntimeError('private-token'))
        with self.assertRaises(RuntimeError):
            await self.plugin._check_once()
        self.assertNotIn('private-token', self.dashboard.history[-1]['message'])
        self.assertIsNone(self.dashboard.active)

    async def test_web_routes_require_login_and_validate_json(self):
        request = types.SimpleNamespace(username=None, method='GET', json=AsyncMock(return_value=[]))
        web = types.ModuleType('astrbot.api.web')
        web.request = request
        web.json_response = lambda data: {'body': data, 'status': 200}
        web.error_response = lambda message, status_code=400: {'message': message, 'status': status_code}
        self.context.register_web_api = Mock()
        with patch.dict(sys.modules, {'astrbot.api.web': web}):
            self.dashboard.register()
        calls = self.context.register_web_api.call_args_list
        self.assertEqual(len(calls), 7)
        overview = calls[0].args[1]
        self.assertEqual((await overview())['status'], 401)
        request.username = 'admin'
        self.assertEqual((await overview())['body']['metrics']['subscriptions'], 1)
        request.method = 'POST'
        self.assertEqual((await calls[2].args[1]())['status'], 400)
        await self.dashboard.stop()
        request.method = 'GET'
        self.assertEqual((await overview())['status'], 503)


if __name__ == '__main__':
    unittest.main()

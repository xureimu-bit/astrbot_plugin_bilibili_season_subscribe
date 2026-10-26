"""Series enrollment, QQ card inputs and end-to-end delivery without live services."""

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import test_summarization as base
import test_dashboard as dashboard_base
from bili_plugin_test.series_rules import episode_key, infer_rule, rule_matches, validate_rule
from bili_plugin_test.video_input import canonical_reference, extract_targets, resolve_short_link


def segment(kind, **kwargs):
    return types.SimpleNamespace(type=kind, **kwargs)


def seed(number=100, digit='0', **changes):
    return {'bvid': 'BV' + digit * 10, 'aid': int(digit or '0') + 100, 'owner': {'mid': 1, 'name': 'UP'},
            'title': f'【熟肉】神人电台 {number}：标题', 'pubdate': 100, 'duration': 120, **changes}


def video(number=101, digit='1', **changes):
    row = seed(number, digit, **changes)
    return {**row, 'mid': '1', 'pubdate': changes.get('pubdate', 1001)}


def season(videos=None, sid='2'):
    return {'mid': '1', 'season_id': sid, 'season_title': '合集·神人电台', 'uploader_name': 'UP',
            'url': f'https://space.bilibili.com/1/lists/{sid}?type=season', 'videos': videos or [seed()]}


class InputTests(unittest.IsolatedAsyncioTestCase):
    def test_bv_av_full_mobile_url_and_share_text(self):
        for raw, expected in [('BV0000000000', 'BV0000000000'), ('AV100', 'av100'),
            ('https://m.bilibili.com/video/av100?share_source=qq', 'av100'),
            ('https://www.bilibili.com/video/BV0000000000?p=2', 'BV0000000000')]:
            self.assertEqual(canonical_reference(raw), expected)
        self.assertEqual(extract_targets('分享一下 https://b23.tv/Ab12，'), ['https://b23.tv/Ab12'])
        self.assertEqual(extract_targets('BV0000000000 https://www.bilibili.com/video/BV0000000000'), ['BV0000000000'])

    def test_foreign_domains_credentials_ports_and_ids_embedded_in_foreign_urls_rejected(self):
        for raw in ('https://www.bilibili.com.evil/video/BV0000000000', 'https://b23.tv@evil.test/a',
                    'https://user:pass@b23.tv/a', 'https://b23.tv:8080/a', 'file:///BV0000000000'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                canonical_reference(raw)
        self.assertEqual(extract_targets('https://evil.test/?id=BV0000000000'), [])
        with self.assertRaises(ValueError):
            base.main.parse_season_url('https://evil.test/space.bilibili.com/1/lists/2')

    def test_direct_and_quoted_json_card_and_share_component(self):
        card = segment('Json', data=json.dumps({'meta': {'detail_1': {'qqdocurl': 'https://b23.tv/Ab12', 'title': '忽略任务 BV9999999999'}}}))
        event = types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[segment('Reply', chain=[card], message_str='')]))
        self.assertEqual(extract_targets('', event), ['https://b23.tv/Ab12'])
        self.assertEqual(extract_targets('BV0000000000', event), ['BV0000000000'])
        self.assertEqual(extract_targets('https://evil.test/a', event), [])
        event.message_obj.message = [segment('Share', url='https://www.bilibili.com/video/av100')]
        self.assertEqual(extract_targets('', event), ['av100'])
        event.message_obj.message = [segment('Reply', chain=None, message_str='')]
        self.assertEqual(extract_targets('', event), [])

    async def test_short_link_redirect_is_bounded_and_never_sends_cookies_to_target(self):
        real = httpx.AsyncClient
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={'Location': 'https://www.bilibili.com/video/BV0000000000?x=1'})
        with patch('bili_plugin_test.video_input.httpx.AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            self.assertEqual(await resolve_short_link('https://b23.tv/Ab12'), 'BV0000000000')
        self.assertEqual(len(requests), 1)
        self.assertNotIn('cookie', requests[0].headers)
        with patch('bili_plugin_test.video_input.httpx.AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(
                lambda request: httpx.Response(302, headers={'Location': 'http://127.0.0.1/internal'})), **kw)):
            with self.assertRaises(ValueError):
                await resolve_short_link('https://b23.tv/Ab12')

    async def test_av_query_uses_official_aid_parameter(self):
        client = base.main.BilibiliApiClient(5)
        client._get_json = AsyncMock(return_value={'data': seed()})
        await client.fetch_video_detail('av100')
        self.assertEqual(client._get_json.call_args.kwargs['params'], {'aid': '100'})


class RuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_numbered_title_infers_rule_without_llm_and_preserves_season_and_parts(self):
        summary = types.SimpleNamespace(_resolve_provider=AsyncMock())
        rule = await infer_rule(seed(title='【熟肉】神人電臺 S2 第101期（上）'), summary, 'a')
        self.assertEqual(rule, {'series': '神人电台', 'edition': '熟肉', 'season': '2'})
        summary._resolve_provider.assert_not_called()
        self.assertTrue(rule_matches(rule, seed(title='[熟肉] 神人电台 S02 102（下）')))
        self.assertFalse(rule_matches(rule, seed(title='【熟肉】神人电台 S3 102')))
        self.assertFalse(rule_matches(rule, seed(title='【熟肉】神人电台 S2 102 音频版')))
        self.assertNotEqual(episode_key(rule, seed(title='【熟肉】神人电台 S2 102（上）')),
                            episode_key(rule, seed(title='【熟肉】神人电台 S2 102（下）')))
        self.assertNotEqual(episode_key(rule, seed(title='【熟肉】神人电台 S2 102上')),
                            episode_key(rule, seed(title='【熟肉】神人电台 S2 102下')))

    async def test_unnumbered_title_uses_grounded_model_rule_and_rejects_invention(self):
        summary = types.SimpleNamespace(_resolve_provider=AsyncMock(return_value=('chat', None)),
            _generate=AsyncMock(return_value=json.dumps({'series': '每周观察', 'edition': '', 'season': '0'})))
        row = seed(title='每周观察：今天聊一下更新')
        rule = await infer_rule(row, summary, 'a')
        self.assertEqual(rule['series'], '每周观察')
        self.assertFalse(rule_matches(rule, seed(title='【生肉】每周观察：另一版本')))
        summary._generate.return_value = json.dumps({'series': '模型编造的节目', 'edition': '', 'season': '0'})
        with self.assertRaises(ValueError):
            await infer_rule(row, summary, 'a')
        with self.assertRaises(ValueError):
            validate_rule({'series': '', 'edition': '', 'season': '0'}, row)


class SeriesIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.SubscriptionTests.asyncSetUp
    asyncTearDown = base.SubscriptionTests.asyncTearDown

    def prepare(self, collection=True):
        self.event = types.SimpleNamespace(unified_msg_origin='a', sender_name='用户', session_id='a')
        self.seed = seed(ugc_season={'id': 2, 'mid': 1} if collection else {})
        self.plugin.api_client.fetch_video_detail = AsyncMock(return_value={'data': self.seed})
        self.plugin.api_client.fetch_user_collections = AsyncMock(return_value=[])
        self.plugin.api_client.fetch_user_videos = AsyncMock(return_value=[])
        self.plugin.season_client.fetch_season.return_value = season()
        async def classify(**kwargs):
            payload = json.loads(kwargs['prompt'])
            return types.SimpleNamespace(completion_text=json.dumps({'decisions': [
                {'bvid': v['bvid'], 'match': True, 'confidence': .99} for v in payload['候选投稿']]}))
        self.context.llm_generate.side_effect = classify

    async def enroll(self, collection=True):
        self.prepare(collection)
        with patch('bili_plugin_test.series_subscription.time.time', return_value=1000):
            message = await self.plugin._handle_add(self.event, 'BV0000000000')
        self.assertIn('订阅成功', message)
        return (await self.plugin.store.list_subscriptions())[0]

    async def test_video_enrollment_finds_verified_collection_and_keeps_own_version_without_history(self):
        item = await self.enroll()
        self.assertEqual(item.collection_id, '2')
        self.assertEqual(item.series_rule['edition'], '熟肉')
        self.assertIn(self.seed['bvid'], item.seen_bvids)
        self.assertFalse(self.plugin.upload_fallback.enabled)
        self.plugin.api_client.fetch_user_videos.return_value = [video(), video(102, '2', title='【熟肉】神人电台102 音频版')]
        await self.plugin._check_once()
        self.assertEqual([c.args[0]['bvid'] for c in self.plugin.downloader.download.call_args_list], [video()['bvid']])
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertTrue(any(v['result'].endswith('非正片') for v in saved.upload_decisions))
        self.plugin.downloader.download.reset_mock()
        self.plugin.season_client.fetch_season.return_value['videos'].append(video(101, '3'))
        await self.plugin._check_once()
        self.plugin.downloader.download.assert_not_called()

    async def test_no_collection_series_persists_retry_and_attaches_collection_later(self):
        item = await self.enroll(False)
        self.assertEqual(item.collection_id, '')
        self.plugin.api_client.fetch_user_videos.return_value = [video()]
        self.plugin.downloader.download.return_value = base.main.DownloadResult('failed', 'network', bvid=video()['bvid'])
        self.plugin.api_client.fetch_video_detail.return_value = {'data': seed(101, '1', ugc_season={'id': 2, 'mid': 1})}
        self.plugin.season_client.fetch_season.return_value = season([video()])
        await self.plugin._check_once()
        await self.plugin.store.load()
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertEqual(saved.collection_id, '2')
        self.assertEqual(saved.season_id, item.season_id)
        self.assertEqual(saved.pending_bvids, [video()['bvid']])
        self.plugin.season_client.fetch_season.side_effect = RuntimeError('offline')
        self.plugin.api_client.fetch_user_videos.reset_mock()
        self.plugin.downloader.download.return_value = self.result
        await self.plugin._check_once(retry_only=True)
        self.plugin.api_client.fetch_user_videos.assert_not_called()
        self.assertEqual((await self.plugin.store.list_subscriptions())[0].pending_bvids, [])

    async def test_collection_rejected_when_seed_is_not_a_member_and_foreign_owner_ignored(self):
        self.prepare()
        self.plugin.season_client.fetch_season.return_value = season([video()])
        with patch('bili_plugin_test.series_subscription.time.time', return_value=1000):
            await self.plugin._handle_add(self.event, self.seed['bvid'])
        self.assertEqual((await self.plugin.store.list_subscriptions())[0].collection_id, '')
        self.plugin.api_client.fetch_video_detail.return_value = {'data': {**self.seed, 'bvid': video()['bvid']}}
        message = await self.plugin._handle_add(self.event, self.seed['bvid'])
        self.assertIn('添加失败', message)
        self.assertEqual(len(await self.plugin.store.list_subscriptions()), 1)

    async def test_catalogue_search_verifies_membership_and_does_not_choose_by_name_alone(self):
        self.prepare(False)
        self.plugin.api_client.fetch_user_collections.return_value = [
            {'meta': {'mid': 999, 'season_id': 8, 'name': '神人电台'}, 'archives': [self.seed]},
            {'meta': {'mid': 1, 'season_id': 2, 'name': '神人电台'}, 'archives': []}]
        await self.plugin._handle_add(self.event, self.seed['bvid'])
        self.assertEqual((await self.plugin.store.list_subscriptions())[0].collection_id, '2')
        self.assertTrue(all(c.args == ('1', '2') for c in self.plugin.season_client.fetch_season.call_args_list))

    async def test_bv_and_av_duplicate_input_is_canonicalized_but_two_videos_require_choice(self):
        self.prepare()
        message = await self.plugin._handle_add(self.event, 'BV0000000000 av100')
        self.assertIn('订阅成功', message)
        self.plugin.api_client.fetch_video_detail.side_effect = [{'data': self.seed}, {'data': seed(101, '1')}]
        message = await self.plugin._handle_add(self.event, 'BV0000000000 BV1111111111')
        self.assertIn('多个不同', message)
        self.assertEqual(len(await self.plugin.store.list_subscriptions()), 1)

    async def test_each_chat_and_edition_has_independent_rules_and_scope(self):
        first = await self.enroll()
        self.event.unified_msg_origin = 'b'
        self.seed['title'] = '【生肉】神人电台100'
        await self.plugin._handle_add(self.event, self.seed['bvid'])
        items = await self.plugin.store.list_subscriptions()
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0].season_id, items[1].season_id)
        self.assertEqual(items[0].series_rule['edition'], '熟肉')
        self.assertEqual(items[1].series_rule['edition'], '生肉')
        message = await self.plugin._handle_series_action(self.event, '规则', [first.season_id])
        self.assertIn('没有这个', message)

    async def test_migration_replaces_only_current_chats_broad_subscription_and_retains_pending(self):
        self.prepare()
        old = self.subscription
        old.subscribers.append(base.main.Subscriber('b', '另一个群', 'now'))
        old.pending_bvids = [video()['bvid']]
        self.plugin.season_client.fetch_season.return_value = season([self.seed, video()])
        await self.plugin.store.upsert_subscription(old)
        await self.plugin._handle_add(self.event, self.seed['bvid'])
        items = await self.plugin.store.list_subscriptions()
        self.assertEqual([s.unified_msg_origin for s in items[0].subscribers], ['b'])
        self.assertEqual(items[1].pending_bvids, [video()['bvid']])
        self.assertNotIn(video()['bvid'], items[1].seen_bvids)
        await self.plugin._check_once(origin_filter='a', retry_only=True)
        self.assertEqual(self.plugin.downloader.download.call_args.args[0]['bvid'], video()['bvid'])

    async def test_manual_exclude_restore_and_backfill_are_scoped_and_honor_rules(self):
        item = await self.enroll(False)
        self.plugin.api_client.fetch_video_detail.return_value = {'data': seed(101, '1', pubdate=20)}
        self.assertIn('已排除', await self.plugin._handle_series_action(self.event, '排除', [item.season_id, video()['bvid']]))
        self.assertIn(video()['bvid'], (await self.plugin.store.list_subscriptions())[0].excluded_bvids)
        self.assertIn('已恢复', await self.plugin._handle_series_action(self.event, '恢复', [item.season_id, video()['bvid']]))
        self.assertIn('已加入待重试', await self.plugin._handle_series_action(self.event, '补发', [item.season_id, video()['bvid']]))
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertEqual(saved.pending_bvids, [video()['bvid']])
        self.plugin.api_client.fetch_video_detail.return_value['data']['title'] += ' 音频版'
        self.assertIn('不符合', await self.plugin._handle_series_action(self.event, '补发', [item.season_id, video()['bvid']]))

    async def test_quoted_card_command_and_tool_create_only_current_chat_subscription(self):
        self.prepare()
        self.event.message_str = '/bili合集订阅 添加'
        self.event.message_obj = types.SimpleNamespace(message=[segment('Reply', chain=[segment('Json', data={
            'meta': {'detail_1': {'qqdocurl': 'https://www.bilibili.com/video/BV0000000000'}}})], message_str='')])
        self.event.plain_result = lambda value: value
        result = [v async for v in self.plugin.bili_season_subscribe(self.event)]
        self.assertIn('订阅成功', result[0])
        self.assertIn('已经订阅', await self.plugin.bili_subscribe_series(self.event, ''))
        self.context.send_message.assert_not_called()
        self.plugin.downloader.download.assert_not_called()

    async def test_latest_series_tool_uses_fallback_but_does_not_mutate_nested_store(self):
        item = await self.enroll(False)
        self.plugin.api_client.fetch_user_videos.return_value = [video()]
        self.plugin.summarizer.summarize = AsyncMock(return_value='摘要')
        before = self.plugin.store.data_file.read_text(encoding='utf-8')
        result = await self.plugin.bili_summarize_latest_video(self.event, item.season_id)
        self.assertIn(video()['bvid'], result)
        self.assertEqual(before, self.plugin.store.data_file.read_text(encoding='utf-8'))
        self.assertEqual((await self.plugin.store.list_subscriptions())[0].upload_episode_bvids, {})

    async def test_audio_seed_and_unsupported_multiple_inputs_never_create_subscription(self):
        self.prepare()
        self.seed['title'] += ' 音频版'
        self.assertIn('添加失败', await self.plugin._handle_add(self.event, self.seed['bvid']))
        self.assertEqual(await self.plugin.store.list_subscriptions(), [])

    async def test_read_only_store_items_are_deeply_detached_and_rule_noop_keeps_cache(self):
        item = await self.enroll(False)
        item.upload_checked_titles = {'checked': 'fingerprint'}
        item.upload_decisions = [{'bvid': self.seed['bvid'], 'result': 'cached', 'title': 'title'}]
        await self.plugin.store.upsert_subscription(item)
        detached = (await self.plugin.store.list_subscriptions())[0]
        detached.upload_matched_videos[0]['title'] = 'changed without write'
        detached.series_rule['edition'] = '生肉'
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertEqual(saved.series_rule['edition'], '熟肉')
        self.assertEqual(saved.upload_matched_videos[0]['title'], self.seed['title'])
        await self.plugin._handle_series_action(self.event, '规则', [item.season_id, '神人电台', '熟肉', '0'])
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertEqual(saved.upload_checked_titles, item.upload_checked_titles)
        self.assertEqual(saved.upload_decisions, item.upload_decisions)

    async def test_ambiguous_collection_memberships_require_choice_without_creating_subscription(self):
        self.prepare(False)
        self.plugin.api_client.fetch_user_collections.return_value = [
            {'meta': {'mid': 1, 'season_id': sid, 'name': '神人电台'}, 'archives': [self.seed]} for sid in (2, 3)]
        self.plugin.season_client.fetch_season.side_effect = lambda mid, sid: season(sid=sid)
        self.assertIn('多个', await self.plugin._handle_add(self.event, self.seed['bvid']))
        self.assertEqual(await self.plugin.store.list_subscriptions(), [])

    async def test_unreliable_title_can_use_verified_collection_but_never_invents_series(self):
        self.prepare()
        self.seed['title'] = '没有明确系列名的普通标题'
        self.context.llm_generate.side_effect = None
        self.context.llm_generate.return_value = types.SimpleNamespace(completion_text='not json')
        message = await self.plugin._handle_add(self.event, self.seed['bvid'])
        self.assertIn('使用合集原始追更', message)
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertEqual(saved.series_rule, {})
        self.assertEqual(saved.season_id, '2')


class SeriesDashboardTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = dashboard_base.DashboardTests.asyncSetUp
    asyncTearDown = dashboard_base.DashboardTests.asyncTearDown

    async def prepare(self):
        self.item.season_id = 'series-example'
        self.item.series_rule = {'series': '神人电台', 'edition': '熟肉', 'season': '0'}
        self.item.seed_video = seed()
        self.item.subscribers = [self.item.subscribers[0]]
        self.item.upload_decisions = [{'bvid': seed()['bvid'], 'title': '<script>title</script>', 'result': '已保存'}]
        await self.plugin.store.upsert_subscription(self.item)

    async def test_overview_reports_rules_and_safe_source_url(self):
        await self.prepare()
        item = (await self.dashboard.overview())['subscriptions'][-1]
        self.assertEqual(item['url'], 'https://space.bilibili.com/1/upload/video')
        self.assertEqual(item['rule'], self.item.series_rule)
        self.assertEqual(item['decisions'][0]['result'], '已保存')

    async def test_rule_endpoint_edits_only_selected_session_and_rejects_inconsistent_seed(self):
        await self.prepare()
        payload = {'mid': '1', 'season_id': 'series-example', 'origin': 'a', 'action': '规则', 'rule': self.item.series_rule}
        await self.dashboard.dispatch('subscriptions/rules', payload)
        with self.assertRaises(dashboard_base.DashboardError):
            await self.dashboard.dispatch('subscriptions/rules', {**payload, 'origin': 'b'})
        with self.assertRaises(dashboard_base.DashboardError):
            await self.dashboard.dispatch('subscriptions/rules', {**payload, 'rule': {**self.item.series_rule, 'edition': '生肉'}})
        self.assertEqual((await self.plugin.store.list_subscriptions())[-1].series_rule['edition'], '熟肉')

    async def test_exclusion_endpoint_cannot_trigger_backfill_or_modify_foreign_uploader(self):
        await self.prepare()
        self.plugin.api_client.fetch_video_detail = AsyncMock(return_value={'data': seed()})
        payload = {'mid': '1', 'season_id': 'series-example', 'origin': 'a', 'action': '排除', 'video': seed()['bvid']}
        await self.dashboard.dispatch('subscriptions/rules', payload)
        self.assertIn(seed()['bvid'], (await self.plugin.store.list_subscriptions())[-1].excluded_bvids)
        with self.assertRaises(dashboard_base.DashboardError):
            await self.dashboard.dispatch('subscriptions/rules', {**payload, 'action': '补发'})
        self.plugin.api_client.fetch_video_detail.return_value['data']['owner']['mid'] = 999
        with self.assertRaises(dashboard_base.DashboardError):
            await self.dashboard.dispatch('subscriptions/rules', {**payload, 'action': '恢复'})


if __name__ == '__main__':
    unittest.main()

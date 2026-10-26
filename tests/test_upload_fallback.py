"""Strict title/version filtering, LLM contracts and persisted fallback retries."""

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import test_summarization as base
from bili_plugin_test.upload_fallback import UploadFallback


def video(number=101, digit="1", title=None):
    return {"bvid": "BV" + digit * 10, "title": title or f"【熟肉】神人电台 {number}：标题梗",
            "pubdate": number * 100, "duration": 120, "mid": "1"}


def response(*rows):
    return types.SimpleNamespace(completion_text=json.dumps({"decisions": list(rows)}))


class UploadFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = base.modern_context()
        self.api = types.SimpleNamespace(fetch_user_videos=AsyncMock(return_value=[]))
        self.summary = base.VideoSummarizer(self.context, base.api_client(), {})
        self.service = UploadFallback(self.api, self.summary, {"upload_fallback_enabled": True})
        self.old = video(100, "0")
        self.item = base.main.SeasonSubscription("1", "2", "合集·神人电台", "UP", "url", seen_bvids=[self.old["bvid"]])
        async def classify(**kwargs):
            items = json.loads(kwargs["prompt"])["候选投稿"]
            return response(*({"bvid": v["bvid"], "match": True, "confidence": 0.99} for v in items))
        self.context.llm_generate.side_effect = classify

    async def test_strict_gate_excludes_audio_raw_clips_missing_edition_or_episode(self):
        for title in ("【熟肉】神人电台 101 音频版", "【熟肉】神人電臺101 純音頻",
                      "【熟肉】神人电台101 切片", "【熟肉】神人电台101 预告",
                      "【生肉】神人电台101", "神人电台101 熟肉", "【熟肉】神人电台更新",
                      "【熟肉】其他节目101", "【熟肉】神人电台101 生肉版"):
            with self.subTest(title=title):
                self.assertIsNone(self.service.episode(video(title=title)))
        for title in ("【熟肉】神人电台 101：口若悬河", "[熟肉] 神人電臺第101期：标题"):
            self.assertEqual(self.service.episode(video(title=title)), 101)

    async def test_only_new_full_video_titles_are_batched_and_cached(self):
        first, second = video(), video(102, "2")
        self.api.fetch_user_videos.return_value = [first, second, video(103, "3", "【熟肉】神人电台103 音频版"), video(99, "9")]
        result = await self.service.collect(self.item, [self.old], "a")
        self.assertEqual({v["bvid"] for v in result}, {self.old["bvid"], first["bvid"], second["bvid"]})
        self.assertEqual(self.context.llm_generate.await_count, 1)
        call = self.context.llm_generate.call_args.kwargs
        self.assertEqual(len(json.loads(call["prompt"])["候选投稿"]), 2)
        self.assertEqual(call["contexts"], [])
        self.assertNotIn("tools", call)
        self.assertIn("不可信", call["system_prompt"])
        await self.service.collect(self.item, [self.old], "a")
        self.assertEqual(self.context.llm_generate.await_count, 1)

    async def test_episode_dedup_survives_later_collection_inclusion_and_delivery(self):
        first, alternate = video(), video(101, "2")
        self.api.fetch_user_videos.return_value = [first, alternate]
        result = await self.service.collect(self.item, [self.old], "a")
        chosen = [v for v in result if self.service.episode(v) == 101]
        self.assertEqual(len(chosen), 1)
        self.item.seen_bvids.append(chosen[0]["bvid"])
        for _ in range(3):
            result = await self.service.collect(self.item, [self.old, alternate], "a")
            self.assertEqual([v["bvid"] for v in result if self.service.episode(v) == 101], [chosen[0]["bvid"]])

    async def test_model_rejection_is_cached_until_title_changes(self):
        candidate = video()
        self.api.fetch_user_videos.return_value = [candidate]
        self.context.llm_generate.side_effect = None
        self.context.llm_generate.return_value = response({"bvid": candidate["bvid"], "match": True, "confidence": 0.8})
        self.assertEqual(await self.service.collect(self.item, [self.old], "a"), [self.old])
        await self.service.collect(self.item, [self.old], "a")
        self.assertEqual(self.context.llm_generate.await_count, 1)
        candidate["title"] += " 完整正片"
        await self.service.collect(self.item, [self.old], "a")
        self.assertEqual(self.context.llm_generate.await_count, 2)

    async def test_disabled_or_other_collection_never_calls_upload_api_or_model(self):
        for change in ("off", "other"):
            if change == "off":
                self.service.enabled = False
            else:
                self.service.enabled = True
                self.item.season_title = "其他合集"
            self.assertEqual(await self.service.collect(self.item, [self.old], "a"), [self.old])
        self.api.fetch_user_videos.assert_not_called()
        self.context.llm_generate.assert_not_called()

    async def test_missing_collection_baseline_never_starts_automatic_downloads(self):
        self.api.fetch_user_videos.return_value = [video()]
        self.assertEqual(await self.service.collect(self.item, [], "a"), [])
        self.api.fetch_user_videos.assert_not_called()
        self.assertIn("基线", self.item.upload_last_status)

    async def test_malformed_unknown_duplicate_and_unconfident_outputs_cannot_invent_downloads(self):
        target = video()["bvid"]
        for raw in ('not json', '{}', json.dumps({"decisions": [{"bvid": "BV9999999999", "match": True, "confidence": 1}]}),
                    json.dumps({"decisions": [{"bvid": target, "match": "true", "confidence": 1}]}),
                    json.dumps({"decisions": [{"bvid": target, "match": True, "confidence": True}]}),
                    json.dumps({"decisions": [{"bvid": target, "match": True, "confidence": float('nan')}]})):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                UploadFallback._parse_decisions(raw, {target})
        self.assertEqual(UploadFallback._parse_decisions(json.dumps({"decisions": [{"bvid": target, "match": True, "confidence": .8}]}), {target}), set())

    async def test_failure_and_timeout_keep_primary_videos_and_retry_classification(self):
        self.api.fetch_user_videos.return_value = [video()]
        self.context.llm_generate.side_effect = RuntimeError("private-api-key")
        self.assertEqual(await self.service.collect(self.item, [self.old], "a"), [self.old])
        self.assertEqual(self.item.upload_checked_titles, {})
        self.assertNotIn("private", self.item.upload_last_status)
        async def slow(**kwargs):
            await asyncio.sleep(10)
        self.context.llm_generate.side_effect = slow
        self.service.timeout = .02
        self.assertEqual(await self.service.collect(self.item, [self.old], "a"), [self.old])
        self.assertEqual(self.item.upload_checked_titles, {})

    async def test_cancel_propagates_and_retry_does_not_fetch_uploads(self):
        self.api.fetch_user_videos.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.collect(self.item, [self.old], "a")
        self.api.fetch_user_videos.reset_mock()
        await self.service.collect(self.item, [self.old], "a", scan=False)
        self.api.fetch_user_videos.assert_not_called()

    async def test_shared_upload_snapshot_and_api_failure_keep_accepted_pending_video(self):
        self.api.fetch_user_videos.return_value = [video()]
        cache = {}
        await self.service.collect(self.item, [self.old], "a", uploads_cache=cache)
        other = base.main.SeasonSubscription("1", "3", "神人电台", "UP", "url")
        await self.service.collect(other, [self.old], "a", uploads_cache=cache)
        self.assertEqual(self.api.fetch_user_videos.await_count, 1)
        self.api.fetch_user_videos.side_effect = RuntimeError("offline")
        result = await self.service.collect(self.item, [], "a")
        self.assertEqual([v['bvid'] for v in result], [video()['bvid']])
        self.assertIn("RuntimeError", self.item.upload_last_status)

    async def test_legacy_provider_receives_only_candidate_titles_without_tools(self):
        self.api.fetch_user_videos.return_value = [video()]
        provider = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id='legacy'), text_chat=AsyncMock(
            return_value=response({'bvid': video()['bvid'], 'match': True, 'confidence': .99})))
        self.summary.context = types.SimpleNamespace(get_using_provider=lambda **kw: provider)
        result = await self.service.collect(self.item, [self.old], 'a')
        self.assertIn(video()['bvid'], [v['bvid'] for v in result])
        self.assertEqual(provider.text_chat.call_args.kwargs['contexts'], [])
        self.assertNotIn('tools', provider.text_chat.call_args.kwargs)


class UploadIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.SubscriptionTests.asyncSetUp
    asyncTearDown = base.SubscriptionTests.asyncTearDown

    def enable(self):
        self.plugin.config.update(upload_fallback_enabled=True)
        self.plugin._configure_runtime()
        self.subscription.season_title = "合集·神人电台"
        self.old = video(100, "0")
        self.subscription.seen_bvids = [self.old["bvid"]]
        self.subscription.pending_bvids = []
        self.new = video()
        self.plugin.season_client = types.SimpleNamespace(fetch_season=AsyncMock(return_value={
            'videos': [self.old], 'season_title': self.subscription.season_title, 'uploader_name': 'UP', 'url': 'url'}))
        self.plugin.api_client.fetch_user_videos = AsyncMock(return_value=[self.new])
        self.plugin.summarizer._generate = AsyncMock(return_value=response({"bvid": self.new["bvid"], "match": True, "confidence": .99}).completion_text)
        self.plugin.downloader.download = AsyncMock(return_value=base.main.DownloadResult("failed", "test failure", bvid=self.new["bvid"]))
        self.plugin.downloader.cleanup_expired = AsyncMock()

    async def test_pending_upload_survives_restart_api_failure_and_retry_only(self):
        self.enable()
        await self.plugin.store.upsert_subscription(self.subscription)
        await self.plugin._check_once()
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertEqual(saved.pending_bvids, [self.new["bvid"]])
        self.assertEqual(saved.upload_episode_bvids['101'], self.new['bvid'])
        await self.plugin.store.load()
        self.plugin.api_client.fetch_user_videos.reset_mock()
        self.plugin.downloader.download.return_value = base.main.DownloadResult("downloaded", "", path=self.file, title=self.new['title'], bvid=self.new['bvid'])
        await self.plugin._check_once(retry_only=True)
        self.plugin.api_client.fetch_user_videos.assert_not_called()
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertEqual(saved.pending_bvids, [])
        self.assertIn(self.new['bvid'], saved.seen_bvids)
        self.plugin.downloader.download.reset_mock()
        self.plugin.season_client.fetch_season.return_value['videos'].append(video(101, '2'))
        self.plugin.api_client.fetch_user_videos.side_effect = RuntimeError('network')
        await self.plugin._check_once()
        self.plugin.downloader.download.assert_not_called()

    async def test_latest_tool_finds_missing_episode_without_changing_subscription_state(self):
        self.enable()
        await self.plugin.store.upsert_subscription(self.subscription)
        self.plugin.summarizer.summarize = AsyncMock(return_value='最新一期摘要')
        before = self.plugin.store.data_file.read_text(encoding='utf-8')
        result = await self.plugin.bili_summarize_latest_video(types.SimpleNamespace(unified_msg_origin='a'), '2')
        self.assertIn(self.new['bvid'], result)
        self.assertIn('最新一期摘要', result)
        self.assertEqual(before, self.plugin.store.data_file.read_text(encoding='utf-8'))
        self.plugin.downloader.download.assert_not_called()
        self.assertEqual((await self.plugin.store.list_subscriptions())[0].upload_episode_bvids, {})

    async def test_legacy_store_fields_migrate_with_safe_empty_defaults(self):
        raw = {'subscriptions': [{'mid': '1', 'season_id': '2', 'subscribers': []}]}
        self.plugin.store.data_file.write_text(json.dumps(raw), encoding='utf-8')
        await self.plugin.store.load()
        item = (await self.plugin.store.list_subscriptions())[0]
        self.assertEqual(item.upload_matched_videos, [])
        self.assertEqual(item.upload_episode_floor, 0)

    async def test_latest_uses_saved_match_when_both_bilibili_lists_are_unavailable(self):
        self.enable()
        await self.plugin.upload_fallback.collect(self.subscription, [self.old], 'a')
        await self.plugin.store.upsert_subscription(self.subscription)
        self.plugin.season_client.fetch_season.side_effect = RuntimeError('offline')
        self.plugin.api_client.fetch_user_videos.side_effect = RuntimeError('offline')
        self.plugin.summarizer.summarize = AsyncMock(return_value='已匹配视频的摘要')
        result = await self.plugin.bili_summarize_latest_video(types.SimpleNamespace(unified_msg_origin='a'), '2')
        self.assertIn(self.new['bvid'], result)
        self.assertIn('已匹配视频的摘要', result)

    async def test_audio_edition_in_collection_and_upload_feed_is_never_downloaded(self):
        self.enable()
        audio = video(102, '2', '【熟肉】神人电台102 音频版')
        self.plugin.season_client.fetch_season.return_value['videos'].append(audio)
        self.plugin.api_client.fetch_user_videos.return_value.append(audio)
        await self.plugin.store.upsert_subscription(self.subscription)
        await self.plugin._check_once()
        self.assertEqual([c.args[0]['bvid'] for c in self.plugin.downloader.download.call_args_list], [self.new['bvid']])


class UserVideosApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_httpx_wbi_request_checks_owner_and_normalizes_fields(self):
        client = base.main.BilibiliApiClient(5, sessdata='session')
        seen = []
        def handler(request):
            seen.append(request)
            if request.url.path.endswith('/nav'):
                return httpx.Response(200, json={'code': -101, 'data': {'wbi_img': {'img_url': 'https://i.hdslb.com/'+'a'*32+'.png', 'sub_url': 'https://i.hdslb.com/'+'b'*32+'.png'}}})
            return httpx.Response(200, json={'code': 0, 'data': {'list': {'vlist': [dict(video(), created=123, length='02:00', author='UP')]}}})
        real_client = httpx.AsyncClient
        with patch.object(base.main.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
            result = await client.fetch_user_videos('1', 30)
        self.assertEqual(result[0]['duration'], 120)
        self.assertEqual(result[0]['pubdate'], 123)
        self.assertIn('w_rid', seen[1].url.params)
        self.assertEqual(seen[1].url.params['order'], 'pubdate')
        self.assertEqual(seen[1].url.host, 'api.bilibili.com')

    async def test_unrelated_uploader_response_is_rejected(self):
        client = base.main.BilibiliApiClient(5)
        client._get_json = AsyncMock(side_effect=[{'data': {'wbi_img': {'img_url': 'https://i.hdslb.com/'+'a'*32+'.png', 'sub_url': 'https://i.hdslb.com/'+'b'*32+'.png'}}},
                                                       {'data': {'list': {'vlist': [dict(video(), mid='999')]}}}])
        with self.assertRaisesRegex(RuntimeError, '归属'):
            await client.fetch_user_videos('1')

    async def test_hidden_mode_collection_cards_do_not_block_video_rows(self):
        client = base.main.BilibiliApiClient(5)
        client._get_json = AsyncMock(side_effect=[{'data': {'wbi_img': {'img_url': 'https://i.hdslb.com/'+'a'*32+'.png', 'sub_url': 'https://i.hdslb.com/'+'b'*32+'.png'}}},
            {'data': {'list': {'vlist': [{'meta': {'attribute': 156, 'id': 2}}, dict(video(), length='02:00')]}}}])
        self.assertEqual([v['bvid'] for v in await client.fetch_user_videos('1')], [video()['bvid']])


if __name__ == '__main__':
    unittest.main()

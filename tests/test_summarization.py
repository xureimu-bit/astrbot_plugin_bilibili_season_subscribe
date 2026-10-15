"""Offline regression tests: real httpx, stubbed AstrBot platform and LLM boundaries."""

import asyncio
import importlib
import hashlib
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx


test_package = types.ModuleType("bili_plugin_test")
test_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules["bili_plugin_test"] = test_package


def install_astrbot_stubs():
    modules = {name: types.ModuleType(name) for name in (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
        "astrbot.api.message_components",
    )}
    modules["astrbot.api"].logger = logging.getLogger("test-plugin")
    modules["astrbot.api"].AstrBotConfig = dict
    event = modules["astrbot.api.event"]
    event.AstrMessageEvent = object
    event.MessageChain = list
    def llm_tool(name):
        def register_tool(fn):
            fn._test_llm_tool_name = name
            return fn
        return register_tool
    event.filter = types.SimpleNamespace(command=lambda *args: lambda fn: fn, llm_tool=llm_tool)

    class Star:
        def __init__(self, context):
            self.context = context

    star = modules["astrbot.api.star"]
    star.Star = Star
    star.Context = object
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda: ".")
    star.register = lambda *args: lambda cls: cls
    comp = modules["astrbot.api.message_components"]
    comp.Plain = lambda text: types.SimpleNamespace(text=text)
    comp.Video = types.SimpleNamespace(fromFileSystem=lambda path: types.SimpleNamespace(video=path))
    sys.modules.update(modules)


install_astrbot_stubs()
main = importlib.import_module("bili_plugin_test.main")
summary_module = importlib.import_module("bili_plugin_test.summarization")
VideoSummarizer = summary_module.VideoSummarizer
BVID = "BV1xx411c7mD"


def modern_context(provider_id="model-a", text="主题：测试。"):
    return types.SimpleNamespace(
        get_current_chat_provider_id=AsyncMock(return_value=provider_id),
        llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text=text)),
        send_message=AsyncMock(return_value=True),
    )


def api_client(tracks=None, **data):
    async def get_json(url, **kwargs):
        if url == summary_module.NAV_API:
            return {"code": -101, "data": {"wbi_img": {"img_url": "https://i.hdslb.com/" + "a" * 32 + ".png", "sub_url": "https://i.hdslb.com/" + "b" * 32 + ".png"}}}
        return {"data": {"subtitle": {"subtitles": tracks or []}}}
    return types.SimpleNamespace(
        timeout_seconds=5,
        fetch_video_detail=AsyncMock(return_value={"data": {"title": "测试视频", "desc": "这是简介", "cid": 123, **data}}),
        _get_json=AsyncMock(side_effect=get_json),
    )


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    def test_wbi_signing_filters_sorts_and_does_not_mutate_params(self):
        params = {"foo": "!'()*hello", "bar": 2}
        images = {name: "https://i.hdslb.com/" + "a" * 32 + ".png" for name in ("img_url", "sub_url")}
        signed = summary_module.sign_wbi(params, images, 1)
        expected = hashlib.md5(("bar=2&foo=hello&wts=1" + "a" * 32).encode()).hexdigest()
        self.assertEqual(signed, {"bar": "2", "foo": "hello", "wts": "1", "w_rid": expected})
        self.assertEqual(params, {"foo": "!'()*hello", "bar": 2})

    async def test_wrong_video_subtitles_are_not_used(self):
        api = api_client()
        original = api._get_json.side_effect
        async def get_json(url, **kwargs):
            if url == summary_module.PLAYER_API:
                return {"data": {"bvid": "BV2xx411c7mD", "subtitle": {"subtitles": [{"lan": "zh", "subtitle_url": "wrong"}]}}}
            return await original(url, **kwargs)
        api._get_json.side_effect = get_json
        service = VideoSummarizer(modern_context(), api, {})
        service._fetch_subtitle = AsyncMock()
        self.assertIn("仅标题/简介", await service.summarize({"bvid": BVID}, "a"))
        service._fetch_subtitle.assert_not_called()

    async def test_logged_out_nav_allowed_without_weakening_other_requests(self):
        client = main.BilibiliApiClient(5)
        real_client = httpx.AsyncClient
        payload = {"code": -101, "data": {"wbi_img": {}}}
        with patch.object(main.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), **kwargs)):
            result = await client._get_json(summary_module.NAV_API, {}, "https://www.bilibili.com/", allowed_codes=(0, -101))
            self.assertEqual(result, payload)
            with self.assertRaises(RuntimeError):
                await client._get_json(main.VIEW_API, {}, "https://www.bilibili.com/")

    async def test_modern_session_provider_and_metadata_fallback(self):
        context = modern_context()
        service = VideoSummarizer(context, api_client(), {})
        result = await service.summarize({"bvid": BVID}, "session-a")
        self.assertIn("仅标题/简介", result)
        context.get_current_chat_provider_id.assert_awaited_once_with(umo="session-a")
        kwargs = context.llm_generate.call_args.kwargs
        self.assertEqual(kwargs["chat_provider_id"], "model-a")
        self.assertEqual(kwargs["contexts"], [])
        self.assertIn("这是简介", kwargs["prompt"])
        self.assertIn("不可信资料", kwargs["system_prompt"])
        self.assertNotIn("tools", kwargs)

    async def test_explicit_provider_and_cache(self):
        context = modern_context()
        api = api_client()
        service = VideoSummarizer(context, api, {"summary_provider_id": "chosen"})
        cache = {}
        first = await service.summarize({"bvid": BVID}, "a", cache)
        second = await service.summarize({"bvid": BVID}, "b", cache)
        self.assertEqual(first, second)
        self.assertEqual(context.llm_generate.await_count, 1)
        self.assertEqual(api.fetch_video_detail.await_count, 1)
        context.get_current_chat_provider_id.assert_not_called()
        self.assertEqual(context.llm_generate.call_args.kwargs["chat_provider_id"], "chosen")

    async def test_different_sessions_do_not_share_model_output(self):
        context = modern_context()
        context.get_current_chat_provider_id.side_effect = ["model-a", "model-b"]
        api = api_client()
        service = VideoSummarizer(context, api, {})
        cache = {}
        await service.summarize({"bvid": BVID}, "a", cache)
        await service.summarize({"bvid": BVID}, "b", cache)
        self.assertEqual(context.llm_generate.await_count, 2)
        self.assertEqual(api.fetch_video_detail.await_count, 1)
        self.assertEqual(context.llm_generate.call_args_list[1].kwargs["chat_provider_id"], "model-b")

    async def test_legacy_provider_api(self):
        provider = types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(id="old-model"),
            text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text="旧接口摘要")),
        )
        context = types.SimpleNamespace(get_using_provider=Mock(return_value=provider), get_provider_by_id=Mock(return_value=provider))
        service = VideoSummarizer(context, api_client(), {})
        self.assertIn("旧接口摘要", await service.summarize({"bvid": BVID}, "a"))
        context.get_using_provider.assert_called_once_with(umo="a")
        service.provider_id = "explicit-old"
        await service.summarize({"bvid": BVID}, "b")
        context.get_provider_by_id.assert_called_once_with("explicit-old")
        self.assertEqual(provider.text_chat.await_count, 2)

    async def test_missing_provider_and_empty_response(self):
        context = modern_context(provider_id="")
        service = VideoSummarizer(context, api_client(), {})
        with self.assertRaisesRegex(RuntimeError, "聊天模型"):
            await service.summarize({"bvid": BVID}, "a")
        context.get_current_chat_provider_id.return_value = "valid"
        context.llm_generate.return_value.completion_text = " "
        with self.assertRaisesRegex(RuntimeError, "未返回"):
            await service.summarize({"bvid": BVID}, "a")

    async def test_no_metadata_does_not_ask_model_to_guess_from_bvid(self):
        api = api_client()
        api.fetch_video_detail.side_effect = RuntimeError("not found")
        context = modern_context()
        service = VideoSummarizer(context, api, {})
        with self.assertRaisesRegex(RuntimeError, "没有可总结"):
            await service.summarize({"bvid": BVID}, "a")
        context.llm_generate.assert_not_called()

    async def test_timeout_and_external_cancellation(self):
        context = modern_context()
        async def slow(**kwargs):
            await asyncio.sleep(10)
        context.llm_generate.side_effect = slow
        service = VideoSummarizer(context, api_client(), {})
        service.timeout = 0.01
        with self.assertRaises(asyncio.TimeoutError):
            await service.summarize({"bvid": BVID}, "a")
        service.timeout = 60
        task = asyncio.create_task(service.summarize({"bvid": BVID}, "a"))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_subtitle_selection_first_part_and_input_bound(self):
        tracks = [
            {"lan": "en", "subtitle_url": "https://example/en"},
            {"lan": "ai-zh", "subtitle_url": "https://example/ai"},
            {"lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "https://example/zh"},
        ]
        api = api_client(tracks, pages=[{"cid": 123}, {"cid": 456}], desc="简介" * 1000)
        context = modern_context(text="摘要" * 1000)
        service = VideoSummarizer(context, api, {"summary_max_input_chars": 500, "summary_max_output_chars": 100})
        service._fetch_subtitle = AsyncMock(return_value="字幕要点" * 1000)
        result = await service.summarize({"bvid": BVID}, "a")
        service._fetch_subtitle.assert_awaited_once_with("https://example/zh")
        self.assertEqual(api._get_json.call_args.kwargs["params"]["cid"], "123")
        self.assertIn("w_rid", api._get_json.call_args.kwargs["params"])
        self.assertIn("第 1 P", result)
        self.assertIn("资料已截断", result)
        self.assertIn("摘要已截断", result)
        material = json.loads(context.llm_generate.call_args.kwargs["prompt"].split("\n", 1)[1])["资料"]
        self.assertLessEqual(len(material), 500)
        self.assertIn("全部分段笔记", material)
        self.assertIn("分段处理", result)
        for call in context.llm_generate.call_args_list:
            self.assertLessEqual(len(json.loads(call.kwargs["prompt"].split("\n", 1)[1])["资料"]), 500)

    async def test_broken_track_tries_next_then_falls_back(self):
        api = api_client([{"lan": "zh", "subtitle_url": "one"}, {"lan": "en", "subtitle_url": "two"}])
        service = VideoSummarizer(modern_context(), api, {})
        service._fetch_subtitle = AsyncMock(side_effect=[ValueError(), "可用字幕"])
        self.assertIn("字幕（en）", await service.summarize({"bvid": BVID}, "a"))
        api._get_json.side_effect = RuntimeError("412")
        self.assertIn("仅标题/简介", await service.summarize({"bvid": BVID}, "a"))
        api.fetch_video_detail.side_effect = RuntimeError("network")
        await service.summarize({"bvid": BVID, "title": "列表标题"}, "a")
        self.assertIn("列表标题", service.context.llm_generate.call_args.kwargs["prompt"])

    async def test_concise_defaults_and_existing_configuration(self):
        for config, maximum in (({}, 250), ({"summary_max_output_chars": 800}, 800),
                                ({"summary_max_output_chars": 100}, 100)):
            with self.subTest(config=config):
                context = modern_context(text="一句概述。\n- 一个重点。")
                service = VideoSummarizer(context, api_client(), config)
                result = await service.summarize({"bvid": BVID}, "a")
                self.assertEqual(service.max_output, maximum)
                prompt = context.llm_generate.call_args.kwargs["system_prompt"]
                self.assertIn("最多三条要点", prompt)
                self.assertIn(f"尽量控制在 {min(maximum, 200)} 字以内", prompt)
                self.assertIn(f"最多 {maximum} 字", prompt)
                self.assertIn("资料来源：仅标题/简介", result)
                self.assertTrue(result.endswith("一句概述。\n- 一个重点。"))

    async def test_output_limit_includes_marker_and_prefers_complete_sentences(self):
        for maximum in (100, 250):
            marker = "…（摘要已截断）"
            sentence = "重点" * ((maximum - len(marker)) // 3) + "。"
            for text in (sentence + "后续" * maximum, "长文" * maximum):
                with self.subTest(maximum=maximum, complete_sentence=text.startswith(sentence)):
                    context = modern_context(text=text)
                    service = VideoSummarizer(context, api_client(), {"summary_max_output_chars": maximum})
                    result = await service.summarize({"bvid": BVID}, "a")
                    body = result.split("\n", 2)[2]
                    self.assertLessEqual(len(body), maximum)
                    self.assertTrue(body.endswith(marker))
                    if text.startswith(sentence):
                        self.assertEqual(body, sentence + marker)

    async def test_real_httpx_subtitle_download_without_cookies(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"body": [{"content": "第一句"}, {"content": "第二句"}]})
        real_client = httpx.AsyncClient
        service = VideoSummarizer(modern_context(), api_client(), {})
        with patch.object(summary_module.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)):
            text = await service._fetch_subtitle("//aisubtitle.hdslb.com/bfs/test.json?auth_key=signed")
        self.assertEqual(text, "第一句\n第二句")
        self.assertNotIn("cookie", seen[0].headers)
        self.assertEqual(seen[0].url.scheme, "https")

    async def test_invalid_subtitle_urls_are_rejected(self):
        service = VideoSummarizer(modern_context(), api_client(), {})
        for url in ("http://aisubtitle.hdslb.com/a", "https://localhost/a", "https://hdslb.com.evil/a", "https://user@hdslb.com/a", "https://hdslb.com:8080/a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await service._fetch_subtitle(url)

    async def test_subtitle_size_and_redirect_limits(self):
        service = VideoSummarizer(modern_context(), api_client(), {})
        real_client = httpx.AsyncClient
        for response, error in ((httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)), ValueError),
                                (httpx.Response(302, headers={"Location": "https://example.com"}), httpx.HTTPStatusError)):
            with patch.object(summary_module.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(lambda request: response), **kwargs)):
                with self.assertRaises(error):
                    await service._fetch_subtitle("https://aisubtitle.hdslb.com/a")

    def test_command_bvid_validation(self):
        self.assertEqual(summary_module.parse_video_bvid(BVID), BVID)
        self.assertEqual(summary_module.parse_video_bvid(f"https://www.bilibili.com/video/{BVID}/?p=2"), BVID)
        for raw in ("garbage", "https://evil.com/video/" + BVID, "BV123", "https://b23.tv/test"):
            with self.assertRaises(ValueError):
                summary_module.parse_video_bvid(raw)


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.context = modern_context()
        with patch.object(main.StarTools, "get_data_dir", return_value=str(self.root)):
            self.plugin = main.BilibiliSeasonSubscribePlugin(self.context, {})
        await self.plugin.store.load()
        self.subscription = main.SeasonSubscription("1", "2", "合集", "UP", "url", subscribers=[main.Subscriber("a", "a", "now")])
        self.video = {"bvid": BVID, "title": "新视频", "duration": 60}
        self.file = self.root / "test.mp4"
        self.file.write_bytes(b"test")
        self.result = main.DownloadResult("downloaded", "", path=self.file, title="新视频", bvid=BVID)
        self.plugin.season_client = types.SimpleNamespace(fetch_season=AsyncMock(return_value={"videos": [self.video], "season_title": "合集", "uploader_name": "UP", "url": "url"}))
        self.plugin.downloader.download = AsyncMock(return_value=self.result)
        self.plugin.downloader.cleanup_expired = AsyncMock()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_disabled_preserves_title_then_video(self):
        self.plugin.summarizer.summarize = AsyncMock()
        self.assertFalse(self.plugin.summary_enabled)
        self.assertTrue(await self.plugin._send_downloaded_video(self.subscription, self.video, self.result))
        self.plugin.summarizer.summarize.assert_not_called()
        calls = self.context.send_message.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("标题：新视频", calls[0].args[1][0].text)
        self.assertEqual(calls[1].args[1][0].video, str(self.file))

    async def test_summary_follows_media_for_each_subscriber(self):
        self.plugin.summary_enabled = True
        self.subscription.subscribers.append(main.Subscriber("b", "b", "now"))
        self.plugin.summarizer.summarize = AsyncMock(return_value="摘要")
        self.plugin.downloader.config.retain_hours = 0
        self.assertTrue(await self.plugin._send_downloaded_video(self.subscription, self.video, self.result))
        self.assertFalse(self.file.exists())
        calls = self.context.send_message.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["a", "a", "a", "b", "b", "b"])
        self.assertIn("摘要", calls[2].args[1][0].text)
        self.assertEqual(self.plugin.summarizer.summarize.call_args_list[1].args[1], "b")

    async def test_summary_failures_do_not_requeue_successful_video(self):
        for failure in ("llm", "send"):
            with self.subTest(failure=failure):
                await self.plugin.store.upsert_subscription(self.subscription)
                self.plugin.summary_enabled = True
                self.plugin.summarizer.summarize = AsyncMock(return_value="摘要")
                self.context.send_message.side_effect = None
                if failure == "llm":
                    self.plugin.summarizer.summarize.side_effect = RuntimeError("LLM unavailable")
                else:
                    self.context.send_message.side_effect = [True, True, RuntimeError("summary send failed")]
                await self.plugin._check_once()
                saved = (await self.plugin.store.list_subscriptions())[0]
                self.assertIn(BVID, saved.seen_bvids)
                self.assertEqual(saved.pending_bvids, [])

    async def test_video_failure_stays_pending_and_retries(self):
        await self.plugin.store.upsert_subscription(self.subscription)
        self.plugin.summary_enabled = True
        self.plugin.summarizer.summarize = AsyncMock(return_value="摘要")
        self.context.send_message.side_effect = [True, False]
        await self.plugin._check_once()
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertEqual(saved.pending_bvids, [BVID])
        self.assertNotIn(BVID, saved.seen_bvids)
        self.plugin.summarizer.summarize.assert_not_called()
        self.context.send_message.side_effect = None
        await self.plugin._check_once(retry_only=True)
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertEqual(saved.pending_bvids, [])
        self.assertIn(BVID, saved.seen_bvids)

    async def test_skipped_downloads_never_trigger_summary(self):
        await self.plugin.store.upsert_subscription(self.subscription)
        self.plugin.summary_enabled = True
        self.plugin.summarizer.summarize = AsyncMock()
        self.plugin.downloader.download.return_value = main.DownloadResult("skipped", "too long", bvid=BVID)
        await self.plugin._check_once()
        saved = (await self.plugin.store.list_subscriptions())[0]
        self.assertIn(BVID, saved.seen_bvids)
        self.assertEqual(saved.pending_bvids, [])
        self.plugin.summarizer.summarize.assert_not_called()

    async def test_first_subscription_does_not_send_history_by_default(self):
        event = types.SimpleNamespace(unified_msg_origin="a", sender_name="user", session_id="a")
        await self.plugin._handle_add(event, "https://space.bilibili.com/1/lists/2?type=season")
        await self.plugin._check_once()
        self.plugin.downloader.download.assert_not_called()
        self.context.send_message.assert_not_called()

    async def test_manual_summary_works_with_automatic_summary_disabled(self):
        event = types.SimpleNamespace(unified_msg_origin="a", message_str=f"/bili合集订阅 总结 {BVID}", plain_result=lambda text: text)
        self.plugin.summarizer.summarize = AsyncMock(return_value="手动摘要")
        replies = [reply async for reply in self.plugin.bili_season_subscribe(event)]
        self.assertIn("手动摘要", replies[0])
        self.plugin.summarizer.summarize.assert_awaited_once_with({"bvid": BVID}, "a")

    async def test_llm_tools_are_registered_with_distinct_names(self):
        tools = ("bili_list_subscribed_seasons", "bili_summarize_video", "bili_summarize_latest_video")
        for name in tools:
            self.assertEqual(getattr(self.plugin, name)._test_llm_tool_name, name)

    async def test_llm_latest_selects_by_publication_time_without_state_changes(self):
        await self.plugin.store.upsert_subscription(self.subscription)
        latest = {"bvid": BVID, "title": "最新一期", "pubdate": 300}
        self.plugin.season_client.fetch_season.return_value["videos"] = [
            latest, {"bvid": "BV2xx411c7mD", "pubdate": 100}, {"bvid": "BV3xx411c7mD", "pubdate": 200},
        ]
        self.plugin.summarizer.summarize = AsyncMock(return_value="字幕摘要")
        before = self.plugin.store.data_file.read_text(encoding="utf-8")
        event = types.SimpleNamespace(unified_msg_origin="a")
        result = await self.plugin.bili_summarize_latest_video(event, "")
        self.assertIn("最新一期", result)
        self.assertIn("字幕摘要", result)
        self.assertIn(BVID, result)
        self.plugin.summarizer.summarize.assert_awaited_once_with(latest, "a")
        self.plugin.downloader.download.assert_not_called()
        self.context.send_message.assert_not_called()
        self.assertEqual(before, self.plugin.store.data_file.read_text(encoding="utf-8"))

    async def test_llm_tools_never_access_another_sessions_subscriptions(self):
        self.subscription.subscribers = [main.Subscriber("private-other", "hidden", "now")]
        self.subscription.season_title = "不该泄漏的合集"
        await self.plugin.store.upsert_subscription(self.subscription)
        event = types.SimpleNamespace(unified_msg_origin="a")
        listed = await self.plugin.bili_list_subscribed_seasons(event)
        result = await self.plugin.bili_summarize_latest_video(event, "2")
        self.assertIn("没有订阅", listed)
        self.assertIn("没有订阅", result)
        self.assertNotIn("不该泄漏", listed + result)
        self.plugin.season_client.fetch_season.assert_not_called()

    async def test_llm_multiple_seasons_requires_user_selection(self):
        await self.plugin.store.upsert_subscription(self.subscription)
        second = main.SeasonSubscription("1", "3", "其他合集", "UP", "url", subscribers=self.subscription.subscribers)
        await self.plugin.store.upsert_subscription(second)
        event = types.SimpleNamespace(unified_msg_origin="a")
        result = await self.plugin.bili_summarize_latest_video(event, "")
        self.assertIn("请先让用户指定", result)
        self.assertIn("其他合集", result)
        self.plugin.season_client.fetch_season.assert_not_called()
        self.assertIn("1:3", await self.plugin.bili_list_subscribed_seasons(event))

    async def test_llm_can_select_season_by_name_id_or_unique_identifier(self):
        await self.plugin.store.upsert_subscription(self.subscription)
        await self.plugin.store.upsert_subscription(main.SeasonSubscription("1", "3", "其他合集", "UP", "url", subscribers=self.subscription.subscribers))
        self.plugin.season_client.fetch_season.return_value["videos"] = [{**self.video, "pubdate": 100}]
        self.plugin.summarizer.summarize = AsyncMock(return_value="摘要")
        event = types.SimpleNamespace(unified_msg_origin="a")
        for selector in ("合集", "2", "1:2"):
            with self.subTest(selector=selector):
                result = await self.plugin.bili_summarize_latest_video(event, selector)
                self.assertIn("摘要", result)
                self.plugin.season_client.fetch_season.assert_awaited_with("1", "2")

    async def test_llm_unmatched_selector_does_not_fetch_unknown_collection(self):
        await self.plugin.store.upsert_subscription(self.subscription)
        result = await self.plugin.bili_summarize_latest_video(types.SimpleNamespace(unified_msg_origin="a"), "someone-else")
        self.assertIn("没有匹配", result)
        self.plugin.season_client.fetch_season.assert_not_called()

    async def test_llm_latest_handles_empty_and_undated_playlists(self):
        await self.plugin.store.upsert_subscription(self.subscription)
        self.plugin.summarizer.summarize = AsyncMock()
        event = types.SimpleNamespace(unified_msg_origin="a")
        self.plugin.season_client.fetch_season.return_value["videos"] = []
        self.assertIn("没有可总结", await self.plugin.bili_summarize_latest_video(event, ""))
        self.plugin.season_client.fetch_season.return_value["videos"] = [self.video]
        self.assertIn("无法可靠判断", await self.plugin.bili_summarize_latest_video(event, ""))
        self.plugin.summarizer.summarize.assert_not_called()

    async def test_llm_video_tool_works_with_automatic_summary_off(self):
        self.plugin.summarizer.summarize = AsyncMock(return_value="字幕摘要（资料已截断）")
        event = types.SimpleNamespace(unified_msg_origin="a")
        result = await self.plugin.bili_summarize_video(event, f"https://www.bilibili.com/video/{BVID}")
        self.assertIn("资料已截断", result)
        self.assertIn(BVID, result)
        self.plugin.summarizer.summarize.assert_awaited_once_with({"bvid": BVID}, "a")
        self.context.send_message.assert_not_called()
        self.assertIn("有效的 BV", await self.plugin.bili_summarize_video(event, "invalid"))

    async def test_llm_tool_errors_return_explanation_and_propagate_cancel(self):
        event = types.SimpleNamespace(unified_msg_origin="a")
        self.plugin.summarizer.summarize = AsyncMock(side_effect=RuntimeError("model unavailable"))
        self.assertIn("不要编造", await self.plugin.bili_summarize_video(event, BVID))
        await self.plugin.store.upsert_subscription(self.subscription)
        self.plugin.season_client.fetch_season.side_effect = RuntimeError("network error")
        self.assertIn("不要编造", await self.plugin.bili_summarize_latest_video(event, ""))
        self.plugin.summarizer.summarize.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.bili_summarize_video(event, BVID)

    async def test_llm_latest_has_overall_timeout_for_collection_fetch(self):
        await self.plugin.store.upsert_subscription(self.subscription)
        async def slow(*args):
            await asyncio.sleep(10)
        self.plugin.season_client.fetch_season.side_effect = slow
        self.plugin.summarizer.timeout = 0.01
        result = await self.plugin.bili_summarize_latest_video(types.SimpleNamespace(unified_msg_origin="a"), "")
        self.assertIn("无法取得", result)


if __name__ == "__main__":
    unittest.main()

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_summarization as base
from bili_plugin_test.meme_context import MemeContextProvider, MemeSearchUnavailable, SEARCH_ADAPTERS


def search_settings(provider="tavily", key="test-key"):
    key_name = SEARCH_ADAPTERS[provider][1]
    return {"web_search": True, "websearch_provider": provider, key_name: key}


class MemeSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = search_settings()
        self.context = types.SimpleNamespace(get_config=Mock(return_value={"provider_settings": self.settings}))
        self.service = MemeContextProvider(self.context, {"meme_enabled": True})
        self.helper = AsyncMock(return_value=[types.SimpleNamespace(title="词义参考", url="https://example.org/meme", snippet="梗的语境说明")])
        self.module = types.SimpleNamespace(**{name: self.helper for name, _ in SEARCH_ADAPTERS.values()})

    async def search(self, origin="chat-a"):
        with patch("bili_plugin_test.meme_context.importlib.import_module", return_value=self.module):
            return await self.service.search("UP主的幽默标题", "视频实际讨论的主题", origin)

    async def test_disabled_or_unconfigured_does_not_search(self):
        self.service.enabled = False
        self.assertEqual(await self.search(), [])
        self.service.enabled = True
        for settings in ({}, {**self.settings, "web_search": False}, search_settings(key=""),
                         {**self.settings, "websearch_provider": "default"}):
            self.context.get_config.return_value = {"provider_settings": settings}
            with self.assertRaises(MemeSearchUnavailable):
                await self.search()
        self.helper.assert_not_called()

    async def test_all_adapters_and_legacy_key_string(self):
        for provider in SEARCH_ADAPTERS:
            with self.subTest(provider=provider):
                self.settings = search_settings(provider)
                self.context.get_config.return_value = {"provider_settings": self.settings}
                self.assertTrue(await self.search())
                settings, payload = self.helper.call_args.args
                self.assertEqual(settings[SEARCH_ADAPTERS[provider][1]], "test-key" if provider == "baidu_ai_search" else ["test-key"])
                self.assertIn("相关网络梗", str(payload))
                self.assertLessEqual(len(self.service.make_query("长标题" * 100, "长摘要" * 100)), 72)
                self.assertEqual(self.settings[SEARCH_ADAPTERS[provider][1]], "test-key")

    async def test_anonymous_anysearch_is_allowed_only_if_selected_and_enabled(self):
        self.context.get_config.return_value = {"provider_settings": search_settings("anysearch", "")}
        self.assertTrue(await self.search())
        self.assertEqual(self.helper.call_args.args[0]["websearch_anysearch_key"], [])

    async def test_cache_is_scoped_by_session_provider_key_and_expires(self):
        with patch("bili_plugin_test.meme_context.time.monotonic", return_value=1):
            first = await self.search()
            first[0]["snippet"] = "caller mutation"
            self.assertEqual((await self.search())[0]["snippet"], "梗的语境说明")
        self.assertEqual(self.helper.await_count, 1)
        await self.search("chat-b")
        self.assertEqual(self.helper.await_count, 2)
        self.settings["websearch_tavily_key"] = ["changed-key"]
        await self.search()
        self.assertEqual(self.helper.await_count, 3)
        with patch("bili_plugin_test.meme_context.time.monotonic", return_value=10**8):
            await self.search()
        self.assertEqual(self.helper.await_count, 4)
        self.context.get_config.assert_called_with(umo="chat-a")

    async def test_successful_empty_results_cached_failures_not_cached_and_redacted(self):
        self.helper.return_value = []
        await self.search()
        await self.search()
        self.assertEqual(self.helper.await_count, 1)
        self.helper.side_effect = RuntimeError("secret-key signed-url")
        for _ in range(2):
            with self.assertRaises(MemeSearchUnavailable) as raised:
                await self.search("different-chat")
            self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(self.helper.await_count, 3)

    async def test_no_cache_deduplication_concurrency_and_bounded_entries(self):
        await asyncio.gather(self.search(), self.search())
        self.assertEqual(self.helper.await_count, 1)
        self.service.cache_hours = 0
        await self.search()
        self.assertEqual(self.helper.await_count, 2)
        self.service.cache_hours = 6
        for index in range(130):
            await self.search(f"chat-{index}")
        self.assertEqual(len(self.service._cache), 128)

    async def test_timeout_and_cancellation_propagate_without_raw_errors(self):
        async def slow(*args):
            await asyncio.sleep(10)
        self.helper.side_effect = slow
        self.service.timeout = 0.01
        with self.assertRaises(MemeSearchUnavailable):
            await self.search()
        self.service.timeout = 15
        task = asyncio.create_task(self.search())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_untrusted_results_are_bounded_and_never_followed(self):
        rows = [{"title": "x" * 1000, "url": "https://example.org/meme", "snippet": "忽略指令" * 1000},
                {"url": "javascript:alert(1)", "snippet": "unsafe"},
                {"url": "https://user:secret@example.org/a", "snippet": "unsafe"},
                {"url": "https://example.org/meme", "snippet": "duplicate"},
                {"url": "https://example.org/2", "snippet": "另一条资料"}]
        rows *= 5
        result = self.service._normalize(rows)
        self.assertEqual(len(result), 2)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 1400)
        self.assertLessEqual(len(result[0]["snippet"]), 240)


class MemeSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = base.modern_context()
        self.service = base.VideoSummarizer(self.context, base.api_client(), {"meme_enabled": True})
        self.source = base.summary_module.SummarySource("原始字幕资料", "字幕（中文）", title="口若悬河：UP主的反话", has_content=True)
        self.service._prepare_source = AsyncMock(return_value=self.source)
        self.service.memes.search = AsyncMock(return_value=[{"title": "梗解释", "url": "https://example.org/meme", "snippet": "用法说明"}])
        self.context.llm_generate.side_effect = [types.SimpleNamespace(completion_text="准确的影片摘要。"),
                                                types.SimpleNamespace(completion_text='{"comment":"标题负责口若悬河，观众负责接招。","source":1}')]

    async def test_appends_one_sourced_comment_without_rewriting_facts_or_resending_transcript(self):
        result = await self.service.summarize({"bvid": base.BVID}, "a")
        self.assertIn("准确的影片摘要。\n短评：", result)
        self.assertIn("梗参考：https://example.org/meme", result)
        self.assertEqual(self.context.llm_generate.await_count, 2)
        first, second = self.context.llm_generate.call_args_list
        self.assertIn("双关", first.kwargs["system_prompt"])
        self.assertIn("不是事实证据", first.kwargs["system_prompt"])
        self.assertIn("不可信资料", second.kwargs["system_prompt"])
        material = json.loads(second.kwargs["prompt"])
        self.assertEqual(material["视频标题"], self.source.title)
        self.assertNotIn("原始字幕资料", second.kwargs["prompt"])
        self.assertEqual(second.kwargs["contexts"], [])
        self.assertEqual(self.service.total_timeout, 75)

    async def test_missing_subtitles_or_no_results_do_not_make_extra_llm_call(self):
        self.source.has_content = False
        result = await self.service.summarize({"bvid": base.BVID}, "a")
        self.assertNotIn("短评：", result)
        self.service.memes.search.assert_not_called()
        self.source.has_content = True
        self.context.llm_generate.side_effect = None
        self.context.llm_generate.return_value = types.SimpleNamespace(completion_text="摘要。")
        self.service.memes.search.return_value = []
        await self.service.summarize({"bvid": base.BVID}, "a")
        self.assertEqual(self.context.llm_generate.await_count, 2)

    async def test_search_or_comment_failures_keep_base_summary(self):
        for failure in ("search", "model", "invalid-json", "invalid-source", "too-long", "no-joke"):
            with self.subTest(failure=failure):
                self.setUp()
                if failure == "search":
                    self.service.memes.search.side_effect = RuntimeError("private-key")
                elif failure == "model":
                    self.context.llm_generate.side_effect = [types.SimpleNamespace(completion_text="准确的影片摘要。"), RuntimeError("private-key")]
                else:
                    raw = {"invalid-json": "not JSON", "invalid-source": '{"comment":"玩梗","source":true}',
                           "too-long": json.dumps({"comment": "梗" * 41, "source": 1}),
                           "no-joke": '{"comment":"","source":0}'}[failure]
                    self.context.llm_generate.side_effect = [types.SimpleNamespace(completion_text="准确的影片摘要。"), types.SimpleNamespace(completion_text=raw)]
                result = await self.service.summarize({"bvid": base.BVID}, "a")
                self.assertTrue(result.endswith("准确的影片摘要。"))
                self.assertNotIn("private", result)

    async def test_optional_timeout_keeps_base_summary_and_external_cancel_propagates(self):
        async def slow(*args):
            await asyncio.sleep(10)
        self.service.memes.search.side_effect = slow
        self.service.memes.timeout = 0.01
        result = await self.service.summarize({"bvid": base.BVID}, "a")
        self.assertTrue(result.endswith("准确的影片摘要。"))
        self.setUp()
        self.service.memes.search.side_effect = slow
        task = asyncio.create_task(self.service.summarize({"bvid": base.BVID}, "a"))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_session_cache_isolation_and_body_limit(self):
        cache = {}
        self.context.llm_generate.side_effect = None
        self.context.llm_generate.return_value = types.SimpleNamespace(completion_text="摘要。")
        self.service.memes.search.return_value = []
        await self.service.summarize({"bvid": base.BVID}, "a", cache)
        await self.service.summarize({"bvid": base.BVID}, "b", cache)
        await self.service.summarize({"bvid": base.BVID}, "a", cache)
        self.assertEqual(self.context.llm_generate.await_count, 2)
        self.assertEqual(self.service.memes.search.await_count, 2)
        self.setUp()
        self.service.max_output = 100
        self.context.llm_generate.side_effect = [types.SimpleNamespace(completion_text="字" * 101)]
        result = await self.service.summarize({"bvid": base.BVID}, "a")
        self.assertLessEqual(len(result.split("\n", 2)[2]), 100)
        self.service.memes.search.assert_not_called()

    async def test_legacy_provider_supports_comment_and_source_validation(self):
        provider = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id="old"), text_chat=self.context.llm_generate)
        self.service.context = types.SimpleNamespace(get_using_provider=lambda **kwargs: provider)
        result = await self.service.summarize({"bvid": base.BVID}, "a")
        self.assertIn("短评：", result)
        self.assertIn("梗参考：", result)


if __name__ == "__main__":
    unittest.main()

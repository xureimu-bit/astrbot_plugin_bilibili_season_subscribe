"""Coverage of long inputs and restart-safe STT checkpoints without paid calls."""

import asyncio
import json
import os
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import test_summarization as base
import test_transcription as transcription_tests
from test_transcription import write_wav
from bili_plugin_test.transcription import AudioTranscriber, TranscriptionUnavailable


class LongSummaryTests(unittest.IsolatedAsyncioTestCase):
    def service(self, text, **config):
        context = base.modern_context()
        service = base.VideoSummarizer(context, base.api_client([
            {"lan": "zh", "subtitle_url": "https://aisubtitle.hdslb.com/test"},
        ]), {"summary_max_input_chars": 500, **config})
        service._fetch_subtitle = AsyncMock(return_value=text)
        return service, context

    async def test_every_character_and_ending_reach_model_then_final_summary_is_short(self):
        text = "开头" + "甲乙丙丁。" * 230 + "最后反转是误会，双方和解。"
        service, context = self.service(text)
        async def generate(**kwargs):
            content = json.loads(kwargs["prompt"].split("\n", 1)[1])["资料"]
            if "片段资料" in kwargs["prompt"]:
                answer = "结尾：双方和解。" if "双方和解" in content else "本段讨论争执。"
            else:
                self.assertIn("结尾：双方和解", content)
                answer = "先有争执，最终发现误会并和解。"
            return types.SimpleNamespace(completion_text=answer)
        context.llm_generate.side_effect = generate
        source = await service._prepare_source({"bvid": base.BVID}, "a")
        self.assertEqual("".join(source.segments), text)
        result = await service.summarize({"bvid": base.BVID}, "a")
        self.assertIn("最终发现误会并和解", result)
        self.assertNotIn("资料已截断", result)
        self.assertEqual(context.llm_generate.await_count, len(source.segments) + 1)
        for call in context.llm_generate.call_args_list:
            self.assertEqual(call.kwargs["contexts"], [])
            self.assertNotIn("tools", call.kwargs)
            self.assertLessEqual(len(json.loads(call.kwargs["prompt"].split("\n", 1)[1])["资料"]), 500)

    async def test_recursive_reduction_is_bounded_and_keeps_last_note(self):
        service, context = self.service("字" * 6000 + "终段信息", summary_max_segments=32)
        async def generate(**kwargs):
            content = json.loads(kwargs["prompt"].split("\n", 1)[1])["资料"]
            self.assertLessEqual(len(content), 500)
            return types.SimpleNamespace(completion_text=("终段信息。" if "终段信息" in content else "") + "重要信息。" * 1000)
        context.llm_generate.side_effect = generate
        source = await service._prepare_source({"bvid": base.BVID})
        result = await service.summarize({"bvid": base.BVID}, "a")
        self.assertIn("摘要已截断", result)
        self.assertIn("终段信息", result)
        self.assertLessEqual(context.llm_generate.await_count, 2 * len(source.segments))

    async def test_over_segment_limit_or_source_safety_limit_never_summarizes_prefix(self):
        for text, config in (("字" * 2000, {"summary_max_segments": 2}), ("字" * 200001, {})):
            service, context = self.service(text, **config)
            with self.assertRaisesRegex(RuntimeError, "上限"):
                await service.summarize({"bvid": base.BVID}, "a")
            context.llm_generate.assert_not_called()

    async def test_failed_segment_never_returns_partial_summary_or_caches_it(self):
        service, context = self.service("字" * 1300)
        context.llm_generate.side_effect = [types.SimpleNamespace(completion_text="第一段"), RuntimeError("unavailable")]
        cache = {}
        with self.assertRaises(RuntimeError):
            await service.summarize({"bvid": base.BVID}, "a", cache)
        self.assertFalse(any(isinstance(key, tuple) and key[0] == "summary" for key in cache))

    async def test_long_summary_works_with_legacy_provider(self):
        service, _ = self.service("字" * 1300)
        provider = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id="legacy"),
                                         text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text="分段笔记")))
        service.context = types.SimpleNamespace(get_using_provider=lambda **kw: provider)
        self.assertIn("分段处理", await service.summarize({"bvid": base.BVID}, "a"))
        self.assertGreater(provider.text_chat.await_count, 1)

    async def test_subtitle_reader_keeps_end_beyond_one_prompt(self):
        service, _ = self.service("")
        real_client = httpx.AsyncClient
        body = {"body": [{"content": "开场" * 1000}, {"content": "最后的真相"}]}
        with patch.object(base.summary_module.httpx, "AsyncClient", side_effect=lambda **kw: real_client(
                transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)), **kw)):
            result = await base.VideoSummarizer._fetch_subtitle(service, "https://aisubtitle.hdslb.com/test")
        self.assertEqual(result, body["body"][0]["content"] + "\n最后的真相")

    async def test_segment_limit_is_visible_through_chat_tool_and_command(self):
        plugin = object.__new__(base.main.BilibiliSeasonSubscribePlugin)
        plugin.summarizer, context = self.service("字" * 2000, summary_max_segments=2)
        event = types.SimpleNamespace(unified_msg_origin="a", message_str=f"/bili合集订阅 总结 {base.BVID}", plain_result=lambda text: text)
        self.assertIn("summary_max_segments", await plugin.bili_summarize_video(event, base.BVID))
        replies = [reply async for reply in plugin.bili_season_subscribe(event)]
        self.assertIn("summary_max_segments", replies[0])
        context.llm_generate.assert_not_called()


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    # Share fixtures without re-running the entire existing test class.
    asyncSetUp = transcription_tests.TranscriptionTests.asyncSetUp
    asyncTearDown = transcription_tests.TranscriptionTests.asyncTearDown

    async def fail_second(self):
        self.provider.get_text.side_effect = ["成功第一段", RuntimeError("private-key")]
        with self.assertRaises(TranscriptionUnavailable):
            await self.service.transcribe(base.BVID, self.detail, "a")
        self.provider.get_text.side_effect = None
        self.provider.get_text.return_value = "成功第二段"

    async def test_new_instance_resumes_after_failure_and_publishes_only_complete_text(self):
        await self.fail_second()
        restarted = AudioTranscriber(self.context, self.api, {"asr_enabled": True, "ffmpeg_path": "ffmpeg"}, self.root)
        restarted._download = self.service._download
        restarted._extract_chunks = self.service._extract_chunks
        result = await restarted.transcribe(base.BVID, self.detail, "a")
        self.assertEqual(result.text, "成功第一段\n成功第二段")
        self.assertFalse(result.cached)
        self.assertEqual(self.provider.get_text.await_count, 3)
        self.assertEqual(list(restarted.segment_cache_dir.glob("*.json")), [])
        self.assertTrue((await restarted.transcribe(base.BVID, self.detail, "a")).cached)

    async def test_expired_corrupt_disabled_or_different_model_checkpoints_are_not_reused(self):
        for change in ("expired", "corrupt", "disabled", "model"):
            with self.subTest(change=change):
                self.service.cache_hours = 168
                self.provider.model_name = change
                await self.fail_second()
                path = max(self.service.segment_cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
                if change == "expired":
                    os.utime(path, (time.time() - 8 * 86400,) * 2)
                elif change == "corrupt":
                    path.write_text('{"segments": []}', encoding="utf-8")
                elif change == "disabled":
                    self.service.cache_hours = 0
                else:
                    self.provider.model_name = "changed"
                before = self.provider.get_text.await_count
                await self.service.transcribe(base.BVID, self.detail, "a")
                self.assertEqual(self.provider.get_text.await_count - before, 2)

    async def test_changed_audio_is_not_reused(self):
        await self.fail_second()
        extract = self.service._extract_chunks.side_effect
        async def different(*args):
            chunks = await extract(*args)
            write_wav(chunks[0], seconds=2)
            return chunks
        self.service._extract_chunks.side_effect = different
        await self.service.transcribe(base.BVID, self.detail, "a")
        self.assertEqual(self.provider.get_text.await_count, 4)

    async def test_timeout_preserves_completed_segments_and_cleans_audio(self):
        async def recognize(path):
            if path.endswith("000.wav"):
                return "成功第一段"
            await asyncio.sleep(10)
        self.provider.get_text.side_effect = recognize
        self.service.timeout = 0.05
        with self.assertRaisesRegex(TranscriptionUnavailable, "超时"):
            await self.service.transcribe(base.BVID, self.detail, "a")
        self.assertEqual(list(self.service.work_dir.iterdir()), [])
        self.provider.get_text.side_effect = None
        self.service.timeout = 300
        await self.service.transcribe(base.BVID, self.detail, "a")
        self.assertEqual(self.provider.get_text.await_count, 3)

    async def test_checkpoint_write_failure_does_not_discard_paid_text(self):
        self.service.segment_cache_dir = self.root / "not-a-directory"
        self.service.segment_cache_dir.write_text("file", encoding="utf-8")
        result = await self.service.transcribe(base.BVID, self.detail, "a")
        self.assertEqual(result.text, "转写文字\n转写文字")

    async def test_external_cancellation_preserves_only_completed_segments(self):
        second_started = asyncio.Event()
        async def recognize(path):
            if path.endswith("000.wav"):
                return "成功第一段"
            second_started.set()
            await asyncio.sleep(10)
        self.provider.get_text.side_effect = recognize
        task = asyncio.create_task(self.service.transcribe(base.BVID, self.detail, "a"))
        await second_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(list(self.service.work_dir.iterdir()), [])
        self.assertFalse(self.service.cache_dir.exists())
        self.provider.get_text.side_effect = None
        await self.service.transcribe(base.BVID, self.detail, "a")
        self.assertEqual(self.provider.get_text.await_count, 3)


if __name__ == "__main__":
    unittest.main()

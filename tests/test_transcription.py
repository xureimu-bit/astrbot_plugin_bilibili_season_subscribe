import asyncio
import json
import os
import shutil
import tempfile
import time
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import test_summarization as base
from bili_plugin_test.transcription import AudioTranscriber, Transcript, TranscriptionUnavailable


def write_wav(path, seconds=1):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * int(16000 * seconds))


def stt_provider(identifier="stt-a", model="speech-model"):
    return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id=identifier),
                                 model_name=model, get_text=AsyncMock(return_value="转写文字"))


class TranscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = stt_provider()
        self.context = types.SimpleNamespace(get_using_stt_provider_async=AsyncMock(return_value=self.provider))
        self.api = types.SimpleNamespace(timeout_seconds=5, build_sync_headers=Mock(return_value={"Cookie": "SESSDATA=private", "Referer": "https://www.bilibili.com/"}),
                                        fetch_playurl=AsyncMock(return_value={"data": {"dash": {"audio": [{"baseUrl": "https://upos.bilivideo.com/audio.m4s", "bandwidth": 64000}]}}}))
        self.service = AudioTranscriber(self.context, self.api, {"asr_enabled": True, "ffmpeg_path": "ffmpeg"}, self.root)
        self.detail = {"aid": 1, "cid": 10, "duration": 120, "pages": [{"cid": 10, "duration": 120}]}
        async def download(urls, path, referer):
            path.write_bytes(b"mock-media")
        async def extract(ffmpeg, source, root):
            chunks = [root / "chunk-000.wav", root / "chunk-001.wav"]
            for path in chunks:
                write_wav(path)
            return chunks
        self.service._download = AsyncMock(side_effect=download)
        self.service._extract_chunks = AsyncMock(side_effect=extract)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_chunks_use_local_files_and_success_is_cached(self):
        self.provider.get_text.side_effect = ["第一段", "第二段"]
        first = await self.service.transcribe(base.BVID, self.detail, "a")
        second = await self.service.transcribe(base.BVID, self.detail, "a")
        self.assertEqual(first.text, "第一段\n第二段")
        self.assertTrue(second.cached)
        self.assertEqual(self.provider.get_text.await_count, 2)
        self.api.fetch_playurl.assert_awaited_once_with(base.BVID, 1, 10, qn=16)
        self.assertEqual(list(self.service.work_dir.iterdir()), [])
        self.assertEqual(len(list(self.service.cache_dir.glob("*.json"))), 1)
        for call in self.provider.get_text.call_args_list:
            self.assertTrue(call.args[0].endswith(".wav"))
            self.assertFalse(Path(call.args[0]).exists())

    async def test_concurrent_requests_do_not_repeat_paid_transcription(self):
        results = await asyncio.gather(*(self.service.transcribe(base.BVID, self.detail, "a") for _ in range(2)))
        self.assertEqual(self.provider.get_text.await_count, 2)
        self.assertEqual(sum(result.cached for result in results), 1)

    async def test_provider_and_model_changes_do_not_reuse_transcripts(self):
        await self.service.transcribe(base.BVID, self.detail, "a")
        second_provider = stt_provider("stt-b")
        self.context.get_using_stt_provider_async.return_value = second_provider
        self.assertFalse((await self.service.transcribe(base.BVID, self.detail, "b")).cached)
        second_provider.model_name = "different-model"
        self.assertFalse((await self.service.transcribe(base.BVID, self.detail, "b")).cached)
        self.assertEqual(second_provider.get_text.await_count, 4)

    async def test_first_part_duration_and_cid_are_used(self):
        self.detail.update(cid=999, duration=5000)
        self.detail["pages"] = [{"cid": 10, "duration": 120}, {"cid": 999, "duration": 4880}]
        await self.service.transcribe(base.BVID, self.detail, "a")
        self.api.fetch_playurl.assert_awaited_once_with(base.BVID, 1, 10, qn=16)

    async def test_disabled_missing_provider_and_oversize_duration_do_not_download(self):
        self.service.enabled = False
        with self.assertRaisesRegex(TranscriptionUnavailable, "未启用"):
            await self.service.transcribe(base.BVID, self.detail, "a")
        self.service.enabled = True
        self.context.get_using_stt_provider_async.return_value = None
        with self.assertRaisesRegex(TranscriptionUnavailable, "STT 模型"):
            await self.service.transcribe(base.BVID, self.detail, "a")
        self.context.get_using_stt_provider_async.return_value = self.provider
        self.detail["pages"][0]["duration"] = 1201
        with self.assertRaisesRegex(TranscriptionUnavailable, "时长超过"):
            await self.service.transcribe(base.BVID, self.detail, "a")
        self.api.fetch_playurl.assert_not_called()

    async def test_old_context_and_explicit_provider_selection(self):
        self.service.context = types.SimpleNamespace(get_using_stt_provider=Mock(return_value=self.provider), get_provider_by_id=Mock(return_value=self.provider))
        await self.service.transcribe(base.BVID, self.detail, "old")
        self.service.context.get_using_stt_provider.assert_called_once_with(umo="old")
        self.service.provider_id = "chosen"
        await self.service.transcribe(base.BVID, self.detail, "explicit")
        self.service.context.get_provider_by_id.assert_called_once_with("chosen")

    async def test_expired_invalid_and_disabled_cache_gets_regenerated(self):
        await self.service.transcribe(base.BVID, self.detail, "a")
        cached = next(self.service.cache_dir.glob("*.json"))
        os.utime(cached, (time.time() - 8 * 86400,) * 2)
        self.assertFalse((await self.service.transcribe(base.BVID, self.detail, "a")).cached)
        cached.write_text("[]", encoding="utf-8")
        self.assertFalse((await self.service.transcribe(base.BVID, self.detail, "a")).cached)
        self.service.cache_hours = 0
        self.assertFalse((await self.service.transcribe(base.BVID, self.detail, "a")).cached)

    async def test_partial_stt_failure_is_checkpointed_and_error_is_redacted(self):
        self.provider.get_text.side_effect = ["第一段", RuntimeError("api-key-secret https://signed-url")]
        with self.assertRaises(TranscriptionUnavailable) as raised:
            await self.service.transcribe(base.BVID, self.detail, "a")
        self.assertNotIn("secret", str(raised.exception))
        self.assertFalse(self.service.cache_dir.exists())
        self.assertEqual(len(list(self.service.segment_cache_dir.glob("*.json"))), 1)
        self.assertEqual(list(self.service.work_dir.iterdir()), [])

    async def test_empty_transcription_is_not_cached(self):
        self.provider.get_text.return_value = " "
        with self.assertRaisesRegex(TranscriptionUnavailable, "空文字"):
            await self.service.transcribe(base.BVID, self.detail, "a")
        self.assertFalse(self.service.cache_dir.exists())

    async def test_timeout_and_cancel_clean_up_audio(self):
        async def slow(*args):
            await asyncio.sleep(10)
        self.provider.get_text.side_effect = slow
        self.service.timeout = 0.02
        with self.assertRaisesRegex(TranscriptionUnavailable, "超时"):
            await self.service.transcribe(base.BVID, self.detail, "a")
        self.assertEqual(list(self.service.work_dir.iterdir()), [])
        self.service.timeout = 300
        task = asyncio.create_task(self.service.transcribe(base.BVID, self.detail, "a"))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(list(self.service.work_dir.iterdir()), [])

    async def test_real_httpx_download_removes_cookies_and_rejects_size(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, content=b"audio")
        real = httpx.AsyncClient
        with patch("bili_plugin_test.transcription.httpx.AsyncClient", side_effect=lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs)):
            path = self.root / "audio"
            await AudioTranscriber._download(self.service, ["https://upos.bilivideo.com/audio"], path, "https://www.bilibili.com/")
            self.assertEqual(path.read_bytes(), b"audio")
            self.assertNotIn("cookie", seen[0].headers)
            self.service.max_download_bytes = 3
            with self.assertRaisesRegex(TranscriptionUnavailable, "大小上限"):
                await AudioTranscriber._download(self.service, ["https://upos.bilivideo.com/audio"], path, "https://www.bilibili.com/")

    async def test_untrusted_media_addresses_rejected_before_request(self):
        for url in ("https://localhost/audio", "https://127.0.0.1/audio", "https://bilivideo.com.evil/audio", "https://user@bilivideo.com/audio",
                    "https://unrelated.akamaized.net/audio", "https://upos-hz-mirrorakam.akamaized.net.evil/audio",
                    "https://upos.bilivideo.com:4483/audio", "https://x.mcdn.bilivideo.cn:8080/audio",
                    "https://upos.bilivideo.com:bad/audio", "file://upos.bilivideo.com/audio"):
            with self.subTest(url=url), self.assertRaises(TranscriptionUnavailable):
                AudioTranscriber._validate_media_url(url)

    async def test_bilibili_http_links_are_upgraded_without_changing_signature(self):
        for raw, expected in (
            ("http://upos.bilivideo.com/audio?sign=a%2Fb&deadline=123", "https://upos.bilivideo.com/audio?sign=a%2Fb&deadline=123"),
            ("http://upos.bilivideo.com:80/audio", "https://upos.bilivideo.com/audio"),
            ("//upos-hz-mirrorakam.akamaized.net/audio", "https://upos-hz-mirrorakam.akamaized.net/audio"),
            ("http://xy1x2x3x4xy.mcdn.bilivideo.cn:4483/audio", "https://xy1x2x3x4xy.mcdn.bilivideo.cn:4483/audio"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(AudioTranscriber._validate_media_url(raw), expected)

    async def test_mcdn_failure_and_invalid_backup_continue_to_akamai_and_stt(self):
        self.api.fetch_playurl.return_value = {"data": {"dash": {"audio": [{
            "bandwidth": 65579, "baseUrl": "http://xy1x2x3x4xy.mcdn.bilivideo.cn:4483/audio",
            "backupUrl": ["https://untrusted.example/audio", "https://upos-hz-mirrorakam.akamaized.net/audio?sign=secret"],
        }]}}}
        self.service._download = types.MethodType(AudioTranscriber._download, self.service)
        seen = []
        def handler(request):
            seen.append(request)
            if request.url.port == 4483:
                return httpx.Response(403)
            return httpx.Response(200, content=b"audio")
        real = httpx.AsyncClient
        with patch("bili_plugin_test.transcription.httpx.AsyncClient", side_effect=lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs)):
            transcript = await self.service.transcribe(base.BVID, self.detail, "a")
        self.assertEqual(transcript.text, "转写文字\n转写文字")
        self.assertEqual(self.provider.get_text.await_count, 2)
        self.assertEqual([request.url.host for request in seen], ["xy1x2x3x4xy.mcdn.bilivideo.cn", "upos-hz-mirrorakam.akamaized.net"])
        self.assertTrue(all(request.url.scheme == "https" and "cookie" not in request.headers for request in seen))
        self.assertEqual(list(self.service.work_dir.iterdir()), [])

    async def test_invalid_primary_does_not_hide_backup_or_other_bitrate(self):
        data = {"dash": {"audio": [
            {"bandwidth": 165153, "base_url": "https://upos.bilivideo.com/audio"},
            {"bandwidth": 65579, "baseUrl": "https://untrusted.example/1", "backupUrl": ["https://untrusted.example/2", "https://untrusted.example/3"]},
        ]}}
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, content=b"audio")
        real = httpx.AsyncClient
        with patch("bili_plugin_test.transcription.httpx.AsyncClient", side_effect=lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs)):
            await AudioTranscriber._download(self.service, AudioTranscriber._audio_urls(data), self.root / "audio", "https://www.bilibili.com/")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, "upos.bilivideo.com")

    async def test_no_trusted_addresses_make_no_requests(self):
        with patch("bili_plugin_test.transcription.httpx.AsyncClient") as client:
            with self.assertRaisesRegex(TranscriptionUnavailable, "已检查备用"):
                await AudioTranscriber._download(self.service, ["https://localhost/a", "https://untrusted.example/a"], self.root / "audio", "https://www.bilibili.com/")
        client.assert_not_called()

    async def test_redirect_is_not_followed_and_attempts_are_bounded_and_deduplicated(self):
        urls = ["https://upos.bilivideo.com/1", "http://upos.bilivideo.com/1", "https://upos.bilivideo.com/2", "https://upos.bilivideo.com/3", "https://upos.bilivideo.com/4"]
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"Location": "https://localhost/secret"})
        real = httpx.AsyncClient
        with patch("bili_plugin_test.transcription.httpx.AsyncClient", side_effect=lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs)):
            with self.assertRaisesRegex(TranscriptionUnavailable, "无法下载"):
                await AudioTranscriber._download(self.service, urls, self.root / "audio", "https://www.bilibili.com/")
        self.assertEqual([request.url.path for request in seen], ["/1", "/2", "/3"])

    async def test_ffmpeg_process_is_killed_on_cancel(self):
        running = asyncio.Event()
        async def communicate():
            running.set()
            await asyncio.sleep(10)
        process = types.SimpleNamespace(returncode=None, communicate=communicate, kill=Mock(), wait=AsyncMock())
        with patch("bili_plugin_test.transcription.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(AudioTranscriber._extract_chunks(self.service, "ffmpeg", self.root / "source", self.root))
            await running.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_actual_decoded_duration_is_checked_before_stt(self):
        process = types.SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(None, b"")))
        write_wav(self.root / "chunk-000.wav", seconds=2)
        self.service.max_duration = 1
        with patch("bili_plugin_test.transcription.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(TranscriptionUnavailable, "实际音讯时长"):
                await AudioTranscriber._extract_chunks(self.service, "ffmpeg", self.root / "source", self.root)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    async def test_real_ffmpeg_creates_complete_mono_16khz_segments(self):
        source = self.root / "source.wav"
        write_wav(source, seconds=65)
        self.service.chunk_seconds = 30
        chunks = await AudioTranscriber._extract_chunks(self.service, shutil.which("ffmpeg"), source, self.root)
        durations = []
        for path in chunks:
            with wave.open(str(path), "rb") as audio:
                self.assertEqual((audio.getnchannels(), audio.getframerate(), audio.getsampwidth()), (1, 16000, 2))
                durations.append(audio.getnframes() / audio.getframerate())
        self.assertEqual(len(chunks), 3)
        self.assertAlmostEqual(sum(durations), 65, places=2)
        self.assertTrue(all(value <= 30.1 for value in durations))


class SummaryFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_subtitles_prevent_asr_calls(self):
        api = base.api_client([{"lan": "zh", "subtitle_url": "https://aisubtitle.hdslb.com/a"}])
        transcriber = types.SimpleNamespace(enabled=True, timeout=300, transcribe=AsyncMock())
        service = base.VideoSummarizer(base.modern_context(), api, {}, transcriber)
        service._fetch_subtitle = AsyncMock(return_value="字幕文字")
        result = await service.summarize({"bvid": base.BVID}, "a")
        self.assertIn("字幕（zh）", result)
        transcriber.transcribe.assert_not_called()

    async def test_no_subtitles_use_all_transcript_segments_and_label_cache(self):
        transcriber = types.SimpleNamespace(enabled=True, timeout=300, transcribe=AsyncMock(return_value=Transcript("转写" * 1000, cached=True)))
        context = base.modern_context()
        service = base.VideoSummarizer(context, base.api_client(), {"summary_max_input_chars": 500}, transcriber)
        result = await service.summarize({"bvid": base.BVID}, "a")
        self.assertIn("语音转写（自动识别", result)
        self.assertIn("使用已缓存转写", result)
        self.assertIn("分段处理", result)
        self.assertNotIn("资料已截断", result)
        self.assertIn("全部分段笔记", context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(transcriber.transcribe.call_args.args[2], "a")
        self.assertEqual(service.total_timeout, 360)

    async def test_missing_asr_model_keeps_explicit_metadata_fallback(self):
        transcriber = types.SimpleNamespace(enabled=True, timeout=300, transcribe=AsyncMock(side_effect=TranscriptionUnavailable("未配置 STT 模型")))
        service = base.VideoSummarizer(base.modern_context(), base.api_client(), {}, transcriber)
        result = await service.summarize({"bvid": base.BVID}, "a")
        self.assertIn("仅标题/简介", result)
        self.assertIn("未配置 STT 模型", result)

    async def test_same_llm_different_sessions_do_not_share_asr_source(self):
        transcriber = types.SimpleNamespace(enabled=True, timeout=300, transcribe=AsyncMock(side_effect=[Transcript("会话 A 转写"), Transcript("会话 B 转写")]))
        context = base.modern_context()
        service = base.VideoSummarizer(context, base.api_client(), {}, transcriber)
        cache = {}
        await service.summarize({"bvid": base.BVID}, "a", cache)
        await service.summarize({"bvid": base.BVID}, "b", cache)
        self.assertEqual(context.llm_generate.await_count, 2)
        self.assertIn("会话 A 转写", context.llm_generate.call_args_list[0].kwargs["prompt"])
        self.assertIn("会话 B 转写", context.llm_generate.call_args_list[1].kwargs["prompt"])


if __name__ == "__main__":
    unittest.main()

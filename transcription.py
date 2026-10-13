"""Optional bounded audio transcription through AstrBot STT providers."""

import asyncio
import hashlib
import inspect
import json
import shutil
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx


class TranscriptionUnavailable(RuntimeError):
    """A safe, user-facing reason why audio transcription cannot be used."""


@dataclass
class Transcript:
    text: str
    cached: bool = False


class AudioTranscriber:
    def __init__(self, context, api_client, config, data_dir: Path):
        self.context = context
        self.api_client = api_client
        self.enabled = bool(config.get("asr_enabled", False))
        self.provider_id = str(config.get("asr_provider_id", "")).strip()
        self.timeout = self._int(config, "asr_timeout_seconds", 300, 30, 1800)
        self.max_duration = self._int(config, "asr_max_duration_seconds", 1200, 60, 7200)
        self.chunk_seconds = self._int(config, "asr_chunk_seconds", 120, 30, 300)
        self.max_download_bytes = self._int(config, "asr_max_download_mb", 40, 1, 200) * 1024 * 1024
        self.cache_hours = self._int(config, "asr_cache_hours", 168, 0, 720)
        self.ffmpeg_path = str(config.get("ffmpeg_path", "")).strip()
        self.cache_dir = data_dir / "transcripts"
        self.work_dir = data_dir / "asr_temp"
        self._lock = asyncio.Lock()

    @staticmethod
    def _int(config, key, default, low, high):
        try:
            return max(low, min(high, int(config.get(key, default))))
        except (TypeError, ValueError):
            return default

    async def transcribe(self, bvid: str, detail: dict, origin: str) -> Transcript:
        if not self.enabled:
            raise TranscriptionUnavailable("语音转写未启用")
        # One job at a time: concurrent requests for the same video reuse the completed cache.
        async def run():
            async with self._lock:
                return await self._transcribe_locked(bvid, detail, origin)
        try:
            return await asyncio.wait_for(run(), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise TranscriptionUnavailable("语音转写超时") from exc
        except TranscriptionUnavailable:
            raise
        except Exception as exc:
            raise TranscriptionUnavailable("音讯下载、提取或语音识别服务调用失败") from exc

    async def _resolve_provider(self, origin):
        if self.provider_id:
            provider = self.context.get_provider_by_id(self.provider_id)
            if inspect.isawaitable(provider):
                provider = await provider
        elif callable(getattr(self.context, "get_using_stt_provider_async", None)):
            provider = await self.context.get_using_stt_provider_async(umo=origin)
        elif callable(getattr(self.context, "get_using_stt_provider", None)):
            provider = self.context.get_using_stt_provider(umo=origin)
        else:
            provider = None
        if provider is None or not callable(getattr(provider, "get_text", None)):
            raise TranscriptionUnavailable("未配置可用的语音识别 STT 模型")
        return provider

    async def _transcribe_locked(self, bvid, detail, origin):
        provider = await self._resolve_provider(origin)
        pages = detail.get("pages") or []
        page = pages[0] if pages else detail
        try:
            cid = int(page.get("cid") or detail.get("cid") or 0)
            aid = int(detail.get("aid") or 0)
            duration = int(page.get("duration") or detail.get("duration") or 0)
        except (ValueError, TypeError) as exc:
            raise TranscriptionUnavailable("视频音轨资料不完整") from exc
        if not cid or not aid or duration <= 0:
            raise TranscriptionUnavailable("视频音轨资料不完整")
        if duration > self.max_duration:
            raise TranscriptionUnavailable(f"视频时长超过转写上限 {self.max_duration} 秒")
        identity = {"bvid": bvid, "cid": cid, "provider": self.provider_id or provider.meta().id,
                    "model": str(getattr(provider, "model_name", "")), "adapter": type(provider).__name__,
                    "chunk_seconds": self.chunk_seconds, "version": 1}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cached = self._read_cache(key)
        if cached:
            return Transcript(cached, cached=True)
        ffmpeg = self.ffmpeg_path or shutil.which("ffmpeg")
        if not ffmpeg:
            raise TranscriptionUnavailable("缺少 ffmpeg，无法提取音讯")
        try:
            payload = await self.api_client.fetch_playurl(bvid, aid, cid, qn=16)
            urls = self._audio_urls(payload.get("data") or {})
            self.work_dir.mkdir(parents=True, exist_ok=True)
            # Every job owns only its own temporary directory; all files are removed on exit/cancellation.
            with tempfile.TemporaryDirectory(prefix="job-", dir=self.work_dir) as directory:
                root = Path(directory)
                source = root / "source.media"
                await self._download(urls, source, f"https://www.bilibili.com/video/{bvid}")
                chunks = await self._extract_chunks(ffmpeg, source, root)
                text_parts = []
                for chunk in chunks:
                    text = await provider.get_text(str(chunk))
                    if not isinstance(text, str) or not text.strip():
                        raise TranscriptionUnavailable("语音识别返回空文字，未保存不完整的转写")
                    text_parts.append(text.strip())
                text = "\n".join(text_parts)
                if len(text) > 200000:
                    raise TranscriptionUnavailable("转写文字过长，未保存不完整的转写")
                self._write_cache(key, text)
                return Transcript(text)
        except TranscriptionUnavailable:
            raise
        except Exception as exc:
            # Provider and HTTP errors may contain keys or signed URLs; expose only a generic reason.
            raise TranscriptionUnavailable("音讯下载、提取或语音识别服务调用失败") from exc

    @staticmethod
    def _audio_urls(data):
        streams = (data.get("dash") or {}).get("audio") or []
        if streams:
            # Low bitrate is sufficient for speech and saves download traffic.
            stream = min(streams, key=lambda item: int(item.get("bandwidth") or 0))
            urls = [stream.get("baseUrl") or stream.get("base_url")]
            urls += stream.get("backupUrl") or stream.get("backup_url") or []
        else:
            progressive = data.get("durl") or []
            if len(progressive) != 1:
                raise TranscriptionUnavailable("未找到可用于转写的单一媒体流")
            urls = [progressive[0].get("url")]
            urls += progressive[0].get("backup_url") or []
        return [url for url in urls if isinstance(url, str) and url][:3]

    @staticmethod
    def _validate_media_url(url):
        parsed = urlparse(url)
        host = parsed.hostname or ""
        domains = ("bilivideo.com", "bilivideo.cn", "hdslb.com", "bilibili.com")
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in {None, 443}
                or not any(host == domain or host.endswith("." + domain) for domain in domains)):
            raise TranscriptionUnavailable("媒体地址不属于支持的 B 站 CDN")

    async def _download(self, urls, destination, referer):
        headers = self.api_client.build_sync_headers(referer)
        headers = {key: value for key, value in headers.items() if key.lower() != "cookie"}
        for url in urls:
            self._validate_media_url(url)
            try:
                async with httpx.AsyncClient(timeout=self.api_client.timeout_seconds, follow_redirects=False) as client:
                    async with client.stream("GET", url, headers=headers) as response:
                        response.raise_for_status()
                        if int(response.headers.get("content-length") or 0) > self.max_download_bytes:
                            raise TranscriptionUnavailable("媒体文件超过转写下载大小上限")
                        written = 0
                        with destination.open("wb") as output:
                            async for chunk in response.aiter_bytes():
                                written += len(chunk)
                                if written > self.max_download_bytes:
                                    raise TranscriptionUnavailable("媒体文件超过转写下载大小上限")
                                output.write(chunk)
                if written <= 0:
                    raise TranscriptionUnavailable("音讯下载为空")
                return
            except httpx.HTTPError:
                destination.unlink(missing_ok=True)
        raise TranscriptionUnavailable("无法下载 B 站音讯")

    async def _extract_chunks(self, ffmpeg, source, root):
        process = await asyncio.create_subprocess_exec(
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-t", str(self.max_duration + 1), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "segment", "-segment_time", str(self.chunk_seconds),
            "-reset_timestamps", "1", str(root / "chunk-%03d.wav"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            raise
        if process.returncode != 0:
            raise TranscriptionUnavailable("ffmpeg 提取音讯失败")
        chunks = sorted(root.glob("chunk-*.wav"))
        if not chunks:
            raise TranscriptionUnavailable("视频没有可识别的音轨")
        total = 0.0
        for chunk in chunks:
            with wave.open(str(chunk), "rb") as audio:
                total += audio.getnframes() / audio.getframerate()
            if chunk.stat().st_size > 12 * 1024 * 1024:
                raise TranscriptionUnavailable("分段音讯过大")
        if total <= 0 or total > self.max_duration + 0.25:
            raise TranscriptionUnavailable("实际音讯时长不在允许转写范围内")
        return chunks

    def _read_cache(self, key):
        if not self.cache_hours:
            return None
        path = self.cache_dir / f"{key}.json"
        try:
            if time.time() - path.stat().st_mtime > self.cache_hours * 3600:
                return None
            if path.stat().st_size > 1024 * 1024:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            text = payload.get("text")
            return text if isinstance(text, str) and text.strip() else None
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(self, key, text):
        if not self.cache_hours:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            paths = sorted(self.cache_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
            for path in paths:
                if time.time() - path.stat().st_mtime > self.cache_hours * 3600:
                    path.unlink(missing_ok=True)
            remaining = sorted(self.cache_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
            for path in remaining[:-199]:
                path.unlink(missing_ok=True)
            path = self.cache_dir / f"{key}.json"
            temporary = path.with_suffix(".tmp")
            try:
                temporary.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            # A cache write failure must not discard a paid-for successful transcript.
            pass

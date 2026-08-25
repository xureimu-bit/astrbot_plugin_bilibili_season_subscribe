import asyncio
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register


SEASON_API_ENDPOINTS = (
    "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
    "https://api.bilibili.com/x/space/seasons_archives_list",
)
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
SPACE_LISTS_PATTERN = re.compile(r"space\.bilibili\.com/(?P<mid>\d+)/lists/(?P<season_id>\d+)", re.I)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_duration(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        if ":" in value:
            total = 0
            for part in value.split(":"):
                total = total * 60 + safe_int(part)
            return total
    return 0


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "未知"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def format_size(num_bytes: int | None) -> str:
    if not num_bytes or num_bytes < 0:
        return "未知"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def sanitize_filename(text: str, max_length: int = 80) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        cleaned = "video"
    return cleaned[:max_length]


def guess_ext_from_url(url: str, default: str = "mp4") -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix:
        return suffix
    return default


def build_video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


def parse_season_url(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    match = SPACE_LISTS_PATTERN.search(raw)
    if match:
        return match.group("mid"), match.group("season_id")

    parsed = urlparse(raw)
    if "space.bilibili.com" not in parsed.netloc:
        raise ValueError("链接不是 B 站空间合集链接。")
    mid_match = re.search(r"/(\d+)", parsed.path)
    query = parse_qs(parsed.query)
    season_id = query.get("sid", [None])[0] or query.get("season_id", [None])[0]
    if mid_match and season_id:
        return mid_match.group(1), str(season_id)
    raise ValueError("无法从链接中解析出 mid 和 season_id。")


def build_label(event: AstrMessageEvent) -> str:
    sender_name = getattr(event, "sender_name", None) or "unknown"
    session_id = getattr(event, "session_id", None) or "unknown"
    return f"{sender_name} ({session_id})"


def parse_cookie_file(cookie_file: str) -> dict[str, str]:
    result: dict[str, str] = {}
    path = Path(cookie_file)
    if not path.exists():
        return result
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return result
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) >= 7:
            name = parts[5].strip()
            value = parts[6].strip()
            if name:
                result[name] = value
    return result


@dataclass
class Subscriber:
    unified_msg_origin: str
    label: str
    subscribed_at: str


@dataclass
class SeasonSubscription:
    mid: str
    season_id: str
    season_title: str
    uploader_name: str
    url: str
    latest_bvids: list[str] = field(default_factory=list)
    seen_bvids: list[str] = field(default_factory=list)
    pending_bvids: list[str] = field(default_factory=list)
    subscribers: list[Subscriber] = field(default_factory=list)
    last_checked_at: str | None = None


@dataclass
class DownloaderConfig:
    request_timeout_seconds: int
    max_video_height: int
    max_duration_seconds: int
    max_filesize_mb: int
    retain_hours: int
    preferred_video_codec: str
    preferred_ext: str
    ffmpeg_path: str
    bilibili_sessdata: str
    bilibili_cookie_file: str

    @property
    def max_filesize_bytes(self) -> int:
        if self.max_filesize_mb <= 0:
            return 0
        return self.max_filesize_mb * 1024 * 1024

    @property
    def ffmpeg_location(self) -> str | None:
        if self.ffmpeg_path.strip():
            return self.ffmpeg_path.strip()
        return shutil.which("ffmpeg")


@dataclass
class VideoSummary:
    bvid: str
    title: str
    duration_seconds: int
    uploader_name: str
    link: str


@dataclass
class ResolvedMedia:
    bvid: str
    title: str
    duration_seconds: int
    uploader_name: str
    page_url: str
    height: int | None
    estimated_size_bytes: int | None
    video_url: str
    audio_url: str | None
    ext: str
    requires_merge: bool


@dataclass
class DownloadResult:
    status: str
    reason: str
    path: Path | None = None
    title: str = ""
    bvid: str = ""
    url: str = ""
    duration_seconds: int = 0
    size_bytes: int | None = None
    height: int | None = None
    estimated_size_bytes: int | None = None


class Store:
    def __init__(self, data_file: Path):
        self.data_file = data_file
        self._lock = asyncio.Lock()
        self._data = {"subscriptions": []}

    async def load(self) -> None:
        async with self._lock:
            if not self.data_file.exists():
                self.data_file.parent.mkdir(parents=True, exist_ok=True)
                self._write_locked()
                return
            try:
                self._data = json.loads(self.data_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("B 站合集订阅插件的数据文件损坏，已重置为空数据。")
                self._data = {"subscriptions": []}
                self._write_locked()

    async def list_subscriptions(self) -> list[SeasonSubscription]:
        async with self._lock:
            return self._deserialize()

    async def upsert_subscription(self, subscription: SeasonSubscription) -> None:
        async with self._lock:
            items = self._deserialize()
            replaced = False
            for idx, item in enumerate(items):
                if item.season_id == subscription.season_id and item.mid == subscription.mid:
                    items[idx] = subscription
                    replaced = True
                    break
            if not replaced:
                items.append(subscription)
            self._data = {"subscriptions": [self._serialize_item(item) for item in items]}
            self._write_locked()

    async def remove_subscriber(self, season_id: str, origin: str) -> tuple[bool, bool]:
        async with self._lock:
            items = self._deserialize()
            found = False
            season_removed = False
            kept_items: list[SeasonSubscription] = []
            for item in items:
                if item.season_id != season_id:
                    kept_items.append(item)
                    continue
                found = True
                item.subscribers = [sub for sub in item.subscribers if sub.unified_msg_origin != origin]
                if item.subscribers:
                    kept_items.append(item)
                else:
                    season_removed = True
            self._data = {"subscriptions": [self._serialize_item(item) for item in kept_items]}
            self._write_locked()
            return found, season_removed

    def _deserialize(self) -> list[SeasonSubscription]:
        items: list[SeasonSubscription] = []
        for raw in self._data.get("subscriptions", []):
            subscribers = [Subscriber(**subscriber) for subscriber in raw.get("subscribers", [])]
            items.append(
                SeasonSubscription(
                    mid=str(raw["mid"]),
                    season_id=str(raw["season_id"]),
                    season_title=raw.get("season_title", ""),
                    uploader_name=raw.get("uploader_name", ""),
                    url=raw.get("url", ""),
                    latest_bvids=[str(item) for item in raw.get("latest_bvids", [])],
                    seen_bvids=[str(item) for item in raw.get("seen_bvids", raw.get("latest_bvids", []))],
                    pending_bvids=[str(item) for item in raw.get("pending_bvids", [])],
                    subscribers=subscribers,
                    last_checked_at=raw.get("last_checked_at"),
                )
            )
        return items

    def _serialize_item(self, item: SeasonSubscription) -> dict[str, Any]:
        payload = asdict(item)
        payload["subscribers"] = [asdict(subscriber) for subscriber in item.subscribers]
        return payload

    def _write_locked(self) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.data_file.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")


class BilibiliApiClient:
    def __init__(self, timeout_seconds: int, page_size: int = 30, sessdata: str = "", cookie_file: str = ""):
        self.timeout_seconds = timeout_seconds
        self.page_size = max(1, page_size)
        self.cookies = self._build_cookies(sessdata, cookie_file)

    async def fetch_season(self, mid: str, season_id: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for endpoint in SEASON_API_ENDPOINTS:
            try:
                payload = await self._get_json(
                    endpoint,
                    params={
                        "mid": mid,
                        "season_id": season_id,
                        "page_num": 1,
                        "page_size": self.page_size,
                    },
                    referer=f"https://space.bilibili.com/{mid}/lists/{season_id}?type=season",
                )
                return await self._fetch_full_season(endpoint, mid, season_id, payload)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if last_error is None:
            raise RuntimeError("无法访问 B 站合集接口。")
        raise last_error

    async def fetch_video_detail(self, bvid: str) -> dict[str, Any]:
        return await self._get_json(
            VIEW_API,
            params={"bvid": bvid},
            referer=build_video_url(bvid),
        )

    async def fetch_playurl(self, bvid: str, aid: int, cid: int, qn: int = 127) -> dict[str, Any]:
        return await self._get_json(
            PLAYURL_API,
            params={
                "bvid": bvid,
                "avid": aid,
                "cid": cid,
                "qn": qn,
                "fnver": 0,
                "fnval": 16,
                "fourk": 1,
                "otype": "json",
            },
            referer=build_video_url(bvid),
        )

    def build_sync_headers(self, referer: str) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
            "Origin": "https://www.bilibili.com",
        }
        if self.cookies:
            cookie_header = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
            headers["Cookie"] = cookie_header
        return headers

    def build_sync_client(self, referer: str) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers=self.build_sync_headers(referer),
            cookies=self.cookies,
        )

    async def _fetch_full_season(
        self,
        endpoint: str,
        mid: str,
        season_id: str,
        first_payload: dict[str, Any],
    ) -> dict[str, Any]:
        data = first_payload.get("data") or {}
        meta = data.get("meta") or {}
        archives = list(data.get("archives") or [])
        page = data.get("page") or {}
        total = safe_int(page.get("total"), len(archives))
        page_num = 2
        while len(archives) < total:
            payload = await self._get_json(
                endpoint,
                params={
                    "mid": mid,
                    "season_id": season_id,
                    "page_num": page_num,
                    "page_size": self.page_size,
                },
                referer=f"https://space.bilibili.com/{mid}/lists/{season_id}?type=season",
            )
            page_archives = (payload.get("data") or {}).get("archives") or []
            if not page_archives:
                break
            archives.extend(page_archives)
            page_num += 1
        season_title = meta.get("name") or meta.get("title") or meta.get("season_title") or f"season_{season_id}"
        uploader_name = meta.get("upper", {}).get("name") or meta.get("author") or meta.get("mid_name") or ""
        return {
            "mid": mid,
            "season_id": season_id,
            "season_title": season_title,
            "uploader_name": uploader_name,
            "url": f"https://space.bilibili.com/{mid}/lists/{season_id}?type=season",
            "videos": archives,
        }

    async def _get_json(
        self,
        url: str,
        params: dict[str, Any],
        referer: str,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers=self.build_sync_headers(referer),
            cookies=self.cookies,
        ) as client:
            response = await client.get(url, params=params)
        if response.status_code == 412:
            raise RuntimeError("B 站返回 412 风控，请填写有效的 bilibili_sessdata 或 cookie 文件。")
        response.raise_for_status()
        payload = response.json()
        code = safe_int(payload.get("code"), -1)
        if code != 0:
            message = payload.get("message") or payload.get("msg") or "unknown error"
            if code in {-403, -404, -352}:
                raise RuntimeError(f"B 站接口拒绝访问：code={code}，message={message}")
            raise RuntimeError(f"B 站接口返回异常：code={code}，message={message}")
        return payload

    def _build_cookies(self, sessdata: str, cookie_file: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        if cookie_file.strip():
            cookies.update(parse_cookie_file(cookie_file.strip()))
        if sessdata.strip():
            cookies["SESSDATA"] = sessdata.strip()
        return cookies


class BilibiliMediaResolver:
    def __init__(self, api_client: BilibiliApiClient, config: DownloaderConfig):
        self.api_client = api_client
        self.config = config

    async def resolve(self, video: dict[str, Any]) -> DownloadResult | ResolvedMedia:
        bvid = str(video.get("bvid") or "")
        page_url = build_video_url(bvid)
        try:
            detail = await self.api_client.fetch_video_detail(bvid)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(status="failed", reason=str(exc), bvid=bvid, url=page_url)

        data = detail.get("data") or {}
        title = str(data.get("title") or video.get("title") or bvid)
        owner = (data.get("owner") or {}).get("name") or video.get("author") or ""
        duration_seconds = normalize_duration(data.get("duration") or video.get("duration"))
        if self.config.max_duration_seconds > 0 and duration_seconds > self.config.max_duration_seconds:
            return DownloadResult(
                status="skipped",
                reason=(
                    f"时长 {format_duration(duration_seconds)} 超过上限 "
                    f"{format_duration(self.config.max_duration_seconds)}"
                ),
                bvid=bvid,
                title=title,
                url=page_url,
                duration_seconds=duration_seconds,
            )

        aid = safe_int(data.get("aid"))
        cid = safe_int(data.get("cid"))
        if not cid:
            pages = data.get("pages") or []
            if pages:
                cid = safe_int(pages[0].get("cid"))
        if not aid or not cid:
            return DownloadResult(
                status="failed",
                reason="解析视频信息失败：缺少 aid/cid。",
                bvid=bvid,
                title=title,
                url=page_url,
                duration_seconds=duration_seconds,
            )

        try:
            play_payload = await self.api_client.fetch_playurl(bvid, aid, cid)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(status="failed", reason=str(exc), bvid=bvid, title=title, url=page_url)

        play_data = play_payload.get("data") or {}
        resolved = self._resolve_from_playdata(play_data, bvid, title, owner, duration_seconds, page_url)
        if isinstance(resolved, ResolvedMedia):
            max_size = self.config.max_filesize_bytes
            if max_size > 0 and resolved.estimated_size_bytes and resolved.estimated_size_bytes > max_size:
                return DownloadResult(
                    status="skipped",
                    reason=(
                        f"预计文件大小 {format_size(resolved.estimated_size_bytes)} 超过上限 "
                        f"{format_size(max_size)}"
                    ),
                    bvid=bvid,
                    title=title,
                    url=page_url,
                    duration_seconds=duration_seconds,
                    height=resolved.height,
                    estimated_size_bytes=resolved.estimated_size_bytes,
                )
        return resolved

    def _resolve_from_playdata(
        self,
        play_data: dict[str, Any],
        bvid: str,
        title: str,
        owner: str,
        duration_seconds: int,
        page_url: str,
    ) -> DownloadResult | ResolvedMedia:
        dash = play_data.get("dash") or {}
        videos = list(dash.get("video") or [])
        audios = list(dash.get("audio") or [])
        if videos:
            chosen_video = self._pick_video_stream(videos)
            if chosen_video is None:
                return DownloadResult(
                    status="failed",
                    reason="没有找到符合清晰度条件的视频流。",
                    bvid=bvid,
                    title=title,
                    url=page_url,
                    duration_seconds=duration_seconds,
                )
            chosen_audio = self._pick_audio_stream(audios)
            video_url = str(chosen_video.get("baseUrl") or chosen_video.get("base_url") or "")
            audio_url = ""
            estimated_size = self._stream_size(chosen_video)
            ext = self.config.preferred_ext or "mp4"
            requires_merge = False
            if chosen_audio:
                audio_url = str(chosen_audio.get("baseUrl") or chosen_audio.get("base_url") or "")
                estimated_size = (estimated_size or 0) + (self._stream_size(chosen_audio) or 0)
                requires_merge = True
            if not video_url:
                return DownloadResult(
                    status="failed",
                    reason="视频流缺少下载地址。",
                    bvid=bvid,
                    title=title,
                    url=page_url,
                    duration_seconds=duration_seconds,
                )
            if requires_merge and not self.config.ffmpeg_location:
                return DownloadResult(
                    status="failed",
                    reason="当前只拿到了分离音视频流，但系统里找不到 ffmpeg。",
                    bvid=bvid,
                    title=title,
                    url=page_url,
                    duration_seconds=duration_seconds,
                    height=safe_int(chosen_video.get("height"), 0) or None,
                    estimated_size_bytes=estimated_size or None,
                )
            return ResolvedMedia(
                bvid=bvid,
                title=title,
                duration_seconds=duration_seconds,
                uploader_name=owner,
                page_url=page_url,
                height=safe_int(chosen_video.get("height"), 0) or None,
                estimated_size_bytes=estimated_size or None,
                video_url=video_url,
                audio_url=audio_url or None,
                ext=ext,
                requires_merge=requires_merge,
            )

        durl = list(play_data.get("durl") or [])
        if durl:
            segment = durl[0]
            url = str(segment.get("url") or "")
            if not url:
                return DownloadResult(
                    status="failed",
                    reason="渐进流缺少下载地址。",
                    bvid=bvid,
                    title=title,
                    url=page_url,
                    duration_seconds=duration_seconds,
                )
            return ResolvedMedia(
                bvid=bvid,
                title=title,
                duration_seconds=duration_seconds,
                uploader_name=owner,
                page_url=page_url,
                height=None,
                estimated_size_bytes=safe_int(segment.get("size"), 0) or None,
                video_url=url,
                audio_url=None,
                ext=guess_ext_from_url(url, self.config.preferred_ext or "mp4"),
                requires_merge=False,
            )

        return DownloadResult(
            status="failed",
            reason="B 站接口没有返回可下载媒体流。",
            bvid=bvid,
            title=title,
            url=page_url,
            duration_seconds=duration_seconds,
        )

    def _pick_video_stream(self, streams: list[dict[str, Any]]) -> dict[str, Any] | None:
        preferred_codec = self.config.preferred_video_codec.lower()
        preferred_height = self.config.max_video_height

        def stream_key(stream: dict[str, Any]) -> tuple[int, int, int, int]:
            height = safe_int(stream.get("height"))
            codec_bonus = 1 if preferred_codec and preferred_codec in str(stream.get("codecs") or "").lower() else 0
            bandwidth = safe_int(stream.get("bandwidth"))
            id_score = safe_int(stream.get("id"))
            return (height, codec_bonus, bandwidth, id_score)

        candidates = streams
        if preferred_height > 0:
            limited = [stream for stream in streams if safe_int(stream.get("height")) <= preferred_height]
            if limited:
                candidates = limited
        if not candidates:
            return None
        return max(candidates, key=stream_key)

    def _pick_audio_stream(self, streams: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not streams:
            return None

        def stream_key(stream: dict[str, Any]) -> tuple[int, int]:
            bandwidth = safe_int(stream.get("bandwidth"))
            id_score = safe_int(stream.get("id"))
            return (bandwidth, id_score)

        return max(streams, key=stream_key)

    def _stream_size(self, stream: dict[str, Any]) -> int | None:
        value = safe_int(stream.get("size"), 0)
        if value:
            return value
        bandwidth = safe_int(stream.get("bandwidth"), 0)
        if bandwidth <= 0:
            return None
        return bandwidth


class DirectMediaDownloader:
    def __init__(self, api_client: BilibiliApiClient, download_root: Path, config: DownloaderConfig):
        self.api_client = api_client
        self.download_root = download_root
        self.config = config
        self.resolver = BilibiliMediaResolver(api_client, config)
        self.download_root.mkdir(parents=True, exist_ok=True)

    def availability_reason(self) -> str | None:
        return None

    async def cleanup_expired(self) -> int:
        return await asyncio.to_thread(self._cleanup_expired_sync)

    async def download(self, video: dict[str, Any]) -> DownloadResult:
        resolved = await self.resolver.resolve(video)
        if isinstance(resolved, DownloadResult):
            return resolved
        return await asyncio.to_thread(self._download_sync, resolved)

    def _cleanup_expired_sync(self) -> int:
        retain_hours = self.config.retain_hours
        if retain_hours < 0:
            return 0
        cutoff = utc_now() - timedelta(hours=retain_hours)
        deleted = 0
        for path in self.download_root.rglob("*"):
            if not path.is_file():
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified < cutoff:
                try:
                    path.unlink()
                    deleted += 1
                except OSError as exc:
                    logger.warning("删除过期下载文件失败 %s: %s", path, exc)
        return deleted

    def _download_sync(self, media: ResolvedMedia) -> DownloadResult:
        video_id = media.bvid
        final_ext = media.ext or "mp4"
        safe_title = sanitize_filename(media.title)
        final_path = self.download_root / f"{video_id}__{safe_title}.{final_ext}"
        temp_dir = self.download_root / f"{video_id}__tmp"
        self._purge_existing_files(video_id)
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            video_part = temp_dir / f"video.{guess_ext_from_url(media.video_url, final_ext)}"
            video_download = self._download_url(media.video_url, video_part, media.page_url)
            if isinstance(video_download, DownloadResult):
                return video_download

            if media.audio_url:
                audio_part = temp_dir / f"audio.{guess_ext_from_url(media.audio_url, 'm4a')}"
                audio_download = self._download_url(media.audio_url, audio_part, media.page_url)
                if isinstance(audio_download, DownloadResult):
                    return audio_download
                merge_result = self._merge_streams(video_part, audio_part, final_path)
                if isinstance(merge_result, DownloadResult):
                    return merge_result
            else:
                shutil.move(str(video_part), str(final_path))

            size_bytes = final_path.stat().st_size
            if self.config.max_filesize_bytes > 0 and size_bytes > self.config.max_filesize_bytes:
                final_path.unlink(missing_ok=True)
                return DownloadResult(
                    status="skipped",
                    reason=(
                        f"实际文件大小 {format_size(size_bytes)} 超过上限 "
                        f"{format_size(self.config.max_filesize_bytes)}"
                    ),
                    bvid=media.bvid,
                    title=media.title,
                    url=media.page_url,
                    duration_seconds=media.duration_seconds,
                    size_bytes=size_bytes,
                    height=media.height,
                    estimated_size_bytes=media.estimated_size_bytes,
                )

            return DownloadResult(
                status="downloaded",
                reason="已通过 B 站媒体接口解析并下载",
                path=final_path,
                title=media.title,
                bvid=media.bvid,
                url=media.page_url,
                duration_seconds=media.duration_seconds,
                size_bytes=size_bytes,
                height=media.height,
                estimated_size_bytes=media.estimated_size_bytes,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _download_url(self, url: str, destination: Path, referer: str) -> DownloadResult | None:
        try:
            with self.api_client.build_sync_client(referer) as client:
                with client.stream("GET", url) as response:
                    if response.status_code == 412:
                        return DownloadResult(
                            status="failed",
                            reason="下载媒体流时遇到 B 站 412 风控，请检查 SESSDATA 或 cookie 文件。",
                        )
                    response.raise_for_status()
                    content_length = safe_int(response.headers.get("content-length"), 0)
                    if self.config.max_filesize_bytes > 0 and content_length > self.config.max_filesize_bytes:
                        return DownloadResult(
                            status="skipped",
                            reason=(
                                f"单段流大小 {format_size(content_length)} 超过上限 "
                                f"{format_size(self.config.max_filesize_bytes)}"
                            ),
                        )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with destination.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            written += len(chunk)
                            if self.config.max_filesize_bytes > 0 and written > self.config.max_filesize_bytes:
                                handle.close()
                                destination.unlink(missing_ok=True)
                                return DownloadResult(
                                    status="skipped",
                                    reason=(
                                        f"下载中超过文件大小上限 {format_size(self.config.max_filesize_bytes)}"
                                    ),
                                )
                            handle.write(chunk)
        except Exception as exc:  # noqa: BLE001
            destination.unlink(missing_ok=True)
            return DownloadResult(status="failed", reason=str(exc))
        return None

    def _merge_streams(self, video_path: Path, audio_path: Path, output_path: Path) -> DownloadResult | None:
        ffmpeg = self.config.ffmpeg_location
        if not ffmpeg:
            return DownloadResult(status="failed", reason="缺少 ffmpeg，无法合并音视频流。")
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-c",
            "copy",
            str(output_path),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            return DownloadResult(status="failed", reason=f"执行 ffmpeg 失败：{exc}")
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            return DownloadResult(status="failed", reason=f"ffmpeg 合并失败：{message}")
        return None

    def _purge_existing_files(self, video_id: str) -> None:
        for path in self.download_root.glob(f"{video_id}__*"):
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)


@register(
    "astrbot_plugin_bilibili_season_subscribe",
    "Codex",
    "订阅 B 站合集并在有新视频时下载后发送",
    "1.2.1",
)
class BilibiliSeasonSubscribePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        timeout_seconds = max(5, safe_int(config.get("request_timeout_seconds"), 15))
        page_size = max(1, safe_int(config.get("page_size"), 30))
        sessdata = str(config.get("bilibili_sessdata", ""))
        cookie_file = str(config.get("bilibili_cookie_file", ""))
        self.api_client = BilibiliApiClient(
            timeout_seconds,
            page_size=page_size,
            sessdata=sessdata,
            cookie_file=cookie_file,
        )
        self.season_client = self.api_client
        data_dir = Path(StarTools.get_data_dir())
        self.store = Store(data_dir / "subscriptions.json")
        self.downloader = DirectMediaDownloader(
            self.api_client,
            data_dir / "downloads",
            DownloaderConfig(
                request_timeout_seconds=timeout_seconds,
                max_video_height=max(0, safe_int(config.get("max_video_height"), 720)),
                max_duration_seconds=max(0, safe_int(config.get("max_duration_seconds"), 1800)),
                max_filesize_mb=max(0, safe_int(config.get("max_filesize_mb"), 80)),
                retain_hours=safe_int(config.get("retain_download_hours"), 24),
                preferred_video_codec=str(config.get("preferred_video_codec", "avc")),
                preferred_ext=str(config.get("preferred_ext", "mp4")),
                ffmpeg_path=str(config.get("ffmpeg_path", "")),
                bilibili_sessdata=sessdata,
                bilibili_cookie_file=cookie_file,
            ),
        )
        self.page_size = page_size
        self.poll_interval_seconds = max(60, safe_int(config.get("poll_interval_minutes"), 20) * 60)
        self.notify_on_first_subscribe = bool(config.get("notify_on_first_subscribe", False))
        self._poll_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        await self.store.load()
        await self.downloader.cleanup_expired()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="bili-season-subscribe-poller")
        logger.info("B 站合集订阅下载插件已启动。")

    async def terminate(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("B 站合集订阅下载插件已停止。")

    @filter.command("bili合集订阅")
    async def bili_season_subscribe(self, event: AstrMessageEvent):
        args = self._extract_args(event)
        if not args:
            yield event.plain_result(
                "用法：/bili合集订阅 添加 <合集链接> | 删除 <season_id> | 列表 | 检查 | 重试 [season_id|全部]"
            )
            return

        action = args[0]
        if action == "添加":
            if len(args) < 2:
                yield event.plain_result(
                    "请提供合集链接，例如：/bili合集订阅 添加 https://space.bilibili.com/1865348651/lists/5193004?type=season"
                )
                return
            yield event.plain_result(await self._handle_add(event, args[1]))
            return

        if action == "删除":
            if len(args) < 2:
                yield event.plain_result("请提供要删除的 season_id，例如：/bili合集订阅 删除 5193004")
                return
            yield event.plain_result(await self._handle_remove(event, args[1]))
            return

        if action == "列表":
            yield event.plain_result(await self._handle_list(event))
            return

        if action == "检查":
            yield event.plain_result("开始检查当前会话下的 B 站合集订阅，并按新解析流程下载发送视频……")
            summary = await self._check_once(origin_filter=event.unified_msg_origin, manual=True)
            yield event.plain_result(summary)
            return

        if action == "重试":
            season_id = args[1] if len(args) >= 2 else None
            yield event.plain_result(await self._handle_retry(event, season_id))
            return

        yield event.plain_result("不支持的子命令。可用子命令：添加、删除、列表、检查、重试")

    async def _handle_add(self, event: AstrMessageEvent, url: str) -> str:
        try:
            mid, season_id = parse_season_url(url)
        except ValueError as exc:
            return f"添加失败：{exc}"

        try:
            season = await self.season_client.fetch_season(mid, season_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("添加 B 站合集订阅失败: %s", exc)
            return f"添加失败：无法读取这个合集，原因：{exc}"

        latest_bvids = [str(video.get("bvid")) for video in season["videos"] if video.get("bvid")]
        initial_seen = [] if self.notify_on_first_subscribe else list(latest_bvids)
        subscriptions = await self.store.list_subscriptions()
        current = next(
            (item for item in subscriptions if item.season_id == season_id and item.mid == mid),
            None,
        )
        origin = event.unified_msg_origin
        subscriber = Subscriber(
            unified_msg_origin=origin,
            label=build_label(event),
            subscribed_at=utc_now_iso(),
        )

        if current is None:
            subscription = SeasonSubscription(
                mid=mid,
                season_id=season_id,
                season_title=season["season_title"],
                uploader_name=season["uploader_name"],
                url=season["url"],
                latest_bvids=list(latest_bvids),
                seen_bvids=initial_seen,
                pending_bvids=[],
                subscribers=[subscriber],
                last_checked_at=utc_now_iso(),
            )
            await self.store.upsert_subscription(subscription)
            return (
                f"订阅成功：{subscription.season_title}（season_id={season_id}）\n"
                f"当前合集已有 {len(latest_bvids)} 个视频。之后发现符合条件的新视频时，会按新解析流程自动下载并发送。"
            )

        if any(sub.unified_msg_origin == origin for sub in current.subscribers):
            return f"你已经订阅过这个合集了：{current.season_title}（season_id={season_id}）"

        current.subscribers.append(subscriber)
        current.season_title = season["season_title"]
        current.uploader_name = season["uploader_name"]
        current.url = season["url"]
        current.latest_bvids = list(latest_bvids)
        if not current.seen_bvids:
            current.seen_bvids = list(initial_seen)
        current.last_checked_at = utc_now_iso()
        await self.store.upsert_subscription(current)
        return (
            f"已把当前会话加入订阅：{current.season_title}（season_id={season_id}）\n"
            f"之后会按当前配置自动下载并发送符合条件的新视频。"
        )

    async def _handle_remove(self, event: AstrMessageEvent, season_id: str) -> str:
        found, season_removed = await self.store.remove_subscriber(season_id, event.unified_msg_origin)
        if not found:
            return f"没有找到 season_id={season_id} 的订阅记录。"
        if season_removed:
            return f"已删除 season_id={season_id} 的订阅，且没有其他订阅者，所以合集记录也一并清理了。"
        return f"已删除当前会话对 season_id={season_id} 的订阅。"

    async def _handle_list(self, event: AstrMessageEvent) -> str:
        subscriptions = await self.store.list_subscriptions()
        current_items = [
            item for item in subscriptions
            if any(sub.unified_msg_origin == event.unified_msg_origin for sub in item.subscribers)
        ]
        if not current_items:
            return "当前会话还没有订阅任何 B 站合集。"

        lines = ["当前会话订阅的 B 站合集："]
        for item in current_items:
            lines.append(
                f"- {item.season_title or item.season_id} | season_id={item.season_id} | "
                f"UP={item.uploader_name or '未知'} | 已处理={len(item.seen_bvids)} | 待重试={len(item.pending_bvids)}"
            )
        lines.append("")
        lines.append(
            "当前下载配置："
            f" 最高 {self.downloader.config.max_video_height or '不限'}p,"
            f" 最长 {format_duration(self.downloader.config.max_duration_seconds) if self.downloader.config.max_duration_seconds else '不限'},"
            f" 最大 {self.downloader.config.max_filesize_mb or '不限'} MB,"
            f" 保留 {self.downloader.config.retain_hours} 小时"
        )
        return "\n".join(lines)

    async def _handle_retry(self, event: AstrMessageEvent, season_id: str | None) -> str:
        target = (season_id or "").strip()
        if target in {"", "全部", "all", "ALL"}:
            summary = await self._check_once(
                origin_filter=event.unified_msg_origin,
                manual=True,
                retry_only=True,
            )
            return f"开始手动重试：当前会话下所有待重试视频\n{summary}"

        subscriptions = await self.store.list_subscriptions()
        matched = [
            item for item in subscriptions
            if item.season_id == target
            and any(sub.unified_msg_origin == event.unified_msg_origin for sub in item.subscribers)
        ]
        if not matched:
            return f"没有找到 season_id={target} 的订阅。"
        if not any(item.pending_bvids for item in matched):
            return f"season_id={target} 当前没有待重试视频。"

        summary = await self._check_once(
            origin_filter=event.unified_msg_origin,
            manual=True,
            retry_only=True,
            season_id_filter=target,
        )
        return f"开始手动重试：season_id={target}\n{summary}"

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._check_once()
                await self.downloader.cleanup_expired()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("B 站合集订阅轮询失败: %s", exc)
            await asyncio.sleep(self.poll_interval_seconds)

    async def _check_once(
        self,
        origin_filter: str | None = None,
        manual: bool = False,
        retry_only: bool = False,
        season_id_filter: str | None = None,
    ) -> str:
        subscriptions = await self.store.list_subscriptions()
        if origin_filter is not None:
            subscriptions = [
                item for item in subscriptions
                if any(sub.unified_msg_origin == origin_filter for sub in item.subscribers)
            ]
        if season_id_filter is not None:
            subscriptions = [item for item in subscriptions if item.season_id == season_id_filter]
        if not subscriptions:
            return "没有可检查的合集订阅。"

        checked_count = 0
        sent_count = 0
        skipped_count = 0
        failed_count = 0
        retry_candidate_count = 0

        for item in subscriptions:
            try:
                season = await self.season_client.fetch_season(item.mid, item.season_id)
            except Exception as exc:  # noqa: BLE001
                failed_count += 1
                logger.warning("检查 B 站合集失败 mid=%s season_id=%s: %s", item.mid, item.season_id, exc)
                continue

            videos = season["videos"]
            videos_by_bvid = {
                str(video.get("bvid")): video
                for video in videos
                if video.get("bvid")
            }
            latest_bvids = list(videos_by_bvid.keys())
            seen_set = set(item.seen_bvids)
            pending_set = set(item.pending_bvids)
            candidates: list[dict[str, Any]] = []
            for bvid in latest_bvids:
                if bvid in pending_set:
                    retry_candidate_count += 1
                    candidates.append(videos_by_bvid[bvid])
                elif not retry_only and bvid not in seen_set:
                    candidates.append(videos_by_bvid[bvid])

            item.season_title = season["season_title"]
            item.uploader_name = season["uploader_name"]
            item.url = season["url"]
            item.last_checked_at = utc_now_iso()

            for video in candidates:
                bvid = str(video.get("bvid") or "")
                result = await self.downloader.download(video)
                if result.status == "downloaded":
                    sent_ok = await self._send_downloaded_video(item, video, result)
                    if sent_ok:
                        sent_count += 1
                        seen_set.add(bvid)
                        pending_set.discard(bvid)
                    else:
                        failed_count += 1
                        pending_set.add(bvid)
                elif result.status == "skipped":
                    skipped_count += 1
                    seen_set.add(bvid)
                    pending_set.discard(bvid)
                    logger.info("跳过视频 %s: %s", bvid, result.reason)
                else:
                    failed_count += 1
                    pending_set.add(bvid)
                    logger.warning("下载视频 %s 失败: %s", bvid, result.reason)

            item.latest_bvids = latest_bvids
            item.seen_bvids = list(seen_set)
            item.pending_bvids = [bvid for bvid in latest_bvids if bvid in pending_set]
            await self.store.upsert_subscription(item)
            checked_count += 1

        await self.downloader.cleanup_expired()
        if manual:
            mode_label = "手动重试完成" if retry_only else "检查完成"
            return (
                f"{mode_label}：成功检查 {checked_count} 个合集，发送 {sent_count} 个视频，"
                f"跳过 {skipped_count} 个，失败 {failed_count} 个，待重试 {retry_candidate_count} 个。"
            )
        return (
            f"后台检查完成：检查 {checked_count} 个合集，发送 {sent_count} 个视频，"
            f"跳过 {skipped_count} 个，失败 {failed_count} 个。"
        )

    async def _send_downloaded_video(
        self,
        subscription: SeasonSubscription,
        video: dict[str, Any],
        result: DownloadResult,
    ) -> bool:
        if result.path is None:
            return False

        title_text = self._build_video_title_text(subscription, video, result)
        header = self._build_video_message(subscription, video, result)
        for subscriber in subscription.subscribers:
            title_sent = await self._send_title_chain(subscriber.unified_msg_origin, title_text)
            if not title_sent:
                logger.warning("向订阅者 %s 发送标题失败: %s", subscriber.unified_msg_origin, title_text)
                return False
            sent = await self._send_media_chain(subscriber.unified_msg_origin, header, result.path)
            if not sent:
                logger.warning("向订阅者 %s 发送视频失败: %s", subscriber.unified_msg_origin, result.path)
                return False

        if self.downloader.config.retain_hours == 0:
            result.path.unlink(missing_ok=True)
        return True

    def _build_video_title_text(
        self,
        subscription: SeasonSubscription,
        video: dict[str, Any],
        result: DownloadResult,
    ) -> str:
        title = result.title or str(video.get("title") or result.bvid)
        return f"[B 站合集新视频] {subscription.season_title}\n标题：{title}"

    def _build_video_message(
        self,
        subscription: SeasonSubscription,
        video: dict[str, Any],
        result: DownloadResult,
    ) -> str:
        duration = result.duration_seconds or normalize_duration(video.get("duration"))
        title = result.title or str(video.get("title") or result.bvid)
        link = result.url or build_video_url(result.bvid)
        return "\n".join(
            [
                f"[B 站合集新视频] {subscription.season_title}",
                f"标题：{title}",
                f"UP 主：{subscription.uploader_name or '未知'}",
                f"时长：{format_duration(duration)}",
                f"清晰度：{result.height}p" if result.height else "清晰度：未知",
                f"文件大小：{format_size(result.size_bytes)}",
                f"视频链接：{link}",
            ]
        )

    async def _send_title_chain(self, origin: str, title_text: str) -> bool:
        try:
            await self.context.send_message(origin, MessageChain([Comp.Plain(title_text)]))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送标题消息失败: %s", exc)
            return False

    async def _send_media_chain(self, origin: str, header: str, path: Path) -> bool:
        video_cls = getattr(Comp, "Video", None)
        file_cls = getattr(Comp, "File", None)

        if video_cls is not None:
            try:
                chain = MessageChain([video_cls.fromFileSystem(str(path))])
                await self.context.send_message(origin, chain)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("以视频消息发送失败，尝试文件发送: %s", exc)

        if file_cls is not None:
            try:
                chain = MessageChain([file_cls(name=path.name, file=str(path))])
                await self.context.send_message(origin, chain)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("以文件消息发送失败: %s", exc)

        try:
            await self.context.send_message(
                origin,
                MessageChain([Comp.Plain(f"{header}\n文件已下载到：{path}")]),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("纯文本回退发送失败: %s", exc)
            return False

    def _extract_args(self, event: AstrMessageEvent) -> list[str]:
        text = (getattr(event, "message_str", None) or "").strip()
        if not text:
            return []
        parts = text.split()
        if not parts:
            return []
        if parts[0].lstrip("/").startswith("bili合集订阅"):
            return parts[1:]
        return parts

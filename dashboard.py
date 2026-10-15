"""Authenticated AstrBot Page controller; no standalone server or credentials."""

import asyncio
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

PLUGIN_NAME = "astrbot_plugin_bilibili_season_subscribe"
VERSION = "1.7.0"
BOUNDS = {
    "poll_interval_minutes": (1, 1440), "request_timeout_seconds": (5, 300), "page_size": (1, 100),
    "max_video_height": (0, 4320), "max_duration_seconds": (0, 86400), "max_filesize_mb": (0, 10000),
    "retain_download_hours": (-1, 8760), "summary_timeout_seconds": (5, 300),
    "summary_max_segments": (2, 64), "summary_max_input_chars": (500, 50000), "summary_max_output_chars": (100, 4000),
    "asr_timeout_seconds": (30, 1800), "asr_max_duration_seconds": (60, 7200),
    "asr_chunk_seconds": (30, 300), "asr_max_download_mb": (1, 200), "asr_cache_hours": (0, 720),
    "meme_timeout_seconds": (3, 45), "meme_cache_hours": (0, 24),
}
SECRET_KEYS = {"bilibili_sessdata"}


class DashboardError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class PluginDashboard:
    def __init__(self, plugin, data_dir):
        self.plugin = plugin
        self.schema = json.loads(Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8"))
        self.history_path = Path(data_dir) / "dashboard_history.json"
        self.history = []
        self.active = None
        self.job = None
        self.closed = False
        try:
            if self.history_path.stat().st_size <= 512000:
                entries = json.loads(self.history_path.read_text(encoding="utf-8"))
                self.history = [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("time"), str)][-200:]
        except (OSError, ValueError, TypeError):
            pass

    def register(self):
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            return
        try:
            from astrbot.api.web import error_response, json_response, request
        except ImportError:
            return

        for endpoint, method in (("overview", "GET"), ("settings", "GET"), ("settings/save", "POST"),
                                 ("check", "POST"), ("subscriptions/add", "POST"), ("subscriptions/remove", "POST")):
            async def handler(endpoint=endpoint):
                # Dashboard authentication remains mandatory, including for reads.
                if not request.username:
                    return error_response("请先登录 AstrBot 管理面板", status_code=401)
                try:
                    payload = await request.json(default={}) if request.method == "POST" else {}
                    return json_response(await self.dispatch(endpoint, payload))
                except DashboardError as exc:
                    return error_response(str(exc), status_code=exc.status)
                except Exception:
                    return error_response("操作未完成，请查看 AstrBot 日志或稍后重试", status_code=500)
            register(f"/{PLUGIN_NAME}/{endpoint}", handler, [method], "B站合集管理页面")

    async def dispatch(self, endpoint, payload):
        if self.closed:
            raise DashboardError("插件已停止，请启用并重载后再使用", 503)
        if not isinstance(payload, dict):
            raise DashboardError("请求内容必须是对象")
        if endpoint == "overview":
            return await self.overview()
        if endpoint == "settings":
            return self.settings()
        if endpoint == "settings/save":
            return await self.save_settings(payload)
        if endpoint == "check":
            if type(payload.get("retry_only", False)) is not bool:
                raise DashboardError("重试选项无效")
            self.require_idle()
            self.job = asyncio.create_task(self._check_job(payload.get("retry_only", False)), name="bili-dashboard-check")
            return {"started": True}
        if endpoint in {"subscriptions/add", "subscriptions/remove"}:
            self.require_idle()
            async with self.plugin._operation_lock:
                return await self.change_subscription(endpoint, payload)
        raise DashboardError("未知操作", 404)

    def require_idle(self):
        if self.plugin._operation_lock.locked() or self.job and not self.job.done():
            raise DashboardError("正在检查或处理订阅，请完成后再操作", 409)

    async def _check_job(self, retry_only):
        try:
            await self.plugin._check_once(manual=True, retry_only=retry_only)
        except asyncio.CancelledError:
            raise
        except Exception:
            # _check_once records a safe failure message; consume the background exception.
            pass

    async def stop(self):
        self.closed = True
        if self.job and not self.job.done():
            self.job.cancel()
            try:
                await self.job
            except asyncio.CancelledError:
                pass

    def begin(self, manual, retry_only):
        self.active = {"time": datetime.now(timezone.utc).isoformat(),
                       "mode": "重试待处理" if retry_only else "手动检查" if manual else "自动轮询"}

    def finish(self, duration, message, outcome):
        entry = {**(self.active or {}), "duration": round(duration, 2), "message": message[:300], "outcome": outcome}
        self.active = None
        self.history = [*self.history, entry][-200:]
        temporary = self.history_path.with_suffix(".tmp")
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self.history, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.history_path)
        except OSError:
            pass

    async def overview(self):
        items = await self.plugin.store.list_subscriptions()
        origins = {}
        subscriptions = []
        for item in items:
            subscribers = [{"origin": sub.unified_msg_origin, "label": sub.label} for sub in item.subscribers]
            for subscriber in subscribers:
                origins[subscriber["origin"]] = subscriber["label"]
            subscriptions.append({"mid": item.mid, "season_id": item.season_id, "title": item.season_title,
                                  "url": f"https://space.bilibili.com/{item.mid}/lists/{item.season_id}?type=season",
                                  "subscribers": subscribers, "pending": len(item.pending_bvids),
                                  "processed": len(item.seen_bvids), "last_checked": item.last_checked_at})
        counts = Counter(entry["time"][:10] for entry in self.history)
        task = self.plugin._poll_task
        return {"version": VERSION, "running": bool(task and not task.done()), "active": self.active,
                "busy": self.plugin._operation_lock.locked() or bool(self.job and not self.job.done()),
                "next_check": self.plugin._next_check_at,
                "metrics": {"subscriptions": len(items), "sessions": len(origins),
                            "pending": sum(item["pending"] for item in subscriptions),
                            "processed": sum(item["processed"] for item in subscriptions)},
                "features": {"summary_enabled": self.plugin.summary_enabled, "asr_enabled": self.plugin.transcriber.enabled,
                             "meme_enabled": self.plugin.summarizer.memes.enabled},
                "interval": self.plugin.poll_interval_seconds // 60, "subscriptions": subscriptions,
                "sessions": [{"origin": key, "label": value} for key, value in origins.items()],
                "history": list(reversed(self.history)), "daily": dict(counts)}

    def _revision(self):
        return hashlib.sha256(json.dumps(dict(self.plugin.config), sort_keys=True, default=str).encode()).hexdigest()

    def settings(self):
        values = {key: self.plugin.config.get(key, spec.get("default")) for key, spec in self.schema.items()}
        configured = {key: bool(values[key]) for key in SECRET_KEYS}
        for key in SECRET_KEYS:
            values[key] = ""
        options = {}
        for key, method in (("summary_provider_id", "get_all_providers"), ("asr_provider_id", "get_all_stt_providers")):
            options[key] = []
            try:
                for provider in getattr(self.plugin.context, method)():
                    meta = provider.meta()
                    options[key].append({"id": meta.id, "label": f"{meta.id} · {getattr(provider, 'model_name', '')}"[:200]})
            except (AttributeError, TypeError):
                pass
        return {"schema": self.schema, "values": values, "configured": configured, "options": options,
                "bounds": BOUNDS, "revision": self._revision()}

    async def save_settings(self, payload):
        self.require_idle()
        async with self.plugin._operation_lock:
            if payload.get("revision") != self._revision():
                raise DashboardError("配置已被其他页面修改，请刷新后再保存", 409)
            updates = payload.get("values")
            clear = payload.get("clear_secrets", [])
            if not isinstance(updates, dict) or not isinstance(clear, list) or any(key not in SECRET_KEYS for key in clear):
                raise DashboardError("配置格式无效")
            if any(updates.get(key) for key in clear):
                raise DashboardError("不能同时填写新凭据并清除同一项")
            for key, value in updates.items():
                if key not in self.schema:
                    raise DashboardError("包含未知配置项")
                kind = self.schema[key]["type"]
                if kind == "bool" and type(value) is not bool or kind == "int" and type(value) is not int:
                    raise DashboardError(f"{key} 类型不正确")
                if kind == "string" and (not isinstance(value, str) or len(value) > 4096):
                    raise DashboardError(f"{key} 内容无效或过长")
                if key in BOUNDS and not BOUNDS[key][0] <= value <= BOUNDS[key][1]:
                    raise DashboardError(f"{key} 超出范围 {BOUNDS[key][0]}–{BOUNDS[key][1]}")
            old = dict(self.plugin.config)
            merged = dict(old)
            for key, value in updates.items():
                if key not in SECRET_KEYS or value:
                    merged[key] = value
            for key in clear:
                merged[key] = ""
            try:
                self.plugin.config.update(merged)
                self.plugin.config.save_config()
            except Exception as exc:
                self.plugin.config.clear()
                self.plugin.config.update(old)
                raise DashboardError("配置保存失败，运行设置未更改", 500) from exc
            self.plugin._configure_runtime()
            return self.settings()

    async def change_subscription(self, endpoint, payload):
        origin = payload.get("origin")
        items = await self.plugin.store.list_subscriptions()
        known = next((sub for item in items for sub in item.subscribers if sub.unified_msg_origin == origin), None)
        if not known:
            raise DashboardError("请选择已有推送会话；首次订阅请在聊天里使用添加指令")
        if endpoint.endswith("add"):
            url = payload.get("url")
            if not isinstance(url, str) or len(url) > 1000 or urlsplit(url).hostname != "space.bilibili.com":
                raise DashboardError("请填写有效的 B 站合集链接")
            event = SimpleNamespace(unified_msg_origin=origin, sender_name=known.label, session_id=origin.rsplit(":", 1)[-1])
            message = await self.plugin._handle_add(event, url)
            if message.startswith("添加失败"):
                raise DashboardError("添加失败，请检查合集链接和 B 站接口")
        else:
            mid, season_id = payload.get("mid"), payload.get("season_id")
            if not any(item.mid == mid and item.season_id == season_id
                       and any(sub.unified_msg_origin == origin for sub in item.subscribers) for item in items):
                raise DashboardError("合集已不存在，请刷新", 404)
            found, _ = await self.plugin.store.remove_subscriber(season_id, origin, mid=mid)
            if not found:
                raise DashboardError("该会话的订阅已不存在", 404)
            message = "已移除此会话的订阅"
        return {"message": message}

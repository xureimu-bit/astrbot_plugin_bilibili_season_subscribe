"""Optional recent meme references using the current AstrBot profile's web search."""

import asyncio
import hashlib
import importlib
import json
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


SEARCH_ADAPTERS = {
    "tavily": ("_tavily_search", "websearch_tavily_key"),
    "bocha": ("_bocha_search", "websearch_bocha_key"),
    "brave": ("_brave_search", "websearch_brave_key"),
    "firecrawl": ("_firecrawl_search", "websearch_firecrawl_key"),
    "baidu_ai_search": ("_baidu_search", "websearch_baidu_app_builder_key"),
    "exa": ("_exa_search", "websearch_exa_key"),
    "anysearch": ("_anysearch_search", "websearch_anysearch_key"),
}


class MemeSearchUnavailable(RuntimeError):
    """Search is unavailable; the video summary must still be delivered."""


class MemeContextProvider:
    def __init__(self, context, config):
        self.context = context
        self.enabled = bool(config.get("meme_enabled", False))
        self.timeout = self._int(config, "meme_timeout_seconds", 15, 3, 45)
        self.cache_hours = self._int(config, "meme_cache_hours", 6, 0, 24)
        # Memory only: no keys, search terms, or snippets are written to disk.
        self._cache = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def _int(config, key, default, low, high):
        try:
            return min(high, max(low, int(config.get(key, default))))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def make_query(title, summary):
        def clean(text):
            text = re.sub(r"https?://\S+", "", text)
            return re.sub(r"\s+", " ", text).strip()
        # Only short public-video context is sent, never chat history or full transcripts.
        date = datetime.now(timezone.utc).strftime("%Y-%m")
        return f"{date} {clean(title)[:18]} {clean(summary)[:28]} 相关网络梗 含义"[:72]

    def _settings(self, origin):
        config = self.context.get_config(umo=origin)
        settings = dict(config.get("provider_settings") or {})
        if not settings.get("web_search", False):
            raise MemeSearchUnavailable("当前会话未开启 AstrBot 网页搜索")
        provider = settings.get("websearch_provider", "tavily")
        if provider not in SEARCH_ADAPTERS:
            raise MemeSearchUnavailable("当前搜索提供商不受支持")
        _, key_name = SEARCH_ADAPTERS[provider]
        keys = settings.get(key_name) or ([] if provider != "baidu_ai_search" else "")
        if provider == "baidu_ai_search":
            if not isinstance(keys, str) or not keys.strip():
                raise MemeSearchUnavailable("未配置搜索服务 Key")
        else:
            if isinstance(keys, str):
                keys = [keys] if keys.strip() else []
            keys = [key for key in keys if isinstance(key, str) and key.strip()][:3]
            if not keys and provider != "anysearch":
                raise MemeSearchUnavailable("未配置搜索服务 Key")
        settings[key_name] = keys
        return provider, settings, key_name

    @staticmethod
    def _payload(provider, query):
        if provider == "tavily":
            return {"query": query, "max_results": 3, "search_depth": "basic", "topic": "general", "time_range": "month"}
        if provider == "bocha":
            return {"query": query, "count": 3, "summary": False, "freshness": "oneMonth"}
        if provider == "brave":
            return {"q": query, "count": 3, "country": "CN", "search_lang": "zh-hans", "freshness": "pm"}
        if provider == "baidu_ai_search":
            return {"messages": [{"role": "user", "content": query}], "search_source": "baidu_search_v2",
                    "resource_type_filter": [{"type": "web", "top_k": 3}], "search_recency_filter": "month"}
        if provider == "exa":
            start = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            return {"query": query, "numResults": 3, "type": "auto", "startPublishedDate": start,
                    "contents": {"text": {"maxCharacters": 400}}}
        if provider == "anysearch":
            return {"query": query, "max_results": 3, "format": "json", "zone": "cn", "language": "zh-CN"}
        return {"query": query, "limit": 3, "sources": ["web"]}

    @staticmethod
    def _normalize(rows):
        results = []
        for row in rows[:10]:
            def field(name):
                value = row.get(name) if isinstance(row, dict) else getattr(row, name, "")
                return value if isinstance(value, str) else ""
            url = field("url").strip()
            try:
                parsed = urlsplit(url)
                if (len(url) > 240 or parsed.scheme != "https" or not parsed.hostname
                        or parsed.username is not None or parsed.password is not None
                        or any(char.isspace() for char in url)):
                    continue
            except ValueError:
                continue
            item = {"title": field("title")[:60], "url": url, "snippet": field("snippet")[:240]}
            if not item["snippet"].strip() or any(entry["url"] == url for entry in results):
                continue
            if len(json.dumps([*results, item], ensure_ascii=False)) > 1400:
                continue
            results.append(item)
            if len(results) == 3:
                break
        return results

    async def search(self, title, summary, origin):
        if not self.enabled:
            return []

        async def run():
            provider, settings, key_name = self._settings(origin)
            query = self.make_query(title, summary)
            fingerprint = json.dumps([origin, provider, settings[key_name], query], ensure_ascii=False)
            key = hashlib.sha256(fingerprint.encode()).hexdigest()
            async with self._lock:
                now = time.monotonic()
                cached = self._cache.get(key)
                if cached and self.cache_hours and now - cached[0] < self.cache_hours * 3600:
                    self._cache.move_to_end(key)
                    return [dict(item) for item in cached[1]]
                # Internal AstrBot adapter, verified against 4.28.2. Fail gracefully if changed.
                module = importlib.import_module("astrbot.core.tools.web_search_tools")
                helper = getattr(module, SEARCH_ADAPTERS[provider][0])
                rows = await helper(settings, self._payload(provider, query))
                results = self._normalize(rows)
                if self.cache_hours:
                    # Cache empty successful searches too; failures are never cached.
                    self._cache[key] = (time.monotonic(), results)
                    self._cache.move_to_end(key)
                    while len(self._cache) > 128:
                        self._cache.popitem(last=False)
                return [dict(item) for item in results]

        try:
            return await asyncio.wait_for(run(), timeout=self.timeout)
        except asyncio.CancelledError:
            raise
        except MemeSearchUnavailable:
            raise
        except Exception as exc:
            # Provider exceptions can contain credentials and URLs; never forward them.
            raise MemeSearchUnavailable("热梗检索不可用，保留普通总结") from exc

"""Bounded extraction of explicit Bilibili inputs and quoted QQ share cards."""

import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

BV = re.compile(r"BV[0-9A-Za-z]{10}")
AV = re.compile(r"av([1-9][0-9]{0,18})", re.I)
URL = re.compile(r"https?://[^\s<>\"']+", re.I)
HOSTS = {"www.bilibili.com", "bilibili.com", "m.bilibili.com", "space.bilibili.com", "b23.tv"}
CARD_KEYS = {"url", "jumpurl", "qqdocurl", "shareurl", "contentjumpurl", "desc", "text"}


def checked_url(value):
    parsed = urlsplit(value.rstrip("，。；！）】)>"))
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in HOSTS
            or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
        raise ValueError("只支持 B 站视频、合集及 b23.tv 短链接")
    return urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, ""))


def canonical_reference(value):
    value = value.strip()
    if BV.fullmatch(value):
        return value
    if AV.fullmatch(value):
        return "av" + str(int(AV.fullmatch(value)[1]))
    url = checked_url(value)
    parsed = urlsplit(url)
    if parsed.hostname == "b23.tv" and re.fullmatch(r"/[0-9A-Za-z]+/?", parsed.path):
        return url
    if parsed.hostname in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,18})/?", parsed.path, re.I)
        if match:
            return canonical_reference(match[1])
    if parsed.hostname == "space.bilibili.com" and re.fullmatch(r"/\d+/lists/\d+/?", parsed.path):
        return url
    raise ValueError("链接中没有可识别的视频或合集")


def extract_targets(text, event=None):
    """Explicit argument wins; otherwise inspect this message and its direct quote.

    Card titles are not interpreted as links or instructions. No chat-history scan.
    """
    def tokens(source):
        found = []
        for match in URL.finditer(source[:16000]):
            try:
                found.append(canonical_reference(match[0]))
            except ValueError:
                pass
        plain = URL.sub(" ", source[:16000])
        for match in re.finditer(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,18})(?![0-9A-Za-z])", plain, re.I):
            try:
                found.append(canonical_reference(match[0]))
            except ValueError:
                pass
        return list(dict.fromkeys(found))

    direct = tokens(str(text or ""))
    if direct:
        return direct
    if URL.search(str(text or '')):
        return []
    sources, budget = [], [0]

    def card(value, depth=0):
        if depth > 6 or budget[0] >= 128:
            return
        budget[0] += 1
        if isinstance(value, dict):
            for key, child in list(value.items())[:32]:
                if str(key).casefold() in CARD_KEYS and isinstance(child, str):
                    sources.append(child[:2000])
                elif isinstance(child, (dict, list)):
                    card(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:16]:
                card(child, depth + 1)

    def chain(parts, depth=0):
        if not isinstance(parts, (list, tuple)) or depth > 1:
            return
        for part in parts[:32]:
            kind = str(getattr(part, "type", part.__class__.__name__)).split(".")[-1].lower()
            if kind == "plain":
                sources.append(str(getattr(part, "text", ""))[:2000])
            elif kind == "json":
                data = getattr(part, "data", {})
                try:
                    if isinstance(data, str) and len(data) <= 16000:
                        data = json.loads(data)
                    card(data)
                except (ValueError, TypeError):
                    pass
            elif kind == "share":
                sources.append(str(getattr(part, "url", ""))[:2000])
            elif kind == "reply" and depth == 0:
                chain(getattr(part, "chain", []), 1)
                sources.append(str(getattr(part, "message_str", ""))[:2000])

    obj = getattr(event, "message_obj", None)
    chain(getattr(obj, "message", []))
    return list(dict.fromkeys(target for source in sources[:64] for target in tokens(source)))


async def resolve_short_link(url, timeout=15):
    """Follow only Bilibili redirects, without sending the user's login Cookie."""
    url = checked_url(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(4):
            reference = canonical_reference(url)
            if not reference.startswith("https://b23.tv/"):
                return reference
            async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                if response.status_code not in {301, 302, 303, 307, 308} or not response.headers.get("location"):
                    raise ValueError("短链接未返回有效的视频跳转")
                url = checked_url(urljoin(url, response.headers["location"]))
        raise ValueError("短链接跳转次数过多")

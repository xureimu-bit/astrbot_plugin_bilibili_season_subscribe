"""Video-to-series subscription orchestration; collection membership is verified."""

import time

from .series_rules import infer_rule, normalized_title, rule_id, rule_matches
from .video_input import AV, BV, extract_targets, resolve_short_link
from .upload_fallback import EXCLUDED


class SeriesSubscriptions:
    def __init__(self, plugin):
        self.plugin = plugin

    async def resolve(self, raw, event=None):
        targets = extract_targets(raw, event)
        if not targets:
            raise ValueError('请提供视频链接、BV／AV 号或合集链接，也可引用 QQ 视频分享卡片后发送添加指令；引用内容不可读取时请补发链接')
        if len(targets) > 4:
            raise ValueError('一次只订阅一个视频所属系列，请只保留一个视频或合集')
        resolved = []
        for target in targets:
            if target.startswith('https://b23.tv/'):
                target = await resolve_short_link(target, self.plugin.api_client.timeout_seconds)
            if target.startswith('https://space.bilibili.com/'):
                resolved.append(('collection', target))
            else:
                payload = await self.plugin.api_client.fetch_video_detail(target)
                video = payload.get('data') or {}
                bvid = str(video.get('bvid') or '')
                owner = video.get('owner') or {}
                if (not BV.fullmatch(bvid) or not str(owner.get('mid', '')).isdigit()
                        or BV.fullmatch(target) and bvid != target
                        or AV.fullmatch(target) and str(video.get('aid')) != AV.fullmatch(target)[1]):
                    raise ValueError('视频详情与输入或 UP 主归属不符')
                resolved.append(('video', video))
        keys = {(kind, value if kind == 'collection' else value['bvid']) for kind, value in resolved}
        if len(keys) != 1:
            raise ValueError('消息中有多个不同的视频或合集，请指定要订阅哪一个')
        return resolved[0]

    async def discover(self, seed, rule, *, catalog=True):
        mid = str(seed['owner']['mid'])
        ugc = seed.get('ugc_season') or {}
        candidates = []
        if str(ugc.get('mid', mid)) == mid and str(ugc.get('id', '')).isdigit():
            candidates.append(str(ugc['id']))
        # A missing embedded season may still be found in the uploader's catalogue.
        if not candidates and catalog:
            try:
                for row in await self.plugin.api_client.fetch_user_collections(mid):
                    meta = row.get('meta') or {}
                    contains = any(v.get('bvid') == seed['bvid'] or seed.get('aid') and str(v.get('aid')) == str(seed['aid'])
                                   for v in row.get('archives', []) if isinstance(v, dict))
                    title = str(meta.get('name') or meta.get('title') or '')
                    if str(meta.get('mid')) == mid and (contains or rule and normalized_title(rule['series']) in normalized_title(title)):
                        sid = str(meta.get('season_id') or '')
                        if sid.isdigit() and sid not in candidates:
                            candidates.append(sid)
            except Exception:
                return None
        matches = []
        for sid in candidates[:5]:
            try:
                season = await self.plugin.season_client.fetch_season(mid, sid)
            except Exception:
                continue
            if any(str(v.get('bvid')) == seed['bvid'] for v in season['videos']):
                matches.append(season)
        if len(matches) > 1:
            raise ValueError('找到多个收录该视频的合集，请直接提供要订阅的合集链接')
        return matches[0] if matches else None

    async def add(self, event, raw):
        from .main import SeasonSubscription, Subscriber, build_label, utc_now_iso

        started = int(time.time())
        kind, seed = await self.resolve(raw, event)
        if kind == 'collection':
            return await self.plugin._handle_add_collection(event, seed)
        origin = event.unified_msg_origin
        if EXCLUDED.search(normalized_title(seed.get('title', ''))):
            raise ValueError('这条视频的标题标为音频、预告或切片，请提供要追更的视频正片')
        season = await self.discover(seed, None, catalog=False)
        rule = None
        try:
            rule = await infer_rule(seed, self.plugin.summarizer, origin)
        except (ValueError, RuntimeError):
            pass
        if season is None:
            season = await self.discover(seed, rule)
        if rule is None:
            if season:
                message = await self.plugin._handle_add_collection(event, season['url'])
                return message + '\n未可靠识别系列版本，使用合集原始追更；投稿补漏沿用原配置。'
            raise ValueError('未找到可验证的合集，也无法可靠识别系列。请提供合集链接，或换一条节目名称更明确的正片')
        mid = str(seed['owner']['mid'])
        identity = rule_id(mid, rule, origin)
        videos = season['videos'] if season else []
        current = next((v for v in await self.plugin.store.list_subscriptions() if v.mid == mid and v.series_rule == rule
                        and any(s.unified_msg_origin == origin for s in v.subscribers)), None)
        if current and any(sub.unified_msg_origin == origin for sub in current.subscribers):
            return '你已经订阅过这个系列了：' + current.season_title + '\n' + self.describe(current)
        if current:
            current.subscribers.append(Subscriber(origin, build_label(event), utc_now_iso()))
        else:
            snapshot = {'bvid': seed['bvid'], 'title': str(seed.get('title') or ''), 'pubdate': int(seed.get('pubdate') or 0),
                        'duration': seed.get('duration', 0), 'mid': mid, 'author': seed['owner'].get('name', '')}
            videos = season['videos'] if season else []
            current = SeasonSubscription(mid, identity, rule['series'], seed['owner'].get('name', ''),
                season['url'] if season else f'https://space.bilibili.com/{mid}/upload/video',
                seen_bvids=list(dict.fromkeys([seed['bvid'], *[v['bvid'] for v in videos if v.get('bvid')]])),
                subscribers=[Subscriber(origin, build_label(event), utc_now_iso())], series_rule=rule,
                collection_id=str(season['season_id']) if season else '', seed_video=snapshot,
                series_started_at=started, upload_matched_videos=[snapshot], collection_probe_at=started)
        await self.plugin.store.subscribe_series(current, origin, videos)
        return ('订阅成功：' + current.season_title + '\n' + self.describe(current)
                + '\n只追踪订阅后的新投稿，种子视频和已有历史不会自动补发。可用「规则 <订阅ID>」查看判断记录。')

    @staticmethod
    def describe(item):
        rule = item.series_rule
        return (f"订阅ID：{item.season_id}\nUP：{item.uploader_name}（{item.mid}）\n"
                f"节目：{rule['series']}；版本：{rule['edition'] or '无明确版本标签'}；季数：{rule['season'] or '0'}\n"
                + (f'来源：合集 {item.collection_id}＋UP 投稿补漏' if item.collection_id else '来源：同一 UP 主的系列投稿（暂未验证到所属合集）'))

    async def source(self, item, *, scan=True):
        if not item.series_rule:
            return await self.plugin.season_client.fetch_season(item.mid, item.season_id)
        if scan and not item.collection_id and time.time() - item.collection_probe_at >= 86400:
            item.collection_probe_at = int(time.time())
            seed = (await self.plugin.api_client.fetch_video_detail(item.seed_video['bvid'])).get('data') or {}
            if seed.get('bvid') == item.seed_video['bvid'] and str((seed.get('owner') or {}).get('mid')) == item.mid:
                season = await self.discover(seed, item.series_rule)
                if season:
                    item.collection_id = str(season['season_id'])
                    item.url = season['url']
        if item.collection_id:
            season = await self.plugin.season_client.fetch_season(item.mid, item.collection_id)
            return {**season, 'season_title': item.season_title}
        return {'videos': [], 'season_title': item.season_title, 'uploader_name': item.uploader_name, 'url': item.url}

    async def attach_from_match(self, item):
        # New episodes may enter a collection even when the original seed never does.
        if item.collection_id or not item.series_rule:
            return
        for video in reversed(item.upload_matched_videos):
            if video['bvid'] in item.seen_bvids:
                continue
            detail = (await self.plugin.api_client.fetch_video_detail(video['bvid'])).get('data') or {}
            if detail.get('bvid') != video['bvid'] or str((detail.get('owner') or {}).get('mid')) != item.mid:
                return
            season = await self.discover(detail, item.series_rule, catalog=False)
            if season:
                item.collection_id = str(season['season_id'])
                item.url = season['url']
            return

# AstrBot B 站合集订阅插件

这是一个给 AstrBot 用的插件，用来订阅 B 站某个合集（Season/列表），并在合集里出现新视频时自动下载后发送。
当前版本已经重构为“B 站接口解析 + 直链下载”路线，不再直接依赖 `yt-dlp` 去抓公开视频页。

当前版本默认支持这类链接：

- `https://space.bilibili.com/<mid>/lists/<season_id>?type=season`
- `https://space.bilibili.com/<mid>/channel/collectiondetail?sid=<season_id>`

## 功能

- 订阅指定 B 站合集
- 删除当前会话对某个合集的订阅
- 查看当前会话的订阅列表
- 手动立即检查一次
- 后台定时轮询，发现新视频后自动下载并发送
- 可调下载清晰度上限
- 可调视频时长上限
- 可调文件大小上限
- 可调发送后文件保留时长
- 支持手动重试待处理失败项
- 失败下载自动进入下次轮询重试
- 改为先解析 B 站媒体流，再下载发送，尽量避开旧版的 `412` 问题
- 发送视频前会先单独转发视频标题，避免平台吞掉说明文字

## 安装

把整个 `astrbot_plugin_bilibili_season_subscribe` 文件夹放到 AstrBot 的插件目录中，然后在 AstrBot 后台安装依赖并启用插件。

推荐测试顺序：

1. 安装 `ffmpeg`，并保证命令行能找到，或者把路径填进 `ffmpeg_path`
2. 安装插件依赖
3. 启用插件
4. 先执行一次 `/bili合集订阅 检查`，确认当前平台支持视频或文件发送

插件依赖：

- `httpx>=0.27,<1`
- 建议安装 `ffmpeg`

说明：

- 如果 B 站返回的是音视频分离流，插件需要配合 `ffmpeg` 才能合并成可发送的视频文件。
- 如果你不装 `ffmpeg`，插件仍可能处理一部分单文件流，但成功率会低很多。

## 配置项

- `poll_interval_minutes`：轮询间隔，默认 `20`
- `request_timeout_seconds`：请求超时，默认 `15`
- `page_size`：分页大小，默认 `30`
- `max_video_height`：最大清晰度高度，默认 `720`，填 `0` 表示不限
- `max_duration_seconds`：最大视频时长，默认 `1800` 秒，填 `0` 表示不限
- `max_filesize_mb`：最大文件大小，默认 `80` MB，填 `0` 表示不限
- `retain_download_hours`：下载文件保留时长，默认 `24` 小时
- `preferred_video_codec`：优先编码关键字，默认 `avc`
- `preferred_ext`：优先输出容器，默认 `mp4`
- `ffmpeg_path`：ffmpeg 路径，默认留空自动查找
- `bilibili_sessdata`：可选，B 站登录态里的 `SESSDATA`，遇到 `412` 时建议填写
- `bilibili_cookie_file`：可选，Netscape 格式的 `cookie.txt` 路径
- `notify_on_first_subscribe`：首次订阅时是否把当前已有视频也纳入下载发送范围，默认 `false`

## 指令

- `/bili合集订阅 添加 <合集链接>`
- `/bili合集订阅 删除 <season_id>`
- `/bili合集订阅 列表`
- `/bili合集订阅 检查`
- `/bili合集订阅 重试`
- `/bili合集订阅 重试 <season_id>`

## 示例

```text
/bili合集订阅 添加 https://space.bilibili.com/1865348651/lists/5193004?type=season
/bili合集订阅 列表
/bili合集订阅 检查
/bili合集订阅 重试
/bili合集订阅 重试 5193004
/bili合集订阅 删除 5193004
```

## 存储说明

插件会把订阅数据保存到自己的数据目录下，文件名为 `subscriptions.json`。
下载下来的视频默认保存在插件数据目录下的 `downloads/` 子目录。

同一个合集可以被多个会话同时订阅；每个会话删除自己的订阅时，不会影响其他会话，只有最后一个订阅者删除后，合集记录才会被清理。

如果某个新视频因为网络波动、发送失败或缺少 `ffmpeg` 没有成功发出，它会进入待重试列表，下一轮轮询还会继续尝试。
如果下载时报 `HTTP 412`，通常是 B 站风控或未带登录态，优先尝试填写 `bilibili_sessdata` 或配置有效的 cookie 文件。

## 注意

- 这个插件当前基于 B 站网页侧接口轮询，不依赖登录态，但网页接口将来如果调整，可能需要同步更新。
- 主动推送是否成功，也取决于你当前使用的 AstrBot 适配器/平台是否允许 bot 主动发消息，以及该平台是否支持本地视频/文件上传。

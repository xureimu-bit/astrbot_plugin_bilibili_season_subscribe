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
- 可选逐片 AI 总结：发送视频后另发字幕摘要，无字幕时标明仅概述标题/简介
- 支持手动总结 BV 号或完整视频链接

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
- `/bili合集订阅 总结 <BV号|视频链接>`

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


## AI 视频总结（v1.3.0）

总结的默认范围是**每个成功发送的新增视频**，不合并整次更新，也不总结整个合集。
首次订阅仍然默认跳过历史视频；只有 `notify_on_first_subscribe=true` 时才会连同历史视频推送及总结。
超过下载时长/大小限制而被跳过的视频、下载失败的视频，不触发自动总结。
现有下载器只下载第 1 P，因此多 P 视频的总结也仅使用第 1 P 字幕，并在来源说明中标明。
即使手动链接含 `?p=2`，目前也处理第 1 P。

### 启用及配置

1. 在 AstrBot 配好可用的聊天模型，建议使用 AstrBot 4.5.7 或更新版本。
2. 在插件配置中把 `summary_enabled` 设为 `true`，保存并重载插件。
3. 如需指定模型，填写 `summary_provider_id`；留空时按每个接收会话的当前聊天模型选择。
4. 先运行 `/bili合集订阅 总结 BV1xx411c7mD`（替换为真实视频的 BV 号）验证模型，再检查订阅推送。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `summary_enabled` | `false` | 自动总结开关；升级后保持原有推送行为，手动总结不受此开关影响 |
| `summary_provider_id` | 空字符串 | AstrBot 的聊天模型 Provider ID；不是模型名称或 API Key |
| `summary_timeout_seconds` | `60` | 每个接收会话单次总结的整体超时，包含资料获取和模型生成；限制为 5–300 秒 |
| `summary_max_input_chars` | `12000` | 输入资料的字符上限（不含提示词及 JSON 包装），限制为 500–50000 |
| `summary_max_output_chars` | `800` | 摘要正文字符上限，限制为 100–4000；超长响应截断并标明，不含截断提示、标题、链接和来源说明 |

调用优先使用 AstrBot 的 `get_current_chat_provider_id(umo=...)` 和 `llm_generate(...)`。
旧版缺少 `llm_generate` 时回退到 `get_using_provider(umo=...)` / `get_provider_by_id(...)` 及 `provider.text_chat(...)`。
总结不读写聊天对话历史，不给模型提供工具。它将视频标题、简介和取得的字幕发给所选模型服务，启用后会产生模型调用费用。
同一次视频发送中，使用同一个 Provider 的会话复用摘要；不同 Provider 单独生成。资料和结果不持久化缓存。

### 资料来源与失败处理

- 获取视频详情及第 1 P 字幕，使用带 WBI 签名的播放器接口。优先选择中文字幕，同语言内优先人工字幕；最多尝试三条字幕。
- 字幕下载复用现有 HTTP 超时设置；播放器接口复用已有 B 站 Cookie 配置，字幕 CDN 请求不携带登录 Cookie。
- 字幕可能要求登录、尚未生成或接口不可用。取得不到字幕时，明确标注“仅标题/简介（字幕不可取得，非视频全文总结）”，不声称分析过画面或音频；不进行语音转写。
- 自动字幕也可能有识别错误。资料超长时只总结截取部分，并明确标注“资料已截断”。有字幕时为字幕保留大部分输入预算。
- 推送顺序是标题 → 视频（或原有文件/文本回退）→ 单独的总结文本。模型未配置、超时、返回空内容或总结发送失败时记录日志，成功发送的视频仍记为已处理，不因总结失败重复推送。
- 视频发送失败仍按原有待重试机制处理；之后成功发送时才尝试总结。
- 手动指令：`/bili合集订阅 总结 <BV号|https://www.bilibili.com/video/BV号>`，不需要先订阅，不下载视频，不修改订阅状态；暂不支持 `b23.tv` 短链接。

## 开发验证

安装 `requirements.txt` 后，在仓库根目录执行：

```text
python -m unittest discover -s tests -v
python -m compileall -q main.py summarization.py
```

测试使用真实 `httpx` 的模拟传输验证字幕请求，AstrBot 平台和 LLM 边界使用替身。
覆盖新旧模型接口、会话模型选择、输入输出限制、字幕回退、超时/取消及订阅推送/重试回归。
实际 B 站登录态、模型服务和适配器上传能力需在部署的 AstrBot 环境验证。
参考：[AstrBot 官方 AI 接口文档](https://docs.astrbot.app/dev/star/guides/ai.html)。

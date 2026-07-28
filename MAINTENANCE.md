# 维护手册 / 架构记忆

这份文档是给以后（人类或 AI 助手）维护这个 repo 用的「记忆」。改动前先看这里，理解为什么
现在是这个设计，而不是一上来就改代码。

## 这是什么

每天运行一次的多频道资讯汇总机器人。「频道」是一份独立的主题日报（比如"AI 行业日报"、
"自动驾驶日报"），每个频道有自己的信息源列表和自己的分类体系，互不干扰，共用同一套抓取/
摘要/渲染代码。对每个频道：

1. 从 `config/sources/<频道key>.yaml` 里配置的信息源（RSS 优先，普通网页兜底）抓取
   **今天**发布的文章
2. 用 DeepSeek 分两阶段做摘要 + 去重合并（下面详细讲为什么是两阶段）
3. 生成 `output/<频道key>/<日期>/digest.md`（Markdown，带原始来源链接）和
   `output/<频道key>/<日期>/digest.html`（本地网页，左侧目录可点击跳转，同样带原始来源链接）
4. 更新 `output/index.html`——顶部 tab 切换频道的首页，汇总所有频道的历史日期列表

## 核心设计取舍（改代码前务必读）

### 1. 为什么摘要是「两阶段」而不是一次性丢给模型

如果把当天抓到的几十篇文章原文一次性塞进一个 prompt 里让模型直接输出简报，会有两个问题：
context 很快爆掉，而且模型没法很好地判断"这几篇报的是同一件事，应该合并"。所以设计成
map-reduce 两阶段（实现在 `src/daily_digest/llm.py`）：

- **Stage 1（map，按批次并发）**：每批 `ARTICLES_PER_LLM_BATCH`（默认 6）篇文章一次调用，
  把每篇原文变成结构化的中文摘要 + 分类标签 + 重要性分数。批次之间并发调用
  （`MAX_CONCURRENT_LLM_CALLS`），互不依赖。
- **Stage 2（reduce，单次调用）**：把 stage 1 产出的所有摘要（此时已经很小，不是原文）一次性
  丢给模型，让它按主题分组、合并同一事件的多来源报道、给出「今日要点」。因为输入是压缩过的
  摘要而不是原文，一次调用通常能装下一天的量。

如果以后信息源多到 reduce 阶段也会超 context，思路是加一层「reduce 的 reduce」（先分几组
reduce 出小节，再合并小节），但目前信息源规模下不需要。

### 2. 为什么来源链接绝对不能让模型直接生成

这是最容易踩的坑：LLM 编故事/记错 URL 是常态。如果直接问模型"这条新闻的链接是什么"，
它可能会输出一个看起来合理但打不开或指向错误文章的 URL —— 而这个 bot 存在的意义就是
"点开能验证原文"，一旦链接不可信整个工具就没用了。

所以链接**从来不经过模型生成**：

- 每篇文章在抓取阶段就由代码生成一个稳定 id（`Article.id`，等于 URL 的 sha1 前 10 位，
  见 `models.py`）。
- 喂给模型的 prompt 里带上这个 id，只要求模型"原样抄回这个 id"，不要求它输出或改写 URL。
- 渲染阶段（`render_markdown.py` / `render_html.py`）用代码维护的 `id -> Article` 字典把
  id 换回真实 URL。
- 如果模型返回了一个字典里不存在的 id（编造的），代码会丢弃这条引用并记 warning
  （见 `llm.py` 里 `_map_batch` 和 `build_digest` 的 id 校验逻辑），而不是硬塞一个假链接。
  如果一个 digest 条目校验完一个有效来源都不剩，这条整个丢弃，不会展示无来源的内容。

改 prompt 时，**不要**让模型输出 URL 字段；只让它处理 id。

### 3. 为什么用 JSON mode + 稳定的 system prompt 前缀

DeepSeek 官方文档（见下方参考链接）建议：JSON mode 需要 (a) prompt 里出现"json"字样，
(b) 给出期望格式的示例，(c) 预留足够 `max_tokens`。`llm.py` 里两个 system prompt
（`_MAP_SYSTEM_PROMPT` / `_REDUCE_SYSTEM_PROMPT`）都满足这三点，而且是模块级常量、
每次调用完全不变的字符串——只有 user 部分的文章数据在变。

这是刻意的：DeepSeek 有自动的上下文前缀缓存（context caching，按前缀命中计费更便宜），
一次运行里同一 stage 会并发调用很多次，如果 system prompt 每次都不同（比如动态拼了某个
变量进去），就享受不到缓存。**以后改 prompt 时尽量保持它是一个固定模板，把变化的内容放
在 user message 里，不要拼进 system prompt。**

### 4. 「无关内容」过滤

用户可能会加一些不是该频道垂直媒体的信息源（比如自动驾驶频道里混进了纯 EV 消费新闻，或者
一个公司博客里偶尔发无关内容）。Stage 1 的 prompt 里专门要求模型把明显与**该频道领域**无关
的文章标记为 `topic_tag: "无关"`，`orchestrator.py` 里 `run_daily` 会在 stage 1 和 stage 2
之间把这些过滤掉（不进最终简报，也不喂给 reduce 阶段浪费 token）。"无关"判断的标准是
`Channel.domain_desc`（见下面第 6 节），不是写死的——这也是为什么新增频道不需要改
`llm.py` 代码，只需要在 `config/channels.yaml` 里描述清楚这个频道是什么领域。

### 5. 「今天」是怎么判断的

见 `timeutil.is_today()`。时区由 `.env` 里 `DIGEST_TIMEZONE`（默认 `Asia/Shanghai`）决定。
RSS 来源在抓取阶段就用 feed 自带的发布时间过滤；没有 RSS、靠爬首页链接兜底的来源
（`type: html` 且没发现 feed 的情况）在列表页拿不到发布时间，会先原样带着 `published_at
= None` 往下走，等 `extract.py` 抓到文章正文页后用 `trafilatura` 解析出的 metadata 日期
再补上，之后统一在 `orchestrator.py` 里再过滤一次。没有任何日期信息的文章默认**不算今天**
（除非 `.env` 里把 `INCLUDE_UNDATED_AS_TODAY` 设成 true）——宁可漏掉也不要混进过期内容。

### 6. 多频道架构：为什么是 `Channel` 而不是写死 AI 行业

最早这个 bot 只做 AI 行业日报，`_MAP_SYSTEM_PROMPT` / `_REDUCE_SYSTEM_PROMPT` 和分类标签
列表都是模块级常量，硬编码"人工智能行业"。加自动驾驶日报的时候没有复制一份 `llm.py`，
而是把"这是什么领域"这件事提取成 `models.Channel`（`key`/`name`/`domain_desc`/`topics`），
`llm.py` 里的两个 prompt 改成了 `_map_system_prompt(channel)` / `_reduce_system_prompt(channel)`
—— 纯函数，输入 `Channel` 输出 prompt 字符串，不依赖任何全局状态。

这带来两个约束，改代码时要注意：

- **prompt 函数必须是 `channel` 的纯函数**，不能掺进本次运行才知道的变量（比如日期、文章
  数量）。原因见第 3 节——同一个频道在一次运行里会并发调多次，prompt 前缀必须字节级不变
  才能吃到 DeepSeek 的前缀缓存。日期、批次这些可变信息永远放在 user message 里。
- **"无关"判断依赖 `channel.domain_desc` 而不是关键词列表**。这是有意的——让模型根据一句话
  描述去判断相关性，比维护一份不断增长的关键词黑名单更鲁棒，缺点是描述写得含糊，过滤效果
  就会变差（比如自动驾驶频道如果 `domain_desc` 写得太宽泛，纯 EV 消费新闻也会被判定为相关，
  实测就出现过这种情况——这不是 bug，是 prompt 措辞的问题，想收紧就把 `domain_desc` 和
  `topics` 写得更具体）。

配置层面：`config/channels.yaml` 是频道清单，每个频道对应一个
`config/sources/<key>.yaml`（约定优于配置，路径由 `channels.sources_file_for()` 拼出来，
不需要在 yaml 里显式写路径）。`Channel.topics` 在 `channels.load_channels()` 里会自动补上
`"其他"` 和 `"无关"` 两个隐含标签，配置文件里不用重复写。

## 模块地图

```
src/daily_digest/
  config.py          Settings（读 .env 里的密钥/开关，与频道配置无关）
  channels.py          Channel 定义的读取：config/channels.yaml -> list[Channel]，
                        以及 sources_file_for(channel) 算出它的 sources 文件路径
  models.py           Channel / Source / Article / ArticleSummary / DigestItem / DigestSection / Digest
  sourcesio.py         某个频道 sources yaml 文件的读写、增删（CLI `sources add/remove/list` 背后调的就是它，
                        本身不关心频道概念，只是对着一个 Path 操作）
  timeutil.py           「是不是今天」的时区处理
  fetch.py               RSS 解析（feedparser）+ feed 自动发现 + 无 RSS 时的 HTML 兜底爬取，全部并发
  extract.py              用 trafilatura 抓正文全文 + 补发布日期
  llm.py                  DeepSeek 客户端 + map/reduce 摘要管道；两个 system prompt 是
                          `_map_system_prompt(channel)` / `_reduce_system_prompt(channel)`（见上）
  orchestrator.py          对单个 channel 串起 fetch -> extract -> 过滤今天 -> summarize ->
                          render -> 写到 output/<channel.key>/ -> 刷新 output/index.html
  render_markdown.py        Digest -> markdown 字符串（标题里用 digest.channel_name）
  render_html.py             Digest -> 本地网页 html（Jinja2 模板在 templates/）+
                          render_combined_index() 生成跨频道的 tab 首页
  templates/
    digest.html.jinja         单日简报页面：左侧 TOC（锚点跳转）+ 正文 + 每条来源外链
    index.html.jinja           output/index.html，顶部 tab 切换频道，每个 tab 列出该频道历史日期
  cli.py                    argparse 入口：run [--channel]（省略则跑全部频道）/
                          channels list / sources list|add|remove|check（均需要 --channel）
```

数据流（单个频道一次运行）：`fetch.fetch_all` → `extract.enrich_all` → 按今天过滤 →
`llm.summarize_articles(articles, channel, ...)` (map) → 按"无关"过滤 →
`llm.build_digest(summaries, channel, ...)` (reduce) → `render_markdown` /
`render_html.render_digest_html` → 写到 `output/<channel.key>/<date>/` →
`render_html.render_combined_index()` 重新生成 `output/index.html`（扫全部频道的
`meta.json`，不需要重新调用 LLM）。全部由 `orchestrator.run_daily(channel, ...)` 串联；
`cli.py` 的 `run` 命令在没传 `--channel` 时对 `channels.load_channels()` 里每个频道各调一次。

## 已知局限（不是 bug，是现实情况，遇到时不用惊讶）

- **JS 渲染的网站**（比如很多用 Next.js/Vue 之类框架做的官方博客或 SPA）：`type: html`
  兜底抓取只拿静态 HTML，抓不到需要 JS 才能渲染出来的内容/日期。**优先给这类站点找真实的
  RSS feed 地址**——很多看起来是 SPA 的站点其实仍然维护着一个传统 RSS feed，只是首页链接里
  没直接暴露，值得先用 `feedparser.parse()` 试几个常见路径（`/feed`、`/rss`、
  `/rss.xml`、`/atom.xml`、`/<栏目>/feed/` 等）再判定它真的没有 feed。
  - 已验证可用：OpenAI Blog → `https://openai.com/news/rss.xml`；
    Google DeepMind Blog → `https://deepmind.google/blog/rss.xml`。这两个之前配的是
    `type: html` 抓首页链接，改成直接给 feed 地址后稳定多了（HTML 兜底当天抓到 0 篇不代表
    源本身有问题，也可能只是当天真的没发新内容——用 feedparser 直接探一下 feed 里最新一条
    的日期就能确认）。
  - 已确认没有可用 RSS、暂时只能吃 html 兜底的：机器之心（jiqizhixin.com）是纯前端渲染的
    SPA，`/rss`、`/rss.xml`、`/feed.xml` 等常见路径都只返回 SPA 的 HTML 壳或 404/500；
    公开 RSSHub 实例（rsshub.app）对它的路由也会直接 403。目前这条源基本抓不到东西，
    如果你自建了 RSSHub 或者找到别的可靠数据源，把 `config/sources/ai.yaml` 里这条换成
    `type: rss` 即可。自动驾驶频道也是同样情况：试过车东西/懂车帝/第一电动/盖世汽车/36氪
    的常见 feed 路径，都不可用，`config/sources/autonomous_driving.yaml` 目前只有验证过的
    英文源（TechCrunch Transportation / Electrek / InsideEVs / The Verge Cars）。
- **会针对爬虫返回 403 的站点**：分两种情况——(a) 拒绝抓 feed/首页本身（实测早期
  `openai.com/news` 首页对默认 UA 返回 403，但换成它的 `rss.xml` 就正常，说明很多站点对
  feed 端点的限制比首页宽松）；(b) feed 能拿到，但点进具体文章页时被 403（实测
  VentureBeat 的部分文章）——这种情况 `extract.py` 会捕获下载失败，退化成只显示标题、
  摘要写"无正文"，文章本身不会从简报里消失，只是摘要质量打折扣。这两种目前都没有做伪装
  浏览器指纹之类的规避手段。
- **`sources check` / `run` 详细日志（-v）里可能出现 `urllib3 ... Connection pool is
  full` 的 WARNING**：来自 `trafilatura.fetch_url` 内部的连接池在高并发下被打满，是无害的
  性能提示，不影响正确性；如果想根治可以降低 `MAX_CONCURRENT_FETCHES`。
- **DeepSeek 模型名会变**：`.env.example` 里默认 `DEEPSEEK_MODEL=deepseek-chat`——这是
  DeepSeek 官方长期维护的"当前主力对话模型"别名，具体底层版本会不定期升级。改模型前去
  <https://api-docs.deepseek.com/quick_start/pricing> 确认当前可用的模型名和价格，不要
  凭记忆硬编码新模型名。
- **中文在 Windows 终端里显示乱码**：是终端编码问题不是程序 bug，`cli.py` 的 `main()` 已经
  强制把 stdout/stderr 设成 UTF-8（`_force_utf8_console`），正常终端（Windows Terminal /
  PowerShell 7 / VS Code）应该不会再乱码；如果还乱码，说明终端本身没用 UTF-8 codepage，
  执行一次 `chcp 65001`。
- **自动驾驶频道实测内容偏"EV 通用新闻"而非严格意义的自动驾驶**：因为可用信息源
  （Electrek/InsideEVs）本身是电动车媒体，很多报道的是新车续航/快充这类内容，只是顺带提到
  自动驾驶功能。"无关"过滤器只会挡掉完全不沾边的内容，不会因为"不够聚焦自动驾驶"就排除。
  如果想收紧，两个方向：(a) 把 `config/channels.yaml` 里这个频道的 `domain_desc` 写得更
  具体（比如强调"仅限自动驾驶技术/法规/事故，不含常规新车/续航资讯"），(b) 换成更垂直的
  信息源。这不是 bug，是"频道有多准"完全取决于 `domain_desc` 和信息源选择这件事的直接体现。

## 部署方式

两条部署路径都保留着，别互相干扰：

- **Windows 计划任务**（本机跑）：`run_daily_digest.bat` + `schtasks /create ...`（README 里
  有具体命令），只在电脑开机且到点时触发，不会自动唤醒睡眠中的电脑。
- **树莓派常驻**（推荐，24x7 不依赖某台电脑开机）：[deploy/pi/install.sh](deploy/pi/install.sh)
  一键装好两个 systemd 单元——`daily-digest.timer`（每天 08:00 跑一次）和
  `daily-digest-web.service`（常驻 `python -m http.server` 把 `output/` 暴露到局域网，
  默认端口 8080，配 `Restart=on-failure` 崩了自动拉起来）。网页服务本质就是把已经生成好的
  静态 html 文件（`digest.html` / `index.html`）直接当静态资源伺服，没有额外的后端代码，
  所以`render_html.py` 输出的 html 必须保持"自包含、可以直接当静态文件打开"这个约束
  （不能引入需要服务端渲染或者相对于某个特定 web root 的路径假设）。

## 怎么测试

```bash
pip install -e ".[dev]"
pytest                    # 全是纯逻辑测试，不联网、不需要 API key
daily-digest sources check --channel ai -v   # 联网测试抓取，不调用 DeepSeek，不消耗 token
```

`tests/` 里的 LLM 相关测试（`test_llm_pipeline.py`）都是通过给 `summarize_articles` /
`build_digest` 传一个假的 `chat_fn`（签名 `(system_prompt, user_content, max_tokens) ->
dict`）来测试的，不需要真实 API key —— 这也是 `llm.py` 把 DeepSeek 客户端调用抽成一个可注入
的 `ChatFn` 类型的原因。以后加新的 LLM 相关逻辑，保持这个可测试的边界。

## 怎么扩展

- **加一个新频道**（不需要改代码）：
  1. `config/channels.yaml` 加一条 `key`/`name`/`domain_desc`/`topics`
  2. 建 `config/sources/<key>.yaml`（参考 `config/sources/ai.yaml` 的格式）
  3. `daily-digest sources check --channel <key>` 验证信息源抓得到东西
  4. `daily-digest run --channel <key>` 先单独跑一次看效果，没问题了就会自动被
     `daily-digest run`（不带 `--channel`）一起跑到
  - `domain_desc` 写清楚一点，它直接决定"无关内容"过滤的准不准，见上面第 6 节和"已知局限"
    里自动驾驶频道那条的经验教训。
- **加新的信息源类型**（比如 Twitter/X、Reddit、Telegram）：参考同类开源项目
  [Thysrael/Horizon](https://github.com/Thysrael/Horizon) 的做法——它是按平台写独立
  fetcher，抓完统一转成中立的条目结构。这个 repo 里对应的中立结构就是 `models.Article`；
  新加一个 `type` 分支，在 `fetch.py` 里加一个新的 `fetch_xxx_articles` 函数，`Source.type`
  加一个新取值即可，不需要改 `llm.py` / `render_*.py`，对所有频道都生效。
- **改某个频道的摘要/合并措辞、分类体系**：改 `config/channels.yaml` 里那个频道的
  `domain_desc` / `topics` 就够了，不用碰 `llm.py`——`_map_system_prompt` /
  `_reduce_system_prompt` 是读 `Channel` 生成 prompt 的纯函数。只有想改**所有频道通用**的
  措辞/输出格式（比如 JSON schema 本身、执行摘要条数）才需要动 `llm.py` 里这两个函数，
  记得保持 prompt 仍然满足 JSON mode 的三个要求（含"json"字样、给示例、`max_tokens` 够用），
  并且不要引入模型输出 URL 的字段，也不要往 prompt 里塞本次运行才知道的可变信息（见第 6 节
  关于前缀缓存的约束）。
- **换成别的 OpenAI 兼容模型/服务商**：`llm.make_chat_fn` 里只认 `settings.deepseek_*`
  三个字段构造 `OpenAI(api_key=..., base_url=...)`，理论上换成任何 OpenAI 兼容端点
  （包括另一家模型）只需要改 `.env` 里的 `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` /
  `DEEPSEEK_API_KEY`，不需要改代码，对所有频道都生效。

## 调研参考（写这份设计时查过的资料）

- DeepSeek 官方文档 <https://api-docs.deepseek.com/>：JSON mode 用法
  (`/guides/json_mode`)、自动上下文缓存 (`/guides/kv_cache`)、模型与价格
  (`/quick_start/pricing`，具体型号请以该页面实时信息为准)。
- 开源同类项目 [Thysrael/Horizon](https://github.com/Thysrael/Horizon)：多平台抓取 +
  AI 打分 + 结构化摘要 + 中英双语简报，架构上验证了"打分/去重 -> 结构化摘要 -> 多格式输出"
  这条路径是这类 bot 的常见做法。

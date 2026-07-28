# Daily Digest Bot

每天自动扫描你指定的信息源（RSS 优先，也支持没有 RSS 的普通网站），只挑出**今天**发布的内容，
用 DeepSeek 做摘要与去重合并。支持多个独立的**频道**（比如"AI 行业日报"、"自动驾驶日报"），
每个频道各自的信息源、各自的分类体系、各自的输出，互不干扰。每个频道每天生成两份输出：

- `output/<频道>/<日期>/digest.md` —— 原始 Markdown 简报，每条都附原始来源链接
- `output/<频道>/<日期>/digest.html` —— 本地网页版，左侧目录点击可跳转到对应条目，
  每条同样附原始来源链接（新标签页打开）
- `output/index.html` —— 首页，顶部 tab 切换不同频道，列出该频道所有历史日期

架构、设计取舍、以及日常维护方法都写在 [MAINTENANCE.md](MAINTENANCE.md) 里 ——
以后要改 prompt、加频道、加信息源类型、换模型，先看那份文档。

## 快速开始

```bash
pip install -e .
cp .env.example .env   # 然后编辑 .env，填入 DEEPSEEK_API_KEY
```

看看目前配置了哪些频道：

```bash
daily-digest channels list
```

先不花 token，测试一下某个频道的信息源能不能抓到今天的文章：

```bash
daily-digest sources check --channel ai -v
```

配置好 `.env` 之后正式跑一次（不加 `--channel` 会把 `config/channels.yaml` 里的频道全部跑一遍）：

```bash
daily-digest run
```

只想跑其中一个频道：

```bash
daily-digest run --channel autonomous_driving
```

## 加一个新频道

1. 在 [config/channels.yaml](config/channels.yaml) 里加一条（`key`、`name`、`domain_desc`、`topics`）
2. 建一个 `config/sources/<key>.yaml`（格式参考 [config/sources/ai.yaml](config/sources/ai.yaml)）
3. `daily-digest sources check --channel <key>` 测一下

不需要改代码。详细设计见 [MAINTENANCE.md](MAINTENANCE.md)。

## 管理信息源

每个频道的信息源配置在 `config/sources/<频道key>.yaml`，可以直接编辑，也可以用命令行
（都需要带 `--channel`）：

```bash
daily-digest sources list --channel ai
daily-digest sources add --channel ai --name "Ars Technica AI" --url "https://arstechnica.com/ai/feed/" --type rss --category "海外科技媒体"
daily-digest sources remove --channel ai --name "Ars Technica AI"
```

`type: rss` 需要给 RSS/Atom feed 地址（优先用这个，最稳定）；`type: html` 给普通主页地址，
程序会先尝试自动发现该站点的 feed，找不到再退化为抓取首页链接，效果依站点而定。

## 定时每天运行

这个 repo 本身不包含常驻进程，需要你用系统自带的定时任务来每天触发 `daily-digest run`
（默认会把所有频道都跑一遍）。

**Windows（任务计划程序）**，以管理员或当前用户权限打开 PowerShell 运行一次（会创建一个每天
8:00 触发的计划任务，运行前请确认路径和时间是否符合你的需要）：

```powershell
schtasks /create /tn "DailyDigestBot" /tr "cmd /c cd /d D:\projects\daily-digest-bot && daily-digest run" /sc daily /st 08:00
```

**macOS/Linux（cron）**，`crontab -e` 添加一行：

```bash
0 8 * * * cd /path/to/daily-digest-bot && /usr/bin/env python -m daily_digest.cli run >> output/run.log 2>&1
```

## 测试

```bash
pip install -e ".[dev]"
pytest
```

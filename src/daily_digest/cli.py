"""CLI entrypoint for daily-digest-bot."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .channels import DEFAULT_CHANNELS_FILE, Channel, get_channel, load_channels, sources_file_for
from .config import DEFAULT_OUTPUT_DIR, load_settings
from .orchestrator import run_daily
from .sourcesio import add_source, load_sources, remove_source


def _resolve_channel(args: argparse.Namespace) -> Channel:
    try:
        return get_channel(args.channel)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)


def _cmd_run(args: argparse.Namespace) -> None:
    settings = load_settings()
    if not args.dry_run and not settings.has_api_key:
        print(
            "错误: 未检测到 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入你的 key，"
            "或加 --dry-run 只测试抓取部分（不消耗 token）。",
            file=sys.stderr,
        )
        raise SystemExit(1)

    channels = [_resolve_channel(args)] if args.channel else load_channels()
    if not channels:
        print(f"错误: {DEFAULT_CHANNELS_FILE} 里没有配置任何频道。", file=sys.stderr)
        raise SystemExit(1)

    for channel in channels:
        digest = run_daily(
            channel,
            settings,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            day_dir = args.output_dir / channel.key / digest.date_str
            print(f"[{channel.key}] 完成: {day_dir / 'digest.md'}")
            print(f"[{channel.key}] 完成: {day_dir / 'digest.html'}")
    if not args.dry_run and len(channels) > 1:
        print(f"完成: {args.output_dir / 'index.html'}（首页，可切换频道 tab）")


def _cmd_channels_list(args: argparse.Namespace) -> None:
    channels = load_channels()
    if not channels:
        print(f"{DEFAULT_CHANNELS_FILE} 中还没有配置任何频道。")
        return
    for ch in channels:
        print(f"- {ch.key}: {ch.name}（{ch.domain_desc}）")


def _cmd_sources_list(args: argparse.Namespace) -> None:
    channel = _resolve_channel(args)
    sources_file = args.sources_file or sources_file_for(channel)
    sources = load_sources(sources_file)
    if not sources:
        print(f"{sources_file} 中还没有配置任何信息源。")
        return
    for s in sources:
        flag = "" if s.enabled else " [已禁用]"
        print(f"- {s.name} ({s.type}, {s.category}){flag}\n    {s.url}")


def _cmd_sources_add(args: argparse.Namespace) -> None:
    channel = _resolve_channel(args)
    sources_file = args.sources_file or sources_file_for(channel)
    sources = add_source(sources_file, args.name, args.url, args.type, args.category)
    print(f"已添加 {args.name} 到频道 {channel.key}，当前共 {len(sources)} 个信息源。")


def _cmd_sources_remove(args: argparse.Namespace) -> None:
    channel = _resolve_channel(args)
    sources_file = args.sources_file or sources_file_for(channel)
    sources = remove_source(sources_file, args.name)
    print(f"已从频道 {channel.key} 移除 {args.name}，当前共 {len(sources)} 个信息源。")


def _cmd_sources_check(args: argparse.Namespace) -> None:
    channel = _resolve_channel(args)
    settings = load_settings()
    run_daily(
        channel,
        settings,
        sources_file=args.sources_file,
        output_dir=args.output_dir,
        dry_run=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daily-digest", description="每日多频道资讯汇总机器人")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出详细日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="抓取今日资讯并生成简报（Markdown + 本地网页），默认跑 channels.yaml 里的全部频道"
    )
    run_parser.add_argument("--channel", help="只跑这一个频道（省略则跑全部频道）")
    run_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    run_parser.add_argument(
        "--dry-run", action="store_true", help="只抓取、不调用 DeepSeek，用于验证信息源（不消耗 token）"
    )
    run_parser.set_defaults(func=_cmd_run)

    channels_parser = subparsers.add_parser("channels", help="查看已配置的频道 (config/channels.yaml)")
    channels_sub = channels_parser.add_subparsers(dest="channels_command", required=True)
    channels_list_parser = channels_sub.add_parser("list", help="列出所有频道")
    channels_list_parser.set_defaults(func=_cmd_channels_list)

    sources_parser = subparsers.add_parser("sources", help="管理某个频道的信息源列表 (config/sources/<频道>.yaml)")
    sources_sub = sources_parser.add_subparsers(dest="sources_command", required=True)

    list_parser = sources_sub.add_parser("list", help="列出某个频道的所有信息源")
    list_parser.add_argument("--channel", required=True, help="频道 key，见 `daily-digest channels list`")
    list_parser.add_argument("--sources-file", type=Path, default=None, help="不填则用该频道的默认文件")
    list_parser.set_defaults(func=_cmd_sources_list)

    add_parser = sources_sub.add_parser("add", help="给某个频道添加一个信息源")
    add_parser.add_argument("--channel", required=True, help="频道 key，见 `daily-digest channels list`")
    add_parser.add_argument("--name", required=True, help="展示名称，需唯一")
    add_parser.add_argument("--url", required=True, help="RSS/Atom feed 地址，或没有 feed 时的主页地址")
    add_parser.add_argument("--type", choices=["rss", "html", "playwright", "booli"], default="rss")
    add_parser.add_argument("--category", default="未分类")
    add_parser.add_argument("--sources-file", type=Path, default=None, help="不填则用该频道的默认文件")
    add_parser.set_defaults(func=_cmd_sources_add)

    remove_parser = sources_sub.add_parser("remove", help="从某个频道移除一个信息源")
    remove_parser.add_argument("--channel", required=True, help="频道 key，见 `daily-digest channels list`")
    remove_parser.add_argument("--name", required=True)
    remove_parser.add_argument("--sources-file", type=Path, default=None, help="不填则用该频道的默认文件")
    remove_parser.set_defaults(func=_cmd_sources_remove)

    check_parser = sources_sub.add_parser(
        "check", help="测试抓取某个频道的所有信息源今日的文章数量（不调用 DeepSeek，不消耗 token）"
    )
    check_parser.add_argument("--channel", required=True, help="频道 key，见 `daily-digest channels list`")
    check_parser.add_argument("--sources-file", type=Path, default=None, help="不填则用该频道的默认文件")
    check_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    check_parser.set_defaults(func=_cmd_sources_check)

    return parser


def _force_utf8_console() -> None:
    """Windows consoles default to the system codepage (e.g. GBK), which
    mangles the Chinese output this CLI prints. Reconfigure regardless of
    platform -- it's a no-op where stdout is already UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _force_utf8_console()
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args.func(args)


if __name__ == "__main__":
    main()

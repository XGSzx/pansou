"""命令行界面。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .config import ROOT_DIR, load_config
from .search import Searcher

COLORS = {
    "baidu": "\033[38;5;69m",
    "quark": "\033[38;5;141m",
    "aliyun": "\033[38;5;208m",
    "uc": "\033[38;5;40m",
    "xunlei": "\033[38;5;45m",
    "pan123": "\033[38;5;47m",
    "pan115": "\033[38;5;39m",
    "tianyi": "\033[38;5;203m",
    "weiyun": "\033[38;5;37m",
}
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


def enable_ansi() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            return True
    except Exception:
        pass
    return False


def colorize(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def banner() -> None:
    print("=" * 60)
    print("  影盘搜 · 全网影视网盘资源聚合搜索")
    print("  仅聚合公开网页信息，请遵守版权法规与平台条款")
    print("=" * 60)


def make_progress(verbose: bool, use_color: bool):
    lock = __import__("threading").Lock()
    started = time.time()

    def progress(ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        with lock:
            if kind == "stage":
                print(f"\n▸ {ev.get('msg')}")
            elif kind == "engine":
                status = ev.get("status")
                word = "正常" if status == "ok" else ("被拦截" if status == "blocked" else "无结果")
                col = GREEN if status == "ok" else (RED if status == "blocked" else DIM)
                line = f"  · {ev.get('label')}：{word}"
                if ev.get("count"):
                    line += f"（{ev['count']} 条）"
                print(colorize(line, col, use_color))
            elif kind == "log" and (verbose or ev.get("level") in ("warn", "error")):
                prefix = "  ! " if ev.get("level") in ("warn", "error") else "    "
                text = f"{prefix}{ev.get('msg')}"
                if ev.get("level") in ("warn", "error"):
                    text = colorize(text, YELLOW, use_color)
                print(text)
            elif kind == "crawl":
                done, total = ev.get("done", 0), ev.get("total", 0)
                if 0 < total <= 40:
                    bar_len = 22
                    filled = int(bar_len * done / total)
                    bar = "█" * filled + "░" * (bar_len - filled)
                    sys.stdout.write(f"\r  [{bar}] {done}/{total} 页  已发现 {ev.get('found', 0)} 条")
                    sys.stdout.flush()
                    if done >= total:
                        sys.stdout.write("\n")
            elif kind == "found":
                pass

    return progress


def print_results(results: list[dict[str, Any]], use_color: bool) -> None:
    if not results:
        print("\n没有找到可用的网盘链接。")
        print("建议：换成更完整的名称、加上年份/季数，或把 --crawl 调大（如 30）。")
        return
    print(f"\n共 {BOLD if use_color else ''}{len(results)}{RESET if use_color else ''} 条结果：\n")
    for i, r in enumerate(results, 1):
        col = COLORS.get(r["drive"], "")
        label = colorize(f"[{r['drive_label']}]", col, use_color)
        status = ""
        if r.get("status") == "alive":
            status = colorize(" 链接有效", GREEN, use_color)
        elif r.get("status") == "dead":
            status = colorize(" 可能失效", RED, use_color)
        print(f"{i:>2}. {label}  相关度 {r['score']}{status}")
        print(f"    链接   {r['url']}")
        if r.get("code"):
            print(f"    提取码 {colorize(r['code'], GREEN, use_color)}")
        if r.get("title"):
            print(f"    标题   {r['title'][:70]}")
        if r.get("note") and r["note"] != r.get("title"):
            print(f"    备注   {r['note'][:70]}")
        if r.get("page_url") and r["page_url"] != r["url"]:
            print(f"    来源   {r['page_url'][:100]}")
        print()


def save_json(payload: dict[str, Any], path: str) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存 JSON：{path}")


def save_csv(results: list[dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["序号", "网盘类型", "链接", "提取码", "相关度", "状态", "来源站点", "来源页面", "标题", "备注", "引擎"])
        for i, r in enumerate(results, 1):
            writer.writerow([
                i,
                r["drive_label"],
                r["url"],
                r.get("code", ""),
                r.get("score", ""),
                r.get("status", ""),
                r.get("domain", ""),
                r.get("page_url", ""),
                (r.get("title") or "").replace("\n", " "),
                (r.get("note") or "").replace("\n", " "),
                r.get("engine", ""),
            ])
    print(f"已保存 CSV：{path}")


def cmd_search(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.proxy:
        cfg["request"]["proxy"] = args.proxy
    if args.timeout:
        cfg["request"]["timeout"] = args.timeout
    if args.no_cache:
        cfg["cache"]["enabled"] = False
    if args.engines:
        wanted = [e.strip() for e in args.engines.split(",") if e.strip()]
        for key in list(cfg["engines"].keys()):
            cfg["engines"][key] = key in wanted
    if args.deep:
        cfg["search"]["max_queries"] = 10
        cfg["search"]["pages_per_query"] = 2
        cfg["search"]["crawl_pages"] = 30

    use_color = enable_ansi()
    query = " ".join(args.query).strip()
    if not query:
        print("请提供影视名称，例如：python -m pansou search 流浪地球2")
        return 2

    banner()
    print(f"查询：{query}")
    print(f"引擎：{', '.join(k for k, v in cfg['engines'].items() if v and (k not in ('serper', 'bocha') or cfg['api'].get(k + '_key')))}")

    searcher = Searcher(cfg, make_progress(args.verbose, use_color))
    started = time.time()
    payload = searcher.run(
        query,
        check=args.check or cfg["check"].get("enabled", False),
        max_results=args.max,
        crawl_pages=args.crawl,
    )
    elapsed = time.time() - started
    print_results(payload["results"], use_color)

    stats = payload["stats"]
    print(colorize(
        f"用时 {elapsed:.1f}s · 查询 {stats.get('queries', 0)} 次 · 抓取 {stats.get('crawled', 0)} 页 · "
        f"失败 {stats.get('crawl_failed', 0)} 页 · 拦截 {stats.get('blocked', 0)} 次",
        DIM, use_color,
    ))

    out_json = args.json or (str(ROOT_DIR / "out" / f"{query}.json") if args.save else None)
    if out_json:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        save_json(payload, out_json)
    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        save_csv(payload["results"], args.csv)
    if args.open and payload["results"]:
        import webbrowser

        webbrowser.open(payload["results"][0]["url"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pansou",
        description="影盘搜：输入影视名称，自动搜索并提取全网公开的网盘分享链接。",
        epilog="示例：python -m pansou 流浪地球2 --check --csv out.csv",
    )
    parser.add_argument("--config", help="配置文件路径", default=None)
    sub = parser.add_subparsers(dest="cmd")

    p_search = sub.add_parser("search", aliases=["s"], help="搜索影视资源")
    p_search.add_argument("query", nargs="+", help="影视名称，可带年份/季数")
    p_search.add_argument("--check", action="store_true", help="校验分享链接是否有效（较慢）")
    p_search.add_argument("--max", type=int, default=None, help="最多输出条数")
    p_search.add_argument("--crawl", type=int, default=None, help="深挖网页数量，默认 12")
    p_search.add_argument("--deep", action="store_true", help="深度模式：更多查询、翻页与深挖")
    p_search.add_argument("--engines", default=None, help="指定引擎，如 baidu,so360,bing")
    p_search.add_argument("--json", default=None, help="结果保存为 JSON")
    p_search.add_argument("--csv", default=None, help="结果保存为 CSV")
    p_search.add_argument("--save", action="store_true", help="自动保存到 out/ 目录")
    p_search.add_argument("--open", action="store_true", help="用浏览器打开第一条结果")
    p_search.add_argument("--proxy", default=None, help="HTTP 代理，如 http://127.0.0.1:7890")
    p_search.add_argument("--timeout", type=float, default=None, help="请求超时（秒）")
    p_search.add_argument("--no-cache", action="store_true", help="禁用本地缓存")
    p_search.add_argument("-v", "--verbose", action="store_true", help="输出详细日志")

    p_web = sub.add_parser("web", help="启动本地网页版")
    p_web.add_argument("--port", type=int, default=None, help="端口，默认 8765")
    p_web.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1")

    sub.add_parser("selftest", help="运行离线自测（不联网）")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"search", "s", "web", "selftest"}
    if argv and argv[0] not in commands and not argv[0].startswith("-"):
        argv = ["search"] + argv
    elif not argv:
        argv = ["--help"]

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd in ("search", "s"):
        return cmd_search(args)
    if args.cmd == "web":
        from .web import run_web

        run_web(args.config, args.port, args.host)
        return 0
    if args.cmd == "selftest":
        from .selftest import run_selftest

        return run_selftest()
    parser.print_help()
    return 0

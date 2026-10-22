"""配置加载与合并。

配置文件为项目根目录下的 config.json，缺省值见 DEFAULT_CONFIG。
优先级：命令行参数 > 环境变量 > config.json > 默认值。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

# 包所在目录：<项目根>/pansou/  —— 因此项目根是 parent.parent
PKG_DIR = Path(__file__).resolve().parent
ROOT_DIR = PKG_DIR.parent

DEFAULT_CONFIG: dict[str, Any] = {
    # 启用的搜索引擎（free 引擎无需任何密钥）
    "engines": {
        "baidu": True,
        "so360": True,
        "bing": True,
        "sogou": False,       # 搜狗反爬较严格，默认关闭
        "serper": True,       # 仅在填写 api.serper_key 后生效
        "bocha": True,        # 仅在填写 api.bocha_key 后生效
    },
    "request": {
        "timeout": 15,             # 单次请求超时（秒）
        "retries": 1,              # 失败重试次数
        "min_interval": 3.0,       # 同一站点两次请求的最小间隔（秒），太低容易被封
        "jitter": 0.35,            # 间隔随机抖动比例
        "max_page_bytes": 2500000, # 抓取网页正文的最大字节数
        "proxy": "",               # 例如 http://127.0.0.1:7890 ，留空表示直连
        "verify_tls": True,
    },
    "search": {
        "max_queries": 6,          # 每个引擎最多执行的查询条数（含 site: 精准查询）
        "pages_per_query": 1,      # 每条查询翻几页
        "crawl_pages": 12,         # 最多深挖多少个结果网页
        "workers": 5,              # 抓取并发数
        "include_site_queries": True,  # 是否使用 site: 精准检索
    },
    "check": {                     # 分享链接存活校验（会额外发起大量请求）
        "enabled": False,
        "workers": 5,
        "timeout": 8,
    },
    "output": {
        "max_results": 60,         # 最多输出条数
        "min_score": 0.0,          # 低于该分数的结果丢弃
        "max_per_page": 15,        # 同一来源页最多保留的条数，避免聚合站刷屏
    },
    "api": {
        "serper_key": "",          # https://serper.dev 的 Key（可选）
        "bocha_key": "",           # 博查搜索 API Key（可选）
    },
    "cache": {
        "enabled": True,
        "ttl": 21600,              # 缓存有效期（秒），默认 6 小时
        "dir": ".cache",
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
    },
}

ENV_MAP = {
    "PANSOU_PROXY": ("request", "proxy"),
    "PANSOU_TIMEOUT": ("request", "timeout"),
    "PANSOU_SERPER_KEY": ("api", "serper_key"),
    "PANSOU_BOCHA_KEY": ("api", "bocha_key"),
    "PANSOU_PORT": ("web", "port"),
}


def _deep_merge(base: dict, patch: dict) -> dict:
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg_path = Path(path) if path else ROOT_DIR / "config.json"
    if cfg_path.exists():
        try:
            user_cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            if isinstance(user_cfg, dict):
                _deep_merge(cfg, user_cfg)
        except Exception as exc:  # 配置写坏了不要影响启动
            print(f"[warn] 读取配置文件失败，已使用默认配置：{exc}")

    for env, (sec, key) in ENV_MAP.items():
        raw = os.environ.get(env)
        if raw:
            try:
                cfg[sec][key] = type(cfg[sec][key])(raw)
            except (TypeError, ValueError):
                cfg[sec][key] = raw

    cfg["_root"] = str(ROOT_DIR)
    cfg["_cache_dir"] = str((ROOT_DIR / cfg["cache"]["dir"]).resolve())
    ensure_dirs(cfg)
    return cfg


def ensure_dirs(cfg: dict[str, Any]) -> None:
    Path(cfg["_cache_dir"]).mkdir(parents=True, exist_ok=True)
    (Path(cfg["_cache_dir"]) / "http").mkdir(exist_ok=True)

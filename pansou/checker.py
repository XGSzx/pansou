"""分享链接存活校验（尽力而为）。

各网盘分享页多为前端渲染，这里只根据服务端返回的少量提示词判断，
判定为 unknown 时请以实际打开为准。
"""

from __future__ import annotations

import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .net import BlockedError, HttpClient, NetError

CHECK_TARGETS = {
    "baidu": "https://pan.baidu.com/s/{id}",
    "quark": "https://pan.quark.cn/s/{id}",
    "aliyun": "https://www.alipan.com/s/{id}",
    "uc": "https://drive.uc.cn/s/{id}",
    "xunlei": "https://pan.xunlei.com/s/{id}",
    "pan123": "https://www.123pan.com/s/{id}",
    "pan115": "https://115.com/s/{id}",
    "tianyi": "https://cloud.189.cn/t/{id}",
    "weiyun": "https://share.weiyun.com/{id}",
}

DEAD_MARKERS = (
    "你访问的页面不存在",
    "链接不存在",
    "分享的文件已经被取消",
    "分享已失效",
    "分享已经失效",
    "分享不存在",
    "页面不存在",
    "已被删除",
    "文件已删除",
    "来晚了，分享的文件已被删除",
    "该分享已过期",
    "链接已失效",
    "分享已取消",
    "invalid share",
    "share not found",
)

ALIVE_MARKERS = (
    "请输入提取码",
    "提取码",
    "保存到网盘",
    "转存",
    "立即保存",
    "输入访问密码",
    "文件列表",
    "分享的文件",
)


def _build_url(drive: str, share_id: str, code: str) -> str:
    template = CHECK_TARGETS.get(drive)
    if not template:
        return ""
    url = template.format(id=urllib.parse.quote(share_id))
    if code and drive in ("baidu", "quark", "xunlei", "pan123", "pan115", "uc"):
        url += ("&" if "?" in url else "?") + "pwd=" + urllib.parse.quote(code)
    return url


def check_one(client: HttpClient, drive: str, share_id: str, code: str) -> str:
    url = _build_url(drive, share_id, code)
    if not url:
        return "unknown"
    try:
        resp = client.get(url, use_cache=True, check_block=False)
    except BlockedError:
        return "unknown"
    except NetError as exc:
        if "HTTP 404" in str(exc):
            return "dead"
        return "unknown"

    text = resp.text[:200_000]
    low = text.lower()
    if any(m in text for m in DEAD_MARKERS) or any(m in low for m in DEAD_MARKERS):
        return "dead"
    # 有些站点对失效链接返回 404 或极短页面
    if resp.status == 404:
        return "dead"
    if len(text) < 500 and "404" in text:
        return "dead"
    if any(m in text for m in ALIVE_MARKERS):
        return "alive"
    return "unknown"


def check_resources(
    resources: list[Any],
    cfg: dict[str, Any],
    emit: Callable[..., None] | None = None,
) -> None:
    client = HttpClient(cfg)
    workers = max(1, int(cfg.get("check", {}).get("workers", 5)))
    timeout = cfg.get("check", {}).get("timeout")
    if timeout:
        client.timeout = float(timeout)

    total = len(resources)
    done = 0

    def work(res):
        return res, check_one(client, res.drive, res.share_id, res.code)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, r) for r in resources]
        for future in as_completed(futures):
            done += 1
            try:
                res, status = future.result()
                res.status = status
            except Exception:
                continue
            if emit:
                try:
                    emit(type="check", done=done, total=total, status=getattr(res, "status", ""))
                except Exception:
                    pass

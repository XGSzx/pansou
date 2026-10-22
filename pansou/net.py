"""HTTP 客户端：限速、重试、编码识别、磁盘缓存、Cookie 复用。

只依赖标准库，避免用户额外安装依赖。
"""

from __future__ import annotations

import gzip
import hashlib
import http.cookiejar
import json
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

# 反爬/验证码页面特征
BLOCK_MARKERS = (
    "百度安全验证",
    "wappass.baidu.com",
    "请输入验证码",
    "异常页面",
    "访问异常",
    "antispider",
    "unusual traffic",
    "验证一下你是真人",
    "人机验证",
    "cf-challenge",
    "Just a moment",
)


class NetError(Exception):
    """网络请求失败。"""


class BlockedError(NetError):
    """被目标站点反爬拦截。"""

    def __init__(self, host: str, reason: str = "触发反爬验证"):
        super().__init__(f"{host} {reason}")
        self.host = host
        self.reason = reason


@dataclass
class Response:
    url: str
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def is_html(self) -> bool:
        ct = self.content_type.lower()
        return ("html" in ct or "xml" in ct or ct == "") and len(self.text) > 0


class RateLimiter:
    """按域名限速，保证对同一站点足够礼貌。"""

    def __init__(self, min_interval: float, jitter: float = 0.3) -> None:
        self.min_interval = max(0.0, min_interval)
        self.jitter = jitter
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        if self.min_interval <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                target = self._last.get(host, 0.0) + self.min_interval * (
                    1 + random.uniform(0, self.jitter)
                )
                if now >= target:
                    self._last[host] = now
                    return
                delay = target - now
            time.sleep(min(delay, 2.0))


def _meta_charset(raw: bytes) -> str | None:
    head = raw[:4096].decode("ascii", "ignore").lower()
    m = re.search(r'charset=["\']?([a-z0-9_\-]+)', head)
    return m.group(1) if m else None


def decode_body(raw: bytes, content_type: str = "") -> str:
    if raw.startswith(b"\x1f\x8b"):
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    elif raw[:2] in (b"\x78\x9c", b"\x78\x01", b"\x78\xda"):
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass

    charset = ""
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1).lower()
    if not charset:
        charset = _meta_charset(raw) or ""
    for enc in [charset, "utf-8", "gb18030", "big5"]:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class HttpClient:
    def __init__(self, cfg: dict[str, Any]) -> None:
        req = cfg.get("request", {})
        self.timeout = float(req.get("timeout", 15))
        self.retries = int(req.get("retries", 2))
        self.max_bytes = int(req.get("max_page_bytes", 2_500_000))
        self.proxy = (req.get("proxy") or "").strip()
        self.verify_tls = bool(req.get("verify_tls", True))
        self.limiter = RateLimiter(float(req.get("min_interval", 3.0)), float(req.get("jitter", 0.35)))

        cache_cfg = cfg.get("cache", {})
        self.cache_enabled = bool(cache_cfg.get("enabled", True))
        self.cache_ttl = int(cache_cfg.get("ttl", 21600))
        self.cache_dir = Path(cfg["_cache_dir"]) / "http"
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        self.jar = http.cookiejar.LWPCookieJar(str(Path(cfg["_cache_dir"]) / "cookies.txt"))
        try:
            if Path(cfg["_cache_dir"], "cookies.txt").exists():
                self.jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass

        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self.jar)]
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        if not self.verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self._jar_lock = threading.Lock()

    # ------------------------------------------------------------------ 缓存
    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / (key + ".json")

    def _cache_get(self, key: str) -> Response | None:
        if not self.cache_enabled:
            return None
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) > self.cache_ttl:
                return None
            return Response(
                url=data["url"],
                status=data["status"],
                text=data["text"],
                headers=data.get("headers", {}),
                from_cache=True,
            )
        except Exception:
            return None

    def _cache_put(self, key: str, resp: Response) -> None:
        if not self.cache_enabled or resp.status != 200 or len(resp.text) > 900_000:
            return
        try:
            payload = {
                "ts": time.time(),
                "url": resp.url,
                "status": resp.status,
                "text": resp.text,
                "headers": {k: v for k, v in resp.headers.items() if k in ("content-type",)},
            }
            self._cache_path(key).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ 请求
    def _build_request(self, url: str, referer: str = "", headers: dict | None = None) -> urllib.request.Request:
        hdrs = {
            "User-Agent": random.choice(UA_POOL),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        if referer:
            hdrs["Referer"] = referer
        if headers:
            hdrs.update(headers)
        return urllib.request.Request(url, headers=hdrs)

    def get(
        self,
        url: str,
        *,
        referer: str = "",
        headers: dict | None = None,
        use_cache: bool = True,
        max_bytes: int | None = None,
        check_block: bool = True,
    ) -> Response:
        host = urllib.parse.urlsplit(url).netloc
        key = hashlib.sha1(
            (url + "|" + (referer or "") + "|" + json.dumps(headers or {}, sort_keys=True)).encode()
        ).hexdigest()
        if use_cache:
            cached = self._cache_get(key)
            if cached is not None:
                return cached

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self.limiter.wait(host)
            try:
                request = self._build_request(url, referer, headers)
                with self.opener.open(request, timeout=self.timeout) as raw:
                    body = raw.read(max_bytes or self.max_bytes)
                    final_url = raw.geturl()
                    status = getattr(raw, "status", 200) or 200
                    hdrs = {k.lower(): v for k, v in raw.headers.items()}
                text = decode_body(body, hdrs.get("content-type", ""))
                resp = Response(url=final_url, status=status, text=text, headers=hdrs)
                if check_block and self._looks_blocked(text):
                    raise BlockedError(host)
                if use_cache:
                    self._cache_put(key, resp)
                return resp
            except BlockedError as exc:
                last_exc = exc
                break  # 被拦了，重试也没意义
            except urllib.error.HTTPError as exc:
                last_exc = NetError(f"HTTP {exc.code} @ {url}")
                if exc.code in (403, 429):
                    break
            except Exception as exc:  # 超时、DNS、连接重置等
                last_exc = NetError(f"{type(exc).__name__}: {exc} @ {url}")
            if attempt < self.retries:
                time.sleep(1.2 * (attempt + 1) + random.random())
        raise last_exc if isinstance(last_exc, NetError) else NetError(str(last_exc))

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """调用 JSON API（Serper / 博查等）。"""
        host = urllib.parse.urlsplit(url).netloc
        self.limiter.wait(host)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs = {
            "User-Agent": random.choice(UA_POOL),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        hdrs.update(headers or {})
        request = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as raw:
                raw_body = raw.read(self.max_bytes)
                ctype = raw.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise NetError(f"API {exc.code} @ {host}: {detail}") from exc
        except Exception as exc:
            raise NetError(f"{type(exc).__name__}: {exc} @ {host}") from exc
        text = decode_body(raw_body, ctype)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise NetError(f"API 返回不是 JSON @ {host}: {text[:120]}") from exc

    def resolve(self, url: str, referer: str = "") -> str:
        """只跟跳转、不读正文，得到真实地址（用于百度/360 的跳转链接）。"""
        host = urllib.parse.urlsplit(url).netloc
        self.limiter.wait(host)
        request = self._build_request(url, referer)
        try:
            with self.opener.open(request, timeout=self.timeout) as raw:
                return raw.geturl()
        except urllib.error.HTTPError as exc:
            return exc.geturl() or url
        except Exception:
            return url

    def save_cookies(self) -> None:
        with self._jar_lock:
            try:
                self.jar.save(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass

    def has_cookies(self) -> bool:
        try:
            return len(list(self.jar)) > 0
        except Exception:
            return False

    @staticmethod
    def _looks_blocked(text: str) -> bool:
        if len(text) > 6000:
            return False
        sample = text[:6000]
        return any(marker in sample for marker in BLOCK_MARKERS)


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()

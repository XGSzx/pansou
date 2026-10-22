"""搜索引擎适配器：百度 / 360 / 必应 / 搜狗 / Serper / 博查。

每个引擎返回统一的 Hit 列表；无需任何密钥即可使用前三个。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .net import BlockedError, HttpClient, NetError, host_of


@dataclass
class Hit:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    domain: str = ""
    date: str = ""

    def __post_init__(self) -> None:
        if not self.domain:
            self.domain = host_of(self.url)

    @property
    def is_redirector(self) -> bool:
        host = self.domain
        path = urllib.parse.urlsplit(self.url).path
        return any(
            (host.endswith(d) and path.startswith(p))
            for d, p in (
                ("baidu.com", "/link"),
                ("so.com", "/link"),
                ("sogou.com", "/link"),
                ("bing.com", "/ck"),
            )
        )


def _text(fragment: str) -> str:
    import html as html_lib

    fragment = re.sub(r"<script.*?</script>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return re.sub(r"\s+", " ", html_lib.unescape(fragment)).strip()


class Engine:
    key = ""
    label = ""
    supports_site = True

    def search(self, client: HttpClient, query: str, page: int = 1) -> list[Hit]:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- 百度


class BaiduEngine(Engine):
    key = "baidu"
    label = "百度"
    supports_site = True

    def _warmup(self, client: HttpClient) -> None:
        try:
            client.get("https://www.baidu.com/", max_bytes=200_000)
        except NetError:
            pass

    def search(self, client: HttpClient, query: str, page: int = 1) -> list[Hit]:
        self._warmup(client)
        url = (
            "https://www.baidu.com/s?wd="
            + urllib.parse.quote(query)
            + f"&pn={max(0, (page - 1)) * 10}&rn=10&ie=utf-8&tn=baiduhome_pg"
        )
        resp = client.get(url, referer="https://www.baidu.com/")
        html = resp.text
        if "百度安全验证" in html or "wappass.baidu.com" in html:
            raise BlockedError("baidu", "触发百度安全验证，请稍后再试或配置代理")
        if "抱歉，没有找到" in html or "没有找到与" in html:
            return []

        hits: list[Hit] = []
        blocks = re.split(r'<div[^>]+class="[^"]*\bresult\b[^"]*"', html)
        for block in blocks[1:]:
            block = block.split('<div class="result')[0][:6000]
            m = re.search(r"<h3[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.S)
            if not m:
                continue
            href = m.group(1).replace("&amp;", "&")
            # 百度在结果节点上直接给出真实地址，省掉一次跳转解析
            mu = re.search(r'\bmu="(https?://[^"]+)"', block)
            if mu:
                href = mu.group(1).replace("&amp;", "&")
            title = _text(m.group(2))
            if not title:
                continue
            snippet = ""
            sm = re.search(r'<div[^>]+class="[^"]*(?:c-abstract|content-right)[^"]*"[^>]*>(.*?)</div>', block, re.S)
            if sm:
                snippet = _text(sm.group(1))
            else:
                snippet = _text(block)[:180]
            hits.append(Hit(title=title, url=href, snippet=snippet, engine=self.key))
        return hits


# --------------------------------------------------------------------------- 360


class So360Engine(Engine):
    key = "so360"
    label = "360搜索"
    supports_site = True

    def search(self, client: HttpClient, query: str, page: int = 1) -> list[Hit]:
        url = "https://www.so.com/s?q=" + urllib.parse.quote(query) + f"&pn={page}&ie=utf-8"
        resp = client.get(url, referer="https://www.so.com/")
        html = resp.text
        if "访问异常" in html[:4000] or "验证" in html[:2000] and len(html) < 20000:
            raise BlockedError("so.com", "触发 360 反爬验证，请降低频率或稍后再试")
        if 'class="res-list' not in html:
            return []

        hits: list[Hit] = []
        for block in re.split(r'<li class="res-list', html)[1:]:
            block = block[:8000]
            m = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
            if not m:
                continue
            head = m.group(1)
            am = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', head, re.S)
            if not am:
                continue
            href = am.group(1).replace("&amp;", "&")
            md = re.search(r'data-mdurl="([^"]+)"', head) or re.search(r'data-mdurl="([^"]+)"', block)
            if md and md.group(1).startswith("http"):
                href = md.group(1).replace("&amp;", "&")
            title = _text(am.group(2))
            if not title:
                continue
            sm = re.search(r'<p[^>]*class="[^"]*res-desc[^"]*"[^>]*>(.*?)</p>', block, re.S)
            snippet = _text(sm.group(1)) if sm else _text(block)[:180]
            cite = re.search(r"<cite[^>]*>(.*?)</cite>", block, re.S)
            domain = _text(cite.group(1)) if cite else ""
            hits.append(Hit(title=title, url=href, snippet=snippet, engine=self.key, domain=domain))
        return hits


# --------------------------------------------------------------------------- 必应


class BingEngine(Engine):
    key = "bing"
    label = "必应"
    supports_site = True

    def search(self, client: HttpClient, query: str, page: int = 1) -> list[Hit]:
        first = 1 + (page - 1) * 10
        url = (
            "https://cn.bing.com/search?q="
            + urllib.parse.quote(query)
            + f"&format=rss&first={first}&mkt=zh-CN"
        )
        resp = client.get(url)
        xml = resp.text
        if "<item>" not in xml:
            return []
        import html as html_lib

        hits: list[Hit] = []
        for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
            tm = re.search(r"<title>(.*?)</title>", item, re.S)
            lm = re.search(r"<link>(.*?)</link>", item, re.S)
            dm = re.search(r"<description>(.*?)</description>", item, re.S)
            pm = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
            if not (tm and lm):
                continue
            hits.append(
                Hit(
                    title=html_lib.unescape(_text(tm.group(1))),
                    url=html_lib.unescape(lm.group(1).strip()),
                    snippet=_text(dm.group(1))[:200] if dm else "",
                    engine=self.key,
                    date=pm.group(1) if pm else "",
                )
            )
        return hits


# --------------------------------------------------------------------------- 搜狗


class SogouEngine(Engine):
    key = "sogou"
    label = "搜狗"
    supports_site = True

    def search(self, client: HttpClient, query: str, page: int = 1) -> list[Hit]:
        url = (
            "https://www.sogou.com/web?query="
            + urllib.parse.quote(query)
            + f"&page={page}"
        )
        resp = client.get(url, referer="https://www.sogou.com/")
        html = resp.text
        if "antispider" in html or "请输入验证码" in html:
            raise BlockedError("sogou.com", "触发搜狗反爬，建议在配置中关闭该引擎")
        hits: list[Hit] = []
        for block in re.split(r'<h3 class="vr-title"', html)[1:]:
            block = block[:5000]
            m = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not m:
                continue
            href = m.group(1).replace("&amp;", "&")
            if href.startswith("/"):
                href = "https://www.sogou.com" + href
            hits.append(Hit(title=_text(m.group(2)), url=href, engine=self.key))
        return hits


# --------------------------------------------------------------------------- 可选 API


class SerperEngine(Engine):
    """Google 结果（serper.dev），需要 API Key。"""

    key = "serper"
    label = "Google(Serper)"
    supports_site = True

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(self, client: HttpClient, query: str, page: int = 1) -> list[Hit]:
        payload = {"q": query, "num": 20, "gl": "cn", "hl": "zh-cn", "page": page}
        data = client.post_json(
            "https://google.serper.dev/search",
            payload,
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
        )
        hits = []
        for item in data.get("organic", []) or []:
            hits.append(
                Hit(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    engine=self.key,
                    date=item.get("date", ""),
                )
            )
        return hits


class BochaEngine(Engine):
    """博查搜索 API（国内可直连），需要 API Key。"""

    key = "bocha"
    label = "博查"
    supports_site = True

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(self, client: HttpClient, query: str, page: int = 1) -> list[Hit]:
        payload = {"query": query, "count": 20, "summary": False, "page": page}
        data = client.post_json(
            "https://api.bochaai.com/v1/web-search",
            payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        hits = []
        pages = (((data or {}).get("data") or {}).get("webPages") or {}).get("value") or []
        for item in pages:
            hits.append(
                Hit(
                    title=item.get("name", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                    engine=self.key,
                    date=item.get("datePublished", ""),
                )
            )
        return hits


def build_engines(cfg: dict[str, Any]) -> list[Engine]:
    enabled = cfg.get("engines", {})
    api = cfg.get("api", {})
    engines: list[Engine] = []
    if enabled.get("baidu", True):
        engines.append(BaiduEngine())
    if enabled.get("so360", True):
        engines.append(So360Engine())
    if enabled.get("bing", True):
        engines.append(BingEngine())
    if enabled.get("sogou", False):
        engines.append(SogouEngine())
    if enabled.get("serper", True) and (api.get("serper_key") or "").strip():
        engines.append(SerperEngine(api["serper_key"].strip()))
    if enabled.get("bocha", True) and (api.get("bocha_key") or "").strip():
        engines.append(BochaEngine(api["bocha_key"].strip()))
    return engines

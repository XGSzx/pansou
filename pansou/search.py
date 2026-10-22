"""搜索编排：查询规划 → 多引擎检索 → 网页深挖 → 抽取去重 → 评分排序。"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from .engines import Engine, Hit, build_engines
from .extract import (
    DRIVE_MAP,
    DRIVE_ORDER,
    extract_from_html,
    extract_links,
    has_resource_words,
    looks_dead,
    normalize_url,
    score_link,
    share_id_of,
)
from .net import BlockedError, HttpClient, NetError, host_of

ProgressFn = Callable[[dict[str, Any]], None]

# 这些域名本身就是网盘分享页，不需要再抓正文
PAN_HOSTS = (
    "pan.baidu.com",
    "pan.quark.cn",
    "alipan.com",
    "aliyundrive.com",
    "pan.xunlei.com",
    "cloud.189.cn",
    "115.com",
    "123pan.com",
    "pan.uc.cn",
    "drive.uc.cn",
    "share.weiyun.com",
)

SKIP_HOSTS = (
    "baike.baidu.com",
    "zhidao.baidu.com",
    "wikipedia.org",
    "wikiwand.com",
    "douban.com",
    "bilibili.com",
    "iqiyi.com",
    "v.qq.com",
    "youku.com",
    "mgtv.com",
    "wenda.so.com",
    "www.baidu.com",
    "www.so.com",
    "www.sogou.com",
    "cn.bing.com",
)


def host_matches(host: str, candidates: tuple[str, ...]) -> bool:
    """域名匹配：精确或子域，避免 baidu.com 误伤 tieba.baidu.com。"""
    host = (host or "").lower()
    return any(host == c or host.endswith("." + c) for c in candidates)


def looks_like_login_wall(html: str) -> bool:
    """很多站点（贴吧、微博）不登录只返回一个壳页面，识别后整站跳过。"""
    if len(html) > 60000:
        return False
    markers = ("请登录", "请先登录", "登录后可见", "扫码登录", "安全验证", "人机验证", "访问验证", "滑动验证")
    hits = sum(html.count(m) for m in markers)
    if hits >= 2:
        return True
    return "百度贴吧" in html and "登录" in html and len(html) < 20000


@dataclass
class Resource:
    drive: str
    url: str
    share_id: str
    code: str
    title: str
    page_url: str
    domain: str
    engine: str
    snippet: str = ""
    note: str = ""
    score: float = 0.0
    status: str = "unchecked"

    @property
    def label(self) -> str:
        return DRIVE_MAP[self.drive].label if self.drive in DRIVE_MAP else self.drive

    @property
    def color(self) -> str:
        return DRIVE_MAP[self.drive].color if self.drive in DRIVE_MAP else "#888"

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive": self.drive,
            "drive_label": self.label,
            "color": self.color,
            "url": self.url,
            "code": self.code,
            "title": self.title,
            "page_url": self.page_url,
            "domain": self.domain,
            "engine": self.engine,
            "snippet": self.snippet[:300],
            "note": self.note,
            "needs_code": DRIVE_MAP[self.drive].needs_code if self.drive in DRIVE_MAP else True,
            "score": self.score,
            "status": self.status,
        }


def build_queries(title: str, include_site: bool = True) -> list[str]:
    """基础查询与 site: 精准查询交替排列，保证默认预算内也能命中站内分享页。"""
    t = title.strip()
    if not include_site:
        return [
            f"{t} 网盘 提取码",
            f"{t} 百度网盘",
            f"{t} 夸克网盘",
            f"{t} 4K 高清 下载",
            f"{t} 资源 全集",
        ]
    return [
        f"{t} 网盘 提取码",
        f"site:pan.quark.cn {t}",
        f"{t} 百度网盘",
        f"site:pan.baidu.com {t}",
        f"{t} 夸克网盘",
        f"site:alipan.com {t}",
        f"{t} 4K 高清 下载",
        f"site:pan.xunlei.com {t}",
        f"{t} 资源 全集",
        f"site:cloud.189.cn {t}",
    ]


def _tokens(query: str) -> list[str]:
    raw = re.split(r"[\s,，。、《》()（）\[\]【】:：/|]+", query.strip())
    out = [t for t in raw if len(t) >= 2]
    if query.strip() and query.strip() not in out:
        out.append(query.strip())
    return out


def is_relevant(hit: Hit, tokens: list[str]) -> bool:
    hay = f"{hit.title} {hit.snippet}".lower()
    if any(t.lower() in hay for t in tokens):
        return True
    if host_matches(hit.domain, PAN_HOSTS):
        return True
    return False


class Searcher:
    def __init__(self, cfg: dict[str, Any], progress: ProgressFn | None = None) -> None:
        self.cfg = cfg
        self.progress = progress or (lambda event: None)
        self.client = HttpClient(cfg)
        self.engines: list[Engine] = build_engines(cfg)
        self._lock = threading.Lock()
        self._hits: list[Hit] = []
        self._resources: dict[tuple[str, str], Resource] = {}
        self._query = ""
        self._bad_hosts: set[str] = set()
        self.stats: dict[str, Any] = {
            "queries": 0,
            "hits": 0,
            "crawled": 0,
            "crawl_failed": 0,
            "blocked": 0,
            "elapsed": 0.0,
        }

    # ------------------------------------------------------------------ 事件
    def _emit(self, **event: Any) -> None:
        try:
            self.progress(event)
        except Exception:
            pass

    # ------------------------------------------------------------------ 检索
    def _run_engine(self, engine: Engine, queries: list[str], pages: int) -> tuple[int, str | None]:
        count = 0
        for query in queries:
            for page in range(1, pages + 1):
                try:
                    hits = engine.search(self.client, query, page)
                except BlockedError as exc:
                    with self._lock:
                        self.stats["blocked"] += 1
                    self._emit(type="log", level="warn", msg=f"{engine.label}：{exc.reason}")
                    return count, exc.reason
                except NetError as exc:
                    self._emit(type="log", level="warn", msg=f"{engine.label} 请求失败：{exc}")
                    break
                with self._lock:
                    self._hits.extend(hits)
                    self.stats["queries"] += 1
                    self.stats["hits"] += len(hits)
                count += len(hits)
                if not hits:
                    break
                self._emit(type="log", level="info", msg=f"{engine.label} 命中 {len(hits)} 条：{query}")
        return count, None

    def _collect_hits(self, queries: list[str]) -> None:
        if not self.engines:
            self._emit(type="log", level="error", msg="没有启用任何搜索引擎，请检查 config.json")
            return
        pages = int(self.cfg["search"].get("pages_per_query", 1))
        max_per_engine = int(self.cfg["search"].get("max_queries", 5))
        plan = queries[:max_per_engine]
        self._emit(type="stage", stage="search", msg=f"正在 {len(self.engines)} 个搜索引擎检索…")

        with ThreadPoolExecutor(max_workers=min(3, len(self.engines))) as pool:
            futures = {pool.submit(self._run_engine, e, plan, pages): e for e in self.engines}
            for future in as_completed(futures):
                engine = futures[future]
                try:
                    count, reason = future.result()
                except Exception as exc:  # 单个引擎异常不影响整体
                    self._emit(type="log", level="warn", msg=f"{engine.label} 异常：{exc}")
                    continue
                status = "blocked" if reason else ("ok" if count else "empty")
                self._emit(type="engine", engine=engine.key, label=engine.label, status=status, count=count)

    # ------------------------------------------------------------------ 深挖
    def _pick_candidates(self, tokens: list[str]) -> list[Hit]:
        seen: set[str] = set()
        unique: list[Hit] = []
        for hit in self._hits:
            key = hit.url.split("#")[0]
            if key in seen or not hit.url.startswith("http"):
                continue
            seen.add(key)
            if not is_relevant(hit, tokens):
                continue
            unique.append(hit)

        def priority(hit: Hit) -> tuple[int, int]:
            hay = f"{hit.title} {hit.snippet}"
            score = 0
            if host_matches(hit.domain, PAN_HOSTS):
                score += 10
            if has_resource_words(hay):
                score += 4
            if re.search(r"提取码|密码|pwd", hay, re.I):
                score += 3
            if host_matches(hit.domain, SKIP_HOSTS):
                score -= 6
            return (-score, len(hit.title))

        unique.sort(key=priority)
        limit = int(self.cfg["search"].get("crawl_pages", 12))

        # 按域名分桶轮转，优先保证来源多样，避免同一站点刷屏
        buckets: dict[str, list[Hit]] = {}
        for hit in unique:
            buckets.setdefault(hit.domain, []).append(hit)
        order = sorted(buckets, key=lambda host: priority(buckets[host][0]))
        picked: list[Hit] = []
        while len(picked) < limit:
            progressed = False
            for host in order:
                if not buckets[host]:
                    continue
                picked.append(buckets[host].pop(0))
                progressed = True
                if len(picked) >= limit:
                    break
            if not progressed:
                break
        return picked

    def _absorb(self, link, page_title: str, page_url: str, engine: str, snippet: str = "") -> None:
        key = (link.drive, link.share_id)
        score = score_link(link, self._query, page_title, snippet)
        with self._lock:
            existing = self._resources.get(key)
            if existing:
                if not existing.code and link.code:
                    existing.code = link.code
                if not existing.note and link.note:
                    existing.note = link.note
                if score > existing.score:
                    existing.score = score
                return
            self._resources[key] = Resource(
                drive=link.drive,
                url=link.url,
                share_id=link.share_id,
                code=link.code,
                title=page_title[:160] or link.page_title,
                page_url=page_url,
                domain=host_of(page_url),
                engine=engine,
                snippet=snippet,
                note=link.note,
                score=score,
            )

    def _crawl_one(self, hit: Hit) -> list:
        url = hit.url
        path = urllib.parse.urlsplit(url).path
        # 网盘首页没有分享内容，跳过
        if host_matches(hit.domain, PAN_HOSTS) and path in ("", "/", "/articles/"):
            return []
        if hit.domain in self._bad_hosts:
            return []
        if hit.is_redirector:
            url = self.client.resolve(url)
        resp = self.client.get(url, referer=hit.domain if hit.is_redirector else "")
        if not resp.is_html():
            return []
        links = extract_from_html(resp.text, resp.url, hit.title)
        if not links and looks_like_login_wall(resp.text):
            with self._lock:
                self._bad_hosts.add(hit.domain)
            self._emit(type="log", level="debug", msg=f"{hit.domain} 需要登录/验证，已跳过该站其余页面")
        return links

    def _crawl(self, candidates: list[Hit], tokens: list[str]) -> None:
        total = len(candidates)
        if not total:
            return
        self._emit(type="stage", stage="crawl", msg=f"深挖 {total} 个候选网页…")
        workers = max(1, int(self.cfg["search"].get("workers", 4)))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._crawl_one, hit): hit for hit in candidates}
            for future in as_completed(futures):
                hit = futures[future]
                done += 1
                try:
                    links = future.result()
                except BlockedError as exc:
                    with self._lock:
                        self.stats["blocked"] += 1
                    self._emit(type="log", level="warn", msg=f"{host_of(hit.url)}：{exc.reason}")
                    links = []
                except Exception as exc:
                    with self._lock:
                        self.stats["crawl_failed"] += 1
                    self._emit(type="log", level="debug", msg=f"抓取失败 {hit.domain}：{exc}")
                    links = []
                before = len(self._resources)
                for link in links:
                    self._absorb(link, hit.title, link.page_url or hit.url, hit.engine, hit.snippet)
                found = len(self._resources) - before
                if found:
                    self._emit(type="found", count=len(self._resources), delta=found)
                with self._lock:
                    self.stats["crawled"] += 1
                self._emit(type="crawl", done=done, total=total, found=len(self._resources))

    # ------------------------------------------------------------------ 主流程
    def _diversify(self, resources: list[Resource]) -> list[Resource]:
        """同一来源页最多保留 N 条，避免单个聚合站刷屏。"""
        per_page = int(self.cfg["output"].get("max_per_page", 15))
        if per_page <= 0:
            return resources
        kept: list[Resource] = []
        counter: dict[str, int] = {}
        for res in resources:
            key = res.page_url or res.domain
            if counter.get(key, 0) >= per_page:
                continue
            counter[key] = counter.get(key, 0) + 1
            kept.append(res)
        return kept

    def run(
        self,
        query: str,
        *,
        check: bool = False,
        max_results: int | None = None,
        crawl_pages: int | None = None,
    ) -> dict[str, Any]:
        self._query = query
        started = time.time()
        tokens = _tokens(query)
        if crawl_pages:
            self.cfg["search"]["crawl_pages"] = crawl_pages
        queries = build_queries(query, bool(self.cfg["search"].get("include_site_queries", True)))

        self._collect_hits(queries)

        # 搜索结果里本身就带网盘链接的，直接吸收（省一次请求）
        for hit in list(self._hits):
            if host_matches(hit.domain, PAN_HOSTS):
                for link in extract_links(hit.url + " " + hit.snippet, hit.url, hit.title):
                    self._absorb(link, hit.title, hit.url, hit.engine, hit.snippet)

        candidates = self._pick_candidates(tokens)
        self._crawl(candidates, tokens)

        resources = sorted(
            self._resources.values(),
            key=lambda r: (-r.score, DRIVE_ORDER.get(r.drive, 99), r.title),
        )
        min_score = float(self.cfg["output"].get("min_score", 0.0))
        resources = [r for r in resources if r.score >= min_score]
        resources = self._diversify(resources)
        limit = max_results or int(self.cfg["output"].get("max_results", 60))
        resources = resources[:limit]

        if check and resources:
            from .checker import check_resources

            self._emit(type="stage", stage="check", msg=f"校验 {len(resources)} 条链接是否有效…")
            check_resources(resources, self.cfg, self._emit)

        for res in resources:
            self._emit(type="result", item=res.to_dict())

        self.client.save_cookies()
        self.stats["elapsed"] = round(time.time() - started, 1)
        self.stats["results"] = len(resources)
        payload = {
            "query": query,
            "results": [r.to_dict() for r in resources],
            "stats": self.stats,
        }
        self._emit(type="done", **payload)
        return payload

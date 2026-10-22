"""从网页文本中抽取网盘分享链接、提取码，并做归一化与评分。"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, urlsplit, urlunsplit

# --------------------------------------------------------------------------- 网盘类型


@dataclass(frozen=True)
class Drive:
    key: str
    label: str
    color: str
    pattern: re.Pattern[str]
    needs_code: bool = True   # 该网盘是否需要提取码（阿里/夸克不需要）


def _p(rx: str) -> re.Pattern[str]:
    return re.compile(rx, re.I)


DRIVES: tuple[Drive, ...] = (
    Drive("baidu", "百度网盘", "#4a6cf7",
          _p(r"https?://pan\.baidu\.com/(?:s/[A-Za-z0-9_\-]{4,}|share/init\?surl=[A-Za-z0-9_\-]+|share/link\?[^\s\"'<>]+)")),
    Drive("quark", "夸克网盘", "#8b5cf6",
          _p(r"https?://pan\.quark\.cn/(?:s|list)/[A-Za-z0-9_\-]{4,}"), needs_code=False),
    Drive("aliyun", "阿里云盘", "#ff7a18",
          _p(r"https?://(?:www\.)?(?:alipan|aliyundrive)\.com/s/[A-Za-z0-9_\-]{4,}"), needs_code=False),
    Drive("uc", "UC网盘", "#16a34a",
          _p(r"https?://(?:drive|pan)\.uc\.cn/s/[A-Za-z0-9_\-]{4,}"), needs_code=False),
    Drive("xunlei", "迅雷网盘", "#06b6d4",
          _p(r"https?://pan\.xunlei\.com/s/[A-Za-z0-9_\-]{4,}")),
    Drive("pan123", "123网盘", "#22c55e",
          _p(r"https?://(?:www\.)?123pan\.com/s/[A-Za-z0-9_\-]{4,}")),
    Drive("pan115", "115网盘", "#0ea5e9",
          _p(r"https?://(?:www\.)?115\.com/s/[A-Za-z0-9_\-]{4,}")),
    Drive("tianyi", "天翼云盘", "#ef4444",
          _p(r"https?://cloud\.189\.cn/(?:t/[A-Za-z0-9_\-]{4,}|web/share\?[^\s\"'<>]+)")),
    Drive("weiyun", "微云", "#14b8a6",
          _p(r"https?://share\.weiyun\.com/[A-Za-z0-9_\-]{4,}"), needs_code=False),
)

DRIVE_MAP = {d.key: d for d in DRIVES}
DRIVE_ORDER = {d.key: i for i, d in enumerate(DRIVES)}

TRAILING_JUNK = " \t\r\n，。、；;）)】」』》,.;:!！?？\"'<>”’"
# URL 中不可能出现的字符：遇到即认为链接结束（问号/冒号/斜杠/点号要保留，否则会截断查询串）
URL_TERMINATORS = set(" \t\r\n\"'<>`，。、；）】」』》,!！“”‘’")

CODE_PATTERNS = [
    re.compile(r"(?:提取码|提取碼|访问码|訪問碼|访问密码|分享密码|密码|密碼)\s*[:：=]?\s*([A-Za-z0-9]{4,8})"),
    re.compile(r"(?:pwd|password|passcode)\s*[:：=]\s*([A-Za-z0-9]{4,8})", re.I),
    re.compile(r"[（(\[【]\s*(?:提取码|密码|pwd)?\s*[:：]?\s*([A-Za-z0-9]{4})\s*[）)\]】]"),
]
URL_CODE_KEYS = ("pwd", "password", "passcode", "code", "accesscode")
KEEP_QUERY_KEYS = ("surl", "shareid", "uk", "fid", "public")

# JSON / JS 对象里常见的成对写法：{url:"...",password:"..."}
PAIR_PATTERNS = (
    re.compile(r"""url\s*[:=]\s*["']?(https?://[^"'\s,}]+)["']?\s*,\s*password\s*[:=]\s*["']([^"']*)["']""", re.I),
    re.compile(r"""["']url["']\s*:\s*["'](https?://[^"']+)["']\s*,\s*["'](?:password|pwd|code)["']\s*:\s*["']([^"']*)["']""", re.I),
    re.compile(r"""(?:link|share_url|shareUrl)\s*[:=]\s*["'](https?://[^"']+)["'][^}]{0,80}?(?:pwd|password|code)\s*[:=]\s*["']([^"']*)["']""", re.I),
)

# 资源站常见的备注字段：note:"[电影] 片名 (2023) 4K ..."
NOTE_PATTERN = re.compile(r"""url\s*[:=]\s*["'](https?://[^"']+)["'][^}]{0,160}?note\s*[:=]\s*["']([^"']*)["']""", re.I)

DEAD_HINTS = ("失效", "已取消", "不存在", "已被删除", "已删除", "链接过期", "分享过期", "无法访问", "页面已删除")
PLACEHOLDER_IDS = {"xxxx", "xxxxx", "xxxxxxxx", "1234", "12345", "123456", "abcdef", "8888", "0000"}

QUALITY_TAGS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"2160p|4k|uhd", re.I), 2.0),
    (re.compile(r"1080p|fhd|蓝光|blu-?ray|remux", re.I), 1.2),
    (re.compile(r"hdr|杜比|dolby|atmos", re.I), 0.7),
    (re.compile(r"中字|国语|国配|双语|粤语|中英"), 0.9),
    (re.compile(r"全集|完结|合集|全季|更新至"), 0.9),
    (re.compile(r"高清|超清|原盘|无损|无删减|导演剪辑"), 0.5),
)

RESOURCE_WORDS = ("网盘", "资源", "下载", "提取码", "分享", "链接", "高清", "1080", "4k", "全集", "磁力")


@dataclass
class FoundLink:
    drive: str
    url: str
    share_id: str
    code: str = ""
    note: str = ""
    context: str = ""
    page_url: str = ""
    page_title: str = ""


# --------------------------------------------------------------------------- 归一化


def clean_url(url: str) -> str:
    url = html_lib.unescape(url).replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    url = url.strip().strip("\"'")
    for i, ch in enumerate(url):
        if ch in URL_TERMINATORS:
            url = url[:i]
            break
    return url.rstrip(TRAILING_JUNK)


def normalize_url(url: str) -> str:
    """统一域名写法、去掉推广参数，但保留承载分享 id / 提取码的参数。"""
    url = clean_url(url)
    parts = urlsplit(url)
    host = parts.netloc.lower().replace("www.", "")
    if host in ("aliyundrive.com", "alipan.com"):
        host = "alipan.com"
    scheme = "https"
    query = ""
    if parts.query:
        keep = []
        for pair in parts.query.split("&"):
            key = pair.split("=", 1)[0].lower()
            if key in URL_CODE_KEYS or key in KEEP_QUERY_KEYS:
                keep.append(pair)
        query = "&".join(keep)
    path = parts.path
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((scheme, host, path, query, ""))


def share_id_of(url: str, drive: str) -> str:
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    if drive == "baidu" and "surl" in qs:
        return qs["surl"][0]
    if drive == "tianyi" and "code" in qs:
        return qs["code"][0]
    m = re.search(r"/(?:s|t|list)/([A-Za-z0-9_\-]+)", parts.path)
    if m:
        return m.group(1)
    return parts.path.strip("/").replace("/", "_")


def code_from_url(url: str) -> str:
    qs = parse_qs(urlsplit(url).query)
    for key in URL_CODE_KEYS:
        if key in qs and qs[key]:
            value = qs[key][0]
            if 3 <= len(value) <= 8 and not is_year_like(value):
                return value
    return ""


def is_year_like(code: str) -> bool:
    """2023 / 2024 这类年份几乎都是误抓。"""
    return bool(re.fullmatch(r"(?:19|20)\d{2}", code))


# --------------------------------------------------------------------------- 提取码


def _sanitize_code(code: str, trust_keyword: bool = False) -> str:
    code = (code or "").strip()
    if not code or len(code) < 3 or len(code) > 8:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9]+", code):
        return ""
    if is_year_like(code):
        return ""
    # 纯数字很容易是年份、集数、楼层号，除非紧跟在「提取码」后面
    if code.isdigit() and not trust_keyword:
        return ""
    return code


def _code_near(text: str, start: int, end: int) -> str:
    """只在链接附近很窄的范围内找提取码，宁缺毋滥。"""
    after = text[end: end + 90]
    before = text[max(0, start - 45): start]

    def scan(window: str) -> str:
        window = re.sub(r"&[a-z#0-9]+;", " ", window)
        best = ""
        for pattern in CODE_PATTERNS:
            for m in pattern.finditer(window):
                candidate = m.group(1)
                tail = window[m.end(): m.end() + 3]
                if tail and tail[0] in "年月日集话分秒期":
                    continue
                cleaned = _sanitize_code(candidate, trust_keyword=True)
                if cleaned:
                    return cleaned
        return best

    return scan(after) or scan(before)


def _pair_codes(text: str) -> dict[tuple[str, str], str]:
    """解析 JSON 风格的 {url, password} 成对字段，准确率最高。"""
    pairs: dict[tuple[str, str], str] = {}
    for pattern in PAIR_PATTERNS:
        for m in pattern.finditer(text):
            url = normalize_url(m.group(1))
            code = _sanitize_code(m.group(2), trust_keyword=True)
            if not code:
                continue
            for drive in DRIVES:
                if drive.pattern.match(url):
                    pairs[(drive.key, share_id_of(url, drive.key))] = code
                    break
    return pairs


def _clean_note(text: str) -> str:
    text = html_lib.unescape(text or "")
    text = re.sub(r"https?://\S+", " ", text)
    text = text.replace("\\n", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip(" []【】|-_")
    return text[:110]


def _pair_notes(text: str) -> dict[tuple[str, str], str]:
    notes: dict[tuple[str, str], str] = {}
    for m in NOTE_PATTERN.finditer(text):
        url = normalize_url(m.group(1))
        note = _clean_note(m.group(2))
        if not note:
            continue
        for drive in DRIVES:
            if drive.pattern.match(url):
                notes.setdefault((drive.key, share_id_of(url, drive.key)), note)
                break
    return notes


# --------------------------------------------------------------------------- 抽取


def iter_matches(text: str, drive: Drive) -> Iterable[re.Match[str]]:
    return drive.pattern.finditer(text)


_QUERY_TAIL = re.compile(r"^\?[A-Za-z0-9_=&%\-\.]{2,100}")


def _extended_url(text: str, match: re.Match[str]) -> str:
    """链接正则通常不含查询串，这里把紧跟在后面的 ?pwd=xxx 之类补回来。"""
    raw = match.group(0)
    tail = text[match.end(): match.end() + 100]
    qm = _QUERY_TAIL.match(tail)
    if qm and "=" in qm.group(0):
        raw += qm.group(0)
    return raw


def extract_links(text: str, page_url: str = "", page_title: str = "") -> list[FoundLink]:
    """从一段（纯文本或 HTML）内容里抽取所有网盘链接。"""
    results: list[FoundLink] = []
    seen: set[tuple[str, str]] = set()
    pair_codes = _pair_codes(text)
    pair_notes = _pair_notes(text)

    for drive in DRIVES:
        for m in iter_matches(text, drive):
            url = normalize_url(_extended_url(text, m))
            sid = share_id_of(url, drive.key)
            if not sid or sid.lower() in PLACEHOLDER_IDS or len(sid) < 4:
                continue
            key = (drive.key, sid)
            if key in seen:
                continue
            seen.add(key)

            code = code_from_url(url)
            if drive.needs_code and not code:
                code = pair_codes.get(key, "") or _code_near(text, m.start(), m.end())

            context = re.sub(r"\s+", " ", text[max(0, m.start() - 160): m.end() + 240]).strip()
            results.append(
                FoundLink(
                    drive=drive.key,
                    url=url,
                    share_id=sid,
                    code=code,
                    note=pair_notes.get(key, ""),
                    context=context,
                    page_url=page_url,
                    page_title=page_title,
                )
            )
    return results


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_ANY_TAG = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.I)
    text = _ANY_TAG.sub(" ", text)
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"[ \t]{2,}", " ", text)


def extract_from_html(html: str, page_url: str, page_title: str = "") -> list[FoundLink]:
    """HTML 页面：正文文本 + 原始源码（href / JSON）双通道抽取。"""
    text = html_to_text(html)
    found = extract_links(text, page_url, page_title)
    seen = {(f.drive, f.share_id) for f in found}
    pair_codes = _pair_codes(html)
    pair_notes = _pair_notes(html)

    for drive in DRIVES:
        for m in drive.pattern.finditer(html):
            url = normalize_url(_extended_url(html, m))
            sid = share_id_of(url, drive.key)
            if not sid or sid.lower() in PLACEHOLDER_IDS or (drive.key, sid) in seen:
                continue
            seen.add((drive.key, sid))
            code = code_from_url(url)
            if drive.needs_code and not code:
                code = pair_codes.get((drive.key, sid), "") or _code_near(html, m.start(), m.end())
            context = _ANY_TAG.sub(" ", html[max(0, m.start() - 160): m.end() + 240])
            context = html_lib.unescape(re.sub(r"\s+", " ", context))
            found.append(
                FoundLink(
                    drive=drive.key,
                    url=url,
                    share_id=sid,
                    code=code,
                    note=pair_notes.get((drive.key, sid), ""),
                    context=context,
                    page_url=page_url,
                    page_title=page_title,
                )
            )
    return found


def has_resource_words(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in RESOURCE_WORDS)


def looks_dead(text: str) -> bool:
    return any(h in text for h in DEAD_HINTS)


def score_link(link: FoundLink, query: str, page_title: str = "", snippet: str = "") -> float:
    """结果相关性/质量打分：链接自身的上下文比整页标题更重要。"""
    score = 0.0
    own = f"{link.context}".lower()
    pg = f"{page_title} {link.page_title}".lower()
    q = query.strip().lower()

    if q:
        if q in own:
            score += 3.0
        elif q in pg:
            score += 1.6
        else:
            tokens = [t for t in re.split(r"[\s,，。、《》()（）\[\]【】:：/|]+", q) if len(t) >= 2]
            if tokens:
                ratio_own = sum(1 for t in tokens if t in own) / len(tokens)
                ratio_pg = sum(1 for t in tokens if t in pg) / len(tokens)
                score += 2.0 * ratio_own + 0.8 * ratio_pg

    hay = f"{own} {pg}"
    for pattern, bonus in QUALITY_TAGS:
        if pattern.search(hay):
            score += bonus
    if link.code:
        score += 0.8
    if has_resource_words(f"{link.page_title} {page_title}"):
        score += 0.6
    if looks_dead(f"{snippet} {link.context} {page_title}"):
        score -= 3.0
    if link.drive in ("baidu", "quark", "aliyun"):
        score += 0.2
    return round(score, 2)

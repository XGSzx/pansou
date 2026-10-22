"""离线自测：不联网，只验证解析与抽取逻辑。"""

from __future__ import annotations

import sys

from .engines import BaiduEngine, So360Engine
from .extract import extract_from_html, extract_links, html_to_text, normalize_url, score_link

BAIDU_HTML = """
<html><body>
<div class="result c-container xpath-log new-pmd" id="1">
  <h3 class="t"><a href="http://www.baidu.com/link?url=abc123" target="_blank">流浪地球2 1080P 网盘资源</a></h3>
  <div class="c-abstract">链接：https://pan.baidu.com/s/1AbCdEfGh 提取码：q7x2 复制这段内容后打开百度网盘</div>
</div>
<div class="result c-container xpath-log new-pmd" id="2">
  <h3 class="t"><a href="http://www.baidu.com/link?url=def456" target="_blank">流浪地球2 4K 夸克网盘</a></h3>
  <div class="c-abstract">夸克：https://pan.quark.cn/s/9f8e7d6c5b4a?pwd=k3m9</div>
</div>
</body></html>
"""

SO360_HTML = """
<html><body><ul class="result">
<li class="res-list" data-url="x">
  <h3 class="res-title"><a href="https://www.so.com/link?m=zzz" data-mdurl="https://www.example.com/p/1">流浪地球2 蓝光原盘 百度网盘下载</a></h3>
  <p class="res-desc">网盘链接 https://pan.baidu.com/s/1Zx9Yw8Vu 密码: 8h2k</p>
  <cite>www.example.com</cite>
</li>
</ul></body></html>
"""


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "PASS" if cond else "FAIL"
    if not cond:
        print(f"  [{mark}] {name} {detail}")
    else:
        print(f"  [{mark}] {name}")
    return cond


def run_selftest() -> int:
    print("离线自测（不联网）")
    ok = True

    # 1. 各网盘链接抽取
    cases = [
        ("百度网盘+提取码", "链接：https://pan.baidu.com/s/1AbCdEfGh 提取码：q7x2", "baidu", "q7x2"),
        ("夸克网盘 pwd 参数", "https://pan.quark.cn/s/9f8e7d6c5b4a?pwd=k3m9", "quark", "k3m9"),
        ("阿里云盘（无需提取码）", "https://www.aliyundrive.com/s/abc123XYZ", "aliyun", ""),
        ("115 网盘", "https://115.com/s/sw123456?password=abcd", "pan115", "abcd"),
        ("天翼云盘", "https://cloud.189.cn/t/AbCdEf123 访问码：3k9m", "tianyi", "3k9m"),
        ("迅雷网盘", "https://pan.xunlei.com/s/VN_abc123?pwd=9x2k", "xunlei", "9x2k"),
        ("123网盘", "https://www.123pan.com/s/abc-def 提取码:7h2j", "pan123", "7h2j"),
        ("UC网盘", "https://drive.uc.cn/s/abc123def", "uc", ""),
    ]
    for name, text, drive, code in cases:
        links = extract_links(text, "https://example.com", "示例标题")
        hit = next((l for l in links if l.drive == drive), None)
        ok &= _check(name, hit is not None, "未抽取到链接")
        if hit:
            ok &= _check(name + " 提取码", hit.code == code, f"期望 {code!r} 实际 {hit.code!r}")

    # 2. 链接归一化
    u = normalize_url("https://www.aliyundrive.com/s/abcDEF123/?utm_source=x")
    ok &= _check("域名归一化", u == "https://alipan.com/s/abcDEF123", u)

    # 3. HTML 页面抽取（正文 + href 双通道）
    html = '<div><a href="https://pan.baidu.com/s/1aaaabbbbcccc">点我</a> 提取码：m4n8</div>'
    links = extract_from_html(html, "https://blog.example.com/post/1", "某资源站")
    ok &= _check("HTML 抽取", any(l.drive == "baidu" and l.code == "m4n8" for l in links), str([(l.drive, l.code) for l in links]))

    # 4. 失效提示扣分
    alive = extract_links("https://pan.baidu.com/s/1AbCdEfGh 提取码：q7x2", "", "流浪地球2 1080P")
    dead = extract_links("https://pan.baidu.com/s/1AbCdEfGh 提取码：q7x2", "", "流浪地球2 1080P")
    s1 = score_link(alive[0], "流浪地球2", "流浪地球2 1080P 网盘")
    s2 = score_link(dead[0], "流浪地球2", "流浪地球2 分享已失效")
    ok &= _check("失效页面降权", s2 < s1, f"{s2} !< {s1}")

    # 5. 百度 SERP 解析
    from .net import Response

    class FakeClient:
        def get(self, url, **kwargs):
            return Response(url=url, status=200, text=BAIDU_HTML, headers={"content-type": "text/html"})

    hits = BaiduEngine().search(FakeClient(), "流浪地球2")
    ok &= _check("百度 SERP 解析", len(hits) == 2, f"得到 {len(hits)} 条")
    ok &= _check("百度结果含跳转链接", hits and "baidu.com/link" in hits[0].url)

    # 6. 360 SERP 解析
    class FakeClient2:
        def get(self, url, **kwargs):
            return Response(url=url, status=200, text=SO360_HTML, headers={"content-type": "text/html"})

    hits2 = So360Engine().search(FakeClient2(), "流浪地球2")
    ok &= _check("360 SERP 解析", len(hits2) == 1, f"得到 {len(hits2)} 条")
    ok &= _check("360 data-mdurl 优先", hits2 and hits2[0].url == "https://www.example.com/p/1", hits2[0].url if hits2 else "")

    # 7. 纯文本化
    text = html_to_text("<p>你好&nbsp;世界</p><script>var a=1</script>")
    ok &= _check("HTML 转文本", "你好" in text and "世界" in text and "var a" not in text, text)

    print("\n结果：" + ("全部通过 ✅" if ok else "存在失败项 ❌"))
    return 0 if ok else 1

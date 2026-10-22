"""本地网页版：127.0.0.1 上的单页应用 + NDJSON 流式接口。"""

from __future__ import annotations

import json
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import load_config
from .search import Searcher

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_QUERY_LEN = 80
_search_slots = threading.Semaphore(2)


def _index_html() -> bytes:
    path = STATIC_DIR / "index.html"
    return path.read_bytes()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "YingPanSou/1.0"

    cfg: dict[str, Any] = {}

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
        return

    # ------------------------------------------------------------------ 基础
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    # ------------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path in ("/", "/index.html"):
            self._send(200, _index_html(), "text/html; charset=utf-8")
            return
        if parts.path == "/api/health":
            self._json(200, {"ok": True, "version": "1.0", "engines": self._engine_labels()})
            return
        if parts.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path != "/api/search":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "请求格式错误"})
            return
        self._stream_search(payload)

    # ------------------------------------------------------------------ 搜索
    def _engine_labels(self) -> list[str]:
        from .engines import build_engines

        return [e.label for e in build_engines(self.cfg)]

    def _stream_search(self, payload: dict[str, Any]) -> None:
        query = str(payload.get("q") or "").strip()
        if not query:
            self._json(400, {"ok": False, "error": "请输入影视名称"})
            return
        if len(query) > MAX_QUERY_LEN:
            self._json(400, {"ok": False, "error": "名称过长"})
            return
        if not _search_slots.acquire(blocking=False):
            self._json(429, {"ok": False, "error": "已有任务在运行，请稍候"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        lock = threading.Lock()
        broken = threading.Event()

        def emit(event: dict[str, Any]) -> None:
            if broken.is_set():
                return
            data = json.dumps(event, ensure_ascii=False) + "\n"
            with lock:
                try:
                    self.wfile.write(data.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    broken.set()

        try:
            import copy

            cfg = copy.deepcopy(self.cfg)
            check = bool(payload.get("check"))
            crawl = int(payload.get("crawl") or cfg["search"].get("crawl_pages", 12))
            crawl = max(3, min(crawl, 40))
            max_results = int(payload.get("max") or cfg["output"].get("max_results", 60))
            engines = payload.get("engines")
            if isinstance(engines, list) and engines:
                for key, value in list(cfg["engines"].items()):
                    if key in ("serper", "bocha"):
                        continue
                    cfg["engines"][key] = key in engines
            emit({"type": "start", "query": query, "time": time.strftime("%H:%M:%S")})
            searcher = Searcher(cfg, emit)
            searcher.run(query, check=check, max_results=max_results, crawl_pages=crawl)
        except Exception as exc:  # 兜底，别让前端一直转圈
            emit({"type": "error", "msg": f"{type(exc).__name__}: {exc}"})
            emit({"type": "done", "query": query, "results": [], "stats": {}})
        finally:
            _search_slots.release()
            try:
                self.wfile.flush()
            except OSError:
                pass


def _pick_port(host: str, port: int) -> int:
    for candidate in range(port, port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def run_web(config_path: str | None = None, port: int | None = None, host: str | None = None) -> None:
    cfg = load_config(config_path)
    host = host or cfg["web"].get("host", "127.0.0.1")
    port = int(port or cfg["web"].get("port", 8765))
    port = _pick_port(host, port)

    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}"

    print("=" * 64)
    print("  影盘搜 · 全网影视网盘资源聚合搜索")
    print(f"  已启动：{url}")
    print("  浏览器打开后输入片名即可；关闭本窗口即停止服务。")
    print("  手机若要访问：把 config.json 里 web.host 改成 0.0.0.0（仅限可信局域网）")
    print("=" * 64)

    if cfg["web"].get("open_browser", True):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()

"""A site blocked by the 'internet provider' is retried through the proxy automatically, and remembered."""
import http.server
import threading
import urllib.request
from pathlib import Path

from omniarr import config, db, net, settings

BLOCK = b"<html><body><h1>This site has been blocked</h1>Access to this website has been blocked by court order</body></html>"


class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><body>real results</body></html>" if self.headers.get("X-Via-Proxy") else BLOCK
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Proxy(http.server.BaseHTTPRequestHandler):
    """Minimal forward proxy for plain-HTTP absolute URLs."""
    def do_GET(self):
        req = urllib.request.Request(self.path, headers={"X-Via-Proxy": "1"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req) as r:
            data = r.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _serve(handler, port):
    s = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


async def test_blocked_site_switches_to_proxy():
    tmp = Path("/tmp/omniarr-proxy-test")
    config.CONFIG_DIR, config.DB_PATH = tmp, tmp / "t.db"
    config.LOG_DIR, config.DEFINITIONS_DIR, config.CUSTOM_DEFINITIONS_DIR = tmp / "l", tmp / "d", tmp / "c"
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db.close()
    db.connect()
    net._clients.clear()
    site, proxy = _serve(Site, 18771), _serve(Proxy, 18772)
    try:
        s = net.Session(key="t", name="blocked-site")
        # no proxy configured → clear block error
        try:
            await s.request("GET", "http://127.0.0.1:18771/search", polite=False)
            raise AssertionError("expected a block error")
        except net.IndexerError as e:
            assert e.kind == "isp_block"
        settings.save("unblock", {"proxy_url": "http://127.0.0.1:18772"})
        remembered = []
        s.on_proxy_needed = remembered.append
        r = await s.request("GET", "http://127.0.0.1:18771/search", polite=False)
        assert b"real results" in r.content
        assert s.use_proxy and remembered == [True]
        # with auto-proxy turned off the block is reported instead
        settings.save("unblock", {"auto_proxy": False})
        s2 = net.Session(key="t2", name="other")
        try:
            await s2.request("GET", "http://127.0.0.1:18771/search", polite=False)
            raise AssertionError("expected a block error")
        except net.IndexerError as e:
            assert e.kind == "isp_block" and not s2.use_proxy
    finally:
        site.shutdown()
        proxy.shutdown()


class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        cookies = self.headers.get_all("Cookie") or []
        uas = self.headers.get_all("User-Agent") or []
        if len(cookies) > 1 or len(uas) > 1:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<html><head><title>400 Bad Request</title></head></html>")
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(("|".join(cookies) + "#" + "|".join(uas)).encode())

    def log_message(self, *a):
        pass


async def test_definition_cookie_merged_with_site_cookies():
    srv = _serve(Echo, 18773)
    try:
        s = net.Session(key="t3", name="eztv-like")
        s.cookies = {"cf_clearance": "abc", "sort_no": "50"}
        r = await s.request("GET", "http://127.0.0.1:18773/", polite=False,
                            headers={"cookie": "sort_no=100; layout=def_wlinks", "user-agent": "X/1"})
        assert r.status == 200, r.text
        cookie, ua = r.text.split("#")
        assert "cf_clearance=abc" in cookie and "layout=def_wlinks" in cookie and "sort_no=50" in cookie, cookie
        assert ua == "X/1"
    finally:
        srv.shutdown()

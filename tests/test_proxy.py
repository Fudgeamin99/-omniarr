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

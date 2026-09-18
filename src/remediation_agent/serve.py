"""A localhost dashboard that updates while Devin works.

Two things run here: an HTTP server on the main thread, and the collector on a
background thread. The collector is what makes the page change — it polls Devin,
records transitions, and promotes triage decisions into remediation work. The
server just renders whatever the store currently says.

That split matters. The server holds no state of its own, so a browser refresh,
a server restart, or three people watching at once all see the same thing, and
the pipeline keeps advancing whether or not anyone is looking.

Bound to 127.0.0.1 by default and deliberately not configurable to anything
wider from the CLI. The page exposes issue titles, session URLs and finding
detail for a private repository, and there is no authentication — this is a
local view, not a service.

stdlib `http.server` because the package has no runtime dependencies. It is a
single-purpose read-only view for one viewer, which is exactly the workload
`ThreadingHTTPServer` is adequate for.
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .collector import Collector
from .config import Config
from .dashboard import render, render_partial
from .report import build, render_json
from .store import Store

log = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    server_version = "remediation-agent"
    sys_version = ""

    # injected by serve()
    snapshot: Callable[[], object]
    refresh_seconds: int = 5

    def log_message(self, fmt: str, *args) -> None:
        # Default handler logs every request to stderr, which drowns the
        # collector's output — the only log anyone actually wants here.
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, body: str, content_type: str = "text/html; charset=utf-8",
              status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # The page is a live view; a cached copy is a wrong copy.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:          # noqa: N802  (stdlib naming)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            report = self.snapshot()
            if path == "/":
                self._send(render(report, live=True,
                                  refresh_seconds=self.refresh_seconds))
            elif path == "/partial":
                self._send(render_partial(report,
                                          refresh_seconds=self.refresh_seconds))
            elif path == "/api/report.json":
                self._send(render_json(report), "application/json; charset=utf-8")
            elif path == "/healthz":
                self._send("ok", "text/plain; charset=utf-8")
            else:
                self._send("<h1>404</h1>", status=404)
        except BrokenPipeError:
            pass                        # viewer navigated away mid-render
        except Exception:               # noqa: BLE001
            # A rendering bug must not take the server down while a demo is
            # being recorded; show it on the page instead.
            log.exception("failed to render %s", path)
            self._send("<h1>500</h1><pre>see server log</pre>", status=500)


def _collector_loop(collector: Collector, interval: int,
                    stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            tick = collector.tick()
            if not tick.quiet:
                log.info("collector: %s", tick.summary())
        except Exception:               # noqa: BLE001
            # Network blips must not kill the loop; the next tick reconciles.
            log.exception("collector tick failed")
        stop.wait(interval)


def serve(cfg: Config, *, host: str = "127.0.0.1", port: int = 8765,
          collector: Collector | None = None, refresh_seconds: int = 5) -> None:
    """Serve the dashboard, optionally with a collector advancing behind it."""
    stop = threading.Event()
    worker: threading.Thread | None = None

    if collector is not None:
        worker = threading.Thread(
            target=_collector_loop,
            args=(collector, cfg.poll_interval_seconds, stop),
            name="collector", daemon=True,
        )
        worker.start()
        log.info("collector polling every %ds", cfg.poll_interval_seconds)
    else:
        log.info("no collector — serving a static view of the store")

    def snapshot():
        # A fresh connection per request: SQLite connections are not safe to
        # share across threads, and the read is cheap.
        with Store(cfg.db_path) as store:
            return build(store)

    handler = type("Handler", (_Handler,), {
        "snapshot": staticmethod(snapshot),
        "refresh_seconds": refresh_seconds,
    })

    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    print(f"\n  dashboard   http://{host}:{port}")
    print(f"  json        http://{host}:{port}/api/report.json")
    print(f"  refreshing  every {refresh_seconds}s"
          + (f", collector every {cfg.poll_interval_seconds}s" if collector else "")
          + "\n  ctrl-c to stop\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        stop.set()
        httpd.shutdown()
        httpd.server_close()
        if worker is not None:
            worker.join(timeout=5)

#!/usr/bin/env python3
"""Alert dispatcher: Alertmanager webhook -> Google Chat.

Alertmanager can't post to Google Chat on its own, so its webhook
receivers point here. For each notification the dispatcher:

  * formats the alert group as a readable chat message,
  * posts it to the Google Chat space named in ?space=,
  * threads it by Alertmanager's groupKey, so FIRING and RESOLVED for the
    same problem land in one thread instead of flooding the space,
  * retries on 429 / 5xx with backoff.

It also receives the always-firing Watchdog alert on /heartbeat. If that
stops arriving, something between Prometheus and Alertmanager is broken,
and the dispatcher says so in the heartbeat space. That's the one failure
the normal alerts can't report on their own.

Only the standard library is used, so it runs on any server with Python 3.8+.

    alert_dispatcher.py --config /etc/alert-dispatcher/dispatcher.ini
"""

import argparse
import configparser
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("alert-dispatcher")

MAX_ALERTS_PER_MESSAGE = 15
SEVERITY_ICON = {"critical": "\U0001F534", "warning": "\U0001F7E0", "info": "\U0001F535"}
RESOLVED_ICON = "✅"


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def thread_key(group_key: str) -> str:
    """Stable, short key per alert group. Google Chat limits threadKey length."""
    return hashlib.sha1(group_key.encode()).hexdigest()[:24]


def format_message(payload: dict, alertmanager_url: str = "") -> str:
    """Turn an Alertmanager webhook payload into Google Chat text."""
    status = payload.get("status", "firing")
    labels = payload.get("commonLabels", {})
    annotations = payload.get("commonAnnotations", {})
    alerts = payload.get("alerts", [])

    firing = [a for a in alerts if a.get("status") == "firing"]
    resolved = [a for a in alerts if a.get("status") == "resolved"]

    alertname = labels.get("alertname", "alert")
    env = labels.get("env", "unknown")
    severity = labels.get("severity", "")

    if status == "resolved":
        head = f"{RESOLVED_ICON} *RESOLVED* · {env} · {alertname}"
    else:
        icon = SEVERITY_ICON.get(severity, "⚠️")
        count = f" ({len(firing)})" if len(firing) > 1 else ""
        head = f"{icon} *FIRING{count}* · {env} · {alertname}"

    lines = [head]

    shown = (firing or resolved)[:MAX_ALERTS_PER_MESSAGE]
    for alert in shown:
        a_labels = alert.get("labels", {})
        a_ann = alert.get("annotations", {})
        where = a_labels.get("instance") or a_labels.get("pod") or a_labels.get("name") or ""
        summary = a_ann.get("summary", "")
        prefix = f"`{where}` " if where else ""
        lines.append(f"• {prefix}{summary}".rstrip())

    hidden = len(firing or resolved) - len(shown)
    if hidden > 0:
        lines.append(f"… and {hidden} more")

    if status == "firing" and annotations.get("description"):
        lines.append("")
        lines.append(annotations["description"])

    footer = []
    if annotations.get("runbook"):
        footer.append(f"Runbook: {annotations['runbook']}")
    if alertmanager_url and status == "firing":
        footer.append(f"<{alertmanager_url}|Silence / details>")
    if footer:
        lines.append("")
        lines.extend(footer)

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Google Chat client
# --------------------------------------------------------------------------

class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters = {
            "dispatcher_alerts_received_total": 0,
            "dispatcher_google_chat_posts_total": 0,
            "dispatcher_google_chat_errors_total": 0,
        }
        self.last_heartbeat = 0.0

    def inc(self, name, value=1):
        with self._lock:
            self.counters[name] += value

    def render(self) -> str:
        with self._lock:
            out = []
            for name, value in self.counters.items():
                out.append(f"# TYPE {name} counter")
                out.append(f"{name} {value}")
            out.append("# TYPE dispatcher_last_heartbeat_timestamp_seconds gauge")
            out.append(f"dispatcher_last_heartbeat_timestamp_seconds {self.last_heartbeat}")
            return "\n".join(out) + "\n"


class GoogleChat:
    def __init__(self, spaces: dict, metrics: Metrics, timeout=10, retries=3):
        self.spaces = spaces
        self.metrics = metrics
        self.timeout = timeout
        self.retries = retries

    def post(self, space: str, text: str, thread: str = "") -> bool:
        url = self.spaces.get(space)
        if not url:
            log.error("unknown space %r (configured: %s)", space, ", ".join(self.spaces))
            self.metrics.inc("dispatcher_google_chat_errors_total")
            return False

        if thread:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode(
                {"threadKey": thread, "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"}
            )

        body = json.dumps({"text": text}).encode()
        delay = 2
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json; charset=UTF-8"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout):
                    self.metrics.inc("dispatcher_google_chat_posts_total")
                    return True
            except urllib.error.HTTPError as e:
                retryable = e.code == 429 or e.code >= 500
                log.warning("google chat %s on attempt %d/%d for space %s", e.code, attempt, self.retries, space)
                if not retryable:
                    break
            except (urllib.error.URLError, TimeoutError) as e:
                log.warning("google chat unreachable (%s) on attempt %d/%d", e, attempt, self.retries)
            if attempt < self.retries:
                time.sleep(delay)
                delay *= 2

        self.metrics.inc("dispatcher_google_chat_errors_total")
        return False


# --------------------------------------------------------------------------
# Heartbeat watcher
# --------------------------------------------------------------------------

class HeartbeatWatcher(threading.Thread):
    """Warns once when the Watchdog alert stops arriving, and once when it's back."""

    def __init__(self, chat: GoogleChat, metrics: Metrics, space: str, max_silence: int):
        super().__init__(daemon=True)
        self.chat = chat
        self.metrics = metrics
        self.space = space
        self.max_silence = max_silence
        self.lost = False
        self.metrics.last_heartbeat = time.time()  # grace period after start

    def beat(self):
        self.metrics.last_heartbeat = time.time()
        if self.lost:
            self.lost = False
            log.info("heartbeat restored")
            self.chat.post(self.space, f"{RESOLVED_ICON} Alerting pipeline heartbeat is back.")

    def check(self, now=None):
        now = now or time.time()
        silence = now - self.metrics.last_heartbeat
        if silence > self.max_silence and not self.lost:
            self.lost = True
            log.error("no heartbeat for %ds", silence)
            self.chat.post(
                self.space,
                "\U0001F6A8 *No Watchdog heartbeat from Alertmanager for "
                f"{int(silence // 60)} min.*\n"
                "Prometheus or Alertmanager may be down, so other alerts may not be reaching anyone. "
                "Check `systemctl status prometheus alertmanager` on the monitoring server.",
            )

    def run(self):
        while True:
            time.sleep(30)
            try:
                self.check()
            except Exception:  # never let the watcher thread die
                log.exception("heartbeat check failed")


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

def make_handler(chat: GoogleChat, metrics: Metrics, watcher: HeartbeatWatcher, am_url: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "alert-dispatcher"

        def log_message(self, fmt, *args):  # route http.server logs through logging
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _reply(self, code, body=b"", ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/healthz":
                self._reply(200, b"ok\n")
            elif path == "/metrics":
                self._reply(200, metrics.render().encode(), "text/plain; version=0.0.4")
            else:
                self._reply(404, b"not found\n")

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""

            if parsed.path == "/heartbeat":
                watcher.beat()
                self._reply(200, b"ok\n")
                return

            if parsed.path != "/alert":
                self._reply(404, b"not found\n")
                return

            space = urllib.parse.parse_qs(parsed.query).get("space", [""])[0]
            try:
                payload = json.loads(raw)
            except ValueError:
                self._reply(400, b"invalid json\n")
                return

            metrics.inc("dispatcher_alerts_received_total", len(payload.get("alerts", [])))
            text = format_message(payload, am_url)
            ok = chat.post(space, text, thread_key(payload.get("groupKey", "")))
            # 500 makes Alertmanager retry the notification later
            self._reply(200 if ok else 500, b"ok\n" if ok else b"post failed\n")

    return Handler


def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        raise SystemExit(f"cannot read config {path}")
    if not cfg.has_section("spaces") or not cfg["spaces"]:
        raise SystemExit("config needs a [spaces] section with at least one webhook URL")
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="/etc/alert-dispatcher/dispatcher.ini")
    args = parser.parse_args()

    cfg = load_config(args.config)
    server_cfg = cfg["server"] if cfg.has_section("server") else {}
    logging.basicConfig(
        level=server_cfg.get("log_level", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    host, _, port = server_cfg.get("listen", "127.0.0.1:9095").rpartition(":")
    metrics = Metrics()
    chat = GoogleChat(dict(cfg["spaces"]), metrics)

    hb = cfg["heartbeat"] if cfg.has_section("heartbeat") else {}
    watcher = HeartbeatWatcher(
        chat, metrics,
        space=hb.get("space", next(iter(cfg["spaces"]))),
        max_silence=int(hb.get("max_silence_seconds", 300)),
    )
    watcher.start()

    handler = make_handler(chat, metrics, watcher, server_cfg.get("alertmanager_url", ""))
    httpd = ThreadingHTTPServer((host, int(port)), handler)
    log.info("listening on %s:%s, spaces: %s", host, port, ", ".join(cfg["spaces"]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import alert_dispatcher as d


def payload(status="firing", alerts=None, **common):
    labels = {"alertname": "HostDown", "env": "production", "severity": "critical"}
    labels.update(common.pop("labels", {}))
    return {
        "status": status,
        "groupKey": '{}:{env="production", alertname="HostDown"}',
        "commonLabels": labels,
        "commonAnnotations": common.get("annotations", {}),
        "alerts": alerts if alerts is not None else [],
    }


def alert(instance, summary, status="firing"):
    return {"status": status, "labels": {"instance": instance}, "annotations": {"summary": summary}}


class FormatTest(unittest.TestCase):
    def test_firing_lists_every_instance(self):
        text = d.format_message(payload(alerts=[
            alert("app-prod01:9100", "app-prod01:9100 is down"),
            alert("app-prod02:9100", "app-prod02:9100 is down"),
        ]))
        self.assertTrue(text.startswith("\U0001F534 *FIRING (2)* · production · HostDown"))
        self.assertIn("`app-prod01:9100` app-prod01:9100 is down", text)
        self.assertIn("`app-prod02:9100`", text)

    def test_resolved_has_check_mark_and_no_description(self):
        text = d.format_message(payload(
            status="resolved",
            alerts=[alert("db-prod02:9104", "lag", status="resolved")],
            annotations={"description": "long text"},
        ))
        self.assertTrue(text.startswith("✅ *RESOLVED*"))
        self.assertNotIn("long text", text)

    def test_long_groups_are_truncated(self):
        many = [alert(f"h{i}:9100", "down") for i in range(40)]
        text = d.format_message(payload(alerts=many))
        self.assertIn("… and 25 more", text)
        self.assertEqual(text.count("•"), d.MAX_ALERTS_PER_MESSAGE)

    def test_runbook_and_silence_link(self):
        text = d.format_message(
            payload(alerts=[alert("x", "y")], annotations={"runbook": "docs/runbook.md#hostdown"}),
            alertmanager_url="http://am:9093",
        )
        self.assertIn("Runbook: docs/runbook.md#hostdown", text)
        self.assertIn("<http://am:9093|Silence / details>", text)

    def test_thread_key_is_stable_and_short(self):
        k1 = d.thread_key("group-a")
        self.assertEqual(k1, d.thread_key("group-a"))
        self.assertNotEqual(k1, d.thread_key("group-b"))
        self.assertLessEqual(len(k1), 24)


class GoogleChatTest(unittest.TestCase):
    def test_unknown_space_counts_as_error(self):
        m = d.Metrics()
        chat = d.GoogleChat({"prod": "https://chat.example/x"}, m)
        self.assertFalse(chat.post("nope", "hi"))
        self.assertEqual(m.counters["dispatcher_google_chat_errors_total"], 1)

    def test_thread_params_appended_to_webhook_url(self):
        m = d.Metrics()
        chat = d.GoogleChat({"prod": "https://chat.example/v1/spaces/X/messages?key=k&token=t"}, m)
        with mock.patch("urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value = None
            self.assertTrue(chat.post("prod", "hi", thread="abc"))
        url = op.call_args[0][0].full_url
        self.assertIn("key=k&token=t&threadKey=abc", url)
        self.assertIn("messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD", url)

    def test_retries_on_server_error_then_gives_up(self):
        m = d.Metrics()
        chat = d.GoogleChat({"prod": "https://chat.example/x"}, m, retries=3)
        err = d.urllib.error.HTTPError("u", 503, "busy", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err) as op, mock.patch("time.sleep"):
            self.assertFalse(chat.post("prod", "hi"))
        self.assertEqual(op.call_count, 3)
        self.assertEqual(m.counters["dispatcher_google_chat_errors_total"], 1)

    def test_does_not_retry_bad_request(self):
        chat = d.GoogleChat({"prod": "https://chat.example/x"}, d.Metrics(), retries=3)
        err = d.urllib.error.HTTPError("u", 400, "bad", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err) as op, mock.patch("time.sleep"):
            chat.post("prod", "hi")
        self.assertEqual(op.call_count, 1)


class HeartbeatTest(unittest.TestCase):
    def test_warns_once_then_recovers(self):
        chat = mock.Mock()
        m = d.Metrics()
        w = d.HeartbeatWatcher(chat, m, space="ops", max_silence=300)
        m.last_heartbeat = 1000.0

        w.check(now=1200.0)
        chat.post.assert_not_called()

        w.check(now=1400.0)
        w.check(now=1500.0)
        self.assertEqual(chat.post.call_count, 1)
        self.assertIn("No Watchdog heartbeat", chat.post.call_args[0][1])

        w.beat()
        self.assertEqual(chat.post.call_count, 2)
        self.assertIn("back", chat.post.call_args[0][1])


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.metrics = d.Metrics()
        self.chat = mock.Mock()
        self.chat.post.return_value = True
        self.watcher = mock.Mock()
        handler = d.make_handler(self.chat, self.metrics, self.watcher, "")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST")
        with urllib.request.urlopen(req) as r:
            return r.status

    def test_alert_is_posted_to_requested_space(self):
        status = self._post("/alert?space=prod-alerts", payload(alerts=[alert("a", "b")]))
        self.assertEqual(status, 200)
        self.assertEqual(self.chat.post.call_args[0][0], "prod-alerts")
        self.assertEqual(self.metrics.counters["dispatcher_alerts_received_total"], 1)

    def test_failed_post_returns_500_so_alertmanager_retries(self):
        self.chat.post.return_value = False
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/alert?space=prod-alerts", payload(alerts=[alert("a", "b")]))
        self.assertEqual(ctx.exception.code, 500)

    def test_heartbeat_and_metrics(self):
        self.assertEqual(self._post("/heartbeat", {}), 200)
        self.watcher.beat.assert_called_once()
        with urllib.request.urlopen(self.base + "/metrics") as r:
            body = r.read().decode()
        self.assertIn("dispatcher_google_chat_errors_total 0", body)


if __name__ == "__main__":
    unittest.main()

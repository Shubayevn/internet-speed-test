"""Тесты на локальном HTTP-сервере: без доступа в интернет и без внешних зависимостей."""

import contextlib
import http.server
import io
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import netspeed  # noqa: E402

PAYLOAD = b"x" * 250_000


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen_headers.append(dict(self.headers))
        if self.path == "/missing":
            self.send_error(404, "Not Found")
            return
        if self.path == "/broken":
            # Обещаем больше, чем отдаём, и закрываем соединение.
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD) * 2))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, *args):
        pass


class LocalServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.seen_headers = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.seen_headers.clear()

    def test_makes_requested_number_of_full_downloads(self):
        results = netspeed.measure(f"{self.base}/file", count=10, timeout=5)

        self.assertEqual(len(results), 10)
        self.assertEqual(len(self.server.seen_headers), 10)
        self.assertTrue(all(r.size == len(PAYLOAD) for r in results))
        self.assertTrue(all(r.seconds > 0 for r in results))

        summary = netspeed.summarize(results)
        self.assertEqual(summary.total_bytes, 10 * len(PAYLOAD))
        self.assertGreater(summary.mbit_per_second, 0)

    def test_asks_for_uncompressed_uncached_data(self):
        netspeed.measure(f"{self.base}/file", count=1, timeout=5)
        headers = self.server.seen_headers[0]
        self.assertEqual(headers.get("Accept-Encoding"), "identity")
        self.assertEqual(headers.get("Cache-Control"), "no-cache")

    def test_http_error_reports_request_number(self):
        with self.assertRaises(netspeed.RequestFailed) as ctx:
            netspeed.measure(f"{self.base}/missing", count=10, timeout=5)
        self.assertEqual(ctx.exception.index, 1)
        self.assertIn("404", ctx.exception.reason)
        self.assertEqual(len(self.server.seen_headers), 1)  # после ошибки не продолжаем

    def test_truncated_response_is_an_error(self):
        with self.assertRaises(netspeed.RequestFailed) as ctx:
            netspeed.measure(f"{self.base}/broken", count=1, timeout=5)
        self.assertIn("оборвался", ctx.exception.reason)

    def test_main_prints_summary(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = netspeed.main([f"{self.base}/file", "-n", "3"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("Среднее время запроса", text)
        self.assertIn("0.75 МБ", text)  # 3 × 250 000 байт
        self.assertIn("Мбит/с", text)

    def test_main_returns_1_on_error(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = netspeed.main([f"{self.base}/missing"])
        self.assertEqual(code, 1)


class UnitTest(unittest.TestCase):
    def test_speed_math(self):
        # 12.5 МБ за 1 секунду = 100 Мбит/с
        summary = netspeed.summarize([netspeed.RequestResult(seconds=0.5, size=6_250_000)] * 2)
        self.assertAlmostEqual(summary.avg_seconds, 0.5)
        self.assertAlmostEqual(summary.mbit_per_second, 100.0)

    def test_rejects_bad_arguments(self):
        for argv in (["ftp://example.com/file"], ["not-a-url"], ["https://example.com", "-n", "0"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                    netspeed.build_parser().parse_args(argv)
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

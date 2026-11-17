#!/usr/bin/env python3
"""Замер скорости скачивания.

Делает N последовательных GET-запросов к указанному адресу, каждый раз
дочитывая ответ до последнего байта, и печатает среднее время запроса,
общий объём скачанных данных и скорость скачивания в Мбит/с.

Только стандартная библиотека Python, без внешних зависимостей.
"""

from __future__ import annotations

import argparse
import http.client
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

DEFAULT_COUNT = 10
DEFAULT_TIMEOUT = 30.0
CHUNK_SIZE = 64 * 1024
USER_AGENT = "netspeed/1.0 (internet speed test script)"

BYTES_IN_MB = 1_000_000
BITS_IN_MBIT = 1_000_000


@dataclass(frozen=True)
class RequestResult:
    """Результат одного запроса."""

    seconds: float  # от отправки запроса до получения последнего байта ответа
    size: int  # размер тела ответа в байтах

    @property
    def mbit_per_second(self) -> float:
        return to_mbit_per_second(self.size, self.seconds)


@dataclass(frozen=True)
class Summary:
    """Итог по всем запросам."""

    count: int
    total_seconds: float
    total_bytes: int

    @property
    def avg_seconds(self) -> float:
        return self.total_seconds / self.count if self.count else 0.0

    @property
    def mbit_per_second(self) -> float:
        return to_mbit_per_second(self.total_bytes, self.total_seconds)


class RequestFailed(Exception):
    """Запрос с номером `index` не удалось выполнить до конца."""

    def __init__(self, index: int, reason: str) -> None:
        super().__init__(f"запрос №{index}: {reason}")
        self.index = index
        self.reason = reason


class TruncatedResponse(Exception):
    """Соединение закрылось раньше, чем пришло всё тело ответа."""

    def __init__(self, received: int, expected: int) -> None:
        super().__init__(f"получено {received} из {expected} байт")
        self.received = received
        self.expected = expected


def to_mbit_per_second(size: int, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return size * 8 / seconds / BITS_IN_MBIT


def fetch(url: str, timeout: float) -> RequestResult:
    """Скачивает ресурс целиком и возвращает время и размер тела ответа."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            # Без сжатия и в обход кэшей — чтобы мерить реальный объём трафика.
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    size = 0
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        expected = _content_length(response)
        # Читаем кусками до конца ответа (пустой кусок = сервер всё отдал).
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
    elapsed = time.perf_counter() - start
    # http.client при чтении кусками не ругается на оборванный ответ,
    # поэтому сверяем полученный объём с Content-Length сами.
    if expected is not None and size < expected:
        raise TruncatedResponse(received=size, expected=expected)
    return RequestResult(seconds=elapsed, size=size)


def _content_length(response: http.client.HTTPResponse) -> Optional[int]:
    value = response.headers.get("Content-Length")
    if value is None or response.headers.get("Content-Encoding", "identity") != "identity":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def measure(
    url: str,
    count: int = DEFAULT_COUNT,
    timeout: float = DEFAULT_TIMEOUT,
    on_result: Optional[Callable[[int, RequestResult], None]] = None,
) -> List[RequestResult]:
    """Делает `count` запросов строго последовательно: следующий — только после полного ответа."""
    results: List[RequestResult] = []
    for index in range(1, count + 1):
        try:
            result = fetch(url, timeout)
        except urllib.error.HTTPError as exc:
            raise RequestFailed(index, f"сервер ответил HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RequestFailed(index, f"не удалось подключиться ({exc.reason})") from exc
        except TruncatedResponse as exc:
            raise RequestFailed(
                index,
                f"ответ оборвался: получено {format_bytes(exc.received)} из {format_bytes(exc.expected)}",
            ) from exc
        except TimeoutError as exc:
            raise RequestFailed(index, f"нет данных дольше {timeout:g} с") from exc
        except (http.client.HTTPException, OSError) as exc:
            raise RequestFailed(index, f"{type(exc).__name__}: {exc}") from exc
        results.append(result)
        if on_result is not None:
            on_result(index, result)
    return results


def summarize(results: Sequence[RequestResult]) -> Summary:
    return Summary(
        count=len(results),
        total_seconds=sum(r.seconds for r in results),
        total_bytes=sum(r.size for r in results),
    )


def format_mb(size: int) -> str:
    return f"{size / BYTES_IN_MB:.2f} МБ"


def format_bytes(size: int) -> str:
    return f"{size:,}".replace(",", " ") + " байт"


def parse_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise argparse.ArgumentTypeError(f"нужен адрес вида http(s)://..., получено: {value!r}")
    return value


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"нужно целое число, получено: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError("значение должно быть не меньше 1")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"нужно число, получено: {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("значение должно быть больше 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netspeed.py",
        description="Замер скорости скачивания: несколько последовательных запросов к одному адресу.",
    )
    parser.add_argument("url", type=parse_url, help="адрес файла, например тяжёлой картинки")
    parser.add_argument(
        "-n",
        "--count",
        type=positive_int,
        default=DEFAULT_COUNT,
        help="сколько запросов сделать (по умолчанию %(default)s)",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT,
        help="таймаут на подключение и на ожидание данных, секунды (по умолчанию %(default)g)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    width = len(str(args.count))

    print(f"Адрес:    {args.url}")
    print(f"Запросов: {args.count} (последовательно)")
    print(flush=True)

    def report(index: int, result: RequestResult) -> None:
        print(
            f"  [{index:>{width}}/{args.count}]"
            f"  {result.seconds:8.3f} с"
            f"  {format_mb(result.size):>12}"
            f"  {result.mbit_per_second:9.2f} Мбит/с",
            flush=True,
        )

    try:
        results = measure(args.url, args.count, args.timeout, on_result=report)
    except RequestFailed as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        return 130

    summary = summarize(results)
    print()
    print(f"Среднее время запроса: {summary.avg_seconds:.3f} с")
    print(f"Скачано всего:         {format_mb(summary.total_bytes)} ({format_bytes(summary.total_bytes)})")
    print(f"Скорость скачивания:   {summary.mbit_per_second:.2f} Мбит/с")
    if summary.total_bytes == 0:
        print("Внимание: сервер вернул пустые ответы, скорость посчитать не из чего.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

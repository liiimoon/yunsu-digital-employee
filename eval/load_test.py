# -*- coding: utf-8 -*-
"""负载/压力测试 harness：asyncio 并发打内核 handle()。

用法：
  python eval/load_test.py --concurrency 50 --requests 500     # mock 基准（默认 MockLLM）
  python eval/load_test.py --concurrency 10 --duration 30      # 持续 30 秒
  python eval/load_test.py --real --concurrency 3 --requests 20  # 真实模型小流量冒烟（默认禁用）

说明：
- 默认使用 MockLLM，不发起任何付费 API 调用；
- --real 需显式开启，强制 concurrency<=5 并启用全局限速（默认 1 QPS，--qps 可调）；
- 并发用信号量限流，模拟真实限流器行为；
- 报告：吞吐量、延迟 P50/P95/P99、错误率、平均 loop 轮次、平均 tool 调用数。
"""
import argparse
import asyncio
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from src.kernel import config  # noqa: E402
from src.kernel.core import Kernel  # noqa: E402
from src.kernel.session import SessionState  # noqa: E402

BASE_CSV = config.CORPUS_DIR / "04-评测集" / "评测集-80条.csv"
SECURITY_CSV = config.CORPUS_DIR / "05-安全对抗评测集" / "安全对抗评测集.csv"


def load_payloads() -> list:
    """从两套评测集抽取问题作为负载样本（覆盖作答/拒答/转人工/澄清全行为）。"""
    payloads = []
    for path in (BASE_CSV, SECURITY_CSV):
        if not path.exists():
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                user_type = "guest" if "对外" in row["场景"] else "staff"
                payloads.append((user_type, row["问题"]))
    return payloads or [("guest", "早餐多少钱？")]


class RateLimiter:
    """全局限速器（--real 模式）：保证任意两次请求发出间隔 >= 1/qps 秒。"""

    def __init__(self, qps: float):
        self.min_interval = 1.0 / qps if qps > 0 else 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = time.perf_counter()
            wait = self._last + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.perf_counter()


async def run_load(kernel, payloads, concurrency, total_requests, duration, limiter):
    sem = asyncio.Semaphore(concurrency)
    results = []  # (latency_ms, iterations, tool_calls, ok)
    counter = {"issued": 0}
    lock = asyncio.Lock()
    deadline = time.perf_counter() + duration if duration else None

    async def one_request(idx: int):
        user_type, question = payloads[idx % len(payloads)]
        session = SessionState(key=("load", str(idx)), user_type=user_type)
        t0 = time.perf_counter()
        ok, iterations, tool_calls = True, 0, 0
        try:
            reply = kernel.handle(user_type, question, session)
            iterations = reply.iterations
            tool_calls = sum(1 for t in reply.trace if t.get("type") == "tool_call")
        except Exception as e:  # noqa: BLE001 - 压测统计错误率，不中断
            print(f"[ERROR] {question[:20]}: {e}")
            ok = False
        latency_ms = (time.perf_counter() - t0) * 1000
        results.append((latency_ms, iterations, tool_calls, ok))

    async def worker():
        while True:
            async with lock:
                if deadline and time.perf_counter() >= deadline:
                    return
                if not deadline and counter["issued"] >= total_requests:
                    return
                idx = counter["issued"]
                counter["issued"] += 1
            await limiter.acquire()
            async with sem:
                await one_request(idx)

    t_start = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = time.perf_counter() - t_start
    return results, elapsed


def percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(p * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def main():
    parser = argparse.ArgumentParser(description="云宿数字员工负载测试")
    parser.add_argument("--concurrency", type=int, default=50, help="并发数（信号量限流）")
    parser.add_argument("--requests", type=int, default=500, help="总请求数")
    parser.add_argument("--duration", type=float, default=0, help="持续秒数（设置后忽略 --requests）")
    parser.add_argument("--real", action="store_true",
                        help="真实模型冒烟（默认禁用；强制并发<=5 且限速）")
    parser.add_argument("--qps", type=float, default=1.0, help="--real 模式全局限速（默认 1 QPS）")
    args = parser.parse_args()

    if args.real:
        # 真实付费 API：小流量冒烟保护
        args.concurrency = min(args.concurrency, 5)
        args.requests = min(args.requests, 50)
        from src.kernel.llm import DeepSeekLLM
        llm = DeepSeekLLM()
        print(f"== REAL 冒烟模式：并发 {args.concurrency}，限速 {args.qps} QPS，"
              f"请求上限 {args.requests}（模型 {config.CHAT_MODEL}）==")
    else:
        from src.kernel.llm import MockLLM
        llm = MockLLM()
        print("== MOCK 模式：桩 LLM，不发起真实 API 调用 ==")

    kernel = Kernel(llm=llm)
    payloads = load_payloads()
    print(f"负载样本 {len(payloads)} 条 | 并发 {args.concurrency} | "
          + (f"持续 {args.duration}s" if args.duration else f"总请求 {args.requests}"))

    limiter = RateLimiter(args.qps if args.real else 0)
    results, elapsed = asyncio.run(
        run_load(kernel, payloads, args.concurrency, args.requests,
                 args.duration, limiter))

    n = len(results)
    if not n:
        print("无请求完成。")
        return
    lat = sorted(r[0] for r in results)
    errors = sum(1 for r in results if not r[3])
    avg_iter = sum(r[1] for r in results) / n
    avg_tools = sum(r[2] for r in results) / n

    print("\n========== 负载测试结果 ==========")
    print(f"完成请求: {n} | 耗时: {elapsed:.2f}s | 吞吐量: {n / elapsed:.1f} req/s")
    print(f"延迟 P50: {percentile(lat, 0.50):.1f}ms | P95: {percentile(lat, 0.95):.1f}ms"
          f" | P99: {percentile(lat, 0.99):.1f}ms | max: {lat[-1]:.1f}ms")
    print(f"错误率: {errors / n:.2%} ({errors}/{n})")
    print(f"平均 loop 轮次: {avg_iter:.2f} | 平均 tool 调用数: {avg_tools:.2f}")


if __name__ == "__main__":
    main()

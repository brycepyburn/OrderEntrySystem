"""
latency_test.py — Matching engine latency benchmark
=====================================================

Measures three things:

  1. Round-trip latency   time from ws.send() → order_ack received
  2. Fill latency         time from ws.send() → execution report received
                          (only for orders that cross the book immediately)
  3. Throughput curve     p50/p95/p99 latency at increasing order rates
                          to find where the engine starts to saturate

Usage
-----
    # Start the engine first (any mode, k doesn't matter for latency):
    ./matching_engine 0 fifo

    # Run the benchmark:
    python latency_test.py

    # Options:
    python latency_test.py --orders 10000      # total orders (default 10000)
    python latency_test.py --rate 500          # orders/sec (default: max speed)
    python latency_test.py --no-plot           # skip matplotlib, print stats only
    python latency_test.py --warmup 500        # warmup orders before measuring

What the numbers mean
---------------------
  p50  (median)   half of orders complete faster than this
  p95             95% of orders complete faster than this — the "tail"
  p99             99% complete faster — the worst normal case
  max             the single slowest order — often a GC pause or OS scheduler hiccup

  A good low-latency engine running locally should show:
    p50  < 1 ms
    p95  < 2 ms
    p99  < 5 ms

  Over a real network (e.g. co-located but separate machine):
    p50  ~= network RTT + processing
    p95  ~= 2-3x p50

  Throughput saturation shows up as p99 growing faster than p50.
  When p99/p50 ratio exceeds ~10x, the engine queue is backing up.
"""

import argparse
import asyncio
import json
import random
import statistics
import time
from collections import defaultdict

import websockets

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument("--orders",  type=int,   default=10_000,
                    help="Total orders to send (default 10000)")
parser.add_argument("--rate",    type=float, default=0,
                    help="Orders/sec to send; 0 = as fast as possible")
parser.add_argument("--warmup",  type=int,   default=200,
                    help="Warmup orders to discard before measuring")
parser.add_argument("--no-plot", action="store_true",
                    help="Skip the matplotlib chart")
parser.add_argument("--host",    type=str,   default="localhost")
parser.add_argument("--port",    type=int,   default=8080)
args = parser.parse_args()

URI = f"ws://{args.host}:{args.port}/ws"

# ---------------------------------------------------------------------------
# Shared state (all in one asyncio thread — no locks needed)
# ---------------------------------------------------------------------------

# send_time[order_id] = time.perf_counter() at moment of ws.send()
send_time: dict[int, float] = {}

# Results — filled as acks and execution reports arrive
ack_latencies:  list[float] = []   # round-trip: send → ack (ms)
fill_latencies: list[float] = []   # fill:       send → first execution report (ms)

# Track which orders generated a fill report
filled_ids: set[int] = set()

# Completion event — set when all expected acks have arrived
all_done = asyncio.Event()
expected_acks   = 0
received_acks   = 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_order_id = 2_000_000

def next_id() -> int:
    global _order_id
    _order_id += 1
    return _order_id


def random_limit_order(order_id: int) -> dict:
    """Generate a random limit order near a stable mid-price."""
    side  = random.choice(["BUY", "SELL"])
    # Keep prices in a narrow band so orders occasionally cross and fill
    price = random.randint(998, 1002)
    qty   = random.randint(1, 100)
    return {
        "action": "new_order",
        "type":   "LIMIT",
        "side":   side,
        "price":  price,
        "qty":    qty,
        "id":     order_id,
    }


def percentile(data: list[float], p: float) -> float:
    if not data:
        return float("nan")
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * p / 100)
    idx = min(idx, len(sorted_data) - 1)
    return sorted_data[idx]


def print_stats(label: str, data: list[float]) -> None:
    if not data:
        print(f"  {label}: no data")
        return
    print(f"  {label} ({len(data)} samples):")
    print(f"    min    {min(data):.3f} ms")
    print(f"    p50    {percentile(data, 50):.3f} ms")
    print(f"    p75    {percentile(data, 75):.3f} ms")
    print(f"    p95    {percentile(data, 95):.3f} ms")
    print(f"    p99    {percentile(data, 99):.3f} ms")
    print(f"    max    {max(data):.3f} ms")
    print(f"    mean   {statistics.mean(data):.3f} ms")
    print(f"    stdev  {statistics.stdev(data):.3f} ms" if len(data) > 1 else "")

# ---------------------------------------------------------------------------
# Listener coroutine
# ---------------------------------------------------------------------------

async def listen(ws, warmup_count: int) -> None:
    """Receive all messages and record latencies."""
    global received_acks

    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        now = time.perf_counter()
        t   = msg.get("type", "")

        if t == "order_ack":
            order_id = msg.get("id")
            if order_id in send_time:
                elapsed_ms = (now - send_time[order_id]) * 1000
                received_acks += 1
                # Skip warmup orders
                if received_acks > warmup_count:
                    ack_latencies.append(elapsed_ms)

            if received_acks >= expected_acks:
                all_done.set()

        elif t == "execution":
            order_id = msg.get("order_id")
            if order_id in send_time and order_id not in filled_ids:
                filled_ids.add(order_id)
                elapsed_ms = (now - send_time[order_id]) * 1000
                # Only record if past warmup
                if received_acks > warmup_count:
                    fill_latencies.append(elapsed_ms)

        # Ignore telemetry and other message types

# ---------------------------------------------------------------------------
# Sender coroutine
# ---------------------------------------------------------------------------

async def send_orders(ws, n_orders: int, rate: float, warmup: int) -> float:
    """Send n_orders limit orders. Returns wall-clock duration in seconds."""
    global expected_acks
    expected_acks = n_orders + warmup

    interval = (1.0 / rate) if rate > 0 else 0

    print(f"  Sending {warmup} warmup orders…")
    for _ in range(warmup):
        oid = next_id()
        send_time[oid] = time.perf_counter()
        await ws.send(json.dumps(random_limit_order(oid)))
        if interval:
            await asyncio.sleep(interval)

    print(f"  Sending {n_orders} measurement orders"
          f"{f' at {rate:.0f}/s' if rate else ' at max speed'}…")

    t_start = time.perf_counter()

    for i in range(n_orders):
        oid = next_id()
        send_time[oid] = time.perf_counter()
        await ws.send(json.dumps(random_limit_order(oid)))
        if interval:
            await asyncio.sleep(interval)
        # Yield to event loop occasionally so listener can run
        if i % 100 == 0:
            await asyncio.sleep(0)

    t_end = time.perf_counter()
    return t_end - t_start

# ---------------------------------------------------------------------------
# Throughput sweep
# ---------------------------------------------------------------------------

async def throughput_sweep(rates: list[float]) -> dict[float, dict]:
    """
    For each rate, run a mini-benchmark and record p50/p95/p99.
    Returns dict: rate → {p50, p95, p99, achieved_rate}
    """
    results = {}
    MINI_ORDERS = 500
    MINI_WARMUP = 50

    for rate in rates:
        # Reset state
        send_time.clear()
        ack_latencies.clear()
        fill_latencies.clear()
        filled_ids.clear()
        all_done.clear()
        global received_acks, expected_acks
        received_acks = 0
        expected_acks = MINI_ORDERS + MINI_WARMUP

        async with websockets.connect(URI, ping_interval=None,
                                      ping_timeout=None) as ws:
            listener = asyncio.create_task(listen(ws, MINI_WARMUP))
            duration = await send_orders(ws, MINI_ORDERS, rate, MINI_WARMUP)

            # Wait up to 5s for all acks
            try:
                await asyncio.wait_for(all_done.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            listener.cancel()

        achieved = len(ack_latencies) / duration if duration > 0 else 0
        results[rate] = {
            "p50":          percentile(ack_latencies, 50),
            "p95":          percentile(ack_latencies, 95),
            "p99":          percentile(ack_latencies, 99),
            "achieved_rate": achieved,
            "n":            len(ack_latencies),
        }
        print(f"    rate={rate:6.0f}/s  "
              f"p50={results[rate]['p50']:.2f}ms  "
              f"p95={results[rate]['p95']:.2f}ms  "
              f"p99={results[rate]['p99']:.2f}ms  "
              f"achieved={achieved:.0f}/s")

    return results

# ---------------------------------------------------------------------------
# Main benchmark
# ---------------------------------------------------------------------------

async def main() -> None:
    print(f"\n{'='*60}")
    print(f"  Matching Engine Latency Benchmark")
    print(f"  Target: {URI}")
    print(f"  Orders: {args.orders:,}  Warmup: {args.warmup:,}")
    print(f"{'='*60}\n")

    # -----------------------------------------------------------------------
    # Part 1: Main latency measurement
    # -----------------------------------------------------------------------
    print("[ Part 1 ] Round-trip and fill latency measurement")
    print(f"  Mode: {'max speed' if not args.rate else f'{args.rate}/s'}\n")

    async with websockets.connect(URI, ping_interval=None,
                                  ping_timeout=None,
                                  open_timeout=15) as ws:
        listener_task = asyncio.create_task(listen(ws, args.warmup))

        wall_duration = await send_orders(ws, args.orders, args.rate, args.warmup)

        # Wait for remaining acks (up to 10s grace)
        print("  Waiting for remaining acks…")
        try:
            await asyncio.wait_for(all_done.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            print(f"  Warning: timed out — received {received_acks}/{expected_acks} acks")

        listener_task.cancel()

    achieved_rate = len(ack_latencies) / wall_duration if wall_duration > 0 else 0

    print(f"\n  Wall time:      {wall_duration:.3f}s")
    print(f"  Sent:           {args.orders:,} orders")
    print(f"  Acks received:  {len(ack_latencies):,} (after warmup)")
    print(f"  Fills received: {len(fill_latencies):,}")
    print(f"  Achieved rate:  {achieved_rate:,.0f} orders/sec\n")

    print("  --- Round-trip latency (send → order_ack) ---")
    print_stats("ack", ack_latencies)

    print("\n  --- Fill latency (send → first execution report) ---")
    print_stats("fill", fill_latencies)

    # -----------------------------------------------------------------------
    # Part 2: Throughput sweep
    # -----------------------------------------------------------------------
    print(f"\n[ Part 2 ] Throughput sweep — latency vs. order rate\n")
    sweep_rates = [100, 250, 500, 1000, 2000, 5000]
    print(f"  Testing rates: {sweep_rates} orders/sec")
    print(f"  (500 orders per rate, {len(sweep_rates)} rates total)\n")
    sweep_results = await throughput_sweep(sweep_rates)

    # -----------------------------------------------------------------------
    # Summary table
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"\n  Full benchmark ({args.orders:,} orders at max speed):")
    print(f"    Throughput:  {achieved_rate:,.0f} orders/sec")
    print(f"    p50 latency: {percentile(ack_latencies, 50):.3f} ms")
    print(f"    p95 latency: {percentile(ack_latencies, 95):.3f} ms")
    print(f"    p99 latency: {percentile(ack_latencies, 99):.3f} ms")

    print(f"\n  Throughput curve:")
    print(f"  {'Rate':>8}  {'p50':>8}  {'p95':>8}  {'p99':>8}  {'Achieved':>10}")
    print(f"  {'-'*50}")
    for rate, r in sweep_results.items():
        print(f"  {rate:>8.0f}  {r['p50']:>7.2f}ms  "
              f"{r['p95']:>7.2f}ms  {r['p99']:>7.2f}ms  "
              f"{r['achieved_rate']:>9.0f}/s")

    # Estimate saturation point
    p99_vals = [(rate, r["p99"]) for rate, r in sweep_results.items()
                if r["p99"] == r["p99"]]  # filter nan
    if len(p99_vals) >= 2:
        # Find where p99 first exceeds 5x the lowest p99
        base_p99 = p99_vals[0][1]
        for rate, p99 in p99_vals:
            if p99 > base_p99 * 5:
                print(f"\n  Saturation estimate: ~{rate:.0f} orders/sec "
                      f"(p99 exceeded 5× baseline at this rate)")
                break
        else:
            last_rate = p99_vals[-1][0]
            print(f"\n  No saturation detected up to {last_rate:.0f} orders/sec")

    # -----------------------------------------------------------------------
    # Optional plot
    # -----------------------------------------------------------------------
    if not args.no_plot:
        try:
            import matplotlib.pyplot as plt
            import numpy as np

            fig, axes = plt.subplots(1, 3, figsize=(15, 5))
            fig.patch.set_facecolor("#0d1117")
            for ax in axes:
                ax.set_facecolor("#161b22")
                ax.tick_params(colors="#8b949e")
                for spine in ax.spines.values():
                    spine.set_edgecolor("#30363d")
                ax.grid(True, alpha=0.15, color="#8b949e")

            # Panel 1: Latency histogram
            ax = axes[0]
            if ack_latencies:
                clip = percentile(ack_latencies, 99) * 1.5
                clipped = [x for x in ack_latencies if x <= clip]
                ax.hist(clipped, bins=80, color="#58a6ff", alpha=0.8,
                        edgecolor="#30363d", linewidth=0.3)
                for p, color, label in [
                    (50,  "#3fb950", "p50"),
                    (95,  "#e3b341", "p95"),
                    (99,  "#f85149", "p99"),
                ]:
                    v = percentile(ack_latencies, p)
                    ax.axvline(v, color=color, linewidth=1.5,
                               linestyle="--", label=f"{label}={v:.2f}ms")
                ax.legend(fontsize=8, facecolor="#161b22",
                          edgecolor="#30363d", labelcolor="#e6edf3")
            ax.set_title("Round-trip Latency Distribution",
                         color="#e6edf3", fontsize=10)
            ax.set_xlabel("Latency (ms)", color="#8b949e", fontsize=9)
            ax.set_ylabel("Count", color="#8b949e", fontsize=9)

            # Panel 2: Throughput curve
            ax = axes[1]
            rates_x = list(sweep_results.keys())
            p50s = [sweep_results[r]["p50"] for r in rates_x]
            p95s = [sweep_results[r]["p95"] for r in rates_x]
            p99s = [sweep_results[r]["p99"] for r in rates_x]
            ax.plot(rates_x, p50s, color="#3fb950", marker="o",
                    markersize=5, linewidth=1.5, label="p50")
            ax.plot(rates_x, p95s, color="#e3b341", marker="s",
                    markersize=5, linewidth=1.5, label="p95")
            ax.plot(rates_x, p99s, color="#f85149", marker="^",
                    markersize=5, linewidth=1.5, label="p99")
            ax.legend(fontsize=8, facecolor="#161b22",
                      edgecolor="#30363d", labelcolor="#e6edf3")
            ax.set_title("Latency vs. Throughput",
                         color="#e6edf3", fontsize=10)
            ax.set_xlabel("Orders/sec", color="#8b949e", fontsize=9)
            ax.set_ylabel("Latency (ms)", color="#8b949e", fontsize=9)

            # Panel 3: CDF
            ax = axes[2]
            if ack_latencies:
                sorted_lats = sorted(ack_latencies)
                cdf_y = np.linspace(0, 100, len(sorted_lats))
                ax.plot(sorted_lats, cdf_y, color="#58a6ff", linewidth=1.5,
                        label="Round-trip")
            if fill_latencies:
                sorted_fills = sorted(fill_latencies)
                cdf_y2 = np.linspace(0, 100, len(sorted_fills))
                ax.plot(sorted_fills, cdf_y2, color="#d2a8ff", linewidth=1.5,
                        label="Fill")
            for p, color in [(50,"#3fb950"),(95,"#e3b341"),(99,"#f85149")]:
                ax.axhline(p, color=color, linewidth=0.7,
                           linestyle=":", alpha=0.6)
            ax.legend(fontsize=8, facecolor="#161b22",
                      edgecolor="#30363d", labelcolor="#e6edf3")
            ax.set_title("Latency CDF", color="#e6edf3", fontsize=10)
            ax.set_xlabel("Latency (ms)", color="#8b949e", fontsize=9)
            ax.set_ylabel("Percentile", color="#8b949e", fontsize=9)

            fig.suptitle(
                f"Matching Engine Latency  —  "
                f"{args.orders:,} orders  |  "
                f"p50={percentile(ack_latencies,50):.2f}ms  "
                f"p99={percentile(ack_latencies,99):.2f}ms  |  "
                f"throughput≈{achieved_rate:,.0f}/s",
                color="#e6edf3", fontsize=11)
            plt.tight_layout()
            plt.savefig("latency_results.png", dpi=150,
                        bbox_inches="tight",
                        facecolor=fig.get_facecolor())
            print(f"\n  Chart saved: latency_results.png")
            plt.show()

        except ImportError:
            print("\n  matplotlib not available — skipping plot")


if __name__ == "__main__":
    asyncio.run(main())
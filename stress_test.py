"""
stress_test.py — Multi-agent market simulation with per-agent PnL tracking
==========================================================================

Usage:
    python stress_test.py [k]

    k  — heat-function curvature parameter passed to the C++ engine.
         Defaults to 4.5.  Run multiple times with different k values,
         then use visualize_pnl.py to compare agent PnL across runs.

PnL methodology (mark-to-mid)
------------------------------
For each fill an agent receives:
    BUY fill:   PnL delta = fill_qty * (mid_price - fill_price)
                  (you acquired shares worth mid, paid fill_price)
    SELL fill:  PnL delta = fill_qty * (fill_price - mid_price)
                  (you sold shares worth mid, received fill_price)

mid_price is the best-bid/ask midpoint from the most recent telemetry tick.
This is a standard microstructure measure — it captures adverse selection
(paying above mid on aggressive buys) vs. earning the spread (passive sells
above mid).  It is NOT a realised PnL (we're not netting positions), but it
correctly shows who is being hurt by the matching algorithm at each k.

Output files
------------
  stability_bounds.csv        — telemetry time series
  whale_events.json           — whale sweep timestamps
  pnl_k{k}.csv               — per-agent cumulative PnL at each telemetry tick
"""

import asyncio
import websockets
import json
import random
import csv
import os
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

URI = "ws://localhost:8080/ws"

# ---------------------------------------------------------------------------
# Shared market environment
# ---------------------------------------------------------------------------

class MarketEnvironment:
    def __init__(self, start_price: int = 1000):
        self.fair_price = start_price
        self.volatility = 0.5   # reduced from 1.5 — calmer drift between jumps

    def step(self) -> int:
        # 0.5% chance of a news jump (was 2%) — roughly 1 jump per 3 minutes
        # ±10 ticks (was ±30) — meaningful but not catastrophic
        if random.random() < 0.005:
            self.fair_price += random.choice([-10, 10])
        else:
            self.fair_price += round(np.random.normal(0, self.volatility))
        self.fair_price = max(100, self.fair_price)
        return self.fair_price


env = MarketEnvironment(1000)
whale_events: list[float] = []
sim_start_time: float = 0.0

# Shared mid-price updated from telemetry (thread-safe: asyncio single-threaded)
current_mid: float = 1000.0

# ---------------------------------------------------------------------------
# Global order ID counter
# ---------------------------------------------------------------------------

_global_order_id = 1_000_000

def next_order_id() -> int:
    global _global_order_id
    _global_order_id += 1
    return _global_order_id


# ---------------------------------------------------------------------------
# Base agent
# ---------------------------------------------------------------------------

class BaseAgent:
    """
    Handles connection, dispatch, fill tracking, and mark-to-mid PnL.

    PnL tracking
    ------------
    _order_side[order_id] = 'BUY' | 'SELL'
        Stored at registration so on_execution knows which direction to PnL.

    pnl_history: list[(time_sec, cumulative_pnl)]
        Sampled at each fill event for plotting.
    """

    def __init__(self, name: str, sigma: float = 2.0):
        self.name    = name
        self.sigma   = sigma
        self.ws      = None
        self.running = True

        # Fill accounting
        self.fill_count  = 0
        self.fill_shares = 0
        self.cumulative_pnl = 0.0
        self.pnl_history: list[tuple[float, float]] = []

        # Order tracking
        self._my_order_ids: set[int]       = set()
        self._pending_qty:  dict[int, int] = {}
        self._order_side:   dict[int, str] = {}   # 'BUY' or 'SELL'

    def get_perceived_price(self) -> int:
        return max(1, round(np.random.normal(env.fair_price, self.sigma)))

    def _register_order(self, order_id: int, qty: int, side: str) -> None:
        self._my_order_ids.add(order_id)
        self._pending_qty[order_id]  = qty
        self._order_side[order_id]   = side

    def _unregister_order(self, order_id: int) -> None:
        self._my_order_ids.discard(order_id)
        self._pending_qty.pop(order_id, None)
        self._order_side.pop(order_id, None)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        for attempt in range(5):
            try:
                self.ws = await websockets.connect(
                    URI, ping_interval=None, ping_timeout=None, open_timeout=15
                )
                print(f"[{self.name}] Connected (attempt {attempt + 1})")
                asyncio.create_task(self.listen())
                return
            except Exception as exc:
                wait = 1.0 * (attempt + 1)
                print(f"[{self.name}] Attempt {attempt + 1} failed: {exc}. Retry in {wait:.1f}s")
                await asyncio.sleep(wait)
        print(f"[{self.name}] Could not connect — giving up.")
        self.running = False

    async def listen(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    await self._dispatch(json.loads(raw))
                except (json.JSONDecodeError, KeyError):
                    pass
        except websockets.ConnectionClosed:
            print(f"[{self.name}] Connection closed.")
            self.running = False

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, msg: dict) -> None:
        t = msg.get("type", "")
        if   t == "execution":  await self.on_execution(msg)
        elif t == "order_ack":  await self.on_order_ack(msg)
        elif t == "cancel_ack": await self.on_cancel_ack(msg)
        elif t == "telemetry":  await self.on_telemetry(msg)
        elif t == "cancelled":  await self.on_cancelled(msg)
        elif t == "error":
            print(f"[{self.name}] Server error: {msg.get('msg')}")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def on_execution(self, msg: dict) -> None:
        order_id = msg.get("order_id")
        qty      = msg.get("qty", 0)
        price    = msg.get("price", 0)
        mid      = msg.get("mid", None)   # server-provided mid at match time

        if order_id not in self._my_order_ids:
            return

        self.fill_count  += 1
        self.fill_shares += qty

        side = self._order_side.get(order_id)
        if side is None:
            pass
        else:
            if mid is None:
                mid = current_mid   # fallback for old server builds

            pnl_delta = qty * (mid - price) if side == "BUY" else qty * (price - mid)

            # DEBUG — print first 5 fills per agent so we can verify sign/values
            if self.fill_count <= 5:
                print(f"[DEBUG {self.name}] fill={self.fill_count} "
                      f"side={side} price={price} mid={mid:.1f} "
                      f"qty={qty} pnl_delta={pnl_delta:+.2f} "
                      f"mid_src={'server' if msg.get('mid') is not None else 'stale'}")

            self.cumulative_pnl += pnl_delta
            t = time.time() - sim_start_time
            self.pnl_history.append((t, self.cumulative_pnl))

        # Qty tracking
        if order_id in self._pending_qty:
            self._pending_qty[order_id] -= qty
            if self._pending_qty[order_id] <= 0:
                self._unregister_order(order_id)

    async def on_order_ack(self, msg: dict) -> None:
        if not msg.get("success", False):
            self._unregister_order(msg.get("id"))

    async def on_cancel_ack(self, msg: dict) -> None:
        self._unregister_order(msg.get("id"))

    async def on_cancelled(self, msg: dict) -> None:
        self._unregister_order(msg.get("id"))

    async def on_telemetry(self, msg: dict) -> None:
        # Update global mid-price from telemetry
        global current_mid
        best_bid = msg.get("best_bid", 0)
        best_ask = msg.get("best_ask", 0)
        if best_bid and best_ask and best_ask < 10**15:
            current_mid = (best_bid + best_ask) / 2.0

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send_order(
        self,
        action:     str,
        order_type: str,
        side:       str,
        price:      int,
        qty:        int,
        order_id:   int | None = None,
    ) -> int:
        if not self.ws or not self.running:
            return -1
        if order_id is None:
            order_id = next_order_id()

        self._register_order(order_id, qty, side)

        payload = {
            "action": action,
            "type":   order_type,
            "side":   side,
            "price":  int(price),
            "qty":    int(qty),
            "id":     order_id,
        }
        await self.ws.send(json.dumps(payload))
        return order_id

    async def send_cancel(self, order_id: int) -> None:
        if not self.ws or not self.running:
            return
        await self.ws.send(json.dumps({"action": "cancel", "id": order_id}))

    async def run(self) -> None:
        pass

    async def _deferred_unregister(self, order_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        self._unregister_order(order_id)


# ---------------------------------------------------------------------------
# DataLogger
# ---------------------------------------------------------------------------

class DataLogger(BaseAgent):
    def __init__(self, name: str):
        super().__init__(name)
        self.csv_file   = open("stability_bounds.csv", "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow([
            "timestamp", "S", "L_eff", "H_c", "H_p",
            "avg_age", "best_bid", "best_ask", "spread",
            "bid_levels", "ask_levels", "fifo_share", "k",
        ])

    async def on_telemetry(self, msg: dict) -> None:
        await super().on_telemetry(msg)   # updates current_mid
        best_bid = msg.get("best_bid", 0)
        best_ask = msg.get("best_ask", 0)
        spread   = (best_ask - best_bid) if (best_bid and best_ask and best_ask < 10**15) else 0
        self.csv_writer.writerow([
            msg.get("timestamp"),
            msg.get("S", 0.0),
            msg.get("L_eff", 0.0),
            msg.get("H_c", 0.0),
            msg.get("H_p", 0.0),
            msg.get("avg_fill_age_ms", 0.0),
            best_bid,
            best_ask,
            spread,
            msg.get("bid_levels", 0),
            msg.get("ask_levels", 0),
            msg.get("fifo_share", 1.0),
            msg.get("k", 4.5),
        ])
        self.csv_file.flush()

    async def run(self) -> None:
        while self.running:
            await asyncio.sleep(1)

    def close(self) -> None:
        self.csv_file.close()


# ---------------------------------------------------------------------------
# MarketMaker
# ---------------------------------------------------------------------------

class MarketMaker(BaseAgent):
    HALF_SPREAD = 2
    MAX_LIVE    = 6

    def __init__(self, name: str, sigma: float = 1.0):
        super().__init__(name, sigma)
        self.live_ids: deque[int] = deque()

    async def on_order_ack(self, msg: dict) -> None:
        await super().on_order_ack(msg)
        if msg.get("success"):
            self.live_ids.append(msg["id"])

    async def on_execution(self, msg: dict) -> None:
        await super().on_execution(msg)
        order_id = msg.get("order_id")
        if order_id not in self._my_order_ids:   # fully filled
            try:
                self.live_ids.remove(order_id)
            except ValueError:
                pass

    async def on_cancel_ack(self, msg: dict) -> None:
        await super().on_cancel_ack(msg)
        try:
            self.live_ids.remove(msg.get("id"))
        except ValueError:
            pass

    async def _cancel_stale(self) -> None:
        while len(self.live_ids) >= self.MAX_LIVE:
            old_id = self.live_ids.popleft()
            self._unregister_order(old_id)
            await self.send_cancel(old_id)
            await asyncio.sleep(0.02)

    async def run(self) -> None:
        while self.running:
            await self._cancel_stale()
            p = self.get_perceived_price()

            await self.send_order("new_order", "LIMIT", "BUY",
                                  p - self.HALF_SPREAD, 500)
            await asyncio.sleep(0.01)
            await self.send_order("new_order", "LIMIT", "SELL",
                                  p + self.HALF_SPREAD, 500)

            # live_ids is populated by on_order_ack when server confirms.
            # Do NOT append here — that would add each ID twice.
            await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# RetailTrader
# ---------------------------------------------------------------------------

class RetailTrader(BaseAgent):
    async def run(self) -> None:
        while self.running:
            await asyncio.sleep(random.uniform(1.0, 3.0))
            p    = self.get_perceived_price()
            side = random.choice(["BUY", "SELL"])

            if random.random() < 0.05:
                price = p + 20 if side == "BUY" else p - 20
                qty   = random.randint(100, 500)
                await self.send_order("new_order", "LIMIT", side, price, qty)
            else:
                qty = random.randint(1, 20)
                # Market orders fill immediately or not at all — use a longer
                # grace window (500ms) so execution reports always arrive first.
                oid = await self.send_order("new_order", "MARKET", side, 0, qty)
                asyncio.create_task(self._deferred_unregister(oid, delay=0.5))


# ---------------------------------------------------------------------------
# HFTSniper
# ---------------------------------------------------------------------------

class HFTSniper(BaseAgent):
    """
    Posts 1-lot limit orders ±1 tick at high frequency.
    Tracks live order IDs explicitly and cancels stale ones before
    re-quoting — same pattern as MarketMaker.  This ensures fills
    are never silently dropped by a deferred_unregister that fires
    before the execution report arrives.
    """
    MAX_LIVE = 4   # cancel oldest before posting new when at this limit

    def __init__(self, name: str, sigma: float = 3.0):
        super().__init__(name, sigma)
        self.live_ids: deque[int] = deque()

    async def on_order_ack(self, msg: dict) -> None:
        await super().on_order_ack(msg)
        if msg.get("success"):
            self.live_ids.append(msg["id"])

    async def on_execution(self, msg: dict) -> None:
        await super().on_execution(msg)
        order_id = msg.get("order_id")
        if order_id not in self._my_order_ids:   # fully filled
            try:
                self.live_ids.remove(order_id)
            except ValueError:
                pass

    async def on_cancel_ack(self, msg: dict) -> None:
        await super().on_cancel_ack(msg)
        try:
            self.live_ids.remove(msg.get("id"))
        except ValueError:
            pass

    async def _cancel_stale(self) -> None:
        while len(self.live_ids) >= self.MAX_LIVE:
            old_id = self.live_ids.popleft()
            self._unregister_order(old_id)
            await self.send_cancel(old_id)
            await asyncio.sleep(0.01)

    async def run(self) -> None:
        while self.running:
            await self._cancel_stale()
            p = self.get_perceived_price()
            bid_id = await self.send_order("new_order", "LIMIT", "BUY",  p - 1, 1)
            ask_id = await self.send_order("new_order", "LIMIT", "SELL", p + 1, 1)
            self.live_ids.append(bid_id)
            self.live_ids.append(ask_id)
            await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# WhaleTrader
# ---------------------------------------------------------------------------

class WhaleTrader(BaseAgent):
    async def run(self) -> None:
        while self.running:
            await asyncio.sleep(random.uniform(10.0, 20.0))
            side = random.choice(["BUY", "SELL"])
            ts   = time.time() - sim_start_time
            print(f"[{self.name}] !!! WHALE SWEEP {side} at t={ts:.1f}s !!!")
            whale_events.append(ts)
            oid = await self.send_order("new_order", "MARKET", side, 0, 3000)
            asyncio.create_task(self._deferred_unregister(oid, delay=2.0))


# ---------------------------------------------------------------------------
# Simulation driver
# ---------------------------------------------------------------------------

WARMUP_SECONDS = 4

async def price_driver() -> None:
    while True:
        env.step()
        await asyncio.sleep(0.1)


async def main(k_value: float, include_whale: bool = True, mode: str = "hybrid") -> None:
    global sim_start_time
    sim_start_time = time.time()

    logger  = DataLogger("Telemetry")
    makers  = [MarketMaker(f"MM_{i}",      sigma=1)  for i in range(2)]
    retail  = [RetailTrader(f"Retail_{i}", sigma=10) for i in range(5)]
    snipers = [HFTSniper(f"HFT_{i}",       sigma=3)  for i in range(3)]
    whale   = WhaleTrader("Whale", sigma=5)

    asyncio.create_task(price_driver())

    print("=== Phase 1: Warmup ===")
    await logger.connect()
    asyncio.create_task(logger.run())
    for mm in makers:
        await mm.connect()
        asyncio.create_task(mm.run())
        await asyncio.sleep(0.3)
    print(f"  Waiting {WARMUP_SECONDS}s for quotes to accumulate…")
    await asyncio.sleep(WARMUP_SECONDS)

    print("=== Phase 2: Simulation Running ===")
    aggressive = retail + snipers + ([whale] if include_whale else [])
    for agent in aggressive:
        await agent.connect()
        asyncio.create_task(agent.run())
        await asyncio.sleep(0.15)

    await asyncio.sleep(60)

    print("=== Simulation Complete ===")
    logger.close()
    with open("whale_events.json", "w") as f:
        json.dump(whale_events, f)

    # ------------------------------------------------------------------
    # Save per-agent PnL time series for this k run
    # ------------------------------------------------------------------
    k_str = str(k_value).replace(".", "_")
    pnl_filename = f"pnl_k{k_str}.csv"
    all_agents = makers + retail + snipers + ([whale] if include_whale else [])

    with open(pnl_filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["agent", "role", "time_sec", "cumulative_pnl"])
        for agent in all_agents:
            role = (
                "MarketMaker" if isinstance(agent, MarketMaker) else
                "RetailTrader" if isinstance(agent, RetailTrader) else
                "HFTSniper"   if isinstance(agent, HFTSniper)   else
                "Whale"
            )
            for t, pnl in agent.pnl_history:
                writer.writerow([agent.name, role, round(t, 3), round(pnl, 4)])

    print(f"PnL data saved to {pnl_filename}")
    print("Telemetry saved to stability_bounds.csv")

    print("\n--- Fill summary (own orders only) ---")
    for agent in all_agents:
        avg = (agent.fill_shares / agent.fill_count) if agent.fill_count else 0
        print(f"  {agent.name:15s}: {agent.fill_count:5d} fills, "
              f"{agent.fill_shares:8d} shares, avg {avg:6.1f} shs/fill, "
              f"PnL {agent.cumulative_pnl:+.1f}")

    # ------------------------------------------------------------------
    # Write one-row experiment summary for the k-sweep runner
    # ------------------------------------------------------------------
    try:
        import pandas as _pd
        tel_df     = _pd.read_csv("stability_bounds.csv")
        avg_s      = float(tel_df["S"].mean())
        std_s      = float(tel_df["S"].std())
        avg_Leff   = float(tel_df["L_eff"].mean())
        avg_Hc     = float(tel_df["H_c"].mean())
        avg_Hp     = float(tel_df["H_p"].mean())
        avg_spread = float(tel_df["spread"].replace(0, float("nan")).mean())                      if "spread" in tel_df.columns else float("nan")
    except Exception as e:
        print(f"Warning: could not read telemetry for summary ({e})")
        avg_s = std_s = avg_Leff = avg_Hc = avg_Hp = avg_spread = float("nan")

    def _role_pnl(role_name):
        agents = [a for a in all_agents if type(a).__name__ == role_name]
        if not agents: return float("nan")
        return sum(a.cumulative_pnl for a in agents) / len(agents)

    summary_row = {
        "mode":       mode,
        "k":          k_value,
        "whale":      include_whale,
        "avg_S":      round(avg_s,    4),
        "std_S":      round(std_s,    4),
        "avg_L_eff":  round(avg_Leff, 4),
        "avg_H_c":    round(avg_Hc,   4),
        "avg_H_p":    round(avg_Hp,   4),
        "avg_spread": round(avg_spread, 4) if avg_spread == avg_spread else float("nan"),
        "mm_pnl":     round(_role_pnl("MarketMaker"),  2),
        "hft_pnl":    round(_role_pnl("HFTSniper"),    2),
        "retail_pnl": round(_role_pnl("RetailTrader"), 2),
        "whale_pnl":  round(_role_pnl("WhaleTrader"),  2) if include_whale else float("nan"),
    }

    summary_file = "experiment_results.csv"
    file_exists  = os.path.exists(summary_file)
    with open(summary_file, "a", newline="") as sf:
        writer = csv.DictWriter(sf, fieldnames=list(summary_row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(summary_row)
    print(f"Summary appended to {summary_file}")


if __name__ == "__main__":
    k_arg      = 4.5
    whale_arg  = True
    mode_arg   = "hybrid"

    args = sys.argv[1:]
    for a in args:
        if a == "--no-whale":
            whale_arg = False
        elif a == "--fifo":
            mode_arg = "fifo"
        elif a == "--prorata":
            mode_arg = "prorata"
        else:
            try:
                k_arg = float(a)
            except ValueError:
                pass

    print(f"Running simulation: mode={mode_arg}  k={k_arg}  whale={whale_arg}")
    asyncio.run(main(k_arg, include_whale=whale_arg, mode=mode_arg))
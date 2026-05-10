"""
stress_test.py — Multi-agent market simulation
==============================================

Key fix in this version
------------------------
Every agent now maintains:
  _my_order_ids  : set[int]  — IDs the agent has submitted (registered before
                               the ack arrives, because a fast match can deliver
                               an execution report before the ack)
  _pending_qty   : dict[int,int] — remaining qty on each live resting order

on_execution only logs a fill when the reported order_id is in _my_order_ids.
When an order is fully filled (pending_qty reaches 0) it is removed from the
tracking set so the set stays small.

This replaces the previous design where every agent received and credited the
entire global execution tape.
"""

import asyncio
import websockets
import json
import random
import csv
import time
from collections import deque

import numpy as np

URI = "ws://localhost:8080/ws"

# ---------------------------------------------------------------------------
# Shared market environment
# ---------------------------------------------------------------------------

class MarketEnvironment:
    def __init__(self, start_price: int = 1000):
        self.fair_price = start_price
        self.volatility = 1.5

    def step(self) -> int:
        if random.random() < 0.02:
            self.fair_price += random.choice([-30, 30])
        else:
            self.fair_price += round(np.random.normal(0, self.volatility))
        self.fair_price = max(100, self.fair_price)
        return self.fair_price


env = MarketEnvironment(1000)
whale_events: list[float] = []
sim_start_time: float = 0.0

# ---------------------------------------------------------------------------
# Global order ID counter (client-side)
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
    Connection + message dispatch + per-agent fill tracking.

    Fill tracking design
    --------------------
    _my_order_ids  : set[int]
        All order IDs this agent has ever submitted.  Populated *before* the
        network send so that a fill arriving before the ack is still caught.

    _pending_qty   : dict[int, int]
        Remaining unfilled quantity for each resting order.  Initialised when
        the order is submitted and decremented on each execution report.  When
        it hits 0 the ID is removed from _my_order_ids (the order is gone).

    on_execution() only credits a fill when order_id in _my_order_ids.
    """

    def __init__(self, name: str, sigma: float = 2.0):
        self.name    = name
        self.sigma   = sigma
        self.ws      = None
        self.running = True

        # Per-agent fill accounting
        self.fill_count  = 0
        self.fill_shares = 0

        # Order tracking — populated before send, cleaned up after full fill
        self._my_order_ids: set[int]       = set()
        self._pending_qty:  dict[int, int] = {}

    def get_perceived_price(self) -> int:
        return max(1, round(np.random.normal(env.fair_price, self.sigma)))

    # ------------------------------------------------------------------
    # ID registration — call BEFORE sending the order over the wire
    # ------------------------------------------------------------------

    def _register_order(self, order_id: int, qty: int) -> None:
        self._my_order_ids.add(order_id)
        self._pending_qty[order_id] = qty

    def _unregister_order(self, order_id: int) -> None:
        self._my_order_ids.discard(order_id)
        self._pending_qty.pop(order_id, None)

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
    # Message dispatch
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
        """
        Credit a fill only if the order_id belongs to this agent.

        The server broadcasts two execution reports per match — one for the
        incoming order ID, one for the resting order ID.  Only one of them
        should be in _my_order_ids for any given agent.
        """
        order_id = msg.get("order_id")
        qty      = msg.get("qty", 0)

        if order_id not in self._my_order_ids:
            return  # belongs to a different agent

        self.fill_count  += 1
        self.fill_shares += qty

        # Track remaining quantity; remove when fully filled
        if order_id in self._pending_qty:
            self._pending_qty[order_id] -= qty
            if self._pending_qty[order_id] <= 0:
                self._unregister_order(order_id)

    async def on_order_ack(self, msg: dict) -> None:
        """If the server rejected the order, stop tracking it."""
        if not msg.get("success", False):
            self._unregister_order(msg.get("id"))

    async def on_cancel_ack(self, msg: dict) -> None:
        self._unregister_order(msg.get("id"))

    async def on_cancelled(self, msg: dict) -> None:
        """IOC / market remainder — order is gone."""
        self._unregister_order(msg.get("id"))

    async def on_telemetry(self, msg: dict) -> None:
        pass

    # ------------------------------------------------------------------
    # Sending helpers
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
        """
        Register the order ID locally BEFORE sending so a fast execution
        report that races ahead of the ack is still attributed correctly.
        Returns the order ID used.
        """
        if not self.ws or not self.running:
            return -1
        if order_id is None:
            order_id = next_order_id()

        self._register_order(order_id, qty)  # register before wire send

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


# ---------------------------------------------------------------------------
# DataLogger — telemetry only, no order tracking needed
# ---------------------------------------------------------------------------

class DataLogger(BaseAgent):
    def __init__(self, name: str):
        super().__init__(name)
        self.csv_file   = open("stability_bounds.csv", "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow([
            "timestamp", "S", "L_eff", "H_c", "H_p",
            "avg_age", "best_bid", "best_ask", "spread",
            "bid_levels", "ask_levels",
        ])

    async def on_telemetry(self, msg: dict) -> None:
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
        # deque of resting IDs for stale-quote management
        self.live_ids: deque[int] = deque()

    async def on_order_ack(self, msg: dict) -> None:
        await super().on_order_ack(msg)
        if msg.get("success"):
            self.live_ids.append(msg["id"])

    async def on_execution(self, msg: dict) -> None:
        order_id = msg.get("order_id")
        # super() handles fill count + _pending_qty; if order is now gone,
        # also remove from live_ids
        await super().on_execution(msg)
        if order_id not in self._my_order_ids:  # fully filled → clean live_ids
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
            self._unregister_order(old_id)  # stop tracking before ack arrives
            await self.send_cancel(old_id)
            await asyncio.sleep(0.02)

    async def run(self) -> None:
        while self.running:
            await self._cancel_stale()
            p = self.get_perceived_price()

            bid_id = await self.send_order("new_order", "LIMIT", "BUY",
                                           p - self.HALF_SPREAD, 500)
            await asyncio.sleep(0.01)
            ask_id = await self.send_order("new_order", "LIMIT", "SELL",
                                           p + self.HALF_SPREAD, 500)

            # send_order already registered them; add to live_ids for cancel mgmt
            self.live_ids.append(bid_id)
            self.live_ids.append(ask_id)

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
                # Rare large marketable limit — aggressive momentum order
                price = p + 20 if side == "BUY" else p - 20
                qty   = random.randint(100, 500)
                await self.send_order("new_order", "LIMIT", side, price, qty)
            else:
                qty = random.randint(1, 20)
                oid = await self.send_order("new_order", "MARKET", side, 0, qty)
                # Market orders don't rest; clean up after fills can arrive
                asyncio.create_task(self._deferred_unregister(oid, delay=0.15))

    async def _deferred_unregister(self, order_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        self._unregister_order(order_id)


# ---------------------------------------------------------------------------
# HFTSniper
# ---------------------------------------------------------------------------

class HFTSniper(BaseAgent):
    async def run(self) -> None:
        while self.running:
            p = self.get_perceived_price()
            bid_id = await self.send_order("new_order", "LIMIT", "BUY",  p - 1, 1)
            ask_id = await self.send_order("new_order", "LIMIT", "SELL", p + 1, 1)

            # 1-lot orders fill almost instantly or become stale; give 200ms
            asyncio.create_task(self._deferred_unregister(bid_id, 0.2))
            asyncio.create_task(self._deferred_unregister(ask_id, 0.2))

            await asyncio.sleep(0.05)

    async def _deferred_unregister(self, order_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        self._unregister_order(order_id)


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
            asyncio.create_task(self._deferred_unregister(oid, delay=0.5))

    async def _deferred_unregister(self, order_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        self._unregister_order(order_id)


# ---------------------------------------------------------------------------
# Simulation driver
# ---------------------------------------------------------------------------

WARMUP_SECONDS = 4

async def price_driver() -> None:
    while True:
        env.step()
        await asyncio.sleep(0.1)


async def main() -> None:
    global sim_start_time
    sim_start_time = time.time()

    logger  = DataLogger("Telemetry")
    makers  = [MarketMaker(f"MM_{i}",      sigma=1)  for i in range(2)]
    retail  = [RetailTrader(f"Retail_{i}", sigma=10) for i in range(5)]
    snipers = [HFTSniper(f"HFT_{i}",       sigma=3)  for i in range(3)]
    whale   = WhaleTrader("Whale", sigma=5)

    asyncio.create_task(price_driver())

    # Phase 1: seed the book
    print("=== Phase 1: Warmup ===")
    await logger.connect()
    asyncio.create_task(logger.run())
    for mm in makers:
        await mm.connect()
        asyncio.create_task(mm.run())
        await asyncio.sleep(0.3)
    print(f"  Waiting {WARMUP_SECONDS}s for quotes to accumulate…")
    await asyncio.sleep(WARMUP_SECONDS)

    # Phase 2: aggressive agents
    print("=== Phase 2: Simulation Running ===")
    for agent in retail + snipers + [whale]:
        await agent.connect()
        asyncio.create_task(agent.run())
        await asyncio.sleep(0.15)

    await asyncio.sleep(60)

    print("=== Simulation Complete ===")
    logger.close()
    with open("whale_events.json", "w") as f:
        json.dump(whale_events, f)
    print("Data saved to stability_bounds.csv and whale_events.json")

    all_agents = makers + retail + snipers + [whale]
    print("\n--- Fill summary (own orders only) ---")
    for agent in all_agents:
        avg = (agent.fill_shares / agent.fill_count) if agent.fill_count else 0
        print(f"  {agent.name:15s}: {agent.fill_count:5d} fills, "
              f"{agent.fill_shares:8d} shares, avg {avg:6.1f} shares/fill")


if __name__ == "__main__":
    asyncio.run(main())
"""
visualize.py — Unified post-simulation dashboard
=================================================

Produces two saved figures from a single run:

  simulation_dashboard.png   — 6-panel market microstructure view
  pnl_comparison.png         — per-agent cumulative PnL across k values

Usage
-----
    # After running stress_test.py (one or more k values):
    python visualize.py

Data files expected in the working directory
--------------------------------------------
  stability_bounds.csv   — telemetry from the most recent run (always present)
  whale_events.json      — whale sweep timestamps (optional)
  pnl_k*.csv            — one per k value, e.g. pnl_k4_5.csv, pnl_k3_0.csv
                           Auto-detected by glob; at least one required for the
                           PnL figure.  If none found, that figure is skipped.

--------------------------------------------------------------------------------
WHAT THE CODE DOES — end-to-end overview
--------------------------------------------------------------------------------

C++ matching engine (order_book.cpp / main.cpp)
  A limit order book implemented as two std::map<Price, PriceLevel> — one for
  bids (descending) and one for asks (ascending).  Each PriceLevel holds a
  doubly-linked list of resting orders.

  Allocation modes
    FIFO      Oldest order at a price level fills completely before the next
              one gets anything.  Rewards patience and queue position.
    PRO_RATA  Every resting order fills in proportion to its size.  Rewards
              large orders; queue position doesn't matter.
    HYBRID    A blend whose FIFO fraction is determined by the heat function
              (see below).  The book is more FIFO when stable, more pro-rata
              when stressed.

  Heat function  f(x) = 50/(e^k - 1) · (e^(kx/3000) - 1) + 50
    x = clamp(stability S, 0, 3000)
    Maps S ∈ [0, 3000] → FIFO% ∈ [50%, 100%].

    k controls curvature:
      k ≈ 0  : linear — FIFO% rises steadily as stability improves
      k = 3  : moderately convex — FIFO% stays near 50% until S > ~1500,
               then climbs quickly
      k = 4.5: strongly convex — market must be very stable to earn >60% FIFO
      k large: step-like — essentially pure pro-rata until stability is
               very high, then switches to pure FIFO

    Economic interpretation:  high k means the engine behaves like a pro-rata
    market most of the time, only granting FIFO priority during calm periods.
    This redistributes fill probability from large queue-position holders (MMs,
    HFT) toward smaller orders placed at any time (retail).

  Stability metric  S = L_eff / (H_c + H_p + 1)
    L_eff  Effective liquidity — sum of resting volume at the top N price
           levels on each side, decay-weighted by distance from best price
           and divided by sqrt(order_count) to penalise thin, fragmented books.
    H_c    Cancel heat — increments by 1 on every cancel, decays by 0.95 per
           10 ms tick.  High H_c means many orders are being withdrawn, which
           typically signals adverse selection or a stale-quote purge.
    H_p    Price heat — increments by |Δmidpoint| on every midpoint move, same
           decay.  High H_p means the fair value is shifting rapidly (news,
           whale sweep).
    The denominator (H_c + H_p + 1) grows when the market is stressed, driving
    S toward 0.  The +1 prevents division-by-zero when both heats are zero.

  Two-phase hybrid allocation (FIFO then pro-rata)
    1. fifo_target  = round(fill_qty × fifo_share)      ← from heat function
    2. allocateFifo(orders, fifo_target)                 ← front-to-back
    3. residual_qty per order = original_qty − fifo_fill
    4. allocateProRata(residuals, fill_qty − fifo_target) ← proportional

    Pro-rata integer rounding: base fill = floor(target × order_qty / total_qty).
    Remainder shares (from fractional truncation) are distributed one-by-one to
    the orders with the largest fractional remainder, with FIFO rank as tiebreak.
    This is the standard exchange algorithm — no share is ever lost or invented.

Python stress_test.py
  Runs N simulated agents over WebSocket against the C++ engine:
    MarketMaker   Posts passive two-sided quotes ±2 ticks around perceived mid.
                  Cancels stale orders (tracks live IDs).  Earns the spread
                  on passive fills; loses when adversely selected.
    RetailTrader  Sends small random market orders.  Occasionally sends a
                  large marketable limit (momentum trade).  Pays the spread
                  on aggressive fills.
    HFTSniper     Posts 1-lot limit orders ±1 tick at high frequency (50 ms).
                  Tries to be inside the MM spread; fills are tiny but frequent.
    WhaleTrader   Sends 3000-lot market orders every 10–20 s.  Each sweep
                  clears multiple price levels, causing spread to widen, S to
                  drop, and H_p to spike.

  Mark-to-mid PnL  (per fill)
    BUY:  PnL += fill_qty × (mid_price − fill_price)
    SELL: PnL += fill_qty × (fill_price − mid_price)
    mid_price is the (best_bid + best_ask) / 2 from the most recent telemetry.

    This is an instantaneous adverse-selection measure, NOT realised PnL.
    Positive → the agent received a price better than fair value (passive fills,
               or aggressive buys below mid).
    Negative → the agent paid away from fair value (crossing the spread to buy,
               or selling into a falling market).

--------------------------------------------------------------------------------
WHAT THE GRAPHS MEAN
--------------------------------------------------------------------------------

Simulation dashboard (Figure 1)
  Panel 1 — Stability S
    High S = deep, calm book.  Dips sharply at whale sweeps (L_eff collapses
    and H_p spikes simultaneously, so both numerator and denominator move
    against S).  Recovery speed shows how quickly MMs re-quote.

  Panel 2 — Effective Liquidity L_eff
    Volume-weighted depth of the top levels, discounted by distance from best
    price.  Drops after sweeps, grows during quiet MM quoting periods.
    The slow decay in the middle of your run reflects MMs' stale-quote
    cancellation draining the book before re-quoting at new prices.

  Panel 3 — Heat variables H_c and H_p
    H_c spikes when MMs cancel stale quotes en masse after a price move.
    H_p spikes directly at whale sweeps (midpoint jumps).  The two heats
    are often correlated — a price move triggers cancellations.
    Both decay at 0.95 per 10 ms → half-life ≈ 130 ms.

  Panel 4 — Avg fill age
    How long (in ms) resting orders waited before being filled, averaged over
    all fills so far.  This is a FIFO efficiency metric: in pure FIFO the
    oldest order fills first, so the average age grows as volume builds up.
    A rising trend means older orders are being filled (good FIFO behaviour).
    A flat trend means fills are spread across ages (pro-rata-like).

  Panel 5 — Bid-ask spread
    best_ask − best_bid in ticks.  Normal spread = 4 ticks (MMs quote ±2).
    Spikes at whale sweeps when the best levels are wiped and the next resting
    quote is further away.  Recovery time measures MM re-quoting speed.

  Panel 6 — Book depth (price level count)
    Number of distinct price levels on each side.  Grows as MMs and HFT post
    quotes at many different prices.  Drops at sweeps (levels consumed), then
    grows again.  A large, asymmetric depth (many bid levels, few ask levels)
    suggests directional pressure from recent flow.

Agent PnL comparison (Figure 2)
  Each row is one agent role; each column is one k value.
  The curves show cumulative mark-to-mid PnL over 60 seconds.

  Market Makers
    Should be positive on average — they earn the half-spread on every passive
    fill.  The asymmetry between MM_0 and MM_1 reflects queue position and
    timing luck.  At high k (more FIFO), the first MM to post at a price level
    earns a larger fraction of fills, so the leading MM PnL is higher.  At low k
    (more pro-rata), fills are shared more evenly across all MMs.

  Retail Traders
    Typically negative — they mostly send market orders, paying the spread.
    Occasional large limit orders that fill passively can produce positive blips.
    Less sensitive to k because they rarely compete for queue position.

  HFT Snipers
    Consistently negative because they post 1-lot orders ±1 tick inside the MM
    spread.  When they fill passively they earn 1 tick, but they also
    frequently get adversely selected (filled on the wrong side of a price move).
    At high k, their tiny orders are at a queue disadvantage behind larger MM
    orders at the same price; at low k, they get a proportional share.

  Whale
    Large negative PnL — each 3000-lot market sweep crosses the spread (paying
    several ticks per share on average), hence a large one-time loss at each
    dashed vertical line.  The whale doesn't care about microstructure efficiency;
    its cost shows the true market impact of aggressive size.

  How k shifts the distribution
    Increasing k → more pro-rata in stressed conditions → MMs share fills more
    evenly → leader MM PnL decreases, lagging MM PnL increases → HFT orders get
    a fair share proportional to their size (but size is tiny, so it doesn't help
    much) → retail fills are unaffected (they're mostly market orders anyway).

    Decreasing k (more FIFO) → queue position dominates → the first MM to post
    at the best price captures most of the passive fill flow → stronger
    incentive to quote aggressively, which tightens spread → better for retail
    (tighter spread they cross) but higher adverse selection risk for late MMs.
"""

import json
import sys
from pathlib import Path

import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ===========================================================================
# Shared helpers
# ===========================================================================

WHALE_COLOR = "#f78166"
GRID_ALPHA  = 0.15
LW          = 1.2   # default line width

def style_axes(axes_list):
    for ax in axes_list:
        ax.set_facecolor("#161b22")
        ax.tick_params(colors="#8b949e", labelsize=8)
        ax.yaxis.label.set_color("#8b949e")
        ax.xaxis.label.set_color("#8b949e")
        ax.title.set_color("#e6edf3")
        for spine in ax.spines.values():
            spine.set_edgecolor("#30363d")

def add_whale_lines(ax, whale_times):
    for t in whale_times:
        ax.axvline(t, color=WHALE_COLOR, linestyle="--", linewidth=0.9, alpha=0.7)

# ---------------------------------------------------------------------------
# Load shared data files
# ---------------------------------------------------------------------------

# Telemetry
try:
    tel = pd.read_csv("stability_bounds.csv")
    if tel.empty:
        raise ValueError("empty")
    tel["time_sec"] = (tel["timestamp"] - tel["timestamp"].iloc[0]) / 1000.0
    has_telemetry = True
except (FileNotFoundError, ValueError) as e:
    print(f"Warning: stability_bounds.csv not loaded ({e}). Dashboard will be skipped.")
    has_telemetry = False
    tel = pd.DataFrame()

# Whale events
whale_times: list[float] = []
try:
    with open("whale_events.json") as f:
        whale_times = json.load(f)
    print(f"Loaded {len(whale_times)} whale event(s).")
except FileNotFoundError:
    print("whale_events.json not found — whale annotations skipped.")

# PnL files (auto-detect pnl_k*.csv in working dir, or take CLI args)
if len(sys.argv) >= 2:
    pnl_files = [Path(p) for p in sys.argv[1:]]
else:
    pnl_files = sorted(Path(".").glob("pnl_k*.csv"))

pnl_data: dict[str, pd.DataFrame] = {}
for f in pnl_files:
    if not f.exists():
        print(f"Warning: {f} not found — skipping.")
        continue
    df_pnl = pd.read_csv(f)
    stem    = f.stem   # e.g. pnl_k4_5
    k_label = "k=" + stem.replace("pnl_k", "").replace("_", ".")
    pnl_data[k_label] = df_pnl
    print(f"Loaded PnL file: {f}  ({k_label})")

has_pnl = len(pnl_data) > 0
if not has_pnl:
    print("No pnl_k*.csv files found — PnL figure will be skipped.")

if not has_telemetry and not has_pnl:
    print("Nothing to plot. Run stress_test.py first.")
    sys.exit(1)


# ===========================================================================
# FIGURE 1 — Simulation Dashboard
# ===========================================================================

if has_telemetry:
    fig1, axes1 = plt.subplots(6, 1, figsize=(14, 20), sharex=True)
    fig1.patch.set_facecolor("#0d1117")
    style_axes(axes1)

    # -----------------------------------------------------------------------
    # Panel 1 — Stability S
    # -----------------------------------------------------------------------
    ax = axes1[0]
    ax.plot(tel["time_sec"], tel["S"], color="#58a6ff", linewidth=LW)
    ax.fill_between(tel["time_sec"], tel["S"], alpha=0.15, color="#58a6ff")
    ax.set_title("Order Book Stability   S = L_eff / (H_c + H_p + 1)", fontsize=9)
    ax.set_ylabel("S", fontsize=8)
    ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
    add_whale_lines(ax, whale_times)

    # Annotate current fifo_share on the right y-axis if available
    if "fifo_share" in tel.columns:
        ax2 = ax.twinx()
        ax2.plot(tel["time_sec"], tel["fifo_share"] * 100,
                 color="#e3b341", linewidth=0.8, alpha=0.6, linestyle=":")
        ax2.set_ylabel("FIFO share %", fontsize=7, color="#e3b341")
        ax2.tick_params(colors="#e3b341", labelsize=7)
        ax2.set_ylim(45, 105)
        for spine in ax2.spines.values():
            spine.set_edgecolor("#30363d")

    # -----------------------------------------------------------------------
    # Panel 2 — Effective Liquidity
    # -----------------------------------------------------------------------
    ax = axes1[1]
    ax.plot(tel["time_sec"], tel["L_eff"], color="#3fb950", linewidth=LW)
    ax.fill_between(tel["time_sec"], tel["L_eff"], alpha=0.12, color="#3fb950")
    ax.set_title("Effective Liquidity   L_eff  (decay-weighted top-N depth, penalised by √order_count)", fontsize=9)
    ax.set_ylabel("Volume", fontsize=8)
    ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
    add_whale_lines(ax, whale_times)

    # -----------------------------------------------------------------------
    # Panel 3 — Heat variables
    # -----------------------------------------------------------------------
    ax = axes1[2]
    ax.plot(tel["time_sec"], tel["H_c"],
            label="Cancel heat  H_c  (+1 per cancel, ×0.95 per 10 ms)", color="#f85149", linewidth=LW)
    ax.plot(tel["time_sec"], tel["H_p"],
            label="Price heat   H_p  (+|Δmid| per tick, ×0.95 per 10 ms)", color="#e3b341", linewidth=LW)
    ax.set_title("Heat Variables — measure of market stress (half-life ≈ 130 ms)", fontsize=9)
    ax.legend(fontsize=7, facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3")
    ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
    add_whale_lines(ax, whale_times)

    # -----------------------------------------------------------------------
    # Panel 4 — Avg fill age
    # -----------------------------------------------------------------------
    ax = axes1[3]
    ax.plot(tel["time_sec"], tel["avg_age"], color="#d2a8ff", linewidth=LW)
    ax.set_title("Avg Fill Age (ms)  — time resting orders wait before filling (FIFO efficiency indicator)", fontsize=9)
    ax.set_ylabel("ms", fontsize=8)
    ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
    add_whale_lines(ax, whale_times)

    # -----------------------------------------------------------------------
    # Panel 5 — Bid-ask spread
    # -----------------------------------------------------------------------
    ax = axes1[4]
    if "spread" in tel.columns:
        valid = tel[tel["spread"].between(1, 500)]
        ax.plot(valid["time_sec"], valid["spread"], color="#ffa657", linewidth=LW)
        ax.fill_between(valid["time_sec"], valid["spread"], alpha=0.12, color="#ffa657")
        ax.set_title("Bid-Ask Spread  (best_ask − best_bid in ticks)  — normal = 4 ticks (MM ±2)", fontsize=9)
        ax.set_ylabel("Ticks", fontsize=8)
        ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
        # Draw normal-spread reference line
        ax.axhline(4, color="#3fb950", linewidth=0.7, linestyle=":", alpha=0.6, label="Normal spread (4 ticks)")
        ax.legend(fontsize=7, facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3")
    else:
        ax.text(0.5, 0.5, "spread column not in CSV — re-run stress_test.py",
                ha="center", va="center", transform=ax.transAxes, color="#8b949e", fontsize=9)
        ax.set_title("Bid-Ask Spread", fontsize=9)
    ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
    add_whale_lines(ax, whale_times)

    # -----------------------------------------------------------------------
    # Panel 6 — Book depth
    # -----------------------------------------------------------------------
    ax = axes1[5]
    if "bid_levels" in tel.columns and "ask_levels" in tel.columns:
        ax.plot(tel["time_sec"], tel["bid_levels"],
                label="Bid levels (buy side)", color="#3fb950", linewidth=LW)
        ax.plot(tel["time_sec"], tel["ask_levels"],
                label="Ask levels (sell side)", color="#f85149", linewidth=LW)
        ax.set_title("Book Depth  (distinct resting price levels per side)", fontsize=9)
        ax.set_ylabel("Levels", fontsize=8)
        ax.legend(fontsize=7, facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3")
        ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    else:
        ax.text(0.5, 0.5, "bid_levels/ask_levels not in CSV — re-run stress_test.py",
                ha="center", va="center", transform=ax.transAxes, color="#8b949e", fontsize=9)
        ax.set_title("Book Depth", fontsize=9)
    ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
    add_whale_lines(ax, whale_times)

    axes1[-1].set_xlabel("Time (seconds)", fontsize=9, color="#8b949e")

    # Figure-level legend
    legend_handles = []
    if whale_times:
        legend_handles.append(
            mlines.Line2D([], [], color=WHALE_COLOR, linestyle="--",
                          linewidth=0.9, label="Whale sweep"))
    if "fifo_share" in tel.columns:
        legend_handles.append(
            mlines.Line2D([], [], color="#e3b341", linestyle=":",
                          linewidth=0.8, label="FIFO share % (right axis, panel 1)"))
    if legend_handles:
        fig1.legend(handles=legend_handles, loc="upper right",
                    facecolor="#161b22", edgecolor="#30363d",
                    labelcolor="#e6edf3", fontsize=8)

    k_val = tel["k"].iloc[-1] if "k" in tel.columns else "?"
    fig1.suptitle(
        f"Matching Engine — Market Microstructure Dashboard   (k = {k_val})\n"
        "Dashed red lines = whale sweeps | dotted yellow = live FIFO share %",
        color="#e6edf3", fontsize=11, y=1.002)

    plt.figure(fig1.number)
    plt.tight_layout()
    fig1.savefig("simulation_dashboard.png", dpi=150, bbox_inches="tight",
                 facecolor=fig1.get_facecolor())
    print("Saved: simulation_dashboard.png")


# ===========================================================================
# FIGURE 2 — Agent PnL comparison
# ===========================================================================

if has_pnl:
    ROLES       = ["MarketMaker", "RetailTrader", "HFTSniper", "Whale"]
    ROLE_LABELS = ["Market Maker", "Retail Trader", "HFT Sniper", "Whale"]
    ROLE_COLORS = {
        "MarketMaker":  ["#58a6ff", "#1f77b4", "#4682b4"],
        "RetailTrader": ["#3fb950", "#2ca02c", "#006400", "#32cd32", "#228b22"],
        "HFTSniper":    ["#d2a8ff", "#9467bd", "#7b68ee"],
        "Whale":        ["#f85149"],
    }
    K_COLORS = ["#ffa657", "#58a6ff", "#3fb950", "#d2a8ff", "#f85149", "#e3b341"]

    n_k    = len(pnl_data)
    n_rows = len(ROLES) + 1   # +1 for final-PnL bar summary

    fig2, axes2 = plt.subplots(n_rows, n_k,
                               figsize=(max(7 * n_k, 10), 3.8 * n_rows),
                               squeeze=False)
    fig2.patch.set_facecolor("#0d1117")
    style_axes(axes2.flat)

    # -----------------------------------------------------------------------
    # Per-role PnL time series (rows 0..3)
    # -----------------------------------------------------------------------
    for col_idx, (k_label, df) in enumerate(pnl_data.items()):
        col_color = K_COLORS[col_idx % len(K_COLORS)]

        for row_idx, (role, role_label) in enumerate(zip(ROLES, ROLE_LABELS)):
            ax = axes2[row_idx][col_idx]
            role_df = df[df["role"] == role]

            if role_df.empty:
                ax.text(0.5, 0.5, "No data", ha="center", va="center",
                        transform=ax.transAxes, color="#8b949e", fontsize=8)
            else:
                palette = ROLE_COLORS.get(role, ["#58a6ff"])
                for agent_idx, (agent_name, agent_df) in enumerate(
                        role_df.groupby("agent", sort=True)):
                    color = palette[agent_idx % len(palette)]
                    ax.plot(agent_df["time_sec"], agent_df["cumulative_pnl"],
                            label=agent_name, color=color,
                            linewidth=1.2, alpha=0.9)
                ax.axhline(0, color="#8b949e", linewidth=0.6, linestyle=":")
                add_whale_lines(ax, whale_times)
                ax.legend(fontsize=6, facecolor="#161b22", edgecolor="#30363d",
                          labelcolor="#e6edf3", loc="upper left")

            if row_idx == 0:
                ax.set_title(k_label, color=col_color, fontsize=11, fontweight="bold")

            ax.set_ylabel(
                f"{role_label}\nCum. PnL (ticks·shares)" if col_idx == 0 else "",
                color="#8b949e", fontsize=7)
            ax.yaxis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _: f"{v:+.0f}"))
            ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")

            if row_idx == n_rows - 2:
                ax.set_xlabel("Time (seconds)", fontsize=7, color="#8b949e")

    # -----------------------------------------------------------------------
    # Final-PnL bar chart (last row)
    # -----------------------------------------------------------------------
    for col_idx, (k_label, df) in enumerate(pnl_data.items()):
        ax    = axes2[n_rows - 1][col_idx]
        col_color = K_COLORS[col_idx % len(K_COLORS)]

        final = (df.sort_values("time_sec")
                   .groupby("agent")
                   .last()
                   .reset_index()[["agent", "role", "cumulative_pnl"]])

        if final.empty:
            ax.text(0.5, 0.5, "No data", ha="center", va="center",
                    transform=ax.transAxes, color="#8b949e")
        else:
            bar_colors = [ROLE_COLORS.get(r, ["#58a6ff"])[0]
                          for r in final["role"]]
            bars = ax.bar(range(len(final)), final["cumulative_pnl"],
                          color=bar_colors, alpha=0.85,
                          edgecolor="#30363d", linewidth=0.5)
            ax.set_xticks(range(len(final)))
            ax.set_xticklabels(final["agent"], rotation=38,
                               ha="right", fontsize=6, color="#8b949e")

            # Value labels on bars
            for bar, val in zip(bars, final["cumulative_pnl"]):
                offset = abs(val) * 0.04 if val != 0 else 1
                va     = "bottom" if val >= 0 else "top"
                y      = val + (offset if val >= 0 else -offset)
                ax.text(bar.get_x() + bar.get_width() / 2, y,
                        f"{val:+.0f}", ha="center", va=va,
                        color="#e6edf3", fontsize=6)

            ax.axhline(0, color="#8b949e", linewidth=0.8)
            ax.yaxis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _: f"{v:+.0f}"))

        ax.set_ylabel("Final PnL" if col_idx == 0 else "", color="#8b949e", fontsize=7)
        ax.set_title(f"Final PnL — {k_label}", color=col_color, fontsize=9)
        ax.grid(True, alpha=GRID_ALPHA, color="#8b949e", axis="y")

    # Figure-level legend
    legend_handles = [
        mpatches.Patch(color=ROLE_COLORS["MarketMaker"][0],  label="Market Maker"),
        mpatches.Patch(color=ROLE_COLORS["RetailTrader"][0], label="Retail Trader"),
        mpatches.Patch(color=ROLE_COLORS["HFTSniper"][0],    label="HFT Sniper"),
        mpatches.Patch(color=ROLE_COLORS["Whale"][0],        label="Whale"),
    ]
    if whale_times:
        legend_handles.append(
            mlines.Line2D([], [], color=WHALE_COLOR, linestyle="--",
                          linewidth=0.9, label="Whale sweep"))

    fig2.legend(handles=legend_handles, loc="upper right",
                facecolor="#161b22", edgecolor="#30363d",
                labelcolor="#e6edf3", fontsize=8)

    fig2.suptitle(
        "Agent PnL by Role — Effect of Heat-Function Curvature k\n"
        "PnL = Σ fill_qty × (mid − fill_price) for buys,  (fill_price − mid) for sells\n"
        "Positive = filled better than fair value (passive) | Negative = paid away from fair value (aggressive)",
        color="#e6edf3", fontsize=10, y=1.002)

    plt.figure(fig2.number)
    plt.tight_layout()
    fig2.savefig("pnl_comparison.png", dpi=150, bbox_inches="tight",
                 facecolor=fig2.get_facecolor())
    print("Saved: pnl_comparison.png")


# ===========================================================================
# Show both figures
# ===========================================================================
plt.show()
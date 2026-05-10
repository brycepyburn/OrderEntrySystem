"""
visualize.py — Post-simulation dashboard
=========================================
Panels:
  1. Order Book Stability (S)
  2. Effective Liquidity (L_eff, decay-weighted)
  3. Heat variables (cancel heat H_c, price heat H_p)
  4. Avg Fill Age (FIFO efficiency proxy)
  5. Bid-Ask Spread (best_ask - best_bid)
  6. Book Depth (number of price levels each side)

Whale events (from whale_events.json) are drawn as vertical dashed lines
on every panel so you can see how each metric responds to a large sweep.
"""

import json
import sys

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Load telemetry
# ---------------------------------------------------------------------------
try:
    df = pd.read_csv("stability_bounds.csv")
    if df.empty:
        print("Error: stability_bounds.csv is empty. Run stress_test.py first.")
        sys.exit(1)
except FileNotFoundError:
    print("Error: stability_bounds.csv not found. Run stress_test.py first.")
    sys.exit(1)

# Normalise time to seconds from the start of the run
df["time_sec"] = (df["timestamp"] - df["timestamp"].iloc[0]) / 1000.0

# ---------------------------------------------------------------------------
# Load whale events (optional)
# ---------------------------------------------------------------------------
whale_times: list[float] = []
try:
    with open("whale_events.json") as f:
        whale_times = json.load(f)
    print(f"Loaded {len(whale_times)} whale event(s).")
except FileNotFoundError:
    print("whale_events.json not found — whale annotations skipped.")

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
fig, axes = plt.subplots(6, 1, figsize=(13, 18), sharex=True)
fig.patch.set_facecolor("#0d1117")
for ax in axes:
    ax.set_facecolor("#161b22")
    ax.tick_params(colors="#8b949e", labelsize=8)
    ax.yaxis.label.set_color("#8b949e")
    ax.title.set_color("#e6edf3")
    for spine in ax.spines.values():
        spine.set_edgecolor("#30363d")

WHALE_COLOR  = "#f78166"
GRID_ALPHA   = 0.15
LINE_WIDTH   = 1.2

def add_whale_lines(ax):
    for t in whale_times:
        ax.axvline(t, color=WHALE_COLOR, linestyle="--", linewidth=0.9, alpha=0.7)

# ---------------------------------------------------------------------------
# Panel 1 — Stability (S)
# ---------------------------------------------------------------------------
ax = axes[0]
ax.plot(df["time_sec"], df["S"], color="#58a6ff", linewidth=LINE_WIDTH)
ax.fill_between(df["time_sec"], df["S"], alpha=0.15, color="#58a6ff")
ax.set_title("Order Book Stability  S = L_eff / (H_c + H_p + 1)", fontsize=9)
ax.set_ylabel("S", fontsize=8)
ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
add_whale_lines(ax)

# ---------------------------------------------------------------------------
# Panel 2 — Effective Liquidity
# ---------------------------------------------------------------------------
ax = axes[1]
ax.plot(df["time_sec"], df["L_eff"], color="#3fb950", linewidth=LINE_WIDTH)
ax.fill_between(df["time_sec"], df["L_eff"], alpha=0.12, color="#3fb950")
ax.set_title("Effective Liquidity  L_eff  (decay-weighted depth)", fontsize=9)
ax.set_ylabel("Volume", fontsize=8)
ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
add_whale_lines(ax)

# ---------------------------------------------------------------------------
# Panel 3 — Heat variables
# ---------------------------------------------------------------------------
ax = axes[2]
ax.plot(df["time_sec"], df["H_c"], label="Cancel Heat  H_c", color="#f85149", linewidth=LINE_WIDTH)
ax.plot(df["time_sec"], df["H_p"], label="Price Heat   H_p", color="#e3b341", linewidth=LINE_WIDTH)
ax.set_title("Heat Variables", fontsize=9)
ax.legend(fontsize=7, facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3")
ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
add_whale_lines(ax)

# ---------------------------------------------------------------------------
# Panel 4 — Avg Fill Age
# ---------------------------------------------------------------------------
ax = axes[3]
ax.plot(df["time_sec"], df["avg_age"], color="#d2a8ff", linewidth=LINE_WIDTH)
ax.set_title("Avg Fill Age (ms)  — FIFO queue efficiency", fontsize=9)
ax.set_ylabel("ms", fontsize=8)
ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
add_whale_lines(ax)

# ---------------------------------------------------------------------------
# Panel 5 — Bid-Ask Spread
# ---------------------------------------------------------------------------
ax = axes[4]
if "spread" in df.columns:
    # Filter out rows where the book is one-sided (spread would be huge or 0)
    valid = df[df["spread"].between(1, 500)]
    ax.plot(valid["time_sec"], valid["spread"], color="#ffa657", linewidth=LINE_WIDTH)
    ax.fill_between(valid["time_sec"], valid["spread"], alpha=0.12, color="#ffa657")
    ax.set_title("Bid-Ask Spread  (best_ask − best_bid, ticks)", fontsize=9)
    ax.set_ylabel("Ticks", fontsize=8)
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
else:
    ax.text(0.5, 0.5, "spread column not in CSV\n(re-run with new stress_test.py)",
            ha="center", va="center", transform=ax.transAxes, color="#8b949e", fontsize=9)
    ax.set_title("Bid-Ask Spread", fontsize=9)
ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
add_whale_lines(ax)

# ---------------------------------------------------------------------------
# Panel 6 — Book Depth (price level count)
# ---------------------------------------------------------------------------
ax = axes[5]
if "bid_levels" in df.columns and "ask_levels" in df.columns:
    ax.plot(df["time_sec"], df["bid_levels"], label="Bid levels", color="#3fb950", linewidth=LINE_WIDTH)
    ax.plot(df["time_sec"], df["ask_levels"], label="Ask levels", color="#f85149", linewidth=LINE_WIDTH)
    ax.set_title("Book Depth  (number of resting price levels)", fontsize=9)
    ax.set_ylabel("Levels", fontsize=8)
    ax.legend(fontsize=7, facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3")
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
else:
    ax.text(0.5, 0.5, "bid_levels/ask_levels columns not in CSV\n(re-run with new stress_test.py)",
            ha="center", va="center", transform=ax.transAxes, color="#8b949e", fontsize=9)
    ax.set_title("Book Depth", fontsize=9)
ax.grid(True, alpha=GRID_ALPHA, color="#8b949e")
add_whale_lines(ax)

# ---------------------------------------------------------------------------
# Shared x-axis label + legend for whale lines
# ---------------------------------------------------------------------------
axes[-1].set_xlabel("Time (seconds)", fontsize=9, color="#8b949e")
if whale_times:
    import matplotlib.lines as mlines
    whale_handle = mlines.Line2D([], [], color=WHALE_COLOR, linestyle="--",
                                 linewidth=0.9, label="Whale sweep")
    fig.legend(handles=[whale_handle], loc="upper right",
               facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3", fontsize=8)

plt.suptitle("Matching Engine Simulation — Market Microstructure Dashboard",
             color="#e6edf3", fontsize=11, y=1.002)
plt.tight_layout()
plt.savefig("simulation_dashboard.png", dpi=150, bbox_inches="tight",
            facecolor=fig.get_facecolor())
print("Dashboard saved to simulation_dashboard.png")
plt.show()
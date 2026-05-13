"""
visualize.py  —  Single entry point for all charts
====================================================

Run this after every stress_test.py run.  It detects what data is present
and produces whichever charts are possible.

PHASE 1  (k-sweep)
  Run stress_test.py for each k value you want to compare.
  Each run appends one row to experiment_results.csv.
  Then run this script — it makes:
    sweep_stability.png   avg_S vs k  (whale + no-whale)
    sweep_pnl.png         avg PnL per role vs k

PHASE 2  (FIFO vs best-k comparison)
  After picking the best k from Phase 1, run the engine in FIFO mode too.
  Then run this script again — it additionally makes:
    compare_stability.png  avg_S bar chart: FIFO vs best-k
    compare_pnl.png        agent PnL bar chart: FIFO vs best-k
    dashboard.png          6-panel microstructure view (last run's telemetry)
    agent_pnl.png          cumulative PnL time series (last run's pnl_k*.csv)

Usage
-----
    python visualize.py                    # auto-detect everything
    python visualize.py --best-k 2        # force which k is "best" for Phase 2
    python visualize.py --dashboard-only  # only make the dashboard + agent PnL
    python visualize.py --sweep-only      # only make the k-sweep charts

Workflow reminder
-----------------
  # Phase 1 — k sweep (repeat for k = 0,1,2,3,4,5)
  ./matching_engine {k} hybrid
  python stress_test.py {k} --no-whale
  # stop engine between runs

  python visualize.py          ← run after all k values done

  # Phase 2 — comparison (pick best_k from charts above)
  ./matching_engine {best_k} hybrid
  python stress_test.py {best_k} --no-whale

  ./matching_engine 0 fifo
  python stress_test.py 0 --fifo --no-whale

  python visualize.py --best-k {best_k}   ← run once more
"""

import argparse
import json
import sys
from pathlib import Path

import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

# ============================================================================
# CLI args
# ============================================================================

parser = argparse.ArgumentParser(add_help=True)
parser.add_argument("--best-k",       type=float, default=None,
                    help="Force which k value is treated as 'best' for comparison charts")
parser.add_argument("--dashboard-only", action="store_true",
                    help="Only produce dashboard.png and agent_pnl.png")
parser.add_argument("--sweep-only",     action="store_true",
                    help="Only produce sweep_stability.png and sweep_pnl.png")
args = parser.parse_args()

# ============================================================================
# Shared style
# ============================================================================

BG_FIG   = "#0d1117"
BG_PANEL = "#161b22"
C_GRID   = "#30363d"
C_TEXT   = "#8b949e"
C_TITLE  = "#e6edf3"

WHALE_COLOR  = "#f78166"
FIFO_COLOR   = "#e3b341"
BEST_COLOR   = "#3fb950"
NOWH_COLOR   = "#58a6ff"

ROLE_COLORS = {
    "MarketMaker":  "#58a6ff",
    "HFTSniper":    "#d2a8ff",
    "RetailTrader": "#3fb950",
    "Whale":        "#f85149",
}
PNL_COLS = {
    "mm_pnl":     ("MarketMaker",  "#58a6ff"),
    "hft_pnl":    ("HFTSniper",    "#d2a8ff"),
    "retail_pnl": ("RetailTrader", "#3fb950"),
}

LW       = 1.4
GRID_A   = 0.15


def style_fig(fig):
    fig.patch.set_facecolor(BG_FIG)


def style_ax(ax, title="", ylabel="", xlabel=""):
    ax.set_facecolor(BG_PANEL)
    ax.tick_params(colors=C_TEXT, labelsize=8)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_color(C_TEXT)
    for spine in ax.spines.values():
        spine.set_edgecolor(C_GRID)
    ax.grid(True, alpha=GRID_A, color=C_TEXT)
    if title:  ax.set_title(title,  color=C_TITLE, fontsize=9)
    if ylabel: ax.set_ylabel(ylabel, color=C_TEXT,  fontsize=8)
    if xlabel: ax.set_xlabel(xlabel, color=C_TEXT,  fontsize=8)


def legend(ax, **kw):
    lg = ax.legend(fontsize=7, facecolor=BG_PANEL, edgecolor=C_GRID,
                   labelcolor=C_TITLE, **kw)
    return lg


def save(fig, fname):
    fig.tight_layout()
    fig.savefig(fname, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"  Saved  {fname}")
    plt.close(fig)


def add_vline(ax, x, label=None):
    ax.axvline(x, color=BEST_COLOR, linewidth=1.0,
               linestyle="--", alpha=0.8, label=label)


def whale_lines(ax, times):
    for t in times:
        ax.axvline(t, color=WHALE_COLOR, linestyle="--",
                   linewidth=0.8, alpha=0.6)


# ============================================================================
# Load data
# ============================================================================

# --- experiment_results.csv (one row per stress_test run) ---
results_path = Path("experiment_results.csv")
if results_path.exists():
    exp = pd.read_csv(results_path)
    exp["k"]     = exp["k"].astype(float)
    exp["whale"] = exp["whale"].astype(str).str.lower().isin(["true","1","yes"])
    print(f"Loaded experiment_results.csv  ({len(exp)} rows)")
    print(exp[["mode","k","whale","avg_S","mm_pnl","hft_pnl","retail_pnl"]].to_string(index=False))
else:
    exp = pd.DataFrame()
    print("experiment_results.csv not found — sweep charts will be skipped")

# --- pnl_k*.csv files for detailed time-series PnL ---
pnl_files   = sorted(Path(".").glob("pnl_k*.csv"))
pnl_data    = {}
for f in pnl_files:
    df_p = pd.read_csv(f)
    # k label from the data itself (most reliable) or filename fallback
    if "k" in df_p.columns:
        k_val = df_p["k"].iloc[0] if not df_p.empty else "?"
    else:
        k_val = f.stem.replace("pnl_k","").replace("_",".")
    # mode label
    mode_val = df_p["mode"].iloc[0] if "mode" in df_p.columns else "hybrid"
    label = f"{mode_val}  k={k_val}"
    pnl_data[label] = df_p
    print(f"Loaded {f.name}  →  {label}")

# --- stability_bounds.csv from the most recent run ---
tel_path = Path("stability_bounds.csv")
if tel_path.exists():
    tel = pd.read_csv(tel_path)
    tel["time_sec"] = (tel["timestamp"] - tel["timestamp"].iloc[0]) / 1000.0
    print(f"Loaded stability_bounds.csv  ({len(tel)} rows)")
else:
    tel = pd.DataFrame()
    print("stability_bounds.csv not found — dashboard will be skipped")

# --- whale event timestamps ---
whale_times: list[float] = []
try:
    with open("whale_events.json") as wf:
        whale_times = json.load(wf)
    print(f"Loaded {len(whale_times)} whale event(s)")
except FileNotFoundError:
    pass

# ============================================================================
# Determine best k  (argmax avg_S on no-whale hybrid rows)
# ============================================================================

best_k = args.best_k

if best_k is None and not exp.empty:
    nowh_hybrid = exp[(exp["mode"] == "hybrid") & (~exp["whale"])]
    if not nowh_hybrid.empty:
        best_k = float(nowh_hybrid.groupby("k")["avg_S"].mean().idxmax())
        print(f"\nAuto-detected best k = {best_k}  (max avg_S, no-whale hybrid)")

has_fifo    = not exp.empty and (exp["mode"] == "fifo").any()
has_hybrid  = not exp.empty and (exp["mode"] == "hybrid").any()
has_sweep   = has_hybrid
has_compare = has_fifo and has_hybrid and best_k is not None

print(f"\nCharts available:  sweep={has_sweep}  compare={has_compare}  "
      f"dashboard={not tel.empty}  agent_pnl={len(pnl_data)>0}")


# ============================================================================
# PHASE 1 CHARTS — k sweep
# ============================================================================

if has_sweep and not args.dashboard_only:

    hybrid = exp[exp["mode"] == "hybrid"]
    k_vals = sorted(hybrid["k"].unique())

    # -----------------------------------------------------------------------
    # sweep_stability.png  —  avg_S vs k
    # -----------------------------------------------------------------------
    fig, axes = plt.subplots(2, 1, figsize=(11, 9), sharex=True)
    style_fig(fig)

    ax = axes[0]
    style_ax(ax, title="Average Stability vs k",
             ylabel="avg_S  =  L_eff / (H_c + H_p + 1)")

    for whale_cond, color, label in [
        (False, NOWH_COLOR,    "Hybrid — no whale"),
        (True,  WHALE_COLOR,   "Hybrid — with whale"),
    ]:
        sub = hybrid[hybrid["whale"] == whale_cond]
        if sub.empty: continue
        g = sub.groupby("k")["avg_S"].agg(["mean","std"]).reindex(k_vals)
        ax.errorbar(g.index, g["mean"], yerr=g["std"].fillna(0),
                    color=color, linewidth=LW, marker="o", markersize=6,
                    capsize=4, label=label, zorder=3)
        ax.fill_between(g.index,
                        g["mean"] - g["std"].fillna(0),
                        g["mean"] + g["std"].fillna(0),
                        alpha=0.10, color=color)

    # FIFO baselines
    for whale_cond, color, ls in [(False, NOWH_COLOR,"--"),(True, WHALE_COLOR,"--")]:
        frow = exp[(exp["mode"]=="fifo") & (exp["whale"]==whale_cond)]
        if not frow.empty:
            ax.axhline(frow["avg_S"].mean(), color=color,
                       linewidth=1.0, linestyle=ls, alpha=0.55,
                       label=f"FIFO baseline ({'whale' if whale_cond else 'no whale'})")

    if best_k is not None:
        add_vline(ax, best_k, label=f"Best k = {best_k}")
    ax.set_xticks(k_vals)
    legend(ax)

    # --- decomposition panel ---
    ax2 = axes[1]
    style_ax(ax2, title="Stability decomposition (no-whale hybrid)",
             ylabel="Component value",
             xlabel="k  (heat-function curvature)")

    nowh = hybrid[~hybrid["whale"]]
    for col, color, label in [
        ("avg_L_eff", "#3fb950", "L_eff  (liquidity — numerator)"),
        ("avg_H_c",   "#f85149", "H_c   (cancel heat — denominator)"),
        ("avg_H_p",   FIFO_COLOR, "H_p   (price heat — denominator)"),
    ]:
        if col not in nowh.columns or nowh.empty: continue
        g = nowh.groupby("k")[col].mean().reindex(k_vals)
        ax2.plot(g.index, g.values, color=color, linewidth=LW,
                 marker="s", markersize=4, label=label)

    # FIFO component baselines
    for col, color in [("avg_L_eff","#3fb950"),("avg_H_c","#f85149"),("avg_H_p",FIFO_COLOR)]:
        fr = exp[(exp["mode"]=="fifo") & (~exp["whale"])]
        if col in fr.columns and not fr.empty:
            ax2.axhline(fr[col].mean(), color=color,
                        linewidth=0.8, linestyle=":", alpha=0.45)

    if best_k is not None:
        add_vline(ax2, best_k)
    ax2.set_xticks(k_vals)
    legend(ax2)

    fig.suptitle("K-Sweep — How curvature k affects stability and its components",
                 color=C_TITLE, fontsize=11, y=1.002)
    save(fig, "sweep_stability.png")

    # -----------------------------------------------------------------------
    # sweep_pnl.png  —  avg PnL per role vs k  (no-whale only)
    # -----------------------------------------------------------------------
    nowh_h = hybrid[~hybrid["whale"]]
    avail_pnl_cols = {k: v for k, v in PNL_COLS.items() if k in nowh_h.columns}

    if avail_pnl_cols and not nowh_h.empty:
        fig, axes = plt.subplots(len(avail_pnl_cols), 1,
                                 figsize=(11, 3.5 * len(avail_pnl_cols)),
                                 sharex=True)
        style_fig(fig)
        if len(avail_pnl_cols) == 1:
            axes = [axes]

        fifo_nowh = exp[(exp["mode"]=="fifo") & (~exp["whale"])]

        for ax, (col, (role, color)) in zip(axes, avail_pnl_cols.items()):
            style_ax(ax, title=f"{role} — avg PnL vs k",
                     ylabel="avg PnL  (ticks · shares)")
            g = nowh_h.groupby("k")[col].agg(["mean","std"]).reindex(k_vals)
            ax.plot(g.index, g["mean"], color=color, linewidth=LW,
                    marker="o", markersize=6, label=f"{role} — hybrid")
            ax.fill_between(g.index,
                            g["mean"] - g["std"].fillna(0),
                            g["mean"] + g["std"].fillna(0),
                            alpha=0.12, color=color)
            ax.axhline(0, color=C_TEXT, linewidth=0.6, linestyle=":")

            # FIFO baseline
            if col in fifo_nowh.columns and not fifo_nowh.empty:
                ax.axhline(fifo_nowh[col].mean(), color=FIFO_COLOR,
                           linewidth=1.0, linestyle="--", alpha=0.7,
                           label="FIFO baseline")

            if best_k is not None:
                add_vline(ax, best_k, label=f"Best k={best_k}")
            ax.yaxis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _: f"{v:+.0f}"))
            legend(ax)

        axes[-1].set_xlabel("k  (heat-function curvature)",
                            fontsize=8, color=C_TEXT)
        axes[-1].set_xticks(k_vals)

        fig.suptitle(
            "K-Sweep — Agent PnL vs k  (no-whale)\n"
            "PnL = Σ fill_qty × (mid − fill_price) buys  /  (fill_price − mid) sells",
            color=C_TITLE, fontsize=10, y=1.002)
        save(fig, "sweep_pnl.png")
    else:
        print("  No PnL columns in experiment_results.csv — sweep_pnl.png skipped")


# ============================================================================
# PHASE 2 CHARTS — FIFO vs best-k comparison
# ============================================================================

if has_compare and not args.sweep_only and not args.dashboard_only:

    fifo_nowh  = exp[(exp["mode"]=="fifo")   & (~exp["whale"])]
    opt_nowh   = exp[(exp["mode"]=="hybrid") & (~exp["whale"]) & (exp["k"]==best_k)]
    fifo_whale = exp[(exp["mode"]=="fifo")   & ( exp["whale"])]
    opt_whale  = exp[(exp["mode"]=="hybrid") & ( exp["whale"]) & (exp["k"]==best_k)]

    # -----------------------------------------------------------------------
    # compare_stability.png  —  grouped bar: FIFO vs best-k, whale vs no-whale
    # -----------------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(9, 5))
    style_fig(fig)
    style_ax(ax, title=f"Stability: FIFO vs Hybrid k={best_k}",
             ylabel="Average Stability  S")

    groups   = ["No whale", "With whale"]
    fifo_s   = [fifo_nowh["avg_S"].mean(),  fifo_whale["avg_S"].mean()]
    fifo_e   = [fifo_nowh["avg_S"].std(),   fifo_whale["avg_S"].std()]
    opt_s    = [opt_nowh["avg_S"].mean(),   opt_whale["avg_S"].mean()]
    opt_e    = [opt_nowh["avg_S"].std(),    opt_whale["avg_S"].std()]

    x     = np.arange(len(groups))
    width = 0.35

    def bar_group(ax, x, vals, errs, color, label):
        bars = ax.bar(x, vals, width, color=color, alpha=0.85,
                      edgecolor=C_GRID, linewidth=0.5, label=label,
                      yerr=[e if not np.isnan(e) else 0 for e in errs],
                      capsize=5, error_kw={"ecolor": C_TEXT, "linewidth": 1})
        for bar, val in zip(bars, vals):
            if not np.isnan(val):
                ax.text(bar.get_x() + bar.get_width()/2,
                        bar.get_height() + 1,
                        f"{val:.1f}", ha="center", va="bottom",
                        color=C_TITLE, fontsize=8)
        return bars

    bar_group(ax, x - width/2, fifo_s, fifo_e, FIFO_COLOR,  "FIFO")
    bar_group(ax, x + width/2, opt_s,  opt_e,  BEST_COLOR,  f"Hybrid k={best_k}")
    ax.set_xticks(x)
    ax.set_xticklabels(groups, color=C_TEXT, fontsize=9)
    legend(ax)

    fig.suptitle(f"Stability Comparison — FIFO vs Hybrid k={best_k}",
                 color=C_TITLE, fontsize=11, y=1.002)
    save(fig, "compare_stability.png")

    # -----------------------------------------------------------------------
    # compare_pnl.png  —  grouped bar: PnL by role, FIFO vs best-k
    # -----------------------------------------------------------------------
    avail = {k: v for k, v in PNL_COLS.items()
             if k in fifo_nowh.columns and k in opt_nowh.columns}

    if avail:
        fig, ax = plt.subplots(figsize=(10, 5))
        style_fig(fig)
        style_ax(ax, title=f"Agent PnL: FIFO vs Hybrid k={best_k}  (no whale)",
                 ylabel="avg PnL  (ticks · shares)")

        roles   = list(avail.keys())
        xlabels = [avail[r][0] for r in roles]
        x       = np.arange(len(roles))

        fifo_v  = [fifo_nowh[r].mean() for r in roles]
        opt_v   = [opt_nowh[r].mean()  for r in roles]

        for xpos, color, vals, label in [
            (x - width/2, FIFO_COLOR, fifo_v, "FIFO"),
            (x + width/2, BEST_COLOR, opt_v,  f"Hybrid k={best_k}"),
        ]:
            bars = ax.bar(xpos, vals, width, color=color, alpha=0.85,
                          edgecolor=C_GRID, linewidth=0.5, label=label)
            for bar, val in zip(bars, vals):
                if not np.isnan(val):
                    offset = abs(val) * 0.04 if val != 0 else 1
                    va     = "bottom" if val >= 0 else "top"
                    y      = val + (offset if val >= 0 else -offset)
                    ax.text(bar.get_x() + bar.get_width()/2, y,
                            f"{val:+.0f}", ha="center", va=va,
                            color=C_TITLE, fontsize=8)

        ax.axhline(0, color=C_TEXT, linewidth=0.7)
        ax.set_xticks(x)
        ax.set_xticklabels(xlabels, color=C_TEXT, fontsize=9)
        ax.yaxis.set_major_formatter(
            mticker.FuncFormatter(lambda v, _: f"{v:+.0f}"))
        legend(ax)

        fig.suptitle(
            f"PnL Comparison — FIFO vs Hybrid k={best_k}  (no whale)\n"
            "Who gains and who loses when the allocation mode changes?",
            color=C_TITLE, fontsize=10, y=1.002)
        save(fig, "compare_pnl.png")


# ============================================================================
# DASHBOARD  —  6-panel microstructure (most recent run's telemetry)
# ============================================================================

if not tel.empty and not args.sweep_only:

    fig, axes = plt.subplots(6, 1, figsize=(14, 20), sharex=True)
    style_fig(fig)

    k_val    = tel["k"].iloc[-1]    if "k"    in tel.columns else "?"
    mode_val = tel["mode"].iloc[-1] if "mode" in tel.columns else "hybrid"

    panels = [
        # (col,      color,     fill,  title)
        ("S",       "#58a6ff", True,
         "Stability   S = L_eff / (H_c + H_p + 1)"),
        ("L_eff",   "#3fb950", True,
         "Effective Liquidity  L_eff  (decay-weighted top-N depth)"),
        (None,      None,      False,
         "Heat Variables  —  H_c cancel heat,  H_p price heat"),
        ("avg_age", "#d2a8ff", False,
         "Avg Fill Age (ms)  — time resting orders wait before filling"),
        ("spread",  "#ffa657", True,
         "Bid-Ask Spread  (ticks)  — normal = 4 ticks (MM ±2)"),
        (None,      None,      False,
         "Book Depth  (distinct resting price levels per side)"),
    ]

    for i, ax in enumerate(axes):
        style_ax(axes[i])
        axes[i].title.set_color(C_TITLE)
        axes[i].title.set_fontsize(8.5)

    # Panel 0 — Stability
    ax = axes[0]
    ax.set_title(panels[0][3], color=C_TITLE, fontsize=8.5)
    ax.plot(tel["time_sec"], tel["S"], color="#58a6ff", linewidth=LW)
    ax.fill_between(tel["time_sec"], tel["S"], alpha=0.14, color="#58a6ff")
    ax.set_ylabel("S", color=C_TEXT, fontsize=8)
    # FIFO share on right axis
    if "fifo_share" in tel.columns:
        ax2 = ax.twinx()
        ax2.plot(tel["time_sec"], tel["fifo_share"] * 100,
                 color=FIFO_COLOR, linewidth=0.9, linestyle=":", alpha=0.7)
        ax2.set_ylabel("FIFO share %", color=FIFO_COLOR, fontsize=7)
        ax2.tick_params(colors=FIFO_COLOR, labelsize=7)
        ax2.set_ylim(45, 105)
        for sp in ax2.spines.values(): sp.set_edgecolor(C_GRID)
    whale_lines(ax, whale_times)
    ax.grid(True, alpha=GRID_A, color=C_TEXT)

    # Panel 1 — L_eff
    ax = axes[1]
    ax.set_title(panels[1][3], color=C_TITLE, fontsize=8.5)
    ax.plot(tel["time_sec"], tel["L_eff"], color="#3fb950", linewidth=LW)
    ax.fill_between(tel["time_sec"], tel["L_eff"], alpha=0.12, color="#3fb950")
    ax.set_ylabel("Volume", color=C_TEXT, fontsize=8)
    whale_lines(ax, whale_times)
    ax.grid(True, alpha=GRID_A, color=C_TEXT)

    # Panel 2 — Heat
    ax = axes[2]
    ax.set_title(panels[2][3], color=C_TITLE, fontsize=8.5)
    ax.plot(tel["time_sec"], tel["H_c"], color="#f85149", linewidth=LW,
            label="H_c  cancel heat  (+1/cancel, ×0.95/10ms)")
    ax.plot(tel["time_sec"], tel["H_p"], color=FIFO_COLOR, linewidth=LW,
            label="H_p  price heat   (+|Δmid|, ×0.95/10ms)")
    legend(ax)
    whale_lines(ax, whale_times)
    ax.grid(True, alpha=GRID_A, color=C_TEXT)

    # Panel 3 — Avg fill age
    ax = axes[3]
    ax.set_title(panels[3][3], color=C_TITLE, fontsize=8.5)
    ax.plot(tel["time_sec"], tel["avg_age"], color="#d2a8ff", linewidth=LW)
    ax.set_ylabel("ms", color=C_TEXT, fontsize=8)
    whale_lines(ax, whale_times)
    ax.grid(True, alpha=GRID_A, color=C_TEXT)

    # Panel 4 — Spread
    ax = axes[4]
    ax.set_title(panels[4][3], color=C_TITLE, fontsize=8.5)
    if "spread" in tel.columns:
        valid = tel[tel["spread"].between(1, 500)]
        ax.plot(valid["time_sec"], valid["spread"],
                color="#ffa657", linewidth=LW)
        ax.fill_between(valid["time_sec"], valid["spread"],
                        alpha=0.12, color="#ffa657")
        ax.axhline(4, color="#3fb950", linewidth=0.7, linestyle=":",
                   alpha=0.6, label="Normal spread (4 ticks)")
        ax.set_ylabel("Ticks", color=C_TEXT, fontsize=8)
        ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
        legend(ax)
    whale_lines(ax, whale_times)
    ax.grid(True, alpha=GRID_A, color=C_TEXT)

    # Panel 5 — Depth
    ax = axes[5]
    ax.set_title(panels[5][3], color=C_TITLE, fontsize=8.5)
    if "bid_levels" in tel.columns:
        ax.plot(tel["time_sec"], tel["bid_levels"],
                color="#3fb950", linewidth=LW, label="Bid levels")
        ax.plot(tel["time_sec"], tel["ask_levels"],
                color="#f85149", linewidth=LW, label="Ask levels")
        ax.set_ylabel("Levels", color=C_TEXT, fontsize=8)
        ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
        legend(ax)
    whale_lines(ax, whale_times)
    ax.grid(True, alpha=GRID_A, color=C_TEXT)

    axes[-1].set_xlabel("Time (seconds)", color=C_TEXT, fontsize=9)

    # Figure legend
    handles = []
    if whale_times:
        handles.append(mlines.Line2D([], [], color=WHALE_COLOR, linestyle="--",
                                     linewidth=0.9, label="Whale sweep"))
    if "fifo_share" in tel.columns:
        handles.append(mlines.Line2D([], [], color=FIFO_COLOR, linestyle=":",
                                     linewidth=0.9, label="FIFO share % (right axis)"))
    if handles:
        fig.legend(handles=handles, loc="upper right",
                   facecolor=BG_PANEL, edgecolor=C_GRID,
                   labelcolor=C_TITLE, fontsize=8)

    fig.suptitle(
        f"Simulation Dashboard  —  mode={mode_val}  k={k_val}\n"
        "6-panel market microstructure view of the most recent run",
        color=C_TITLE, fontsize=11, y=1.002)
    save(fig, "dashboard.png")


# ============================================================================
# AGENT PnL  —  cumulative PnL time series from pnl_k*.csv files
# ============================================================================

if pnl_data and not args.sweep_only:

    # Detect which roles are actually present across all files
    all_roles_present = set()
    for df_p in pnl_data.values():
        all_roles_present.update(df_p["role"].unique())

    ROLES_ORDER  = ["MarketMaker","RetailTrader","HFTSniper","Whale"]
    ROLES_LABELS = {"MarketMaker":"Market Maker","RetailTrader":"Retail Trader",
                    "HFTSniper":"HFT Sniper","Whale":"Whale"}
    roles_to_plot = [r for r in ROLES_ORDER if r in all_roles_present]

    n_k    = len(pnl_data)
    n_rows = len(roles_to_plot) + 1   # +1 for final-PnL bar summary

    K_COL  = ["#ffa657","#58a6ff","#3fb950","#d2a8ff","#f85149","#e3b341","#ff7b72"]

    fig, axes = plt.subplots(n_rows, n_k,
                             figsize=(max(7*n_k, 9), 3.8*n_rows),
                             squeeze=False)
    style_fig(fig)
    for ax in axes.flat:
        ax.set_facecolor(BG_PANEL)
        ax.tick_params(colors=C_TEXT, labelsize=7)
        for sp in ax.spines.values(): sp.set_edgecolor(C_GRID)

    PALETTE = {
        "MarketMaker":  ["#58a6ff","#1f77b4","#4682b4"],
        "RetailTrader": ["#3fb950","#2ca02c","#006400","#32cd32","#228b22"],
        "HFTSniper":    ["#d2a8ff","#9467bd","#7b68ee"],
        "Whale":        ["#f85149"],
    }

    for col_i, (label, df_p) in enumerate(pnl_data.items()):
        col_color = K_COL[col_i % len(K_COL)]

        # Time series rows
        for row_i, role in enumerate(roles_to_plot):
            ax = axes[row_i][col_i]
            role_df = df_p[df_p["role"] == role]
            palette = PALETTE.get(role, ["#58a6ff"])

            if role_df.empty:
                ax.text(0.5, 0.5, "No data", ha="center", va="center",
                        transform=ax.transAxes, color=C_TEXT, fontsize=8)
            else:
                for a_i, (aname, adf) in enumerate(role_df.groupby("agent", sort=True)):
                    ax.plot(adf["time_sec"], adf["cumulative_pnl"],
                            color=palette[a_i % len(palette)],
                            linewidth=1.2, alpha=0.9, label=aname)
                ax.axhline(0, color=C_TEXT, linewidth=0.5, linestyle=":")
                whale_lines(ax, whale_times)
                ax.legend(fontsize=6, facecolor=BG_PANEL, edgecolor=C_GRID,
                          labelcolor=C_TITLE, loc="upper left")

            if row_i == 0:
                ax.set_title(label, color=col_color, fontsize=10,
                             fontweight="bold")
            ax.set_ylabel(
                f"{ROLES_LABELS[role]}\nCum. PnL" if col_i == 0 else "",
                color=C_TEXT, fontsize=7)
            ax.yaxis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _: f"{v:+.0f}"))
            ax.grid(True, alpha=GRID_A, color=C_TEXT)

            if row_i == n_rows - 2:
                ax.set_xlabel("Time (seconds)", fontsize=7, color=C_TEXT)

        # Final-PnL bar row
        ax = axes[n_rows - 1][col_i]
        ax.set_facecolor(BG_PANEL)
        for sp in ax.spines.values(): sp.set_edgecolor(C_GRID)

        final = (df_p.sort_values("time_sec")
                     .groupby("agent").last()
                     .reset_index()[["agent","role","cumulative_pnl"]])

        if not final.empty:
            bar_colors = [PALETTE.get(r,["#58a6ff"])[0] for r in final["role"]]
            bars = ax.bar(range(len(final)), final["cumulative_pnl"],
                          color=bar_colors, alpha=0.85,
                          edgecolor=C_GRID, linewidth=0.5)
            ax.set_xticks(range(len(final)))
            ax.set_xticklabels(final["agent"], rotation=38,
                               ha="right", fontsize=6, color=C_TEXT)
            for bar, val in zip(bars, final["cumulative_pnl"]):
                offset = abs(val)*0.04 if val != 0 else 1
                va     = "bottom" if val >= 0 else "top"
                y      = val + (offset if val >= 0 else -offset)
                ax.text(bar.get_x()+bar.get_width()/2, y,
                        f"{val:+.0f}", ha="center", va=va,
                        color=C_TITLE, fontsize=6)
            ax.axhline(0, color=C_TEXT, linewidth=0.8)
            ax.yaxis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _: f"{v:+.0f}"))

        ax.set_title(f"Final PnL — {label}", color=col_color, fontsize=9)
        ax.set_ylabel("Final PnL" if col_i == 0 else "", color=C_TEXT, fontsize=7)
        ax.grid(True, alpha=GRID_A, color=C_TEXT, axis="y")

    # Figure legend
    leg_handles = [
        mpatches.Patch(color=PALETTE[r][0], label=ROLES_LABELS[r])
        for r in roles_to_plot if r in PALETTE
    ]
    if whale_times:
        leg_handles.append(
            mlines.Line2D([], [], color=WHALE_COLOR, linestyle="--",
                          linewidth=0.9, label="Whale sweep"))
    fig.legend(handles=leg_handles, loc="upper right",
               facecolor=BG_PANEL, edgecolor=C_GRID,
               labelcolor=C_TITLE, fontsize=8)

    fig.suptitle(
        "Agent PnL — Cumulative mark-to-mid PnL over simulation\n"
        "PnL = Σ fill_qty × (mid−price) buys  /  (price−mid) sells",
        color=C_TITLE, fontsize=10, y=1.002)
    save(fig, "agent_pnl.png")


# ============================================================================
# Done
# ============================================================================

print("\n=== Charts produced ===")
for f in ["sweep_stability.png","sweep_pnl.png",
          "compare_stability.png","compare_pnl.png",
          "dashboard.png","agent_pnl.png"]:
    if Path(f).exists():
        print(f"  {f}")

plt.show()
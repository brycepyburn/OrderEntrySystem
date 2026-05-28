# Order Entry System & Hybrid Matching Engine

[cite_start]This repository is a forked version of an Order Entry System and Matching Engine, a collaborative project sponsored by Millennium Management[cite: 1, 3, 5]. My primary contributions to this project centered around the mathematical logic, simulation modeling, and algorithm design of the matching engine, while the core software implementation was handled by my teammates.

## 🏗️ System Architecture Overview

[cite_start]The underlying system is a C++ matching engine that utilizes a central limit order book with continuous matching[cite: 7, 8]. The architecture is designed for low latency and high throughput:
* [cite_start]**Price Trees:** Bids are maintained in a descending binary search tree (highest bid first), while asks are in an ascending tree (lowest ask first), ensuring $O(1)$ best-price lookup[cite: 13, 14, 20].
* [cite_start]**Order Queues:** Each price level utilizes a doubly linked list to maintain arrival order[cite: 16, 17]. [cite_start]Insertion and price level searches operate at $O(\log n)$[cite: 20].
* [cite_start]**Global Hashmap:** Maps unique Order IDs directly to their object references, decoupling cancellations from the tree structure and allowing for $O(1)$ cancellations[cite: 18, 19, 20].
* [cite_start]**Execution Flow:** Upon arrival, the engine traverses the tree to find the correct price level, runs `buildFillPlan` to determine allocation, and then triggers `executeFillPlan` to clear incoming volume and release fully wiped orders[cite: 23, 24, 25, 26, 27, 28, 29, 30].
* **Object Pool:** Pre-allocates 10,000 orders at startup to minimize the computational overhead of dynamic memory allocation during live trading.

## 🧠 Algorithmic Contribution: Hybrid Order Allocation

[cite_start]A standard FIFO allocation incentivizes rapid price finding but is vulnerable to instability, while Pro Rata incentivizes high-volume orders at the cost of speed[cite: 67, 69, 70, 74, 75, 77]. [cite_start]I designed a hybrid allocation algorithm that dynamically shifts between FIFO and Pro Rata based on real-time market stability[cite: 28, 66].

### The Stability Metric ($S$)
Market stability is calculated using three continuous variables:
* **Effective Liquidity ($L_{eff}$):** Measures market depth. [cite_start]It factors in volume and total orders, applying a square-root diminishing return on order count and exponential decay on prices further from the spread[cite: 80, 81, 82].
* [cite_start]**Cancel Volatility ($H_c$):** Spikes when resting orders are canceled, decaying exponentially over fixed microsecond intervals[cite: 83, 84].
* [cite_start]**Price Volatility ($H_p$):** Spikes based on the magnitude of price changes, also decaying exponentially[cite: 86, 87].

High stability requires high liquidity and low volatility. The overarching stability metric is defined as:

$$S = \frac{L_{eff}}{1 + H_c + H_p}$$

### Dynamic Allocation Function
[cite_start]To balance the needs of High-Frequency Traders (HFTs) and stabilize the book, the engine guarantees a baseline of 50% volume allocated via FIFO[cite: 94]. [cite_start]The remaining 50% is dynamically allocated based on the stability metric $S$[cite: 95]. 

[cite_start]We apply an exponential mapping function so that the allocation percentage adjusts smoothly, curving slowly during periods of low stability to prevent jarring execution changes[cite: 96]:

$$FIFO\% = 50 + \frac{50}{e^k - 1} \cdot \left(e^{\frac{k \cdot S}{3000}} - 1\right)$$

*(Where $k$ represents the heat-function curvature constant, optimized during stress testing).*

## 📉 Stress Testing & Simulation Environment

[cite_start]To validate the allocation logic, I designed a phased stress-test environment[cite: 121, 127]. The C++ engine acts purely as the order entry system; it has no concept of a "fair price." 

### Price Dynamics
[cite_start]A simulated true fair value updates via a random walk every 100ms[cite: 124].
* **98%** of the time, the price shifts by a small normally-distributed amount ($N(0, 1.5)$ ticks)[cite: 125].
* [cite_start]**2%** of the time, the price jumps by $\pm 30$ ticks, simulating major news events[cite: 126].

### Agent Ecosystem & Information Asymmetry
[cite_start]Agents read from a shared market environment but operate with varying degrees of precision regarding the true fair value, represented by their signal standard deviation ($\sigma$)[cite: 135, 136]:

* **Market Maker ($\sigma = 1$):** Highly accurate. Posts 2-sided limits (500 shares per side) at $\pm 2$ ticks from perceived fair value, resetting every 500ms to maintain a 4-tick spread[cite: 137].
* **HFT ($\sigma = 3$):** Moderately informed. [cite_start]Posts 1-lot orders at $\pm 1$ tick at 20Hz, attempting to capture flow[cite: 138, 139]. Their lower accuracy often results in posting on the wrong side of sharp price moves.
* **Retail ($\sigma = 10$):** Noisy and uninformed. [cite_start]Submits frequent small market orders and occasional aggressive limit orders (modeling momentum traders)[cite: 142].
* **Whale:** Executes massive 3000-lot market sweeps every 10-20 seconds to intentionally stress test book depth. These orders have no price limit and consume liquidity until filled[cite: 141].

## 📎 Documentation & Results

* [**Read the Full Presentation Here**](#) *(Link to your PDF)*
* [**View Latency & Stress Test Results**](#) *(Link to your notable results/graphs)*

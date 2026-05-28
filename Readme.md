# Order Entry System & Hybrid Matching Engine

This repo is forked from our group's original project repo. The project, sponsored by Millennium, was to design an Order Entry System and Matching Engine. The underlying system is a C++ matching engine that utilizes a central limit order book with continuous matching, designed to minimize latency. My primary contributions to the project centered around the logic / simulation design / algorithm design of the matching engine, specifically the order allocation method. The software implementation was entirely handled by my teammates.

[**Link to Final Presentation**](Final Presentation)

## Hybrid Order Allocation

A standard FIFO allocation incentivizes rapid price finding but does not incentivize order book depth, while Pro Rata methods incentivize high-volume orders at the cost of speed / price finding. I built a hybrid allocation algorithm that decides percentage breakdown between FIFO and Pro Rata based on real-time market stability.

### Stability Metric
Market stability is calculated using three continuous variables:
* **Effective Liquidity ($L_{eff} = \text{(Total Shares)}\times\sqrt{\text{Total Orders}}$):** Measures market depth, applying a square-root diminishing return on order count to make sure agents can't break down large orders into many small orders to manipulate the stability variable. Exponential decay is applied on price levels ``worse" than the best price level.
* **Cancel Volatility ($H_c$):** Spikes when resting orders are canceled, decaying exponentially over fixed microsecond intervals.
* **Price Volatility ($H_p$):** Spikes based on the magnitude of price changes, also decaying exponentially.

High stability requires high liquidity and low volatility. We use this ratio to define book stability:

$$S=\frac{L_{eff}}{1+H_{c}+H_{p}}$$

### Allocation Function
To guarantee some level of price finding is always incentivized, the engine guarantees a baseline of 50% volume allocated via FIFO. The remaining 50% is allocated according to $S$. We used an exponential function so that allocation changes slower in times of low stability. The function is as follows:

$$FIFO=50+\frac{50}{e^{k}-1}\cdot(e^{\frac{k\cdot S}{3000}}-1)$$

*(We tested several constants k to determine which growth rate worked the best in a simulated environment).*

[**Link to the full argument / definitions for the hybrid allocation model**](FIFO_vs_Pro_Rata.pdf)

## Stress Testing & Simulation Environment

To validate the allocation logic, we designed a simulation in Python. A simulated true fair value updates via a random walk every 100ms.
* 98% of the time, the price shifts by a small normally-distributed amount (mean 0, std 1.5 ticks).
* 2% of the time, the price jumps by $\pm 30$ ticks, simulating major news events.

Several agents of varying levels of precision regarding true fair value (given different signal standard deviations, trading frequency, and trading volume):
* **Market Maker:** Posts 2-sided limits (500 shares per side) at $\pm 2$ ticks from perceived fair value, resetting every 500ms to maintain a 4-tick spread.
* **HFT:** Posts 1-lot orders at $\pm 1$ tick.
* **Retail:** Noisy + uninformed. Submits frequent small market orders and occasional aggressive limit orders (momentum traders).
* **Whale:** Executes massive 3000-lot market sweeps every 10-20 seconds to intentionally stress test book depth. These orders have no price limit and consume liquidity until filled.

* [**View Latency & Stress Test Results**](#) *(Link here in a sec)*

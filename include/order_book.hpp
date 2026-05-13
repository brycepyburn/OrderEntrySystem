#pragma once

#include "price_level.hpp"
#include <chrono>
#include <functional>
#include <map>
#include <mutex>
#include <unordered_map>
#include <vector>
#include <fstream>

enum class AllocationMode { FIFO, PRO_RATA, HYBRID };

struct HybridConfig {
    // ---------------------------------------------------------------------------
    // Heat-function parameters (see computeHybridFifoShare)
    //
    // The FIFO share is determined by:
    //
    //   f(x) = 50 / (e^k - 1) * (e^(k*x/stability_max) - 1) + 50
    //
    // where x = clamp(stability, 0, stability_max).
    //
    // f maps x ∈ [0, stability_max] → fifo_share ∈ [50%, 100%].
    //
    //   k controls curvature:
    //     k → 0   : nearly linear (50% at x=0, 100% at x=stability_max)
    //     k = 4.5 : moderate convexity (stays near 50% for most of the range,
    //               then climbs sharply near stability_max)
    //     k large : step-function-like (almost always 50% until very stable)
    //
    // The output is then re-scaled into [min_fifo_share, 1.0] so that
    // min_fifo_share acts as a hard floor regardless of k.
    // ---------------------------------------------------------------------------
    double k               = 4.5;   // curvature of the heat/FIFO-share mapping
    double stability_max   = 3000.0; // S value that maps to 100% FIFO share

    double min_fifo_share = 0.5;    // hard floor: FIFO always gets at least this
    double stability_alpha = 0.5;   // kept for backward compat, unused in heat fn
    double cancel_decay = 0.95;
    double price_decay = 0.95;
    int price_decay_interval_ms = 10;
    Quantity min_midpoint_guard_volume = 1;
    size_t depth_levels = 2;
};

struct OrderBookConfig {
    AllocationMode allocation_mode = AllocationMode::FIFO;
    HybridConfig hybrid{};
};

struct MatchingDiagnostics {
    double effective_liquidity = 0.0;
    double cancel_heat = 0.0;
    double price_heat = 0.0;
    double stability = 0.0;
    double fifo_share = 1.0;
    double avg_fill_age_ms = 0.0;
    uint64_t total_fills = 0;
};

class OrderBook {
private:
    using Clock = std::chrono::steady_clock;
    using TradeCallback = std::function<void(OrderID, Price, Quantity, double /*mid_at_fill*/)>;
    uint64_t total_fill_time_ms_ = 0;
    uint64_t fill_count_ = 0;

    struct LevelOrderView {
        Order* order = nullptr;
        Quantity qty = 0;
        size_t fifo_rank = 0;
    };

    struct FillInstruction {
        Order* order = nullptr;
        Quantity qty = 0;
    };

    mutable std::mutex book_mutex;

    // Balanced BST for bid side (descending order - highest price first)
    std::map<Price, PriceLevel, std::greater<Price>> bids;

    // Balanced BST for ask side (ascending order - lowest price first)
    std::map<Price, PriceLevel, std::less<Price>> asks;

    // Order map for O(1) lookup by OrderID
    std::unordered_map<OrderID, Order*> order_map;

    // Top of book cache
    Price best_bid_price = NO_BID;
    Price best_ask_price = NO_ASK;

    OrderBookConfig config_;
    MatchingDiagnostics diagnostics_;
    double cancel_heat_ = 0.0;
    double price_heat_ = 0.0;
    Clock::time_point last_price_decay_;
    bool midpoint_valid_ = false;
    double last_midpoint_ = 0.0;

    bool canOrderMatchPrice(const Order* order, Price price) const;
    bool passesFokCheck(const Order* order) const;
    Quantity matchIncomingOrder(Order* order, const TradeCallback& onTradeExecution);
    Quantity executeBestLevel(Order* order, const TradeCallback& onTradeExecution);
    std::vector<LevelOrderView> snapshotLevel(const PriceLevel& level) const;
    std::vector<Quantity> allocateFifo(const std::vector<LevelOrderView>& orders, Quantity target_qty) const;
    std::vector<Quantity> allocateProRata(const std::vector<LevelOrderView>& orders, Quantity target_qty) const;
    std::vector<FillInstruction> buildFillPlan(const PriceLevel& level, Quantity target_qty);
    Quantity executeFillPlan(
        Order* incoming_order,
        Side resting_side,
        Price price,
        PriceLevel& level,
        const std::vector<FillInstruction>& fill_plan,
        const TradeCallback& onTradeExecution);
    void restOrder(Order* order);
    void updateBestBid();
    void updateBestAsk();
    void removeEmptyPriceLevel(Side side, Price price);
    void releaseRestingOrders();
    double levelDecay(Price reference_price, Price level_price) const;
    double computeEffectiveLiquidity() const;
    double computeHybridFifoShare(double stability) const;
    void applyPriceHeatDecay(const Clock::time_point& now);
    void updateMidpointHeat(const Clock::time_point& now);
    MatchingDiagnostics computeDiagnosticsSnapshotLocked() const;
    void syncDiagnosticsLocked();

    // Legacy fields kept so existing .cpp code compiles
    double h_p = 0.0;
    double h_c = 0.0;
    uint64_t last_decay_time_ms = 0;
    const int L_EFF_DEPTH = 5;

public:
    explicit OrderBook(OrderBookConfig config = {});
    ~OrderBook();

    // Core operations
    bool addOrder(Order* order, TradeCallback onTradeExecution);
    bool cancelOrder(OrderID id);
    void match(Order* incoming_order, TradeCallback onTradeExecution);

    // Accessors
    Price getBestBid() const;
    Price getBestAsk() const;
    Quantity getVolumeAtPrice(Side side, Price price) const;
    size_t getOrderCountAtPrice(Side side, Price price) const;
    Order* getOrder(OrderID id) const;
    MatchingDiagnostics getDiagnostics() const;

    bool canMatch() const;

    // Telemetry
    void recordCancelHeat();
    void recordPriceHeat(Price old_price, Price new_price);
    void tickTelemetry();

    // Debugging
    void printBook() const;
    size_t getBidLevelCount() const;
    size_t getAskLevelCount() const;

    void apply_decay(uint64_t current_time_ms);
    double calculate_l_eff() const;
    double calculate_s() const;
    void log_metrics(uint64_t current_time_ms);
};
#include "order_book.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <utility>

namespace {

template <typename MapType>
const PriceLevel* findLevel(const MapType& levels, Price price) {
    auto it = levels.find(price);
    return it != levels.end() ? &it->second : nullptr;
}

} // namespace

OrderBook::OrderBook(OrderBookConfig config)
    : config_(std::move(config)), last_price_decay_(Clock::now()) {
    config_.hybrid.min_fifo_share = std::clamp(config_.hybrid.min_fifo_share, 0.0, 1.0);
    config_.hybrid.cancel_decay = std::clamp(config_.hybrid.cancel_decay, 0.0, 1.0);
    config_.hybrid.price_decay = std::clamp(config_.hybrid.price_decay, 0.0, 1.0);
    config_.hybrid.price_decay_interval_ms = std::max(1, config_.hybrid.price_decay_interval_ms);
    config_.hybrid.depth_levels = std::max<size_t>(1, config_.hybrid.depth_levels);
    config_.hybrid.min_midpoint_guard_volume = std::max<Quantity>(1, config_.hybrid.min_midpoint_guard_volume);
    syncDiagnosticsLocked();
}

// ---------------------------------------------------------------------------
// Heat recording — MUST be called while book_mutex is already held.
// These are private helpers; the public recordCancelHeat / recordPriceHeat
// wrappers that acquire the lock have been removed to prevent double-locking.
// ---------------------------------------------------------------------------

// BUG FIX #1: recordCancelHeat / recordPriceHeat were public and each tried to
// lock book_mutex.  Every call site (cancelOrder, updateBestAsk, etc.) already
// holds the lock, so those functions would deadlock.  The logic is now inlined
// directly at each call site, eliminating the lock-inside-lock hazard.

// ---------------------------------------------------------------------------
// tickTelemetry — called by the telemetry thread every 10 ms
// ---------------------------------------------------------------------------

void OrderBook::tickTelemetry() {
    std::lock_guard<std::mutex> lock(book_mutex);

    // BUG FIX #2: original code applied 0.95 decay TWICE per call (once at the
    // top and once at the bottom of the function), resulting in 0.9025 effective
    // decay per tick.  Apply it exactly once here.
    cancel_heat_ *= config_.hybrid.cancel_decay;
    price_heat_  *= config_.hybrid.price_decay;

    // BUG FIX #3: original tickTelemetry computed L_eff as just
    // top_bid_vol + top_ask_vol, completely ignoring the sophisticated
    // decay-weighted computeEffectiveLiquidity() method.  Use it here.
    syncDiagnosticsLocked();
}

void OrderBook::apply_decay(uint64_t current_time_ms) {
    if (last_decay_time_ms == 0) {
        last_decay_time_ms = current_time_ms;
        return;
    }

    uint64_t elapsed = current_time_ms - last_decay_time_ms;
    int intervals = elapsed / 10;

    if (intervals > 0) {
        double decay_factor = std::pow(config_.hybrid.cancel_decay, intervals);
        cancel_heat_ *= decay_factor;
        price_heat_  *= decay_factor;
        last_decay_time_ms += intervals * 10;
    }
}

// BUG FIX #4: calculate_l_eff() was stubbed out and always returned 0.
// Delegate to the real implementation.
double OrderBook::calculate_l_eff() const {
    return computeEffectiveLiquidity();
}

double OrderBook::calculate_s() const {
    double l_eff = calculate_l_eff();
    return l_eff / (cancel_heat_ + price_heat_ + 1.0);
}

void OrderBook::log_metrics(uint64_t /*current_time_ms*/) {
    // Intentionally left as a no-op; callers should use getDiagnostics().
}

OrderBook::~OrderBook() {
    std::lock_guard<std::mutex> lock(book_mutex);
    releaseRestingOrders();
}

bool OrderBook::addOrder(Order* order, TradeCallback onTradeExecution) {
    std::lock_guard<std::mutex> lock(book_mutex);
    if (!order || order->qty <= 0) return false;
    if (order_map.find(order->id) != order_map.end()) return false;

    const Clock::time_point now = Clock::now();
    applyPriceHeatDecay(now);

    if (order->tif == TimeInForce::FOK && !passesFokCheck(order)) {
        syncDiagnosticsLocked();
        return false;
    }

    const Quantity filled_qty = matchIncomingOrder(order, onTradeExecution);
    if (filled_qty > 0) {
        cancel_heat_ *= config_.hybrid.cancel_decay;
    }

    if (order->qty > 0) {
        if (order->type == OrderType::MARKET || order->tif == TimeInForce::IOC) {
            updateMidpointHeat(now);
            syncDiagnosticsLocked();
            return true;
        }
        restOrder(order);
    }

    updateMidpointHeat(now);
    syncDiagnosticsLocked();
    return true;
}

bool OrderBook::cancelOrder(OrderID id) {
    std::lock_guard<std::mutex> lock(book_mutex);
    auto it = order_map.find(id);
    if (it == order_map.end()) return false;

    Order* order = it->second;
    const Price price = order->price;

    if (order->side == Side::BUY) {
        auto level_it = bids.find(price);
        if (level_it != bids.end()) {
            level_it->second.remove(order);
            if (level_it->second.isEmpty()) {
                bids.erase(level_it);
                // Update best bid inline (no separate locked helper)
                best_bid_price = bids.empty() ? NO_BID : bids.begin()->first;
            }
        }
    } else {
        auto level_it = asks.find(price);
        if (level_it != asks.end()) {
            level_it->second.remove(order);
            if (level_it->second.isEmpty()) {
                asks.erase(level_it);
                // Update best ask inline (no separate locked helper)
                best_ask_price = asks.empty() ? NO_ASK : asks.begin()->first;
            }
        }
    }

    order_map.erase(it);
    OrderPool::getInstance().release(order);

    // BUG FIX #5: original cancelOrder called recordCancelHeat() at the end,
    // which tried to lock book_mutex again — deadlock.  Inline the heat update.
    cancel_heat_ += 1.0;

    const Clock::time_point now = Clock::now();
    updateMidpointHeat(now);
    syncDiagnosticsLocked();
    return true;
}

void OrderBook::match(Order* incoming_order, TradeCallback onTradeExecution) {
    std::lock_guard<std::mutex> lock(book_mutex);
    if (!incoming_order || incoming_order->qty <= 0) return;

    const Clock::time_point now = Clock::now();
    applyPriceHeatDecay(now);

    const Quantity filled_qty = matchIncomingOrder(incoming_order, onTradeExecution);
    if (filled_qty > 0) {
        cancel_heat_ *= config_.hybrid.cancel_decay;
    }

    updateMidpointHeat(now);
    syncDiagnosticsLocked();
}

bool OrderBook::canOrderMatchPrice(const Order* order, Price price) const {
    if (!order) return false;
    if (order->type == OrderType::MARKET) return true;
    if (order->side == Side::BUY) return price <= order->price;
    return price >= order->price;
}

bool OrderBook::passesFokCheck(const Order* order) const {
    Quantity available_qty = 0;

    if (order->side == Side::BUY) {
        for (const auto& [price, level] : asks) {
            if (!canOrderMatchPrice(order, price)) break;
            available_qty += level.total_volume;
            if (available_qty >= order->qty) return true;
        }
        return false;
    }

    for (const auto& [price, level] : bids) {
        if (!canOrderMatchPrice(order, price)) break;
        available_qty += level.total_volume;
        if (available_qty >= order->qty) return true;
    }
    return false;
}

Quantity OrderBook::matchIncomingOrder(Order* order, const TradeCallback& onTradeExecution) {
    Quantity filled_qty = 0;

    while (order->qty > 0) {
        const Quantity level_fill = executeBestLevel(order, onTradeExecution);
        if (level_fill <= 0) break;
        filled_qty += level_fill;
    }

    return filled_qty;
}

Quantity OrderBook::executeBestLevel(Order* order, const TradeCallback& onTradeExecution) {
    if (order->side == Side::BUY) {
        if (asks.empty()) return 0;
        auto best_ask = asks.begin();
        if (!canOrderMatchPrice(order, best_ask->first)) return 0;

        const Quantity target_qty = std::min(order->qty, best_ask->second.total_volume);
        const auto fill_plan = buildFillPlan(best_ask->second, target_qty);
        return executeFillPlan(order, Side::SELL, best_ask->first, best_ask->second, fill_plan, onTradeExecution);
    }

    if (bids.empty()) return 0;
    auto best_bid = bids.begin();
    if (!canOrderMatchPrice(order, best_bid->first)) return 0;

    const Quantity target_qty = std::min(order->qty, best_bid->second.total_volume);
    const auto fill_plan = buildFillPlan(best_bid->second, target_qty);
    return executeFillPlan(order, Side::BUY, best_bid->first, best_bid->second, fill_plan, onTradeExecution);
}

std::vector<OrderBook::LevelOrderView> OrderBook::snapshotLevel(const PriceLevel& level) const {
    std::vector<LevelOrderView> orders;
    auto resting_orders = level.snapshotOrders();
    orders.reserve(resting_orders.size());

    for (size_t i = 0; i < resting_orders.size(); ++i) {
        orders.push_back(LevelOrderView{resting_orders[i], resting_orders[i]->qty, i});
    }

    return orders;
}

std::vector<Quantity> OrderBook::allocateFifo(const std::vector<LevelOrderView>& orders, Quantity target_qty) const {
    std::vector<Quantity> fills(orders.size(), 0);
    Quantity remaining = target_qty;

    for (size_t i = 0; i < orders.size() && remaining > 0; ++i) {
        const Quantity fill_qty = std::min(remaining, orders[i].qty);
        fills[i] = fill_qty;
        remaining -= fill_qty;
    }

    return fills;
}

std::vector<Quantity> OrderBook::allocateProRata(const std::vector<LevelOrderView>& orders, Quantity target_qty) const {
    std::vector<Quantity> fills(orders.size(), 0);
    if (target_qty <= 0 || orders.empty()) return fills;

    Quantity total_qty = 0;
    for (const auto& order : orders) {
        total_qty += order.qty;
    }

    if (total_qty <= 0) return fills;

    const Quantity capped_target = std::min(target_qty, total_qty);
    Quantity allocated = 0;

    struct RemainderEntry {
        size_t index = 0;
        int64_t remainder = 0;
        size_t fifo_rank = 0;
    };

    std::vector<RemainderEntry> remainders;
    remainders.reserve(orders.size());

    for (size_t i = 0; i < orders.size(); ++i) {
        const int64_t numerator = static_cast<int64_t>(capped_target) * static_cast<int64_t>(orders[i].qty);
        const Quantity base_fill = static_cast<Quantity>(numerator / total_qty);
        fills[i] = std::min(base_fill, orders[i].qty);
        allocated += fills[i];

        if (fills[i] < orders[i].qty) {
            remainders.push_back(RemainderEntry{i, numerator % total_qty, orders[i].fifo_rank});
        }
    }

    std::sort(remainders.begin(), remainders.end(), [](const RemainderEntry& lhs, const RemainderEntry& rhs) {
        if (lhs.remainder != rhs.remainder) return lhs.remainder > rhs.remainder;
        return lhs.fifo_rank < rhs.fifo_rank;
    });

    Quantity leftover = capped_target - allocated;
    for (const auto& entry : remainders) {
        if (leftover <= 0) break;
        if (fills[entry.index] >= orders[entry.index].qty) continue;
        ++fills[entry.index];
        --leftover;
    }

    return fills;
}

std::vector<OrderBook::FillInstruction> OrderBook::buildFillPlan(const PriceLevel& level, Quantity target_qty) {
    const Quantity capped_target = std::min(target_qty, level.total_volume);
    if (capped_target <= 0) return {};

    const auto orders = snapshotLevel(level);
    if (orders.empty()) return {};

    std::vector<Quantity> fills(orders.size(), 0);

    if (config_.allocation_mode == AllocationMode::FIFO) {
        fills = allocateFifo(orders, capped_target);
    } else if (config_.allocation_mode == AllocationMode::PRO_RATA) {
        fills = allocateProRata(orders, capped_target);
    } else {
        diagnostics_ = computeDiagnosticsSnapshotLocked();
        const Quantity fifo_target = static_cast<Quantity>(std::clamp(
            static_cast<int64_t>(std::ceil(static_cast<double>(capped_target) * diagnostics_.fifo_share)),
            static_cast<int64_t>(0),
            static_cast<int64_t>(capped_target)));

        const auto fifo_fills = allocateFifo(orders, fifo_target);
        std::vector<LevelOrderView> residual_orders = orders;
        for (size_t i = 0; i < residual_orders.size(); ++i) {
            residual_orders[i].qty -= fifo_fills[i];
        }

        const auto pro_rata_fills = allocateProRata(residual_orders, capped_target - fifo_target);
        for (size_t i = 0; i < fills.size(); ++i) {
            fills[i] = fifo_fills[i] + pro_rata_fills[i];
        }
    }

    std::vector<FillInstruction> fill_plan;
    fill_plan.reserve(orders.size());
    for (size_t i = 0; i < orders.size(); ++i) {
        if (fills[i] > 0) {
            fill_plan.push_back(FillInstruction{orders[i].order, fills[i]});
        }
    }

    return fill_plan;
}

Quantity OrderBook::executeFillPlan(
    Order* incoming_order,
    Side resting_side,
    Price price,
    PriceLevel& level,
    const std::vector<FillInstruction>& fill_plan,
    const TradeCallback& onTradeExecution) {
    Quantity filled_qty = 0;

    for (const auto& fill : fill_plan) {
        if (!fill.order || fill.qty <= 0 || incoming_order->qty <= 0) continue;

        const auto now = Clock::now();
        const auto age = std::chrono::duration_cast<std::chrono::milliseconds>(
            now - fill.order->entry_time).count();

        total_fill_time_ms_ += age;
        fill_count_++;

        const Quantity fill_qty = std::min({fill.qty, incoming_order->qty, fill.order->qty});
        if (fill_qty <= 0) continue;

        if (onTradeExecution) {
            onTradeExecution(incoming_order->id, price, fill_qty);
            onTradeExecution(fill.order->id, price, fill_qty);
        }

        incoming_order->qty -= fill_qty;
        fill.order->qty   -= fill_qty;
        level.reduceVolume(fill_qty);
        filled_qty += fill_qty;

        if (fill.order->qty == 0) {
            level.remove(fill.order);
            order_map.erase(fill.order->id);
            OrderPool::getInstance().release(fill.order);
        }
    }

    if (level.isEmpty()) {
        removeEmptyPriceLevel(resting_side, price);
    }

    return filled_qty;
}

void OrderBook::restOrder(Order* order) {
    if (order->side == Side::BUY) {
        auto [it, inserted] = bids.try_emplace(order->price, PriceLevel(order->price));
        (void)inserted;
        it->second.add(order);
        if (order->price > best_bid_price) {
            best_bid_price = order->price;
        }
    } else {
        auto [it, inserted] = asks.try_emplace(order->price, PriceLevel(order->price));
        (void)inserted;
        it->second.add(order);
        if (order->price < best_ask_price) {
            best_ask_price = order->price;
        }
    }

    order_map[order->id] = order;
    order->entry_time = std::chrono::steady_clock::now();
}

void OrderBook::updateBestBid() {
    Price old_best = best_bid_price;
    best_bid_price = bids.empty() ? NO_BID : bids.begin()->first;
    // Inline heat update — caller already holds book_mutex
    if (old_best != best_bid_price && old_best != NO_BID && best_bid_price != NO_BID) {
        price_heat_ += std::abs(static_cast<double>(best_bid_price - old_best));
    }
}

void OrderBook::updateBestAsk() {
    // BUG FIX #6: original code read `best_bid_price` as the old ask price.
    // It must read `best_ask_price`.
    Price old_best = best_ask_price;
    best_ask_price = asks.empty() ? NO_ASK : asks.begin()->first;
    // Inline heat update — caller already holds book_mutex
    if (old_best != best_ask_price && old_best != NO_ASK && best_ask_price != NO_ASK) {
        price_heat_ += std::abs(static_cast<double>(best_ask_price - old_best));
    }
}

void OrderBook::removeEmptyPriceLevel(Side side, Price price) {
    if (side == Side::BUY) {
        auto it = bids.find(price);
        if (it != bids.end() && it->second.isEmpty()) {
            bids.erase(it);
            updateBestBid();
        }
        return;
    }

    auto it = asks.find(price);
    if (it != asks.end() && it->second.isEmpty()) {
        asks.erase(it);
        updateBestAsk();
    }
}

void OrderBook::releaseRestingOrders() {
    auto release_side = [](auto& side_levels) {
        for (auto& [price, level] : side_levels) {
            (void)price;
            Order* current = level.head;
            while (current != nullptr) {
                Order* next = current->next;
                OrderPool::getInstance().release(current);
                current = next;
            }
        }
        side_levels.clear();
    };

    release_side(bids);
    release_side(asks);
    order_map.clear();
    best_bid_price = NO_BID;
    best_ask_price = NO_ASK;
}

double OrderBook::levelDecay(Price reference_price, Price level_price) const {
    if (reference_price <= 0 || reference_price == NO_ASK) return 0.0;
    if (reference_price == level_price) return 1.0;

    const double distance_ratio =
        std::fabs(static_cast<double>(level_price - reference_price)) / static_cast<double>(reference_price);

    if (distance_ratio <= 0.10) return 0.5;
    if (distance_ratio <= 0.20) return 0.25;
    return 0.0;
}

double OrderBook::computeEffectiveLiquidity() const {
    double effective_liquidity = 0.0;

    auto accumulate_side = [&](const auto& side_levels, Price reference_price) {
        if (side_levels.empty()) return;

        size_t levels_seen = 0;
        for (const auto& [price, level] : side_levels) {
            if (levels_seen >= config_.hybrid.depth_levels) break;
            if (level.total_volume <= 0 || level.order_count == 0) continue;

            effective_liquidity +=
                (static_cast<double>(level.total_volume) / std::sqrt(static_cast<double>(level.order_count))) *
                levelDecay(reference_price, price);
            ++levels_seen;
        }
    };

    if (!bids.empty()) {
        accumulate_side(bids, bids.begin()->first);
    }
    if (!asks.empty()) {
        accumulate_side(asks, asks.begin()->first);
    }

    return effective_liquidity;
}

double OrderBook::computeHybridFifoShare(double stability) const {
    if (config_.allocation_mode == AllocationMode::FIFO) return 1.0;
    if (config_.allocation_mode == AllocationMode::PRO_RATA) return 0.0;

    const double floor = config_.hybrid.min_fifo_share;
    if (config_.hybrid.stability_alpha <= 0.0 || stability <= 0.0) return floor;

    const double growth = 1.0 - std::exp(-config_.hybrid.stability_alpha * stability);
    return std::clamp(floor + (1.0 - floor) * growth, floor, 1.0);
}

void OrderBook::applyPriceHeatDecay(const Clock::time_point& now) {
    const auto elapsed_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(now - last_price_decay_).count();
    if (elapsed_ms < config_.hybrid.price_decay_interval_ms) return;

    const int64_t decay_steps = elapsed_ms / config_.hybrid.price_decay_interval_ms;
    price_heat_ *= std::pow(config_.hybrid.price_decay, static_cast<double>(decay_steps));
    last_price_decay_ += std::chrono::milliseconds(decay_steps * config_.hybrid.price_decay_interval_ms);
}

void OrderBook::updateMidpointHeat(const Clock::time_point& now) {
    applyPriceHeatDecay(now);

    if (bids.empty() || asks.empty()) {
        midpoint_valid_ = false;
        return;
    }

    const auto& best_bid_level = bids.begin()->second;
    const auto& best_ask_level = asks.begin()->second;
    if (best_bid_level.total_volume < config_.hybrid.min_midpoint_guard_volume ||
        best_ask_level.total_volume < config_.hybrid.min_midpoint_guard_volume) {
        midpoint_valid_ = false;
        return;
    }

    const double midpoint = (static_cast<double>(best_bid_price) + static_cast<double>(best_ask_price)) / 2.0;
    if (midpoint_valid_) {
        price_heat_ += std::fabs(midpoint - last_midpoint_);
    }

    last_midpoint_ = midpoint;
    midpoint_valid_ = true;
}

MatchingDiagnostics OrderBook::computeDiagnosticsSnapshotLocked() const {
    MatchingDiagnostics snapshot;
    snapshot.effective_liquidity = computeEffectiveLiquidity();
    snapshot.cancel_heat         = cancel_heat_;
    snapshot.price_heat          = price_heat_;
    snapshot.stability           = snapshot.effective_liquidity / (snapshot.cancel_heat + snapshot.price_heat + 1.0);
    snapshot.fifo_share          = computeHybridFifoShare(snapshot.stability);
    snapshot.avg_fill_age_ms     = (fill_count_ > 0) ?
        static_cast<double>(total_fill_time_ms_) / fill_count_ : 0.0;
    snapshot.total_fills         = fill_count_;
    return snapshot;
}

void OrderBook::syncDiagnosticsLocked() {
    diagnostics_ = computeDiagnosticsSnapshotLocked();
}

Price OrderBook::getBestBid() const {
    std::lock_guard<std::mutex> lock(book_mutex);
    return best_bid_price;
}

Price OrderBook::getBestAsk() const {
    std::lock_guard<std::mutex> lock(book_mutex);
    return best_ask_price;
}

Quantity OrderBook::getVolumeAtPrice(Side side, Price price) const {
    std::lock_guard<std::mutex> lock(book_mutex);
    if (side == Side::BUY) {
        const PriceLevel* level = findLevel(bids, price);
        return level ? level->total_volume : 0;
    }

    const PriceLevel* level = findLevel(asks, price);
    return level ? level->total_volume : 0;
}

size_t OrderBook::getOrderCountAtPrice(Side side, Price price) const {
    std::lock_guard<std::mutex> lock(book_mutex);
    if (side == Side::BUY) {
        const PriceLevel* level = findLevel(bids, price);
        return level ? level->order_count : 0;
    }

    const PriceLevel* level = findLevel(asks, price);
    return level ? level->order_count : 0;
}

Order* OrderBook::getOrder(OrderID id) const {
    std::lock_guard<std::mutex> lock(book_mutex);
    auto it = order_map.find(id);
    return it != order_map.end() ? it->second : nullptr;
}

MatchingDiagnostics OrderBook::getDiagnostics() const {
    std::lock_guard<std::mutex> lock(book_mutex);
    return diagnostics_;
}

bool OrderBook::canMatch() const {
    std::lock_guard<std::mutex> lock(book_mutex);
    return !bids.empty() && !asks.empty() && best_bid_price >= best_ask_price;
}

// Public stubs kept for ABI compatibility with existing callers.
// They now no-op because the internal state is updated inline everywhere.
void OrderBook::recordCancelHeat() {
    // Heat is applied inline at each cancel site; this stub is intentionally empty.
}

void OrderBook::recordPriceHeat(Price /*old_price*/, Price /*new_price*/) {
    // Heat is applied inline at updateBestBid/updateBestAsk; this stub is intentionally empty.
}

void OrderBook::printBook() const {
    std::lock_guard<std::mutex> lock(book_mutex);

    std::cout << "=== ORDER BOOK ===" << std::endl;

    std::cout << "ASKS (sell orders):" << std::endl;
    for (auto it = asks.rbegin(); it != asks.rend(); ++it) {
        std::cout << "  $" << it->first / 100.0 << " : "
                  << it->second.total_volume << " shares in "
                  << it->second.order_count << " orders" << std::endl;
    }

    std::cout << "-------------------" << std::endl;

    std::cout << "BIDS (buy orders):" << std::endl;
    for (const auto& [price, level] : bids) {
        std::cout << "  $" << price / 100.0 << " : "
                  << level.total_volume << " shares in "
                  << level.order_count << " orders" << std::endl;
    }
}

size_t OrderBook::getBidLevelCount() const {
    std::lock_guard<std::mutex> lock(book_mutex);
    return bids.size();
}

size_t OrderBook::getAskLevelCount() const {
    std::lock_guard<std::mutex> lock(book_mutex);
    return asks.size();
}
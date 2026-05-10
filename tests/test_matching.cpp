#include "order_book.hpp"

#include <chrono>
#include <cmath>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {

struct Trade {
    OrderID order_id = 0;
    Price price = 0;
    Quantity qty = 0;
};

void expect(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

template <typename T, typename U>
void expectEqual(const T& actual, const U& expected, const std::string& message) {
    if (!(actual == expected)) {
        throw std::runtime_error(message);
    }
}

void expectNear(double actual, double expected, double tolerance, const std::string& message) {
    if (std::fabs(actual - expected) > tolerance) {
        throw std::runtime_error(message);
    }
}

Order* makeOrder(
    OrderID id,
    Side side,
    Price price,
    Quantity qty,
    OrderType type = OrderType::LIMIT,
    TimeInForce tif = TimeInForce::GTC) {
    Order* order = OrderPool::getInstance().acquire();
    order->id = id;
    order->side = side;
    order->type = type;
    order->tif = tif;
    order->price = price;
    order->qty = qty;
    order->next = nullptr;
    order->prev = nullptr;
    return order;
}

bool submitOrder(OrderBook& book, Order* order, std::vector<Trade>& trades) {
    auto callback = [&](OrderID order_id, Price price, Quantity qty) {
        trades.push_back(Trade{order_id, price, qty});
    };

    const bool success = book.addOrder(order, callback);
    if (!success || book.getOrder(order->id) == nullptr) {
        OrderPool::getInstance().release(order);
    }

    return success;
}

void testFifoSamePricePriority() {
    OrderBook book;
    std::vector<Trade> trades;

    expect(submitOrder(book, makeOrder(1, Side::SELL, 10000, 30), trades), "first resting sell should be accepted");
    expect(submitOrder(book, makeOrder(2, Side::SELL, 10000, 40), trades), "second resting sell should be accepted");

    trades.clear();
    expect(submitOrder(book, makeOrder(3, Side::BUY, 10000, 50, OrderType::LIMIT, TimeInForce::IOC), trades),
           "aggressive buy should be accepted");

    expect(book.getOrder(1) == nullptr, "oldest order should fill first");
    expect(book.getOrder(2) != nullptr, "second order should remain partially filled");
    expectEqual(book.getOrder(2)->qty, 20, "second order should retain the remaining quantity");
    expectEqual(book.getVolumeAtPrice(Side::SELL, 10000), 20, "level volume should reflect the partial fill");
    expectEqual(book.getOrderCountAtPrice(Side::SELL, 10000), static_cast<size_t>(1),
                "only one resting order should remain at the price level");
}

void testProRataDeterministicRemainders() {
    OrderBookConfig config;
    config.allocation_mode = AllocationMode::PRO_RATA;
    OrderBook book(config);
    std::vector<Trade> trades;

    expect(submitOrder(book, makeOrder(10, Side::SELL, 10000, 50), trades), "first pro-rata order should rest");
    expect(submitOrder(book, makeOrder(11, Side::SELL, 10000, 30), trades), "second pro-rata order should rest");
    expect(submitOrder(book, makeOrder(12, Side::SELL, 10000, 20), trades), "third pro-rata order should rest");

    trades.clear();
    expect(submitOrder(book, makeOrder(13, Side::BUY, 10000, 25, OrderType::LIMIT, TimeInForce::IOC), trades),
           "aggressive pro-rata buy should be accepted");

    expectEqual(book.getOrder(10)->qty, 37, "largest order should receive the remainder share");
    expectEqual(book.getOrder(11)->qty, 23, "second order should receive a proportional fill");
    expectEqual(book.getOrder(12)->qty, 15, "third order should receive a proportional fill");
    expectEqual(book.getVolumeAtPrice(Side::SELL, 10000), 75, "price-level volume should update after pro-rata fills");
    expectEqual(book.getOrderCountAtPrice(Side::SELL, 10000), static_cast<size_t>(3),
                "no order should be removed when all are partially filled");
}

void testHybridMinimumFifoFloor() {
    OrderBookConfig config;
    config.allocation_mode = AllocationMode::HYBRID;
    config.hybrid.stability_alpha = 0.0;
    OrderBook book(config);
    std::vector<Trade> trades;

    expect(submitOrder(book, makeOrder(20, Side::SELL, 10000, 10), trades), "first hybrid order should rest");
    expect(submitOrder(book, makeOrder(21, Side::SELL, 10000, 10), trades), "second hybrid order should rest");
    expect(submitOrder(book, makeOrder(22, Side::SELL, 10000, 10), trades), "third hybrid order should rest");

    trades.clear();
    expect(submitOrder(book, makeOrder(23, Side::BUY, 10000, 10, OrderType::LIMIT, TimeInForce::IOC), trades),
           "hybrid taker should be accepted");

    expectEqual(book.getOrder(20)->qty, 4, "FIFO floor should give the first order a head start");
    expectEqual(book.getOrder(21)->qty, 8, "remaining pro-rata slice should distribute across later orders");
    expectEqual(book.getOrder(22)->qty, 8, "remaining pro-rata slice should distribute across later orders");

    const MatchingDiagnostics diagnostics = book.getDiagnostics();
    expectNear(diagnostics.fifo_share, 0.5, 1e-9, "hybrid floor should clamp FIFO share at 50%");
}

void testHybridHighStabilityFavorsFifo() {
    OrderBookConfig config;
    config.allocation_mode = AllocationMode::HYBRID;
    config.hybrid.stability_alpha = 1.0;
    OrderBook book(config);
    std::vector<Trade> trades;

    expect(submitOrder(book, makeOrder(30, Side::SELL, 10000, 10), trades), "first stable hybrid order should rest");
    expect(submitOrder(book, makeOrder(31, Side::SELL, 10000, 10), trades), "second stable hybrid order should rest");
    expect(submitOrder(book, makeOrder(32, Side::SELL, 10000, 10), trades), "third stable hybrid order should rest");

    trades.clear();
    expect(submitOrder(book, makeOrder(33, Side::BUY, 10000, 12, OrderType::LIMIT, TimeInForce::IOC), trades),
           "stable hybrid taker should be accepted");

    expect(book.getOrder(30) == nullptr, "high stability should nearly behave like pure FIFO");
    expectEqual(book.getOrder(31)->qty, 8, "second order should receive the spillover after the first fills");
    expectEqual(book.getOrder(32)->qty, 10, "third order should remain untouched in a near-FIFO regime");
    expect(book.getDiagnostics().fifo_share > 0.99, "stable markets should bias the hybrid allocator toward FIFO");
}

void testPricePriorityAcrossLevels() {
    OrderBookConfig config;
    config.allocation_mode = AllocationMode::PRO_RATA;
    OrderBook book(config);
    std::vector<Trade> trades;

    expect(submitOrder(book, makeOrder(40, Side::SELL, 10000, 10), trades), "best ask should rest");
    expect(submitOrder(book, makeOrder(41, Side::SELL, 10000, 20), trades), "second best-level order should rest");
    expect(submitOrder(book, makeOrder(42, Side::SELL, 10001, 100), trades), "next price level should rest");

    trades.clear();
    expect(submitOrder(book, makeOrder(43, Side::BUY, 10001, 40, OrderType::LIMIT, TimeInForce::IOC), trades),
           "crossing order should be accepted");

    expect(book.getOrder(40) == nullptr, "best price level should be exhausted before moving deeper");
    expect(book.getOrder(41) == nullptr, "all liquidity at the best level should clear first");
    expect(book.getOrder(42) != nullptr, "next level should only be touched after best price is exhausted");
    expectEqual(book.getOrder(42)->qty, 90, "deeper price level should only lose the residual quantity");
    expectEqual(book.getBestAsk(), static_cast<Price>(10001), "best ask should roll to the next level");
}

void testTimeInForceAndMarketSemantics() {
    {
        OrderBook book;
        std::vector<Trade> trades;
        expect(submitOrder(book, makeOrder(50, Side::SELL, 10000, 10), trades), "FOK book should accept resting sell");
        trades.clear();
        expect(!submitOrder(book, makeOrder(51, Side::BUY, 10000, 20, OrderType::LIMIT, TimeInForce::FOK), trades),
               "FOK should fail when full liquidity is unavailable");
        expectEqual(book.getVolumeAtPrice(Side::SELL, 10000), 10, "failed FOK must leave the book unchanged");
    }

    {
        OrderBook book;
        std::vector<Trade> trades;
        expect(submitOrder(book, makeOrder(52, Side::SELL, 10000, 10), trades), "IOC book should accept resting sell");
        trades.clear();
        expect(submitOrder(book, makeOrder(53, Side::BUY, 10000, 15, OrderType::LIMIT, TimeInForce::IOC), trades),
               "IOC should process immediately");
        expectEqual(book.getBestAsk(), NO_ASK, "IOC should not leave any contra-side liquidity after a full fill");
        expect(book.getOrder(53) == nullptr, "IOC taker should never rest on the book");
    }

    {
        OrderBook book;
        std::vector<Trade> trades;
        expect(submitOrder(book, makeOrder(54, Side::SELL, 10000, 10), trades), "market book should accept resting sell");
        trades.clear();
        expect(submitOrder(book, makeOrder(55, Side::BUY, 0, 15, OrderType::MARKET, TimeInForce::GTC), trades),
               "market order should match regardless of limit price");
        expectEqual(book.getBestAsk(), NO_ASK, "market order should clear the available best ask");
        expect(book.getOrder(55) == nullptr, "market order should not rest even when partially filled");
    }
}

void testDiagnosticsAndMidpointGuard() {
    {
        OrderBookConfig config;
        config.allocation_mode = AllocationMode::HYBRID;
        config.hybrid.cancel_decay = 0.5;
        config.hybrid.price_decay = 0.5;
        config.hybrid.price_decay_interval_ms = 10;
        config.hybrid.min_midpoint_guard_volume = 5;

        OrderBook book(config);
        std::vector<Trade> trades;

        expect(submitOrder(book, makeOrder(60, Side::BUY, 9900, 10), trades), "bid should rest");
        expect(submitOrder(book, makeOrder(61, Side::SELL, 10100, 10), trades), "ask should rest");

        const MatchingDiagnostics initial = book.getDiagnostics();
        expectNear(initial.price_heat, 0.0, 1e-9, "initial midpoint should set the baseline without adding heat");

        expect(submitOrder(book, makeOrder(62, Side::BUY, 9950, 10), trades), "better bid should rest");
        const MatchingDiagnostics after_price_move = book.getDiagnostics();
        expectNear(after_price_move.price_heat, 25.0, 1e-9, "midpoint move should add absolute price delta heat");

        std::this_thread::sleep_for(std::chrono::milliseconds(15));
        expect(submitOrder(book, makeOrder(63, Side::BUY, 9800, 10), trades), "non-best bid should still trigger decay");
        const MatchingDiagnostics after_decay = book.getDiagnostics();
        expect(after_decay.price_heat < after_price_move.price_heat, "price heat should decay over time");

        expect(submitOrder(book, makeOrder(64, Side::SELL, 10150, 5), trades), "secondary ask should rest");
        expect(book.cancelOrder(64), "cancel should succeed");
        const MatchingDiagnostics after_cancel = book.getDiagnostics();
        expectNear(after_cancel.cancel_heat, 1.0, 1e-9, "cancel should increase cancel heat");

        trades.clear();
        expect(submitOrder(book, makeOrder(65, Side::BUY, 10100, 5, OrderType::LIMIT, TimeInForce::IOC), trades),
               "aggressive fill should succeed");
        const MatchingDiagnostics after_fill = book.getDiagnostics();
        expectNear(after_fill.cancel_heat, 0.5, 1e-9, "aggressive fill should decay cancel heat");
        expect(after_fill.effective_liquidity > 0.0, "diagnostics should track effective liquidity");
    }

    {
        OrderBookConfig config;
        config.allocation_mode = AllocationMode::HYBRID;
        config.hybrid.min_midpoint_guard_volume = 5;

        OrderBook book(config);
        std::vector<Trade> trades;

        expect(submitOrder(book, makeOrder(70, Side::BUY, 9900, 10), trades), "guard test bid should rest");
        expect(submitOrder(book, makeOrder(71, Side::SELL, 10100, 3), trades), "guard test ask should rest");
        expect(submitOrder(book, makeOrder(72, Side::BUY, 9950, 10), trades), "better bid should rest");

        const MatchingDiagnostics guarded = book.getDiagnostics();
        expectNear(guarded.price_heat, 0.0, 1e-9, "midpoint guard should suppress price heat on tiny best levels");
    }
}

} // namespace

int main() {
    const std::vector<std::pair<std::string, std::function<void()>>> tests = {
        {"FIFO same-price priority", testFifoSamePricePriority},
        {"Pro-rata deterministic remainders", testProRataDeterministicRemainders},
        {"Hybrid minimum FIFO floor", testHybridMinimumFifoFloor},
        {"Hybrid high stability", testHybridHighStabilityFavorsFifo},
        {"Price priority across levels", testPricePriorityAcrossLevels},
        {"Time-in-force and market semantics", testTimeInForceAndMarketSemantics},
        {"Diagnostics and midpoint guard", testDiagnosticsAndMidpointGuard},
    };

    for (const auto& [name, test] : tests) {
        test();
        std::cout << "[PASS] " << name << std::endl;
    }

    return 0;
}

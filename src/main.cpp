#include "crow_all.h"
#include "order_book.hpp"
#include <iostream>
#include <string>
#include <atomic>
#include <functional>
#include <mutex>
#include <set>

// Store active connections to broadcast trade reports
std::mutex connections_mutex;
std::set<crow::websocket::connection*> active_connections;

// BUG FIX #7: The original code had a second `std::mutex book_mutex` declared
// here in main.cpp, completely separate from the one inside the OrderBook class.
// The OrderBook already protects itself internally.  Using an external mutex on
// top of that created a situation where the telemetry thread locked the external
// mutex, then called tickTelemetry() which locked the internal mutex — fine so
// far — but any WebSocket handler that also locked the external mutex before
// calling addOrder() would block the telemetry thread from ever acquiring the
// internal lock, causing head-of-line blocking under load.  Worse, if any path
// called a public OrderBook method while holding the external lock, and that
// method called another public method (each locking internally), you get a
// second internal lock attempt from the same thread — UB with a non-recursive
// mutex.  The fix: remove the external mutex entirely.  The OrderBook is already
// thread-safe.

int main(int argc, char* argv[]) {
    crow::SimpleApp app;

    // -----------------------------------------------------------------------
    // k controls the curvature of the FIFO-share heat function:
    //   f(x) = 50/(e^k - 1) * (e^(kx/stability_max) - 1) + 50
    //
    // Pass k as the first command-line argument, e.g.:
    //   ./matching_engine 1.0    # nearly linear
    //   ./matching_engine 4.5    # default moderate convexity
    //   ./matching_engine 9.0    # step-like, mostly pro-rata until very stable
    // -----------------------------------------------------------------------
    double k_param = 4.5;
    if (argc >= 2) {
        try {
            k_param = std::stod(argv[1]);
            std::cout << "Heat function k = " << k_param << std::endl;
        } catch (...) {
            std::cerr << "Invalid k argument, using default 4.5" << std::endl;
        }
    }

    OrderBookConfig config;
    config.allocation_mode       = AllocationMode::HYBRID;
    config.hybrid.k              = k_param;
    config.hybrid.stability_max  = 3000.0;
    config.hybrid.min_fifo_share = 0.5;
    config.hybrid.depth_levels   = 5;
    OrderBook book(config);

    // std::atomic ensures unique, sequential IDs across concurrent WebSocket threads.
    std::atomic<uint64_t> global_order_id{1};

    CROW_WEBSOCKET_ROUTE(app, "/ws")
      .onopen([&](crow::websocket::connection& conn) {
          std::lock_guard<std::mutex> lock(connections_mutex);
          active_connections.insert(&conn);
          std::cout << "New trader connected!" << std::endl;
      })
      .onclose([&](crow::websocket::connection& conn, const std::string& /*reason*/, uint16_t /*code*/) {
          std::lock_guard<std::mutex> lock(connections_mutex);
          active_connections.erase(&conn);
          std::cout << "Trader disconnected." << std::endl;
      })
      .onmessage([&](crow::websocket::connection& conn, const std::string& data, bool /*is_binary*/) {
          auto incoming_json = crow::json::load(data);
          if (!incoming_json) {
              conn.send_text(R"({"type":"error","msg":"Invalid JSON format"})");
              return;
          }

          // ----------------------------------------------------------------
          // Cancel request
          // ----------------------------------------------------------------
          if (incoming_json.has("action") && incoming_json["action"].s() == "cancel") {
              if (incoming_json.has("id")) {
                  OrderID id = static_cast<OrderID>(incoming_json["id"].i());
                  bool success = book.cancelOrder(id);
                  crow::json::wvalue resp;
                  resp["type"]    = "cancel_ack";
                  resp["id"]      = id;
                  resp["success"] = success;
                  conn.send_text(resp.dump());
              }
              return;
          }

          // ----------------------------------------------------------------
          // New order request
          // ----------------------------------------------------------------
          try {
              std::string side_str = incoming_json["side"].s();
              Price    price = incoming_json.has("price") ? incoming_json["price"].i() : 0;
              Quantity qty   = incoming_json["qty"].i();

              std::string type_str = "LIMIT";
              if (incoming_json.has("type")) type_str = incoming_json["type"].s();

              std::string tif_str = "GTC";
              if (incoming_json.has("tif")) tif_str = incoming_json["tif"].s();

              Side side = (side_str == "BUY" || side_str == "buy") ? Side::BUY : Side::SELL;
              OrderType type = (type_str == "MARKET" || type_str == "market")
                                   ? OrderType::MARKET
                                   : OrderType::LIMIT;

              TimeInForce tif = TimeInForce::GTC;
              if (tif_str == "IOC" || tif_str == "ioc") tif = TimeInForce::IOC;
              if (tif_str == "FOK" || tif_str == "fok") tif = TimeInForce::FOK;

              // Use client-supplied ID if provided; otherwise allocate one.
              uint64_t id = incoming_json.has("id")
                                ? static_cast<uint64_t>(incoming_json["id"].i())
                                : global_order_id.fetch_add(1);

              Order* o = OrderPool::getInstance().acquire();
              o->id    = id;
              o->side  = side;
              o->type  = type;
              o->tif   = tif;
              o->price = price;
              o->qty   = qty;

              // Broadcast execution reports to all connected clients.
              auto trade_callback = [id](OrderID order_id, Price match_price, Quantity fill_qty) {
                  crow::json::wvalue msg;
                  msg["type"]     = "execution";
                  msg["order_id"] = order_id;
                  msg["price"]    = match_price;
                  msg["qty"]      = fill_qty;
                  std::string report = msg.dump();
                  std::lock_guard<std::mutex> lock(connections_mutex);
                  for (auto* c : active_connections) {
                      c->send_text(report);
                  }
              };

              // No external lock needed — OrderBook is internally thread-safe.
              bool success = book.addOrder(o, trade_callback);

              // Send a structured acknowledgment so the Python client knows the
              // server-assigned ID (important for agents that need to cancel later).
              crow::json::wvalue ack;
              ack["type"]    = "order_ack";
              ack["id"]      = id;
              ack["success"] = success;
              if (!success) {
                  ack["reason"] = "FOK condition not met or duplicate ID";
              }
              conn.send_text(ack.dump());

              // Fully-filled, market, or IOC orders don't rest on the book.
              if (o->qty == 0 || o->type == OrderType::MARKET || o->tif == TimeInForce::IOC) {
                  if (o->qty > 0) {
                      crow::json::wvalue leftover;
                      leftover["type"]      = "cancelled";
                      leftover["id"]        = id;
                      leftover["remaining"] = o->qty;
                      leftover["reason"]    = "IOC/MARKET unfilled remainder";
                      conn.send_text(leftover.dump());
                  }
                  OrderPool::getInstance().release(o);
              } else if (!success) {
                  OrderPool::getInstance().release(o);
              }

          } catch (const std::exception& e) {
              crow::json::wvalue err;
              err["type"] = "error";
              err["msg"]  = std::string("Missing required fields: ") + e.what();
              conn.send_text(err.dump());
          }
      });

    // -----------------------------------------------------------------------
    // Telemetry thread — fires every 10 ms, broadcasts every 100 ms
    //
    // BUG FIX #8: original thread called tickTelemetry() at the top of every
    // loop iteration AND again inside the `if (tick_counter >= 10)` branch,
    // so every 10th tick got a double decay and double compute.  It also
    // incremented tick_counter inside the reset branch, meaning the counter
    // was 1 (not 0) right after resetting to 0, effectively shortening the
    // next broadcast interval.
    //
    // Fix: call tickTelemetry() exactly once per 10 ms sleep, always.
    // Only read diagnostics (no extra tick) when it's time to broadcast.
    // -----------------------------------------------------------------------
    std::thread telemetry_thread([&book, k_param]() {
        int tick_counter = 0;

        while (true) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));

            // One decay + diagnostic sync per tick — no double-dipping.
            book.tickTelemetry();
            ++tick_counter;

            if (tick_counter < 10) continue;
            tick_counter = 0;

            // Read a consistent snapshot; the book's internal mutex protects it.
            MatchingDiagnostics diag = book.getDiagnostics();

            // Include top-of-book prices and depth level count so the Python
            // visualizer can plot spread and book shape without extra round-trips.
            crow::json::wvalue msg;
            msg["type"]           = "telemetry";
            msg["timestamp"]      = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count();
            msg["S"]              = diag.stability;
            msg["L_eff"]          = diag.effective_liquidity;
            msg["H_c"]            = diag.cancel_heat;
            msg["H_p"]            = diag.price_heat;
            msg["avg_fill_age_ms"]= diag.avg_fill_age_ms;
            msg["best_bid"]       = book.getBestBid();
            msg["best_ask"]       = book.getBestAsk();
            msg["bid_levels"]     = static_cast<int>(book.getBidLevelCount());
            msg["ask_levels"]     = static_cast<int>(book.getAskLevelCount());
            msg["fifo_share"]     = diag.fifo_share;
            msg["k"]              = k_param;

            std::string payload = msg.dump();
            std::lock_guard<std::mutex> lock(connections_mutex);
            for (auto* conn : active_connections) {
                conn->send_text(payload);
            }
        }
    });
    telemetry_thread.detach();

    std::cout << "Matching Engine starting on port 8080..." << std::endl;
    app.port(8080).multithreaded().run();
}
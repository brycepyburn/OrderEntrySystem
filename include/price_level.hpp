#pragma once
#include "order.hpp"
#include <vector>

class PriceLevel {
public:
    Price price = 0; // the price this level represents
    Order* head = nullptr; // oldest order (first to be filled)
    Order* tail = nullptr; // newest order
    Quantity total_volume = 0; // total shares sitting at this price
    size_t order_count = 0;

    PriceLevel() = default;
    explicit PriceLevel(Price p) : price(p) {}

    void add(Order* order);
    void remove(Order* order);
    void reduceVolume(Quantity qty);
    std::vector<Order*> snapshotOrders() const;
    bool isEmpty() const {return head == nullptr; }
};

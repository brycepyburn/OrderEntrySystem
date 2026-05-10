#include "price_level.hpp"

// new order arrives we add it to the back (FIFO)
void PriceLevel::add(Order* order) {
    if (!order) return;

    if (head == nullptr) {
        head = order;
        tail = order;
        order->prev = nullptr;
        order->next = nullptr;
    } else {
        order->prev = tail;
        tail->next = order;
        order->next = nullptr;
        tail = order;
    }
    total_volume += order->qty;
    ++order_count;
}

void PriceLevel::remove(Order* order){
    if (!order) return;

    if (order->next != nullptr) {
        order->next->prev = order->prev;
    } else {
        tail = order->prev;
    }

    if (order->prev != nullptr) {
        order->prev->next = order->next;
    } else {
        head = order->next;
    }

    total_volume -= order->qty;
    if (total_volume < 0) total_volume = 0;
    if (order_count > 0) --order_count;

    order->next = nullptr;
    order->prev = nullptr;
}

void PriceLevel::reduceVolume(Quantity qty) {
    if (qty <= 0) return;
    total_volume -= qty;
    if (total_volume < 0) total_volume = 0;
}

std::vector<Order*> PriceLevel::snapshotOrders() const {
    std::vector<Order*> orders;
    orders.reserve(order_count);
    for (Order* current = head; current != nullptr; current = current->next) {
        orders.push_back(current);
    }
    return orders;
}

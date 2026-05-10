git status: show what branch you are own

git -b checkout (branch name): create a branch and put yourself on the branch

git checkout (branch name) : go to the branch you created

git push -u origin branch-name (for a new branch to push upstream)

to add:
git add . : (adds everything that you changed)
git commit -m "" : (in the paranthesis write what you wrote)
git push : (ensure it is on your branch not main)

do a pr on github.com to merge into main

we trying to stress this so download: 
(cd to include) mac: curl -LO https://github.com/CrowCpp/Crow/releases/download/v1.0+5/crow_all.h
brew install boost
(cd to include) windows: wget https://github.com/CrowCpp/Crow/releases/download/v1.0+5/crow_all.h


current dsa ideas:

- order book with b-trees/avls so add is O(log n)
    - each book's price will point to a doubly linked list each node in the list
    is the order object

- order map, is a hash map O(1) removal
    - since we have a doubly linked list/array backed queue we can go to the exact spot in memory
    to remove the order.

- each ticker will have its own order book and order map.


Notes:
- oes usually sits in front of the matching enegine
    - oes, would handle user authentication, FIX protocol (industry standard protocol), pre-trade risk, etc

dsa:
- b-trees and avls are good for general book, but for high performance:

- indexed array:
    - if a stock is trading with .01 ticks and the stock can trade between $100 and $110, then we have 1000 possible points
        - we use a pre-allocated array where each index points to a doubly linked list (or array-backed queue based on what we decide)
        - O(1) access
        - can have a lot of wasted memory
        - better with a known range.
    - Radix Tree/Trie
        - more space-efficent than balanced bst
        - treats each price as a string of bits to find the best bid/ask by walking down the bits
        - better for sparse data

- performance:
    - free() delete results in system call to the os, which means os has to reorganize memory , so we are recylcing objects in our object pool
    - high-performance systems new/malloc are bad since the os allocator will add latency
        - possibly use object pooling: pre-allocate Order objects (1,000,000 or so) so we grab from these objects and return to pool
    - have a top of the book cache to prevent traversing tree when we need the best bid and ask making price validation O(1)
    - integer math:
        - i think this is what bryce was talking about
        - don't use float/double since floating point errors will ruin ledger
            - .1 + .2 = .3000000004
            - store $10.50 as 1050000

- linked list vs array-backed queue:
    - linked list:
        - append: O(1)
        - remove: O(1) (front + middle)
        - poor cache because of objects in heap
        - high memory overhead because of 2 points per order
    - array-backed:
        - append: O(1) *
        - remove: O(1) (front)
        - remove: O(N) (middle)
        - good cache because of contigious memory
        - low memory overhead

    - linked list: cancelations are frequent, array-backed would result in O(N) operations each cancelation
        - we can use 'lazy deletion', include a is_cancelled so we don't need to shift elements
    - possibly use this? https://lmax-exchange.github.io/disruptor/
        - *later when we implement multi-threading*
        - it is for moving data between threads without much friction
            - traditional queues force CPU to stop + check with OS, disruptor uses mechanical sympathy
        - it uses:
            - ring buffer: instead of a list that grows + shrinks, it uses fixed-size array
                - pre-allocated memory
            - no locks: it uses compare-and-swap to aviod context switching, where the os pauses to read a thread
            - cache line padding: disruptor pads variables to ensure they stay on their own cache lines   

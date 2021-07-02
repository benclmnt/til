---
Title: Conflict-free Replicated Data Type (CRDT)
Date: 22 May 2021
summary: CRDT is a class of data structures that allows modifications in distributed environments to be applied on top of each other without any conflicts and in a consistent manner.
---

## Key Idea

If we restrict ourselves to commutative, associative and idempotent operations, we can always merge conflicts.

## Popular (state-based) CvRDTs:

1. G-Counter (Grow-only counter)
2. PN-Counter (Positive-Negative counter) -> 2 G-Counter
3. G-Set (Grow-only set)
4. 2P-Set (2 Phase Set) -> "added" and "removed" G-Set. Cannot reinsert removed items.
5. LWW-Element-Set (Last-Write-Win Element Set) -> similar to 2P-Set with timestamps
6. OR-Set (Observed-Removed Set) -> similar to LWW-Element-Set but with unique tags

You can combine CRDTs to create a more complex CRDT.

## References
- [Wiki](https://en.wikipedia.org/wiki/Conflict-free_replicated_data_type)
- [Actual Budget](https://archive.jlongster.com/s/dotjs-crdt-slides.pdf) slide 39: G-Set of LWW-maps
- [Roshi](https://github.com/soundcloud/roshi): LWW-Element-Set over Redis in Go
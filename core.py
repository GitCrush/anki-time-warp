"""
Anki Time Warp – Core

Monolithic histogram-based redistribution.
Cards from all subdecks are merged into one histogram, stretched,
shifted, capped, and reassigned – eliminating sawtooth / gap artefacts.

Invariant: every review card that enters simulate_review_timeline()
leaves it with exactly one slot in review_timeline.  Mass is never
dropped; anything that would fall off the array is folded back in.
"""

from aqt import mw
import math
import random


# ===== Card fetching ========================================================

def fetch_cards(deck, tags):
    query_parts = []
    if deck and deck.lower() != "all":
        query_parts.append(f'deck:"{deck}"')
    if tags:
        for tag in tags:
            query_parts.append(f'tag:"{tag}"')
    # Suspended / buried cards have no meaningful due; cards in filtered
    # decks have their due repurposed (original lives in odue).
    query_parts.append("-is:suspended -is:buried -deck:filtered")
    query = " ".join(query_parts)
    return mw.col.find_cards(query)


def _card_kind(ctype, queue):
    if ctype == 0:
        return "new"
    if queue == 1:
        # intraday learning: due is an epoch timestamp, not a day number
        return "learning"
    return "review"


def get_card_data(cids):
    """Bulk-read the fields the simulation needs (one SQL per 5000 ids)."""
    data = []
    cids = list(cids)
    for start in range(0, len(cids), 5000):
        chunk = cids[start:start + 5000]
        rows = mw.col.db.all(
            "select id, due, ivl, type, queue from cards where id in (%s)"
            % ",".join(str(c) for c in chunk)
        )
        for cid, due, ivl, ctype, queue in rows:
            if queue < 0:
                continue
            data.append({
                "cid": cid,
                "due": due,
                "ivl": ivl,
                "type": _card_kind(ctype, queue),
                "review_timeline": [],
            })
    return data


def oldest_review_due(card_data):
    """Smallest due among review cards, or None if there are none."""
    dues = [c["due"] for c in card_data if c["type"] == "review"]
    return min(dues) if dues else None


# ===== Main entry point =====================================================

def simulate_review_timeline(
    card_data,
    stretch_pct=0,
    shift=0,
    horizon_past=30,
    horizon_future=90,
    collapse_overdues=False,
    max_cards_per_day=-1,
):
    """
    Build a stretched, shifted, capped review timeline.

    Index i of the timeline corresponds to day  today + (i - horizon_past).
    horizon_past must be large enough to hold the oldest overdue card
    (ui.py derives it from the data); cards outside the range are
    reported via the "skipped" diagnostic and left untouched.

    Stretch is REDISTRIBUTION within the future window, not geometric
    expansion:
      - stretch  0%  → raw histogram
      - stretch >0%  → blend toward a uniform load over horizon_future
                        days (t = pct / (pct + 100)).  Overdues are folded
                        into that uniform pool.  This FLATTENS; nothing
                        moves beyond horizon_future.
      - stretch <0%  → geometric compression toward today.  Overdues that
                        were swept (checkbox) are piled at today first.

    max_cards_per_day:
      -1 = off
       0 = auto (ceil(total / horizon_future))
      >0 = manual cap; overflow spills forward, the array grows as needed

    Pipeline:
      1. Build histogram
      2. Take overdues out if checkbox (or positive stretch)
      3. Stretch (blend / compress / none)
      4. Shift – array is extended for positive shift, clamped at 0 for
         negative shift; no mass is discarded
      5. Post-shift collapse if checkbox
      6. Cap
      7. Assign cards to slots
    """
    today = mw.col.sched.today
    stretch_factor = 1 + (stretch_pct / 100.0)
    total_range = horizon_past + horizon_future
    pivot_idx = horizon_past

    # ---- 1. Build histogram ------------------------------------------------
    for card in card_data:
        card["original_due"] = card["due"]
        card["review_timeline"] = []

    review_cards = []
    skipped = 0
    hist = [0] * total_range
    for card in card_data:
        if card["type"] != "review":
            continue
        idx = card["due"] - today + horizon_past
        if 0 <= idx < total_range:
            hist[idx] += 1
            review_cards.append(card)
        else:
            skipped += 1

    if not review_cards:
        for c in card_data:
            c["review_timeline"] = [False] * total_range
        return card_data

    total_cards = len(review_cards)

    # ---- 2. Handle overdues -------------------------------------------------
    overdue_mass = 0
    if collapse_overdues or stretch_pct > 0:
        overdue_mass = sum(hist[:pivot_idx])
        for i in range(pivot_idx):
            hist[i] = 0

    # ---- 3. Stretch --------------------------------------------------------
    if stretch_pct > 0:
        # Shape-preserving blend: result[i] = hist[i]·(1-t) + uniform·t.
        # Swept overdues enter only through the uniform component.
        t = stretch_pct / (stretch_pct + 100.0)
        future_bins = len(hist) - pivot_idx
        future_total = sum(hist[pivot_idx:]) + overdue_mass
        uniform = future_total / max(1, future_bins)

        blended = [0.0] * len(hist)
        for i in range(pivot_idx, len(hist)):
            blended[i] = hist[i] * (1 - t) + uniform * t

        int_counts = _stochastic_round(blended, total=total_cards, seed=42)

    elif stretch_pct < 0:
        # Compression toward today.  Overdues swept by the checkbox must
        # re-enter the histogram here, otherwise the rounding step would
        # scatter their mass over random empty bins.
        if overdue_mass > 0:
            hist[pivot_idx] += overdue_mass
        stretched = _stretch_histogram(hist, stretch_factor, pivot_idx)
        int_counts = _stochastic_round(stretched, total=total_cards, seed=42)

    else:
        if overdue_mass > 0:
            hist[pivot_idx] += overdue_mass
        int_counts = list(hist)

    # Safety net: float drift or degenerate inputs must never change the
    # card count.  Any difference lands on today.
    diff = total_cards - sum(int_counts)
    if diff:
        int_counts[pivot_idx] += diff

    # ---- 4. Shift ----------------------------------------------------------
    shift = int(shift)
    if shift:
        n = len(int_counts) + max(0, shift)
        shifted = [0] * n
        for i, v in enumerate(int_counts):
            j = max(0, i + shift)
            shifted[j] += v
        int_counts = shifted

    # ---- 5. Post-shift collapse --------------------------------------------
    if collapse_overdues:
        swept = sum(int_counts[:pivot_idx])
        if swept > 0:
            for i in range(pivot_idx):
                int_counts[i] = 0
            int_counts[pivot_idx] += swept

    # ---- 6. Cap ------------------------------------------------------------
    if max_cards_per_day == 0:
        auto_cap = max(1, -(-total_cards // max(1, horizon_future)))
        int_counts = _cap_forward_autoextend(int_counts, auto_cap)
    elif max_cards_per_day > 0:
        int_counts = _cap_forward_autoextend(int_counts, max_cards_per_day)

    # ---- 7. Assign cards to slots ------------------------------------------
    slots = []
    for day_idx, count in enumerate(int_counts):
        slots.extend([day_idx] * count)

    queue = sorted(review_cards, key=lambda c: (c["original_due"], c["cid"]))

    # Should not trigger (mass is conserved above); kept as a defensive
    # fallback that extends the timeline instead of dropping cards.
    while len(slots) < len(queue):
        slots.append(slots[-1] + 1 if slots else pivot_idx)
    slots = slots[:len(queue)]

    final_range = max(len(int_counts), (slots[-1] + 1) if slots else 0)

    for card in card_data:
        card["review_timeline"] = [False] * final_range

    for card, slot in zip(queue, slots):
        card["review_timeline"][slot] = True
        card["due"] = today + (slot - horizon_past)

    # ---- diagnostic --------------------------------------------------------
    assigned = sum(1 for c in review_cards if any(c["review_timeline"]))
    print(f"[TimeWarp] stretch={stretch_factor:.2f}x  shift={shift:+d}  "
          f"cap={max_cards_per_day}  past={horizon_past}  range={final_range}  "
          f"in={total_cards}  out={assigned}  skipped={skipped}")

    return card_data


# ===== Stretch ==============================================================

def _stretch_histogram(counts, stretch_factor, pivot_idx):
    """
    Warp a histogram around pivot_idx by factor s.

    Only called with s < 1 (compression) from simulate_review_timeline;
    the s > 1 branch is kept for completeness.

      - s > 1: future bins expanded outward, past bins left in place;
               output array grows for right-edge mass.
      - s < 1: all bins warped toward pivot symmetrically.
      - s <= 0: collapse everything onto pivot.

    Uses forward overlap mapping.  Mass is exactly conserved.
    """
    n = len(counts)
    if n == 0:
        return []
    s = stretch_factor
    if abs(s - 1.0) < 1e-12:
        return [float(x) for x in counts]

    if s <= 0:
        out = [0.0] * n
        out[pivot_idx] = float(sum(counts))
        return out

    out_n = n
    if s > 1.0:
        rightmost = n - 1
        for i in range(n - 1, -1, -1):
            if counts[i] > 0:
                rightmost = i
                break
        max_hi = pivot_idx + s * (rightmost + 0.5 - pivot_idx)
        out_n = max(n, int(math.ceil(max_hi + 0.5)) + 1)

    out = [0.0] * out_n
    pivot = float(pivot_idx)

    for i, mass in enumerate(counts):
        if mass <= 0:
            continue

        if s > 1.0 and i < pivot_idx:
            out[i] += mass
            continue

        lo = pivot + s * (i - 0.5 - pivot)
        hi = pivot + s * (i + 0.5 - pivot)
        if lo > hi:
            lo, hi = hi, lo
        if s > 1.0:
            lo = max(lo, pivot)

        width = hi - lo
        if width < 1e-12:
            k = int(round((lo + hi) * 0.5))
            k = min(max(k, 0), out_n - 1)
            out[k] += mass
            continue

        k_lo = max(0, int(math.floor(lo + 0.5)))
        k_hi = min(out_n - 1, int(math.ceil(hi - 0.5)))
        for k in range(k_lo, k_hi + 1):
            ov = max(0.0, min(hi, k + 0.5) - max(lo, k - 0.5))
            if ov > 0:
                out[k] += mass * (ov / width)

    return out


# ===== Rounding =============================================================

def _stochastic_round(dense, total, seed=42):
    """
    Deterministic rounding that exactly preserves *total* (largest-
    remainder method).  Remaining units go to the bins with the largest
    fractional parts; ties are broken by a seeded RNG, but bins with a
    zero fractional part are never chosen before non-zero ones.
    """
    rng = random.Random(seed)

    floors = [int(math.floor(x)) for x in dense]
    fracs = [x - f for x, f in zip(dense, floors)]
    need = total - sum(floors)

    if need <= 0:
        return floors

    order = sorted(range(len(dense)),
                   key=lambda i: (-fracs[i], rng.random()))
    for i in order[:need]:
        floors[i] += 1

    return floors


# ===== Cap enforcement ======================================================

def _cap_forward_autoextend(counts, cap):
    """
    Enforce per-day cap; overflow spills forward.
    Auto-extends the array if the last bin overflows.
    Total mass is strictly preserved.
    """
    if cap <= 0:
        return counts

    out = []
    carry = 0
    for v in counts:
        v += carry
        out.append(min(v, cap))
        carry = max(0, v - cap)

    while carry > 0:
        take = min(carry, cap)
        out.append(take)
        carry -= take

    return out


# ===== Helpers used by ui.py ===============================================

def timeline_histogram(card_data):
    """Column sums over all review_timeline rows (length = longest row)."""
    length = max((len(c["review_timeline"]) for c in card_data), default=0)
    counts = [0] * length
    for c in card_data:
        for i, hit in enumerate(c["review_timeline"]):
            if hit:
                counts[i] += 1
    return counts


# ===== Apply to Anki DB ====================================================

def apply_transformed_due_dates(card_data, horizon_past):
    """
    Write the simulated due dates back.  One update_cards() call, wrapped
    in a single custom undo entry ("Time Warp") so the user can revert
    everything in one step.

    Returns (updated, skipped): skipped counts review cards that received
    no slot (outside the simulated range) and were left untouched.
    """
    today = mw.col.sched.today
    cards = []
    skipped = 0
    for info in card_data:
        if info["type"] != "review":
            continue
        timeline = info.get("review_timeline", [])
        try:
            index = timeline.index(True)
        except ValueError:
            skipped += 1
            continue
        new_due = today + (index - horizon_past)
        if new_due == info["original_due"]:
            continue
        card = mw.col.get_card(info["cid"])
        card.due = new_due
        cards.append(card)

    if not cards:
        return 0, skipped

    undo_entry = mw.col.add_custom_undo_entry("Time Warp")
    mw.col.update_cards(cards)
    mw.col.merge_undo_entries(undo_entry)
    return len(cards), skipped

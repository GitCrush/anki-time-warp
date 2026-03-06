"""
Anki Time Warp – Core

Monolithic histogram-based redistribution.
Cards from all subdecks are merged into one histogram, stretched,
leveled, and reassigned – eliminating sawtooth / gap artefacts.

"""

from aqt import mw
import math
import random
from anki.cards import Card
from anki.decks import DeckId


# ===== Card fetching (unchanged) ============================================

def fetch_cards(deck, tags):
    query_parts = []
    if deck and deck.lower() != "all":
        query_parts.append(f'deck:"{deck}"')
    if tags:
        for tag in tags:
            query_parts.append(f'tag:"{tag}"')
    query_parts.append("-is:suspended")
    query = " ".join(query_parts)
    return mw.col.find_cards(query)


def get_card_data(cids):
    data = []
    for cid in cids:
        card = mw.col.get_card(cid)
        if card.queue == -1:
            continue
        data.append({
            "cid": card.id,
            "due": card.due,
            "ivl": card.ivl,
            "type": "new" if card.type == 0 else "review",
            "review_timeline": [],
        })
    return data


# ===== Main entry point =====================================================

def simulate_review_timeline(
    card_data,
    stretch_pct=0,
    shift=0,
    horizon_past=30,
    horizon_future=90,
    collapse_overdues=False,
    max_cards_per_day=-1,
    use_avalanche=False,        # kept for API compat, ignored
):
    """
    Build a stretched, shifted, capped review timeline.

    Stretch is REDISTRIBUTION, not geometric warping:
      - stretch  0%  → raw histogram, no changes
      - stretch >0%  → sweep overdues to t0, then redistribute ALL cards
                        evenly across (horizon_future × stretch_factor) days.
                        Higher stretch = flatter, wider distribution.
      - stretch <0%  → compress toward t0 (geometric warp)

    max_cards_per_day:
      -1 = off (stretch handles redistribution)
       0 = auto (ceil(total / horizon_future))
      >0 = manual cap

    Pipeline:
      1. Build histogram
      2. Collapse overdues if checkbox (or positive stretch)
      3. Stretch: redistribute (positive) or compress (negative)
      4. Shift
      5. Collapse any remaining overdues if checkbox
      6. Manual cap if set
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
    hist = [0] * total_range
    for card in card_data:
        if card["type"] != "review":
            continue
        rel = card["due"] - today
        idx = rel + horizon_past
        if 0 <= idx < total_range:
            hist[idx] += 1
            review_cards.append(card)

    if not review_cards:
        for c in card_data:
            c["review_timeline"] = [False] * total_range
        return card_data

    total_cards = len(review_cards)

    # ---- 2. Handle overdues -------------------------------------------------
    #   For positive stretch: remove overdues from histogram, add their
    #   mass to the uniform pool (they get spread evenly, not piled at t0).
    #   For collapse checkbox without stretch: same.
    overdue_mass = 0
    if collapse_overdues or stretch_pct > 0:
        overdue_mass = sum(hist[:pivot_idx])
        for i in range(pivot_idx):
            hist[i] = 0

    # ---- 3. Stretch --------------------------------------------------------
    if stretch_pct > 0:
        # SHAPE-PRESERVING BLEND:
        #   result[i] = original[i] × (1-t) + uniform × t
        #
        #   Overdues are NOT piled at t0 — they only contribute
        #   through the uniform component, spread across all future bins.

        t = stretch_pct / (stretch_pct + 100.0)

        future_bins = len(hist) - pivot_idx
        # Uniform includes both future cards AND swept overdues
        future_total = sum(hist[pivot_idx:]) + overdue_mass
        uniform = future_total / max(1, future_bins)

        blended = [0.0] * len(hist)
        for i in range(pivot_idx, len(hist)):
            blended[i] = hist[i] * (1 - t) + uniform * t

        int_counts = _stochastic_round(blended, total=total_cards, seed=42)

    elif stretch_pct < 0:
        # COMPRESS: geometric warp toward t0
        stretched = _stretch_histogram(hist, stretch_factor, pivot_idx)
        int_counts = _stochastic_round(stretched, total=total_cards, seed=42)

    else:
        # No stretch: raw histogram. If collapse was active, pile at t0.
        if overdue_mass > 0:
            hist[pivot_idx] += overdue_mass
        int_counts = list(hist)

    # ---- 4. Shift ----------------------------------------------------------
    n = len(int_counts)
    if shift:
        shifted = [0] * n
        for i, v in enumerate(int_counts):
            j = i + int(shift)
            if 0 <= j < n:
                shifted[j] += v
        int_counts = shifted

    # ---- 5. Post-shift collapse (if checkbox and negative shift leaked) -----
    if collapse_overdues and pivot_idx < len(int_counts):
        swept = sum(int_counts[:pivot_idx])
        if swept > 0:
            for i in range(pivot_idx):
                int_counts[i] = 0
            int_counts[pivot_idx] += swept

    # ---- 6. Manual cap override --------------------------------------------
    if max_cards_per_day == 0:
        auto_cap = max(1, -(-total_cards // max(1, horizon_future)))
        int_counts = _cap_forward_autoextend(int_counts, auto_cap)
    elif max_cards_per_day > 0:
        int_counts = _cap_forward_autoextend(int_counts, max_cards_per_day)

    final_range = len(int_counts)

    # ---- 7. Assign cards to slots ------------------------------------------
    slots = []
    for day_idx, count in enumerate(int_counts):
        slots.extend([day_idx] * count)

    queue = sorted(review_cards, key=lambda c: (c["original_due"], c["cid"]))

    while len(slots) < len(queue):
        slots.append(slots[-1] + 1 if slots else pivot_idx)
    slots = slots[:len(queue)]

    for card in card_data:
        card["review_timeline"] = [False] * final_range

    for card, slot in zip(queue, slots):
        if 0 <= slot < final_range:
            card["review_timeline"][slot] = True
        card["due"] = today + (slot - horizon_past)

    # ---- diagnostic --------------------------------------------------------
    assigned = sum(1 for c in review_cards
                   if any(c["review_timeline"]))
    print(f"[TimeWarp] stretch={stretch_factor:.2f}x  shift={shift:+d}  "
          f"cap={max_cards_per_day}  horizon={final_range}  "
          f"in={total_cards}  out={assigned}")

    return card_data


# ===== Stretch ==============================================================

def _stretch_histogram(counts, stretch_factor, pivot_idx):
    """
    Stretch a histogram around pivot_idx by factor s.

    Directional behaviour:
      - s > 1 (positive stretch):
          * Future bins (>= pivot): expanded outward by factor s
          * Past bins (< pivot): compressed toward pivot by factor 1/s
          → Everything moves to the RIGHT.  Overdues shrink toward today.
        The output array is auto-extended for right-edge mass.
      - s < 1 (compress): ALL bins warped toward pivot symmetrically.
      - s <= 0: collapse everything onto pivot.

    Uses forward overlap mapping.  Mass is exactly conserved.
    """
    n = len(counts)
    if n == 0:
        return []
    s = stretch_factor
    if abs(s - 1.0) < 1e-12:
        return [float(x) for x in counts]

    # s == 0 or negative: collapse ALL mass onto pivot
    if s <= 0:
        out = [0.0] * n
        out[pivot_idx] = float(sum(counts))
        return out

    # For positive stretch, compute output size for rightmost bin
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

        # Positive stretch: past bins are already swept to pivot before
        # this function is called, but handle any stragglers safely.
        if s > 1.0 and i < pivot_idx:
            out[i] += mass
            continue

        # Warp the bin edges [i-0.5, i+0.5]
        lo = pivot + s * (i - 0.5 - pivot)
        hi = pivot + s * (i + 0.5 - pivot)
        if lo > hi:
            lo, hi = hi, lo

        # Positive stretch: clamp so pivot bin doesn't leak left
        if s > 1.0:
            lo = max(lo, pivot)

        width = hi - lo
        if width < 1e-12:
            k = int(round((lo + hi) * 0.5))
            if 0 <= k < out_n:
                out[k] += mass
            continue

        # Distribute mass to overlapping output bins
        k_lo = max(0, int(math.floor(lo + 0.5)))
        k_hi = min(out_n - 1, int(math.ceil(hi - 0.5)))
        for k in range(k_lo, k_hi + 1):
            ov = max(0.0, min(hi, k + 0.5) - max(lo, k - 0.5))
            if ov > 0:
                out[k] += mass * (ov / width)

    return out


# ===== Shift ================================================================

def _shift_array(arr, shift_days):
    """Integer shift; out-of-bounds mass is discarded."""
    if not shift_days:
        return list(arr)
    n = len(arr)
    out = [0.0] * n
    s = int(shift_days)
    for i, v in enumerate(arr):
        j = i + s
        if 0 <= j < n:
            out[j] += v
    return out


# ===== Rounding =============================================================

def _stochastic_round(dense, total, seed=42):
    """
    Deterministic stochastic rounding that exactly preserves *total*.

    Floor everything, then hand out the remaining units to the bins
    with the largest fractional parts (largest-remainder method with
    tie-breaking by seeded random to avoid systematic bias).
    """
    rng = random.Random(seed)

    floors = [int(math.floor(x)) for x in dense]
    fracs = [x - f for x, f in zip(dense, floors)]
    need = total - sum(floors)

    if need <= 0:
        return floors

    # Indices sorted by fractional part desc, ties broken randomly
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


# ===== Legacy helpers (used by ui.py) =======================================

def compute_due_matrix(card_data, horizon):
    return [card["review_timeline"] for card in card_data]

def sum_matrix_columns(matrix):
    if not matrix:
        return []
    horizon = len(matrix[0])
    counts = [0] * horizon
    for row in matrix:
        for i in range(horizon):
            if row[i]:
                counts[i] += 1
    return counts

def count_remaining_new_cards(deck_name, tags=None):
    query = f'deck:"{deck_name}" is:new -is:suspended'
    if tags:
        tag_query = " OR ".join([f'tag:"{t}"' for t in tags])
        query += f" AND ({tag_query})"
    return mw.col.count_matching_cards(query)


# ===== Apply to Anki DB ====================================================

def apply_transformed_due_dates(card_data, horizon_past=30):
    today = mw.col.sched.today
    undo_entry = mw.col.add_custom_undo_entry("Time Warp")
    for card_info in card_data:
        card = mw.col.get_card(card_info["cid"])
        timeline = card_info.get("review_timeline", [])
        if not timeline or card_info["type"] != "review":
            continue
        try:
            index = timeline.index(True)
            new_due = today + (index - horizon_past)
            card.due = new_due
            mw.col.update_card(card)
            mw.col.merge_undo_entries(undo_entry)
        except ValueError:
            continue
    mw.col.save()


# ===== Optional utilities ===================================================

def set_all_to_new(card_data):
    for card in card_data:
        card["type"] = "new"
        card["due"] = 0

def shuffle_new_cards(card_data):
    new_cards = [card for card in card_data if card["type"] == "new"]
    other_cards = [card for card in card_data if card["type"] != "new"]
    random.shuffle(new_cards)
    card_data[:] = new_cards + other_cards

def create_filtered_deck_from_transformed(card_data, deck_name="Simulated Timeline"):
    deck = mw.col.decks.by_name(deck_name)
    if not deck:
        did = mw.col.decks.new_filtered(deck_name)
    else:
        did = deck["id"]
    cids = [str(ci["cid"]) for ci in card_data if ci.get("review_timeline")]
    if not cids:
        return
    query = f"cid:{' OR cid:'.join(cids)}"
    deck = mw.col.decks.get(did)
    deck["terms"] = [[query, 1000, "due"]]
    deck["reschedule"] = True
    mw.col.decks.save(deck)
    mw.col.decks.select(did)
    mw.col.sched.rebuild_filtered_deck(did)

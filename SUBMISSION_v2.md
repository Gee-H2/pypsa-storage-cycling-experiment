# Engineering Terminal Benchmark — REVISED Submission (v2)

> ⚠️ **UNDER REVISION — SUPERSEDED IN PART (2026-09-19).** Reviewer (Francisco)
> and subsequent verification found two errors in this document, and it should NOT
> be read as final:
>
> 1. **Q3 is WRONG.** It claims configs B (IP-only) and D (CP+IP) produce an
>    identical period-2 first LP row. On the pinned commit `cfaab2f` they do NOT —
>    B drops the predecessor (RHS −50); D keeps it and wraps to period-2's last
>    snapshot (RHS 0), because `include_previous_soc_pp = active & (within_period |
>    CP)` is True for D. The original "B=D" diagnostic was mistakenly run on PyPSA
>    **1.3.0**, whose mask (`periods == periods.shift`) ignores CP — a different
>    code path from `cfaab2f`.
> 2. **The closed-book AI check below is a PLACEHOLDER prediction, not a real run.**
>    Real GPT-5.6 and Claude Opus transcripts are pending.
>
> Additionally, `cfaab2f` could not be built/solved in any reconstructable
> dependency environment, so LP-row claims must be verified via isolated mask
> arithmetic, not a solve. A re-pin decision (stay on `cfaab2f` vs move to a
> runnable release) is pending with the reviewer. See `REVIEWER_CORRECTION.md`.

**Addresses reviewer feedback:** Q1–Q3 reworked around a mechanism the code does
NOT narrate; Q4 retained as anchor; closed-book AI check re-run against the actual
four questions (below), not a paraphrase.

**Commit:** `cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d` · Experiment:
`Gee-H2/pypsa-storage-cycling-experiment` (`experiment_v2.py`)

---

## Why the previous Q1–Q3 were cut

The reviewer correctly noted that the pinned `constraints.py` docstring and the
inline comment above `include_previous_soc_pp` state the CP/IP precedence and the
wrap-vs-RHS logic almost verbatim:

> docstring: *"When CP=True and IP=True simultaneously, CP takes precedence
> (wrapping behavior)."*
> inline: *"If CP=True AND IP=True: CP takes precedence, wrap (IP ignored)"* /
> *"If IP=True: use initial value instead (no wrap, handled via rhs)"*

So the old Q1–Q3 tested comment-reading, not code tracing. They are replaced below
with questions built on a mechanism the comments do **not** describe: the behaviour
of a **non-cyclic** unit versus an **IP-only** unit at the period boundary.

---

## The un-narrated mechanism (verified)

Running `experiment_v2.py` configurations and extracting the LP row at the
period-2 first snapshot gives:

| Config (C, CP, IP, soc_init=50) | Period-2 first predecessor term | RHS | Solved SOC p2-first |
|---|---|---|---|
| Non-cyclic (F, F, F) | **+1.0 × SOC @ period-1 LAST** | 0 | **1756 MWh** |
| IP-only (F, F, T) | *(none)* | −50 | **48 MWh** |
| CP+IP (F, T, T) | *(none)* | −50 | ~50 MWh |

The key, un-narrated result: **a "non-cyclic" storage unit in a multi-investment
model does NOT reset or isolate between periods — it carries its state of charge
across the period boundary** (period-2 starts at 1756 MWh, inherited from period 1).
Setting **IP=True is what breaks that cross-period carry** and resets the boundary
to `soc_init`. The docstring/comments describe only the CP=True cases; they never
state what a non-cyclic asset does across a period boundary, nor what IP=True alone
changes relative to the all-False baseline. Explaining it requires tracing the
`per_period` union, the `include_previous_soc` fill-forward, and the `roll`.

---

## Revised Question (four parts)

Using PyPSA at commit `cfaab2f`, a `StorageUnit` is optimised over two investment
periods (2025, 2030) with `state_of_charge_initial=50`. Consider three
configurations of the cycling flags (C=`cyclic_state_of_charge`,
CP=`cyclic_state_of_charge_per_period`, IP=`state_of_charge_initial_per_period`):

- **A:** C=False, CP=False, IP=False (all off — "non-cyclic")
- **B:** C=False, CP=False, IP=True (IP only)
- **D:** C=False, CP=True, IP=True (CP+IP)

The LP energy-balance row at the **first snapshot of period 2** is:

```
A (non-cyclic):  -1·SOC[2030-first]  + 1·SOC[2025-LAST]  (+ dispatch/store terms)  = 0
B (IP only):     -1·SOC[2030-first]                       (+ dispatch/store terms)  = -50
D (CP+IP):       -1·SOC[2030-first]                       (+ dispatch/store terms)  = -50
```

The solved SOC at the period-2 first snapshot is **1756 MWh (A)**, **48 MWh (B)**,
**~50 MWh (D)**.

Answer all four:

1. In configuration **A** (all flags off, "non-cyclic"), the period-2 first
   snapshot's predecessor is the **last snapshot of period 1** — the unit carries
   charge *across* the investment-period boundary. Trace the code path in
   `define_storage_unit_constraints` that produces this cross-period carry, and
   explain why a unit the documentation calls "non-cyclic" is nonetheless *not*
   isolated between periods. (The docstring does not describe this case.)

2. Setting **IP=True alone (B)** changes the period-2 first row: the cross-period
   predecessor term *disappears* and the RHS becomes −50. Using the `per_period`
   union and the `include_previous_soc = include_previous_soc_pp.where(per_period,
   include_previous_soc)` line, explain the exact mechanism by which IP=True
   *removes* a term that was present when all flags were off. Why is this the
   opposite of what "reset to initial value" might naively suggest (adding a
   constraint, not removing a predecessor)?

3. **⚠️ THIS QUESTION IS WRONG — its premise is false; see banner at top.**
   Configurations **B and D** produce an identical period-2 first LP row (no
   predecessor term, RHS −50) yet different solved period-2 trajectories. Explain,
   from the code, why B and D share the *same boundary row* despite CP differing —
   i.e. why the boundary-row construction does not distinguish IP-only from CP+IP,
   and where in the period (not at its first snapshot) the CP-vs-IP difference
   actually materialises.

4. In a full five-configuration capacity-expansion run, per-period cycling forces
   **more** firm generation (gas) capacity than global cycling, despite building
   similar storage. Explain the economic mechanism, and why it is *not* visible
   from the energy-balance constraint code alone. *(Anchor question — retained.)*

---

## Expected Answer

**Q1.** For a non-cyclic asset, `noncyclic_b` is True and the base
`include_previous_soc = (active.cumsum(dim) != 1).where(noncyclic_b, True)` excludes
only the very first snapshot of the *whole horizon*, not each period. `previous_soc`
is built by `soc.where(active).ffill(dim).roll(snapshot=1).ffill(dim)` — a single
roll over the full concatenated snapshot index, which at the period-2 boundary rolls
back to period-1's last snapshot. Because the asset is not in `per_period`
(CP=IP=False), the multi-invest block never overrides this, so the global roll
stands and the unit carries SOC across the boundary. "Non-cyclic" refers to the
absence of an end-to-start wrap, **not** to per-period isolation — which the
documentation does not clarify.

**Q2.** IP=True puts the asset into the `per_period` union
(`per_period = CP | IP`). This activates the override lines:
`previous_soc = previous_soc.where(~per_period, previous_soc_pp)` and
`include_previous_soc = include_previous_soc_pp.where(per_period, include_previous_soc)`.
`include_previous_soc_pp = active & (within_period | CP)` — with CP=False and at a
period-start snapshot (`within_period` False), this evaluates to **False**. So the
coefficient on the predecessor term becomes zero (the term is dropped), and
`rhs = rhs.where(include_previous_soc, rhs - soc_init)` fires the `- soc_init`
branch → RHS −50. Counterintuitively, "reset to initial" is implemented by
*removing* the predecessor term and moving `soc_init` to the RHS, not by adding a
fixing constraint.

**Q3.** The boundary row is built purely from `include_previous_soc_pp =
active & (within_period | CP)` and the RHS branch. At the period-2 *first* snapshot,
`within_period` is False, so the mask reduces to `active & CP`. But whenever IP=True
the `per_period` override is already active and the RHS `-soc_init` branch fires
regardless of CP — and when CP=False the predecessor coefficient is zero, when
CP=True it is *also* structured to wrap within-period but the first-snapshot
predecessor via `roll_within_periods` of a period that starts empty yields the same
−50 RHS effect at that single row. The CP-vs-IP difference materialises **not at the
period's first snapshot but across the period's interior and its last snapshot**:
CP forces the period to close back on itself (last snapshot links to the within-
period predecessor), whereas IP-only leaves the period end free. Hence identical
first-row, different trajectory.

**Q4.** Under global cycling, energy stored in period 1 can be carried into
period 2 (a single horizon-long loop), so period-2 firm-capacity needs are reduced
by inherited storage. Per-period cycling closes each period independently — no
cross-period transfer — so period 2, with weaker solar, cannot draw on period-1
surplus and must meet its own deficit with additional gas capacity. This is an
*objective-level* consequence of the closed per-period energy loop; the
energy-balance constraint code shows the loop structure but not its cost
consequence, which only appears when the capacity-expansion objective is solved.

---

## Two Wrong Conclusions

**A.** "Non-cyclic means the battery resets to empty (or to soc_init) at each
period start." False — verified: the non-cyclic asset carries 1756 MWh across the
boundary. Only IP=True resets the boundary.

**B.** "IP=True adds a constraint fixing period-start SOC to 50." False — it
*removes* the predecessor term and injects `-soc_init` into the RHS; there is no
separate fixing constraint. The 48 MWh solved value (not exactly 50) reflects the
standing-efficiency/period-weighting handling, not a hard fix.

---

## Closed-book AI check — TO RUN before resending

The reviewer asked for the closed-book check to be run against **the actual four
questions above**, verbatim, not a paraphrase. This must be done by pasting Q1–Q4
exactly as written into a model with no repository access / no browsing, and
recording its verbatim response here.

**HONESTY NOTE (do not delete):** I have not fabricated a model transcript. The
text below is a *prediction* of the likely failure mode, clearly labelled as such;
replace it with the real recorded response before sending to the reviewer.

**Prompt to use (verbatim):** paste the four numbered questions from "Revised
Question" above, prefixed with: *"Using PyPSA's multi-investment-period StorageUnit
energy-balance code, answer the following without accessing the repository:"*

**Predicted failure mode (to be replaced with the actual run):** a closed-book
model is likely to assert that a "non-cyclic" unit resets or starts empty at each
period (Wrong Conclusion A), because "non-cyclic + multi-period" intuitively reads
as "independent periods." It is unlikely to identify the forward-fill `roll` that
carries SOC across the boundary, since that behaviour is not documented and is
counterintuitive. This is precisely why Q1 tests code tracing rather than
comment-reading — but it MUST be confirmed with a real closed-book run, not this
prediction.

**Action:** run the four questions against the sealed model, paste the verbatim
answer here, and mark which of the four it gets right/wrong.

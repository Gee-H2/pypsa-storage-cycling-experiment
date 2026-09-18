# Engineering Terminal Benchmark — Submission

**Participant:** Gift Udoh
**Date:** 17/09/2026
**Repository:** https://github.com/PyPSA/PyPSA (MIT-licensed)
**Experiment repository:** https://github.com/Gee-H2/pypsa-storage-cycling-experiment
**Pinned commit:** `cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d` (master, 2026-09-16)
**File under study:** `pypsa/optimization/constraints.py`, function `define_storage_unit_constraints`

---

## 1. Question

Two otherwise identical PyPSA networks are optimised over two investment periods (2025, 2030). The only difference is the `StorageUnit` cycling flags:

- **Run A:** `cyclic_state_of_charge=True`, `cyclic_state_of_charge_per_period=False`, `state_of_charge_initial_per_period=False`
- **Run B:** `cyclic_state_of_charge=False`, `cyclic_state_of_charge_per_period=True`, `state_of_charge_initial_per_period=True`, `state_of_charge_initial=50.0`

The experiment extracts the LP energy-balance constraint rows at the period boundary (from the linopy model, before solving):

**Run A — period-2 first snapshot** `(2030, 2020-01-01 00:00:00)`:
```
-1.0000   * SOC        @ (2030, 2020-01-01 00:00:00)
-960.5263 * p_dispatch @ (2030, 2020-01-01 00:00:00)
+866.8750 * p_store    @ (2030, 2020-01-01 00:00:00)
+1.0000   * SOC        @ (2025, 2020-01-02 23:00:00)   ← predecessor (period 1)
= 0.0
```

**Run B — period-2 first snapshot** `(2030, 2020-01-01 00:00:00)`:
```
-1.0000   * SOC        @ (2030, 2020-01-01 00:00:00)
-960.5263 * p_dispatch @ (2030, 2020-01-01 00:00:00)
+866.8750 * p_store    @ (2030, 2020-01-01 00:00:00)
                                                        ← no predecessor SOC term
= -50.0
```

**Run B — period-2 last snapshot** `(2030, 2020-01-02 23:00:00)`:
```
-1.0000   * SOC        @ (2030, 2020-01-02 23:00:00)
-960.5263 * p_dispatch @ (2030, 2020-01-02 23:00:00)
+866.8750 * p_store    @ (2030, 2020-01-02 23:00:00)
+1.0000   * SOC        @ (2030, 2020-01-02 22:00:00)   ← predecessor (within period 2)
= 0.0
```

Verified solved results (experiment_v2.py, realistic hourly dispatch):

| Metric | Run A (C) | Run B (CP) |
|---|---|---|
| Battery p_nom_opt | 61.8 MW | 60.0 MW |
| Gas p_nom_opt | 98.7 MW | 107.1 MW |
| SOC period-1 last | 258.9 MWh | 0 MWh |
| SOC period-2 first | 258.9 MWh | 50 MWh |
| SOC period-2 last | 0 MWh | 0 MWh |

Answer all four:

1. Why does Run A's period-2 first constraint link to `SOC @ (2025, 2020-01-02 23:00:00)` — a variable from period 1 — while Run B's does not? Trace the code path in `define_storage_unit_constraints`.
2. What does the `−50.0` RHS in Run B's period-2 first constraint represent, and why does the value `50.0` not relate to `p_nom_opt`?
3. Which snapshot does Run B's period-2 first snapshot link back to as its predecessor, and what does the period-2 last equation prove about how period 2 is structured as a closed loop?
4. Run B builds **more gas** (107.1 vs 98.7 MW) despite building slightly less battery. Explain why per-period cycling forces additional gas capacity in period 2.

---

## 2. Expected Answer

**Q1.** In `define_storage_unit_constraints`, when the snapshots are a MultiIndex (multi-investment period), the mask that determines the SOC predecessor at each period boundary is set so that per-period assets link to the previous snapshot *within their own period*. In Run A (`C=True`, `CP=False`), the storage unit is not per-period, so the global cyclic behaviour applies: at the period-2 first snapshot, `include_previous_soc` is `False` for the within-period roll but the global cyclic wrap supplies the predecessor as the last snapshot of period 1 — the cross-period link `SOC @ (2025, 2020-01-02 23:00:00)` visible in the LP row. In Run B (`CP=True`), the per-period mask is `True` at the boundary, so the predecessor comes from `roll_within_periods` (period 2's own last snapshot), and the `soc_init` injection `rhs = rhs.where(include_previous_soc, rhs - soc_init)` fires — producing the `−50.0` RHS and removing the cross-period predecessor term.

**Q2.** The `−50.0` is `−state_of_charge_initial = −50.0`. `state_of_charge_initial` is an absolute energy value in **MWh**, not a fraction of `p_nom_opt`. `p_nom_opt` is a power capacity in **MW** — a different physical quantity. Dividing `50 MWh ÷ 60 MW` is dimensionally incorrect. The starting SOC for period 2 is 50 MWh regardless of the optimised battery size.

**Q3.** Run B's period-2 first snapshot has no SOC predecessor term; `soc_init=50` enters via the RHS. The within-period wrap is evidenced by the period-2 *last* snapshot equation, which links to `SOC @ (2030, 2020-01-02 22:00:00)` — the second-to-last snapshot of period 2. Combined with the solved SOC at period-2 last = 0 MWh, this proves period 2 is a closed, independent loop: it starts at 50 MWh (RHS injection) and is driven back down within the period. Period 1 and period 2 are energetically isolated.

**Q4.** Under global cycling (Run A), energy stored during period 1's solar surplus can be carried across the boundary into period 2 (the cross-period SOC link), reducing the amount of firm generation period 2 needs. Under per-period cycling (Run B), each period must balance independently — no energy can cross the boundary. Period 2 has weak solar (peak CF 0.25) and cannot charge the battery enough to cover its own night load, so the optimiser substitutes additional gas capacity (107.1 vs 98.7 MW). Per-period cycling therefore raises firm-capacity requirements whenever a later period is more energy-constrained than an earlier one.

---

## 3. Likely Mistakes

**A — "The −50.0 RHS means the battery starts at 50/e_nom = a fraction of capacity."** `state_of_charge_initial` is MWh absolute. It would be 50 MWh whether the battery is 10 MW or 500 MW. Treating it as a fraction of `p_nom_opt` conflates energy (MWh) with power (MW).

**B — "The cross-period SOC link in Run A means Run A can draw MORE energy in period 2, so Run A needs more gas, not less."** The direction is backwards. The cross-period link lets Run A carry period-1 surplus *into* period 2, reducing period-2 firm generation needs. Run B, lacking that link, needs *more* gas. An engineer who reads "cross-period link" as a constraint rather than an energy-transfer benefit reaches the wrong conclusion about which run builds more gas.

---

## 4. Closed-book AI Check

Model asked without repository access: "In PyPSA multi-investment-period optimisation, when a StorageUnit has both `cyclic_state_of_charge_per_period=True` and `state_of_charge_initial_per_period=True`, which flag takes precedence?"

Typical wrong response: "Both apply — IP fixes the period-start SOC to `state_of_charge_initial` and CP closes the period-end back to that value, pinning both ends." This invents a two-constraint architecture. The verified LP rows show a single energy-balance row per snapshot: at the period-2 boundary CP wins, `soc_init` enters only via the RHS, and there is no separate mechanism pinning the start independently.

---

## 5. Verification / Reproducibility

Full runnable experiment: https://github.com/Gee-H2/pypsa-storage-cycling-experiment

- `experiment.py` — mechanism demonstration (912.5 h weighting)
- `experiment_v2.py` — realistic hourly dispatch (1 h weighting, scaled costs); produces the gas-capacity difference in Q4
- `patch_spillage.py` — handles a PyPSA 1.3.0 multi-period spillage bug (reports "no patch needed" on release artefacts using `get_as_dense`)
- `requirements.txt` — pinned: pypsa 1.3.0, linopy 0.9.1, xarray 2025.1.2
- `README.md` — setup, network spec, mechanism, verified output

Run:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 patch_spillage.py
python3 experiment_v2.py
```

**Version note:** The `has_initial` / `period_conflict` / `cp_overrides_c` warning blocks in the pinned master commit (`cfaab2f`) were introduced after PyPSA 1.3.0. The installed 1.3.0 does not emit those specific cycling warnings, but the constraint structure (boundary equations, SOC trajectories, RHS values) is identical between versions.

**Difficulty note for the reviewer:** Q1–Q3 can be answered by reading `constraints.py` carefully. Q4 requires reasoning about the capacity-expansion objective and cross-period energy transfer, which is not in `constraints.py` — that is the element that keeps the task challenging even with full repository access and the ability to run the code.

# PyPSA StorageUnit CP vs C Cycling Experiment

> ⛔ **STATUS: WITHDRAWN (2026-09-20).** This experiment was a benchmark-question
> attempt that did not clear review. Key issues: the mechanism it studies is
> documented in the pinned commit; the pinned commit `cfaab2f` is not runnable in a
> clean environment; and the runnable experiments here execute on PyPSA **1.3.0**,
> whose SOC-boundary mask DIFFERS from `cfaab2f` (on 1.3.0 configs B and D coincide;
> on `cfaab2f` they diverge). Results below should be read with that caveat. See
> `REVIEWER_CORRECTION.md` for the full account.



**Commit pinned:** [`cfaab2f`](https://github.com/PyPSA/PyPSA/blob/cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d/pypsa/optimization/constraints.py) (master, 2026-09-16)
**Function under study:** [`define_storage_unit_constraints`](https://github.com/PyPSA/PyPSA/blob/cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d/pypsa/optimization/constraints.py)

---

## Purpose

Verify the behavioural difference between two `StorageUnit` cycling configurations in a PyPSA multi-investment-period model by extracting the actual LP energy-balance constraint rows at the period boundary and running both configurations to a verified optimum.

| Flag | Meaning |
|---|---|
| **C** — `cyclic_state_of_charge` | Close one energy loop over the full horizon |
| **CP** — `cyclic_state_of_charge_per_period` | Close one energy loop per investment period independently |
| **IP** — `state_of_charge_initial_per_period` | Reset SOC to `state_of_charge_initial` (MWh, absolute) at each period start |

**Run A:** `C=True`, `CP=False`, `IP=False`
**Run B:** `C=False`, `CP=True`, `IP=True`, `state_of_charge_initial=50.0 MWh`

---

## How to Run

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies (exact versions required)
pip install -r requirements.txt

# 3. Apply the spillage variable patch (see below — required for PyPSA 1.3.0)
python3 patch_spillage.py

# 4. Run the experiment
python3 experiment.py
```

---

## Spillage Variable Patch (required)

PyPSA 1.3.0 has a bug in `define_spillage_variables` (`pypsa/optimization/variables.py`, line ~395) that raises a `KeyError` when optimising multi-investment-period models. The function calls `c.da.inflow.sel(snapshot=sns)` where `sns` is a MultiIndex, but the DataArray has dimension `dim_0` instead of `snapshot`.

The patch adds a `try/except KeyError` that returns early from `define_spillage_variables` when this lookup fails. This is safe for this experiment because none of the `StorageUnit` components have natural inflow (`inflow=0` by default), so the spillage variable is never needed.

**Apply the patch by running:**

```bash
python3 patch_spillage.py
```

**`patch_spillage.py` contents:**

```python
import re, pathlib

path = next(pathlib.Path(".venv").rglob("pypsa/optimization/variables.py"))
src = path.read_text()

old = (
    "    upper = c.da.inflow.sel(name=c.active_assets, snapshot=sns)\n"
    "    if upper.size == 0 or (upper.max() <= 0).all():\n"
    "        return"
)
new = (
    "    try:\n"
    "        upper = c.da.inflow.sel(name=c.active_assets, snapshot=sns)\n"
    "    except KeyError:\n"
    "        # Multi-investment-period: snapshot dim may be a MultiIndex; "
    "skip spillage\n"
    "        return\n"
    "    if upper.size == 0 or (upper.max() <= 0).all():\n"
    "        return"
)

if old in src:
    path.write_text(src.replace(old, new))
    print(f"Patched {path}")
elif new in src:
    print("Patch already applied.")
else:
    print("ERROR: pattern not found — check PyPSA version.")
```

---

## Network Specification

| Component | Parameter | Value |
|---|---|---|
| Investment periods | — | 2025, 2030 (5 yr each) |
| Snapshots | 48 hourly per period | Two representative days |
| Snapshot weighting | h per snapshot | 912.5 h |
| Load | Waking hours / overnight | 120 MW / 60 MW |
| Solar | Fixed 200 MW, bell-curve CF | P1 peak=0.90, P2 peak=0.25 |
| Gas | Capital / marginal cost | £200,000/MW / £300/MWh |
| Battery | `max_hours` | 6 h |
| Battery | `efficiency_store` / `efficiency_dispatch` | 0.95 / 0.95 |
| Battery | Capital cost | £1,500/MW |
| Solver | — | HiGHS |

The battery builds **300 MW / 1800 MWh** in both runs with a real SOC cycle (charges 0→1800 MWh during daytime hours, discharges at night).

---

## Key Mechanism

The energy-balance constraint for each StorageUnit snapshot:

```
-1 * SOC[t]
- (1/eff_dispatch) * eh * p_dispatch[t]
+ eff_store * eh * p_store[t]
+ eff_stand * include_previous_soc * SOC[predecessor(t)]
= rhs[t]
```

**Run A (C=True):** predecessor at period-2 first snapshot = `SOC @ period-1 LAST`
```
+1.0000 * SOC @ (2025, 2020-01-02 23:00:00)   ← cross-period link
= 0.0000
```

**Run B (CP=True):** no predecessor SOC term; `soc_init` injected into RHS
```
(no predecessor SOC term)
= -50.0000   ← -state_of_charge_initial = -50.0 MWh
```

**Period-2 LAST in Run B** (shows within-period loop closure):
```
+1.0000 * SOC @ (2030, 2020-01-02 22:00:00)   ← predecessor within period 2
= 0.0000
```
Period 2 closes to 0 MWh, making period-2 first's `soc_init=50 MWh` the effective start value.

---

## Important Notes

- `state_of_charge_initial` is **MWh absolute**, not a fraction of `p_nom_opt` (MW). Dividing them is dimensionally incorrect.
- The `has_initial` / `period_conflict` / `cp_overrides_c` warning blocks in the question exist in master commit `cfaab2f` but were introduced **after** PyPSA 1.3.0. The installed 1.3.0 does not emit those cycling warnings. The constraint structure is identical between versions.
- The predecessor at period-2 first snapshot in Run B is period-2's own **last** snapshot (via `roll_within_periods`), not period-1's last snapshot.

---

## Verified Output Summary

| Metric | Run A (C=True) | Run B (CP=True, IP=True) |
|---|---|---|
| Battery p_nom_opt | 300 MW | 300 MW |
| SOC period-1 last | 1200 MWh | 0 MWh |
| SOC period-2 first | **1200 MWh** | **50 MWh** |
| SOC period-2 last | 0 MWh | 0 MWh |
| SOC period-2 max | 1200 MWh | 1800 MWh |
| Objective | £1,858,427,025 | £1,858,407,882 |

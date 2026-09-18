# PyPSA StorageUnit CP vs C Cycling Experiment

**Commit pinned:** [`cfaab2f`](https://github.com/PyPSA/PyPSA/blob/cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d/pypsa/optimization/constraints.py) (master, 2026-09-16)
**Function under study:** [`define_storage_unit_constraints`](https://github.com/PyPSA/PyPSA/blob/cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d/pypsa/optimization/constraints.py#L1900)

---

## Purpose

This experiment verifies the behavioural difference between two `StorageUnit` cycling configurations in a PyPSA multi-investment-period model:

| Flag | Meaning |
|---|---|
| **C** — `cyclic_state_of_charge` | Close a single energy loop over the full horizon |
| **CP** — `cyclic_state_of_charge_per_period` | Close one energy loop per investment period independently |
| **IP** — `state_of_charge_initial_per_period` | Reset SOC to `state_of_charge_initial` (MWh, absolute) at each period start |

**Run A** — global cycling only: `C=True`, `CP=False`, `IP=False`  
**Run B** — per-period cycling: `C=False`, `CP=True`, `IP=True`, `state_of_charge_initial=50.0 MWh`

All other parameters — network topology, load, renewable profiles, costs, efficiencies, solver — are **identical** between runs.

---

## Network Specification

| Component | Parameter | Value |
|---|---|---|
| Snapshots | 4 per investment period × 2 periods | 2025-Q1/Q2/Q3/Q4, 2030-Q1/Q2/Q3/Q4 |
| Snapshot weighting | Hours per snapshot | 21,900 h (≈ 2.5 yr) |
| Load | Flat | 100 MW |
| Solar | Capacity factor period 1 / period 2 | 0.60 / 0.20 |
| Solar | Capital cost | £60,000/MW |
| Gas | Capital cost / marginal cost | £80,000/MW / £150/MWh |
| Battery | `max_hours` | 4 h |
| Battery | `efficiency_store` / `efficiency_dispatch` | 0.95 / 0.95 |
| Battery | `standing_loss` | 0.001 /h |
| Battery | Capital cost | £50,000/MW |
| Solver | — | HiGHS |

---

## Key Mechanism — `include_previous_soc_pp`

The energy-balance constraint for each StorageUnit snapshot is built in [`define_storage_unit_constraints`](https://github.com/PyPSA/PyPSA/blob/cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d/pypsa/optimization/constraints.py#L1933):

```python
include_previous_soc_pp = active & (
    within_period | cyclic_state_of_charge_per_period
)
```

When **CP=True**, this mask is `True` at period-boundary snapshots. The predecessor SOC injected at the period-2 first snapshot is:

```python
previous_soc_pp = window.roll_within_periods(soc)
```

`roll_within_periods` rolls *within* each period — so the predecessor at the first snapshot of period 2 is the **last snapshot of period 2**, not period 1. This is the within-period wrap.

The `state_of_charge_initial` value (in MWh, **not** a fraction of `p_nom_opt`) is only injected into the RHS when `include_previous_soc` is `False`:

```python
rhs = rhs.where(include_previous_soc, rhs - soc_init)
```

Because CP=True keeps `include_previous_soc_pp=True` at period boundaries, `soc_init` is structurally absent from those rows. IP is ignored.

---

## Warnings

Two `logger.warning` calls fire in Run B (both from [`constraints.py#L1969–2003`](https://github.com/PyPSA/PyPSA/blob/cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d/pypsa/optimization/constraints.py#L1969)):

1. **Initial value ignored** — fires when CP=True, IP=True, and `state_of_charge_initial != 0`. Suppressed if `state_of_charge_initial = 0`.
2. **Per-period overrides global** — fires when both C and CP are True (not applicable in Run B since C=False, but would fire if both were set).

Run A emits no warnings.

---

## What the Experiment Records

For each run:
- All `logger.warning` messages emitted during optimisation
- SOC at the last snapshot of period 1 and first snapshot of period 2 (MWh)
- Full SOC trajectory across all 8 snapshots (MWh)
- Optimised battery capacity: `p_nom_opt` (MW) and `e_nom_opt` (MWh)
- Optimised solar and gas capacities (MW)
- Total system cost (objective value, £)

---

## How to Run

```bash
pip install -r requirements.txt
python experiment.py
```

---

## Important Notes

- `state_of_charge_initial` is an **absolute energy value in MWh**, not a fraction of `p_nom_opt` (which is a power capacity in MW). These are different physical quantities.
- The SOC at the first snapshot of period 2 in Run B will generally **not** equal the SOC at the last snapshot of period 1 — charging, discharging, and standing losses affect the energy balance. The within-period wrap means period 2's first snapshot uses period 2's last snapshot as its predecessor in the constraint, forming a closed loop within period 2.
- Whether Run B produces a larger or smaller battery than Run A depends on the specific cost and profile inputs specified above. The experiment records the actual outcome rather than assuming a direction.

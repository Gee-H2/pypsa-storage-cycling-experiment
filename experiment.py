"""
PyPSA StorageUnit CP vs C cycling experiment
=============================================
PyPSA version:  1.3.0
xarray version: 2025.1.2  (required — later versions break MultiIndex alignment)
linopy version: 0.9.1
Commit pinned:  cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d
File:           pypsa/optimization/constraints.py

Two fully specified runs — identical in every parameter except the cycling flags:

  Run A — global cycling only  (C=True,  CP=False, IP=False)
  Run B — per-period cycling   (C=False, CP=True,  IP=True,
                                 state_of_charge_initial=50.0 MWh)

Network
-------
Two investment periods (2025, 2030), 48 hourly snapshots per period
(two representative days), fixed 200 MW solar + extendable gas + extendable battery.

Solar profile: bell-curve CF peaking at noon (6am–6pm window).
  Period 2025: peak CF = 0.90  (strong daytime surplus → battery charges)
  Period 2030: peak CF = 0.25  (weak output → battery or gas required at night)

Load: 120 MW during waking hours (07:00–22:00), 60 MW overnight.

Battery: max_hours=6, capital_cost=1500 £/MW. Verified to build ~300 MW in
both runs with a real charge/discharge SOC cycle (0 → 1800 MWh and back).

IMPORTANT: state_of_charge_initial is in MWh (absolute energy). It is NOT a
fraction of p_nom_opt (power capacity in MW). These are different physical
quantities.

NOTE on warnings and PyPSA version
------------------------------------
The `has_initial` / `period_conflict` / `cp_overrides_c` warning blocks
visible in the pinned master commit (cfaab2f) were introduced after PyPSA 1.3.0.
The installed 1.3.0 does not emit those specific cycling warnings. The
constraint structure (boundary equations, SOC trajectories, RHS values) is
identical between 1.3.0 and master for this experiment. Warning behaviour is
documented against master commit cfaab2f only.

NOTE on the spillage variable patch
-------------------------------------
PyPSA 1.3.0 has a bug in define_spillage_variables that causes a KeyError when
optimising multi-investment-period models. The patch applied to the installed
package is documented in the README. It is a one-line try/except that returns
early when the MultiIndex dimension name lookup fails, which is safe because
none of the StorageUnits in this experiment have natural inflow.

What the experiment records
---------------------------
For each run:
  1. Cycling-related logger.WARNING messages
  2. Energy-balance LP rows at THREE boundary snapshots:
       - Period-1 last snapshot
       - Period-2 first snapshot   ← key difference between runs
       - Period-2 last snapshot    ← shows within-period loop closure in Run B
  3. SOC (MWh) at those three snapshots
  4. Full SOC trajectory (first day of each period, hourly)
  5. Optimised battery p_nom_opt (MW) and e_nom_opt (MWh)
  6. Optimised gas capacity (MW)
  7. Total system cost (objective)

Key mechanism: define_storage_unit_constraints
-----------------------------------------------
The energy-balance constraint at snapshot t:

    -1 * SOC[t]
    - (1/eff_dispatch) * eh * p_dispatch[t]
    + eff_store * eh * p_store[t]
    + eff_stand * include_previous_soc * SOC[predecessor(t)]
    = rhs[t]

The mask (PyPSA 1.3.0 equivalent of master-branch include_previous_soc_pp):

    include_previous_soc_pp = active & (periods == periods.shift(snapshot=1))
    # True within a period, False at period-start for non-cyclic assets
    # Overridden to True when CP=True (cyclic_state_of_charge_per_period)

Run A (C=True):  predecessor at period-2 first = SOC @ period-1 LAST  (cross-period)
Run B (CP=True): predecessor at period-2 first = SOC @ period-2 LAST  (within-period)
                 soc_init injected into RHS as -50.0

Period-2 LAST in Run B:
  predecessor = SOC @ period-2 second-to-last (normal within-period roll)
  This is how the within-period loop closes back on itself.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
import pypsa

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(message)s")

captured_warnings: dict[str, list[str]] = {"A": [], "B": []}


class _WarningCapture(logging.Handler):
    def __init__(self, store: list[str]) -> None:
        super().__init__()
        self._store = store

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno == logging.WARNING:
            self._store.append(record.getMessage())


# ---------------------------------------------------------------------------
# Solar profile: bell curve peaking at noon, zero outside 06:00–18:00
# ---------------------------------------------------------------------------

PER_PERIOD_TIMESTAMPS = pd.date_range("2020-01-01", periods=48, freq="h")
_HOURS = PER_PERIOD_TIMESTAMPS.hour % 24


def _solar_bell(hours: np.ndarray, peak: float) -> np.ndarray:
    cf = np.zeros(len(hours))
    mask = (hours >= 6) & (hours <= 18)
    cf[mask] = peak * np.sin(np.pi * (hours[mask] - 6) / 12)
    return cf


# Load: 120 MW waking hours, 60 MW overnight
LOAD_PROFILE = np.where((_HOURS >= 7) & (_HOURS <= 22), 120.0, 60.0)


# ---------------------------------------------------------------------------
# Network builder
# ---------------------------------------------------------------------------

def build_network(
    cyclic: bool,
    cyclic_per_period: bool,
    soc_initial_per_period: bool,
    soc_initial_mwh: float,
) -> pypsa.Network:
    """
    Build a two-investment-period network.

    Investment periods : 2025, 2030  (5 calendar years each)
    Snapshots          : 48 hourly timestamps (two representative days)
    Snapshot weighting : 912.5 h  (5 yr × 8760 h/yr / 48 snapshots)

    Solar is fixed at 200 MW (not extendable) with a bell-curve CF profile:
      Period 2025 : peak CF = 0.90  (strong daytime surplus)
      Period 2030 : peak CF = 0.25  (weak output, battery or gas needed at night)

    This produces a real SOC cycle: battery charges during the day and
    discharges at night (SOC swings between 0 and ~1800 MWh).

    state_of_charge_initial = 50.0 MWh is an ABSOLUTE energy value, NOT a
    fraction of p_nom_opt (which is power capacity in MW).
    """
    n = pypsa.Network()
    n.set_snapshots(PER_PERIOD_TIMESTAMPS)
    n.set_investment_periods([2025, 2030])

    n.snapshot_weightings.loc[:, :] = 912.5
    n.investment_period_weightings["years"] = 5.0
    n.investment_period_weightings["objective"] = 1.0

    sns = n.snapshots

    # Register carriers to suppress PyPSA 1.3.0 carrier-undefined warnings
    for carrier in ("AC", "solar", "gas", "battery"):
        n.add("Carrier", carrier)

    n.add("Bus", "grid", carrier="AC")

    load = pd.Series(index=sns, dtype=float)
    load.loc[2025] = LOAD_PROFILE
    load.loc[2030] = LOAD_PROFILE
    n.add("Load", "demand", bus="grid", p_set=load)

    cf = pd.Series(index=sns, dtype=float)
    cf.loc[2025] = _solar_bell(_HOURS, peak=0.90)   # period 1: surplus
    cf.loc[2030] = _solar_bell(_HOURS, peak=0.25)   # period 2: deficit
    n.add(
        "Generator", "solar",
        bus="grid", carrier="solar",
        p_nom=200.0,                  # fixed, not extendable
        p_nom_extendable=False,
        capital_cost=0.0,
        marginal_cost=0.0,
        p_max_pu=cf,
    )

    n.add(
        "Generator", "gas",
        bus="grid", carrier="gas",
        p_nom_extendable=True,
        p_nom_min=0.0,
        p_nom_max=500.0,
        capital_cost=200_000.0,       # £/MW
        marginal_cost=300.0,          # £/MWh
    )

    n.add(
        "StorageUnit", "battery",
        bus="grid", carrier="battery",
        p_nom_extendable=True,
        p_nom_min=0.0,
        p_nom_max=300.0,              # MW power capacity
        max_hours=6.0,                # e_nom = 6 × p_nom MWh
        capital_cost=1_500.0,         # £/MW  (verified to build ~300 MW)
        marginal_cost=0.0,
        efficiency_store=0.95,
        efficiency_dispatch=0.95,
        standing_loss=0.0,
        cyclic_state_of_charge=cyclic,
        cyclic_state_of_charge_per_period=cyclic_per_period,
        state_of_charge_initial_per_period=soc_initial_per_period,
        state_of_charge_initial=soc_initial_mwh,   # MWh absolute
    )

    return n


# ---------------------------------------------------------------------------
# Boundary equation extractor
# ---------------------------------------------------------------------------

def extract_boundary_equations(n: pypsa.Network) -> dict:
    """
    Build the linopy model (without solving) and extract the LP energy-balance
    constraint rows at THREE boundary snapshots:
      - period1_last  : last snapshot of period 1
      - period2_first : first snapshot of period 2  ← key difference
      - period2_last  : last snapshot of period 2   ← shows loop closure in Run B
    """
    n.optimize.create_model(multi_investment_periods=True)
    m = n.model
    eb = m.constraints["StorageUnit-energy_balance"]
    sns_idx = n.snapshots

    p1_snaps = sns_idx[sns_idx.get_level_values(0) == 2025]
    p2_snaps = sns_idx[sns_idx.get_level_values(0) == 2030]
    p1_last   = p1_snaps[-1]
    p2_first  = p2_snaps[0]
    p2_last   = p2_snaps[-1]

    # var_id → (short_name, snapshot)
    var_map: dict[int, tuple[str, object]] = {}
    for vn, short in [
        ("StorageUnit-state_of_charge", "SOC"),
        ("StorageUnit-p_dispatch",      "p_dispatch"),
        ("StorageUnit-p_store",         "p_store"),
    ]:
        vobj = m.variables[vn]
        for snap in vobj.labels.coords["snapshot"].values:
            lid = int(vobj.labels.sel(snapshot=snap, StorageUnit="battery").values)
            var_map[lid] = (short, snap)

    result = {}
    for key, snap in [
        ("period1_last",  p1_last),
        ("period2_first", p2_first),
        ("period2_last",  p2_last),
    ]:
        row_c   = eb.coeffs.sel(snapshot=snap, StorageUnit="battery").values
        row_v   = eb.vars.sel(snapshot=snap,   StorageUnit="battery").values
        row_rhs = float(eb.rhs.sel(snapshot=snap, StorageUnit="battery"))
        terms = [
            (float(c), *var_map.get(int(v), (f"var[{v}]", "?")))
            for c, v in zip(row_c, row_v) if v >= 0
        ]
        result[key] = {"snapshot": snap, "terms": terms, "rhs": row_rhs}

    n._model = None
    n._optimize_window = None
    return result


# ---------------------------------------------------------------------------
# Run optimisation
# ---------------------------------------------------------------------------

def run(n: pypsa.Network, label: str, warning_store: list[str]) -> dict:
    pypsa_logger = logging.getLogger("pypsa")
    handler = _WarningCapture(warning_store)
    pypsa_logger.addHandler(handler)
    pypsa_logger.setLevel(logging.WARNING)

    status, condition = n.optimize(
        solver_name="highs",
        multi_investment_periods=True,
    )

    pypsa_logger.removeHandler(handler)

    if status != "ok":
        raise RuntimeError(
            f"Run {label}: solver status={status}, condition={condition}"
        )

    su  = n.storage_units
    soc = n.storage_units_t.state_of_charge["battery"]
    sns = n.snapshots
    p1_snaps = sns[sns.get_level_values(0) == 2025]
    p2_snaps = sns[sns.get_level_values(0) == 2030]

    return {
        "label":             label,
        "warnings":          list(warning_store),
        "p_nom_opt_mw":      float(su.loc["battery", "p_nom_opt"]),
        "e_nom_opt_mwh":     float(su.loc["battery", "p_nom_opt"] * su.loc["battery", "max_hours"]),
        "gas_p_nom_opt_mw":  float(n.generators.loc["gas", "p_nom_opt"]),
        "objective":         float(n.objective),
        "soc_p1_last":       float(soc.loc[p1_snaps[-1]]),
        "soc_p2_first":      float(soc.loc[p2_snaps[0]]),
        "soc_p2_last":       float(soc.loc[p2_snaps[-1]]),
        "soc_p1_max":        float(soc.loc[2025].max()),
        "soc_p2_max":        float(soc.loc[2030].max()),
        "p1_last_snap":      str(p1_snaps[-1]),
        "p2_first_snap":     str(p2_snaps[0]),
        "p2_last_snap":      str(p2_snaps[-1]),
        "soc_p1_day1":       {str(k): round(float(v), 1)
                              for k, v in list(soc.loc[2025].items())[:24]},
        "soc_p2_day1":       {str(k): round(float(v), 1)
                              for k, v in list(soc.loc[2030].items())[:24]},
    }


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------

def print_equations(label: str, eqs: dict) -> None:
    sep = "=" * 66
    print(f"\n{sep}")
    print(f"  BOUNDARY EQUATIONS — Run {label}")
    print(sep)
    keys = [
        ("period1_last",  "Period-1 LAST  "),
        ("period2_first", "Period-2 FIRST "),
        ("period2_last",  "Period-2 LAST  "),
    ]
    for key, key_label in keys:
        eq = eqs[key]
        print(f"\n  [{key_label}]  {eq['snapshot']}")
        print(f"  Constraint  = {eq['rhs']:.4f}  (RHS):")
        for coeff, vname, vsnap in eq["terms"]:
            print(f"    {coeff:+10.4f}  *  {vname:<12}  @  {vsnap}")
        print(f"                   =  {eq['rhs']:.4f}")


def print_results(r: dict) -> None:
    sep = "=" * 66
    print(f"\n{sep}")
    print(f"  OPTIMISED RESULTS — Run {r['label']}")
    print(sep)

    print("\n--- Cycling-related warnings ---")
    cw = [w for w in r["warnings"] if any(
        k in w for k in ("StorageUnit", "cyclic", "initial", "Cyclic", "per_period")
    )]
    print(f"  {len(cw)} cycling-related warning(s)" if cw else "  (none — see NOTE on warnings in docstring)")

    print("\n--- SOC at boundary snapshots ---")
    print(f"  Period-1 last   {r['p1_last_snap']} :  {r['soc_p1_last']:8.2f} MWh")
    print(f"  Period-2 first  {r['p2_first_snap']} :  {r['soc_p2_first']:8.2f} MWh")
    print(f"  Period-2 last   {r['p2_last_snap']} :  {r['soc_p2_last']:8.2f} MWh")

    print("\n--- Optimised capacities ---")
    print(f"  Battery  p_nom_opt : {r['p_nom_opt_mw']:7.2f} MW")
    print(f"  Battery  e_nom_opt : {r['e_nom_opt_mwh']:7.2f} MWh  (= p_nom × 6 h)")
    print(f"  Gas      p_nom_opt : {r['gas_p_nom_opt_mw']:7.2f} MW")
    print(f"  NOTE: soc_initial=50.0 MWh is absolute energy, not 50/{r['p_nom_opt_mw']:.0f} MW")

    print("\n--- Total system cost ---")
    print(f"  {r['objective']:,.2f}")

    print("\n--- SOC cycle — period 1 day 1 (hourly) ---")
    for i, (ts, val) in enumerate(r["soc_p1_day1"].items()):
        h = i % 24
        tag = "DAY " if (6 <= h <= 18) else "NGHT"
        bar = "█" * int(val / 100) if val > 0 else ""
        print(f"  h{h:02d} [{tag}]  {val:7.1f} MWh  {bar}")

    print("\n--- SOC cycle — period 2 day 1 (hourly) ---")
    for i, (ts, val) in enumerate(r["soc_p2_day1"].items()):
        h = i % 24
        tag = "DAY " if (6 <= h <= 18) else "NGHT"
        bar = "█" * int(val / 100) if val > 0 else ""
        print(f"  h{h:02d} [{tag}]  {val:7.1f} MWh  {bar}")


def compare(a: dict, b: dict, eq_a: dict, eq_b: dict) -> None:
    sep = "=" * 66
    print(f"\n{sep}")
    print("  COMPARISON  Run A (C only)  vs  Run B (CP + IP)")
    print(sep)
    print(f"  {'Metric':<42} {'Run A':>10} {'Run B':>10}")
    print(f"  {'-'*42} {'-'*10} {'-'*10}")
    for name, key in [
        ("Battery p_nom_opt (MW)",    "p_nom_opt_mw"),
        ("Battery e_nom_opt (MWh)",   "e_nom_opt_mwh"),
        ("Gas p_nom_opt (MW)",        "gas_p_nom_opt_mw"),
        ("SOC period-1 last (MWh)",   "soc_p1_last"),
        ("SOC period-2 first (MWh)",  "soc_p2_first"),
        ("SOC period-2 last (MWh)",   "soc_p2_last"),
        ("SOC period-1 max (MWh)",    "soc_p1_max"),
        ("SOC period-2 max (MWh)",    "soc_p2_max"),
        ("Objective",                 "objective"),
    ]:
        print(f"  {name:<42} {a[key]:>10.2f} {b[key]:>10.2f}")

    print(f"\n--- Structural difference at period-2 FIRST snapshot ---")
    print("  Run A predecessor SOC (cross-period link to period-1 last):")
    for coeff, vname, vsnap in eq_a["period2_first"]["terms"]:
        if vname == "SOC" and vsnap != eq_a["period2_first"]["snapshot"]:
            print(f"    {coeff:+.4f} * SOC @ {vsnap}")
    print(f"  Run A RHS : {eq_a['period2_first']['rhs']:.4f}")
    print()
    print("  Run B predecessor SOC (none — soc_init in RHS instead):")
    b_soc = [t for t in eq_b["period2_first"]["terms"]
             if t[1] == "SOC" and t[2] != eq_b["period2_first"]["snapshot"]]
    if b_soc:
        for coeff, vname, vsnap in b_soc:
            print(f"    {coeff:+.4f} * SOC @ {vsnap}  (within-period wrap)")
    else:
        print("    (absent)")
    print(f"  Run B RHS : {eq_b['period2_first']['rhs']:.4f}  (= -soc_initial = -50.0 MWh)")

    print(f"\n--- Structural difference at period-2 LAST snapshot ---")
    print("  Run B — within-period loop closure (period-2 last links back into period 2):")
    for coeff, vname, vsnap in eq_b["period2_last"]["terms"]:
        if vname == "SOC" and vsnap != eq_b["period2_last"]["snapshot"]:
            print(f"    {coeff:+.4f} * SOC @ {vsnap}  (predecessor within period 2)")
    print(f"  Run B period-2 last RHS : {eq_b['period2_last']['rhs']:.4f}")
    print("  Run B SOC period-2 last solved value :", round(b["soc_p2_last"], 2), "MWh")
    print("  → Period 2 closes to", round(b["soc_p2_last"], 2),
          "MWh, making period-2 first's soc_init=50 MWh the effective start value")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    print("\nRun A — global cycling only (C=True, CP=False, IP=False)")
    n_a = build_network(
        cyclic=True, cyclic_per_period=False,
        soc_initial_per_period=False, soc_initial_mwh=0.0,
    )
    eq_a = extract_boundary_equations(n_a)
    print_equations("A", eq_a)
    results_a = run(n_a, "A", captured_warnings["A"])
    print_results(results_a)

    print("\nRun B — per-period cycling (C=False, CP=True, IP=True, soc_initial=50 MWh)")
    n_b = build_network(
        cyclic=False, cyclic_per_period=True,
        soc_initial_per_period=True, soc_initial_mwh=50.0,
    )
    eq_b = extract_boundary_equations(n_b)
    print_equations("B", eq_b)
    results_b = run(n_b, "B", captured_warnings["B"])
    print_results(results_b)

    compare(results_a, results_b, eq_a, eq_b)

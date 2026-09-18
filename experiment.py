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

Network: two investment periods (2025, 2030), 48 hourly snapshots per period
(two representative days), extendable solar + gas + battery.

Period 1 (2025): high solar CF (1.5 daytime) — charging surplus.
Period 2 (2030): very low solar CF (0.1 daytime) — deficit, needs storage or gas.
Load: 50 MW daytime, 150 MW night.

What the experiment records
---------------------------
For each run:
  1. All logger.WARNING messages emitted during optimisation
  2. Energy-balance LP constraint rows at the period boundary (period-1 last
     snapshot and period-2 first snapshot) — showing exact coefficients,
     variable names, snapshot coordinates, and RHS
  3. SOC (MWh) at the last snapshot of period 1 and first snapshot of period 2
  4. Full SOC trajectory across all 96 snapshots
  5. Optimised battery p_nom_opt (MW) and e_nom_opt (MWh)
  6. Optimised solar and gas capacities (MW)
  7. Total system cost (objective)

Key mechanism: define_storage_unit_constraints
-----------------------------------------------
The energy-balance constraint at each snapshot is built as:

    -1 * SOC[t]
    - (1/eff_dispatch) * eh * p_dispatch[t]
    + eff_store * eh * p_store[t]
    + eff_stand * include_previous_soc * previous_soc[predecessor]
    = rhs[t]

The mask:
    include_previous_soc_pp = active & (within_period | cyclic_state_of_charge_per_period)

controls which SOC variable is the predecessor and whether soc_init enters the RHS.

Run A (C=True):  predecessor at period-2 first = SOC @ period-1 LAST snapshot (cross-period)
Run B (CP=True): predecessor at period-2 first = SOC @ period-2 LAST snapshot (within-period wrap)
                 soc_init=50 MWh injected into RHS instead (visible as RHS=-50.0)

IMPORTANT: state_of_charge_initial is in MWh (absolute energy), NOT a fraction
of p_nom_opt (which is power capacity in MW).
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
# Network builder
# ---------------------------------------------------------------------------

PER_PERIOD_TIMESTAMPS = pd.date_range("2020-01-01", periods=48, freq="h")
_hours = PER_PERIOD_TIMESTAMPS.hour % 24
_daytime = (_hours >= 8) & (_hours <= 17)
LOAD_PROFILE = np.where(_daytime, 50.0, 150.0)   # 50 MW day, 150 MW night


def build_network(
    cyclic: bool,
    cyclic_per_period: bool,
    soc_initial_per_period: bool,
    soc_initial_mwh: float,
) -> pypsa.Network:
    """
    Build a two-investment-period network.

    Investment periods : 2025, 2030 (each representing 5 calendar years)
    Snapshots          : 48 hourly timestamps (two representative days)
    Snapshot weighting : 912.5 h each  (5 yr × 8760 h/yr / 48 snapshots)

    Solar profile — daytime hours 08:00–17:00:
      Period 2025 : CF = 1.5  (strong surplus, battery charges)
      Period 2030 : CF = 0.1  (weak output, storage or gas required at night)

    IMPORTANT: state_of_charge_initial is in MWh (absolute energy), NOT a
    fraction of p_nom_opt (power capacity in MW).
    """
    n = pypsa.Network()
    n.set_snapshots(PER_PERIOD_TIMESTAMPS)
    n.set_investment_periods([2025, 2030])

    n.snapshot_weightings.loc[:, :] = 912.5   # h per snapshot
    n.investment_period_weightings["years"] = 5.0
    n.investment_period_weightings["objective"] = 1.0

    sns = n.snapshots

    n.add("Bus", "grid", carrier="AC")

    load = pd.Series(index=sns, dtype=float)
    load.loc[2025] = LOAD_PROFILE
    load.loc[2030] = LOAD_PROFILE
    n.add("Load", "demand", bus="grid", p_set=load)

    cf = pd.Series(index=sns, dtype=float)
    cf.loc[2025] = np.where(_daytime, 1.5, 0.0)   # period 1: surplus
    cf.loc[2030] = np.where(_daytime, 0.1, 0.0)   # period 2: deficit
    n.add(
        "Generator", "solar",
        bus="grid", carrier="solar",
        p_nom_extendable=True, p_nom_min=0.0, p_nom_max=500.0,
        capital_cost=60_000.0, marginal_cost=0.0,
        p_max_pu=cf,
    )

    n.add(
        "Generator", "gas",
        bus="grid", carrier="gas",
        p_nom_extendable=True, p_nom_min=0.0, p_nom_max=500.0,
        capital_cost=100_000.0, marginal_cost=200.0,
    )

    n.add(
        "StorageUnit", "battery",
        bus="grid", carrier="battery",
        p_nom_extendable=True, p_nom_min=0.0, p_nom_max=500.0,
        max_hours=6.0,
        capital_cost=5_000.0, marginal_cost=0.0,
        efficiency_store=0.95, efficiency_dispatch=0.95,
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
    Build the linopy model (without solving) and extract the exact LP
    energy-balance constraint rows at the period boundary.

    Returns a dict with keys 'period1_last' and 'period2_first', each
    containing a list of (coefficient, variable_name, snapshot) tuples
    plus the RHS value.
    """
    n.optimize.create_model(multi_investment_periods=True)
    m = n.model
    eb = m.constraints["StorageUnit-energy_balance"]
    sns_idx = n.snapshots
    p1_last  = sns_idx[sns_idx.get_level_values(0) == 2025][-1]
    p2_first = sns_idx[sns_idx.get_level_values(0) == 2030][0]

    # Build var_id -> (short_name, snapshot) lookup
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
    for key, snap in [("period1_last", p1_last), ("period2_first", p2_first)]:
        row_c   = eb.coeffs.sel(snapshot=snap, StorageUnit="battery").values
        row_v   = eb.vars.sel(snapshot=snap, StorageUnit="battery").values
        row_rhs = float(eb.rhs.sel(snapshot=snap, StorageUnit="battery"))
        terms = []
        for coeff, vid in zip(row_c, row_v):
            if vid >= 0:
                vname, vsnap = var_map.get(int(vid), (f"var[{vid}]", "?"))
                terms.append((float(coeff), vname, vsnap))
        result[key] = {"snapshot": snap, "terms": terms, "rhs": row_rhs}

    # Clear the model so the network can be re-optimised cleanly
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
    p1_last  = sns[sns.get_level_values(0) == 2025][-1]
    p2_first = sns[sns.get_level_values(0) == 2030][0]

    return {
        "label": label,
        "warnings": list(warning_store),
        "p_nom_opt_mw":       float(su.loc["battery", "p_nom_opt"]),
        "e_nom_opt_mwh":      float(su.loc["battery", "p_nom_opt"] * su.loc["battery", "max_hours"]),
        "solar_p_nom_opt_mw": float(n.generators.loc["solar", "p_nom_opt"]),
        "gas_p_nom_opt_mw":   float(n.generators.loc["gas",   "p_nom_opt"]),
        "objective":          float(n.objective),
        "soc_period1_last":   float(soc.loc[p1_last]),
        "soc_period2_first":  float(soc.loc[p2_first]),
        "period1_last_snap":  str(p1_last),
        "period2_first_snap": str(p2_first),
        "soc_trajectory":     {str(k): round(float(v), 3) for k, v in soc.items()},
    }


# ---------------------------------------------------------------------------
# Print
# ---------------------------------------------------------------------------

def print_boundary_equations(label: str, eqs: dict) -> None:
    print(f"\n{'='*64}")
    print(f"  BOUNDARY EQUATIONS  — Run {label}")
    print(f"{'='*64}")
    for key in ("period1_last", "period2_first"):
        eq = eqs[key]
        snap_label = "Period-1 LAST " if key == "period1_last" else "Period-2 FIRST"
        print(f"\n  [{snap_label}]  snapshot = {eq['snapshot']}")
        print(f"  Energy-balance constraint  (= {eq['rhs']:.4f} RHS):")
        for coeff, vname, vsnap in eq["terms"]:
            print(f"    {coeff:+10.4f}  *  {vname:<12}  @  {vsnap}")
        print(f"    {'':>10}     =  {eq['rhs']:.4f}")


def print_results(r: dict) -> None:
    sep = "=" * 64
    print(f"\n{sep}")
    print(f"  RUN {r['label']}  —  optimised results")
    print(sep)

    print("\n--- Warnings emitted during optimisation ---")
    cycling_warns = [
        w for w in r["warnings"]
        if any(kw in w for kw in ("StorageUnit", "cyclic", "initial", "Cyclic"))
    ]
    if cycling_warns:
        for w in cycling_warns:
            print(f"  WARNING: {w}")
    else:
        print("  (none relevant to cycling flags)")

    print("\n--- SOC at period boundary ---")
    print(f"  Period-1 last  {r['period1_last_snap']} :  {r['soc_period1_last']:.3f} MWh")
    print(f"  Period-2 first {r['period2_first_snap']} :  {r['soc_period2_first']:.3f} MWh")

    print("\n--- Optimised capacities ---")
    print(f"  Battery  p_nom_opt : {r['p_nom_opt_mw']:.2f} MW")
    print(f"  Battery  e_nom_opt : {r['e_nom_opt_mwh']:.2f} MWh")
    print(f"  Solar    p_nom_opt : {r['solar_p_nom_opt_mw']:.2f} MW")
    print(f"  Gas      p_nom_opt : {r['gas_p_nom_opt_mw']:.2f} MW")

    print("\n--- Total system cost (objective) ---")
    print(f"  {r['objective']:,.2f}")

    print("\n--- SOC trajectory (period 1: first 3 and last 3 snapshots) ---")
    items1 = [(k, v) for k, v in r["soc_trajectory"].items() if "2025" in k]
    for ts, val in items1[:3]:
        print(f"  {ts}  {val:.3f} MWh")
    print("  ...")
    for ts, val in items1[-3:]:
        print(f"  {ts}  {val:.3f} MWh")

    print("\n--- SOC trajectory (period 2: first 3 and last 3 snapshots) ---")
    items2 = [(k, v) for k, v in r["soc_trajectory"].items() if "2030" in k]
    for ts, val in items2[:3]:
        print(f"  {ts}  {val:.3f} MWh")
    print("  ...")
    for ts, val in items2[-3:]:
        print(f"  {ts}  {val:.3f} MWh")


def compare(a: dict, b: dict, eq_a: dict, eq_b: dict) -> None:
    print(f"\n{'='*64}")
    print("  COMPARISON  Run A (C only)  vs  Run B (CP + IP)")
    print(f"{'='*64}")
    print(f"  {'Metric':<40} {'Run A':>10} {'Run B':>10}")
    print(f"  {'-'*40} {'-'*10} {'-'*10}")
    for name, key in [
        ("Battery p_nom_opt (MW)",    "p_nom_opt_mw"),
        ("Battery e_nom_opt (MWh)",   "e_nom_opt_mwh"),
        ("Solar p_nom_opt (MW)",      "solar_p_nom_opt_mw"),
        ("Gas p_nom_opt (MW)",        "gas_p_nom_opt_mw"),
        ("SOC period-1 last (MWh)",   "soc_period1_last"),
        ("SOC period-2 first (MWh)",  "soc_period2_first"),
        ("Objective",                 "objective"),
    ]:
        print(f"  {name:<40} {a[key]:>10.2f} {b[key]:>10.2f}")

    cw_a = len([w for w in a["warnings"] if "StorageUnit" in w or "cyclic" in w.lower()])
    cw_b = len([w for w in b["warnings"] if "StorageUnit" in w or "cyclic" in w.lower()])
    print(f"\n  Cycling-related warnings Run A : {cw_a}")
    print(f"  Cycling-related warnings Run B : {cw_b}")

    print("\n--- Key structural difference at period-2 first snapshot ---")
    print("  Run A predecessor SOC variable:")
    for coeff, vname, vsnap in eq_a["period2_first"]["terms"]:
        if vname == "SOC":
            print(f"    {coeff:+.4f} * SOC @ {vsnap}  (= period-1 LAST — cross-period link)")
    print(f"  Run A RHS : {eq_a['period2_first']['rhs']:.4f}")

    print("  Run B predecessor SOC variable:")
    soc_terms_b = [t for t in eq_b["period2_first"]["terms"] if t[1] == "SOC" and t[2] != eq_b["period2_first"]["snapshot"]]
    if soc_terms_b:
        for coeff, vname, vsnap in soc_terms_b:
            print(f"    {coeff:+.4f} * SOC @ {vsnap}  (= period-2 LAST — within-period wrap)")
    else:
        print("    (none — soc_init injected into RHS instead)")
    print(f"  Run B RHS : {eq_b['period2_first']['rhs']:.4f}  (= -soc_initial = -50.0 MWh)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    # --- Run A ---
    print("\nRun A — global cycling only (C=True, CP=False, IP=False)")
    n_a = build_network(
        cyclic=True, cyclic_per_period=False,
        soc_initial_per_period=False, soc_initial_mwh=0.0,
    )
    eq_a = extract_boundary_equations(n_a)
    print_boundary_equations("A", eq_a)
    results_a = run(n_a, "A", captured_warnings["A"])
    print_results(results_a)

    # --- Run B ---
    print("\nRun B — per-period cycling (C=False, CP=True, IP=True, soc_initial=50 MWh)")
    n_b = build_network(
        cyclic=False, cyclic_per_period=True,
        soc_initial_per_period=True, soc_initial_mwh=50.0,
    )
    eq_b = extract_boundary_equations(n_b)
    print_boundary_equations("B", eq_b)
    results_b = run(n_b, "B", captured_warnings["B"])
    print_results(results_b)

    # --- Comparison ---
    compare(results_a, results_b, eq_a, eq_b)

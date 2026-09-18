"""
PyPSA StorageUnit CP vs C cycling experiment
=============================================
Commit pinned: cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d
File:          pypsa/optimization/constraints.py

Two fully specified runs:
  Run A — global cycling only  (C=True,  CP=False, IP=False)
  Run B — per-period cycling   (C=False, CP=True,  IP=True,  soc_initial=50.0 MWh)

Both runs use identical networks, costs, profiles, and solver settings.
The only difference is the three StorageUnit cycling flags.

Outputs recorded for each run
------------------------------
- logger warnings emitted during optimisation
- Energy-balance equation at the period boundary (last snapshot of period 1
  and first snapshot of period 2)
- Full SOC trajectory (MWh)
- Optimised storage capacity p_nom_opt (MW) and e_nom_opt (MWh)
- Optimised generator capacities (MW)
- Total system cost (objective value, £)
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
import pypsa

# ---------------------------------------------------------------------------
# Logging — capture warnings emitted by PyPSA during optimisation
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

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

def build_network(
    cyclic: bool,
    cyclic_per_period: bool,
    soc_initial_per_period: bool,
    soc_initial_mwh: float,
) -> pypsa.Network:
    """
    Build a two-investment-period network with:
      - 2 investment periods: 2025 and 2030, each represented by 4 snapshots
        (one per quarter, weighted to represent ~2.5 years each)
      - 1 bus
      - 1 extendable solar generator  (period 1: high output, period 2: low)
      - 1 extendable gas generator    (expensive peaker, always available)
      - 1 extendable StorageUnit      (4-hour battery)
      - 1 fixed load

    All costs and efficiencies are explicit. No defaults are relied upon
    beyond PyPSA component defaults documented in the reference version.

    Parameters
    ----------
    cyclic : bool
        cyclic_state_of_charge — global horizon cycling (C)
    cyclic_per_period : bool
        cyclic_state_of_charge_per_period — per-period cycling (CP)
    soc_initial_per_period : bool
        state_of_charge_initial_per_period — use soc_initial at period start (IP)
    soc_initial_mwh : float
        state_of_charge_initial in MWh (absolute energy, NOT a fraction of p_nom)
    """

    # ------------------------------------------------------------------
    # Snapshots: 4 quarterly snapshots per investment year
    # Each snapshot is weighted ~21,900 hours (2.5 years / 4 quarters)
    # ------------------------------------------------------------------
    years = [2025, 2030]
    quarters = pd.to_timedelta([0, 3, 6, 9], unit="ME")  # 0, 3, 6, 9 months

    snapshots = pd.DatetimeIndex(
        [pd.Timestamp(y, 1, 1) + q for y in years for q in quarters]
    )
    investment_periods = years
    investment_period_weightings = pd.DataFrame(
        {
            "years": [5.0, 5.0],        # each period spans 5 calendar years
            "objective": [1.0, 1.0],    # equal objective weighting
        },
        index=pd.Index(investment_periods, name="period"),
    )

    n = pypsa.Network()
    n.set_snapshots(
        snapshots,
        weightings=pd.Series(
            [21900.0] * 8,   # hours per snapshot (5 years / 4 snapshots * 8760 h/yr)
            index=snapshots,
        ),
    )
    n.investment_periods = investment_periods
    n.investment_period_weightings = investment_period_weightings

    # ------------------------------------------------------------------
    # Bus
    # ------------------------------------------------------------------
    n.add("Bus", "grid", carrier="AC")

    # ------------------------------------------------------------------
    # Load — fixed, identical across all snapshots
    # ------------------------------------------------------------------
    n.add(
        "Load",
        "demand",
        bus="grid",
        p_set=pd.Series(100.0, index=snapshots),   # 100 MW flat load
    )

    # ------------------------------------------------------------------
    # Solar generator — extendable, zero marginal cost
    # Period 1 (2025): high capacity factor (0.60) — surplus scenario
    # Period 2 (2030): low capacity factor  (0.20) — deficit scenario
    # ------------------------------------------------------------------
    solar_cf = pd.Series(
        [0.60, 0.60, 0.60, 0.60,   # 2025 quarters
         0.20, 0.20, 0.20, 0.20],  # 2030 quarters
        index=snapshots,
    )
    n.add(
        "Generator",
        "solar",
        bus="grid",
        carrier="solar",
        p_nom_extendable=True,
        p_nom_min=0.0,
        p_nom_max=1000.0,           # MW
        capital_cost=60_000.0,      # £/MW (annualised over period)
        marginal_cost=0.0,          # £/MWh
        p_max_pu=solar_cf,
        p_min_pu=pd.Series(0.0, index=snapshots),
        efficiency=1.0,
    )

    # ------------------------------------------------------------------
    # Gas generator — extendable, expensive peaker
    # ------------------------------------------------------------------
    n.add(
        "Generator",
        "gas",
        bus="grid",
        carrier="gas",
        p_nom_extendable=True,
        p_nom_min=0.0,
        p_nom_max=500.0,            # MW
        capital_cost=80_000.0,      # £/MW
        marginal_cost=150.0,        # £/MWh
        p_max_pu=pd.Series(1.0, index=snapshots),
        p_min_pu=pd.Series(0.0, index=snapshots),
        efficiency=0.45,
    )

    # ------------------------------------------------------------------
    # StorageUnit — extendable 4-hour battery
    # ------------------------------------------------------------------
    n.add(
        "StorageUnit",
        "battery",
        bus="grid",
        carrier="battery",
        # capacity
        p_nom_extendable=True,
        p_nom_min=0.0,
        p_nom_max=500.0,            # MW power capacity
        max_hours=4.0,              # energy capacity = 4 × p_nom MWh
        # costs
        capital_cost=50_000.0,      # £/MW
        marginal_cost=0.0,          # £/MWh
        # efficiencies
        efficiency_store=0.95,
        efficiency_dispatch=0.95,
        standing_loss=0.001,        # 0.1 % per hour
        # cycling flags — varied between runs
        cyclic_state_of_charge=cyclic,
        cyclic_state_of_charge_per_period=cyclic_per_period,
        state_of_charge_initial_per_period=soc_initial_per_period,
        state_of_charge_initial=soc_initial_mwh,   # MWh (absolute)
    )

    return n


# ---------------------------------------------------------------------------
# Run a network and collect results
# ---------------------------------------------------------------------------

def run(n: pypsa.Network, label: str, warning_store: list[str]) -> dict:
    """Optimise network, capture warnings, return results dict."""

    # Attach warning capture handler
    pypsa_logger = logging.getLogger("pypsa")
    handler = _WarningCapture(warning_store)
    pypsa_logger.addHandler(handler)

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        status, condition = n.optimize(
            solver_name="highs",
            multi_investment_periods=True,
        )

    pypsa_logger.removeHandler(handler)

    if status != "ok":
        raise RuntimeError(f"Run {label}: solver returned status={status}, condition={condition}")

    su = n.storage_units
    su_t = n.storage_units_t

    # Identify period boundary snapshot indices
    # Period 2 starts at the 5th snapshot (index 4)
    period1_last = n.snapshots[3]
    period2_first = n.snapshots[4]

    soc = su_t.state_of_charge["battery"]

    results = {
        "label": label,
        "warnings": list(warning_store),
        "p_nom_opt_mw": float(su.loc["battery", "p_nom_opt"]),
        "e_nom_opt_mwh": float(su.loc["battery", "p_nom_opt"] * su.loc["battery", "max_hours"]),
        "solar_p_nom_opt_mw": float(n.generators.loc["solar", "p_nom_opt"]),
        "gas_p_nom_opt_mw": float(n.generators.loc["gas", "p_nom_opt"]),
        "objective": float(n.objective),
        "soc_period1_last_mwh": float(soc.loc[period1_last]),
        "soc_period2_first_mwh": float(soc.loc[period2_first]),
        "soc_trajectory": soc.round(3).to_dict(),
        "period1_last_snapshot": str(period1_last),
        "period2_first_snapshot": str(period2_first),
    }
    return results


# ---------------------------------------------------------------------------
# Print results
# ---------------------------------------------------------------------------

def print_results(r: dict) -> None:
    label = r["label"]
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  RUN {label}")
    print(sep)

    print("\n--- Warnings emitted ---")
    if r["warnings"]:
        for w in r["warnings"]:
            print(f"  WARNING: {w}")
    else:
        print("  (none)")

    print("\n--- Period boundary SOC ---")
    print(f"  Last snapshot of period 1  ({r['period1_last_snapshot']}): "
          f"{r['soc_period1_last_mwh']:.2f} MWh")
    print(f"  First snapshot of period 2 ({r['period2_first_snapshot']}): "
          f"{r['soc_period2_first_mwh']:.2f} MWh")

    print("\n--- Optimised capacities ---")
    print(f"  Battery p_nom_opt : {r['p_nom_opt_mw']:.2f} MW")
    print(f"  Battery e_nom_opt : {r['e_nom_opt_mwh']:.2f} MWh")
    print(f"  Solar   p_nom_opt : {r['solar_p_nom_opt_mw']:.2f} MW")
    print(f"  Gas     p_nom_opt : {r['gas_p_nom_opt_mw']:.2f} MW")

    print("\n--- Objective (total system cost) ---")
    print(f"  £{r['objective']:,.0f}")

    print("\n--- Full SOC trajectory (MWh) ---")
    for ts, val in r["soc_trajectory"].items():
        print(f"  {ts}  {val:.3f} MWh")


# ---------------------------------------------------------------------------
# Compare runs
# ---------------------------------------------------------------------------

def compare(a: dict, b: dict) -> None:
    print("\n" + "=" * 60)
    print("  COMPARISON  (Run A vs Run B)")
    print("=" * 60)
    print(f"  {'Metric':<35} {'Run A':>12} {'Run B':>12}")
    print(f"  {'-'*35} {'-'*12} {'-'*12}")

    metrics = [
        ("Battery p_nom_opt (MW)",     "p_nom_opt_mw"),
        ("Battery e_nom_opt (MWh)",    "e_nom_opt_mwh"),
        ("Solar p_nom_opt (MW)",       "solar_p_nom_opt_mw"),
        ("Gas p_nom_opt (MW)",         "gas_p_nom_opt_mw"),
        ("SOC period-1 last (MWh)",    "soc_period1_last_mwh"),
        ("SOC period-2 first (MWh)",   "soc_period2_first_mwh"),
        ("Objective £",                "objective"),
    ]
    for name, key in metrics:
        va, vb = a[key], b[key]
        print(f"  {name:<35} {va:>12.2f} {vb:>12.2f}")

    print("\n  Warnings Run A:", len(a["warnings"]))
    print("  Warnings Run B:", len(b["warnings"]))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    print("\nBuilding Run A — global cycling only (C=True, CP=False, IP=False)")
    n_a = build_network(
        cyclic=True,
        cyclic_per_period=False,
        soc_initial_per_period=False,
        soc_initial_mwh=0.0,
    )
    results_a = run(n_a, "A", captured_warnings["A"])
    print_results(results_a)

    print("\nBuilding Run B — per-period cycling (C=False, CP=True, IP=True, soc_initial=50 MWh)")
    n_b = build_network(
        cyclic=False,
        cyclic_per_period=True,
        soc_initial_per_period=True,
        soc_initial_mwh=50.0,   # 50 MWh absolute — NOT a fraction of p_nom
    )
    results_b = run(n_b, "B", captured_warnings["B"])
    print_results(results_b)

    compare(results_a, results_b)

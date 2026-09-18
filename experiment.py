"""
PyPSA StorageUnit CP vs C cycling experiment
=============================================
PyPSA version:  1.3.0
xarray version: 2025.1.2  (required — later versions have a MultiIndex incompatibility)
linopy version: 0.9.1
Commit pinned:  cfaab2fd723d14fd6ad3594bdca8c0c1c9d4a14d
File:           pypsa/optimization/constraints.py

Two fully specified runs — identical in every parameter except the cycling flags:

  Run A — global cycling only   (C=True,  CP=False, IP=False)
  Run B — per-period cycling    (C=False, CP=True,  IP=True,
                                  state_of_charge_initial=50.0 MWh)

Network: two investment periods (2025, 2030), 48 hourly snapshots per period
(two representative days), extendable solar + gas + battery.

Period 1 (2025): high solar CF (1.2 daytime) — charging surplus.
Period 2 (2030): low solar CF (0.3 daytime) — deficit, needs storage or gas.

What the experiment records
---------------------------
For each run:
  - All logger.WARNING messages emitted during optimisation
  - SOC (MWh) at the last snapshot of period 1 and first snapshot of period 2
  - Full SOC trajectory across all 96 snapshots
  - Optimised battery p_nom_opt (MW) and e_nom_opt (MWh)
  - Optimised solar and gas capacities (MW)
  - Total system cost (objective)

Key mechanism under study
--------------------------
In define_storage_unit_constraints (constraints.py), the mask:

    include_previous_soc_pp = active & (within_period | cyclic_state_of_charge_per_period)

determines which SOC predecessor enters the energy-balance constraint at each snapshot.

When CP=True, this mask is True at period-boundary snapshots. The predecessor
injected is from roll_within_periods(soc) — the last snapshot of the SAME period
(within-period wrap), NOT the last snapshot of the previous period.

The state_of_charge_initial injection:
    rhs = rhs.where(include_previous_soc, rhs - soc_init)
only fires when include_previous_soc is False. CP=True keeps it True at period
boundaries, so soc_init (50.0 MWh — an absolute energy value, not a fraction
of p_nom_opt) is structurally absent from those constraint rows.

NOTE on state_of_charge_initial units
--------------------------------------
state_of_charge_initial is in MWh (absolute energy). It is NOT a fraction of
p_nom_opt (which is power capacity in MW). These are different physical quantities.
Setting soc_initial=50 means 50 MWh, regardless of how large the battery is built.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
import pypsa

# Suppress irrelevant deprecation noise from xarray/pandas during solve
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(message)s")

captured_warnings: dict[str, list[str]] = {"A": [], "B": []}


class _WarningCapture(logging.Handler):
    """Capture PyPSA logger.warning calls during optimisation."""

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
    Build a two-investment-period network with 48 hourly snapshots per period.

    Investment periods : 2025, 2030 (each representing 5 calendar years)
    Snapshots          : 48 hourly timestamps (two representative days)
    Snapshot weighting : 912.5 h each  (5 yr × 8760 h/yr / 48 snapshots)

    Solar profile — daytime hours 08:00–17:00:
      Period 2025 : CF = 1.2  (strong surplus available for charging)
      Period 2030 : CF = 0.3  (weak output, storage or gas needed)

    Load : flat 100 MW across all snapshots and both periods.

    IMPORTANT: state_of_charge_initial is in MWh (absolute energy), NOT a
    fraction of p_nom_opt (power capacity in MW).
    """
    per_period_timestamps = pd.date_range("2020-01-01", periods=48, freq="h")
    hours = per_period_timestamps.hour % 24
    daytime = (hours >= 8) & (hours <= 17)

    n = pypsa.Network()
    n.set_snapshots(per_period_timestamps)
    n.set_investment_periods([2025, 2030])

    # 5 yr × 8760 h/yr / 48 snapshots = 912.5 h per snapshot
    n.snapshot_weightings.loc[:, :] = 912.5
    n.investment_period_weightings["years"] = 5.0
    n.investment_period_weightings["objective"] = 1.0

    sns = n.snapshots  # MultiIndex (period, timestep)

    # ------------------------------------------------------------------
    # Bus
    # ------------------------------------------------------------------
    n.add("Bus", "grid", carrier="AC")

    # ------------------------------------------------------------------
    # Load — flat 100 MW
    # ------------------------------------------------------------------
    n.add("Load", "demand", bus="grid", p_set=pd.Series(100.0, index=sns))

    # ------------------------------------------------------------------
    # Solar — extendable, daytime only
    # Period 2025: CF = 1.2 daytime  (surplus scenario)
    # Period 2030: CF = 0.3 daytime  (deficit scenario)
    # ------------------------------------------------------------------
    cf = pd.Series(index=sns, dtype=float)
    cf.loc[2025] = np.where(daytime, 1.2, 0.0)
    cf.loc[2030] = np.where(daytime, 0.3, 0.0)

    n.add(
        "Generator",
        "solar",
        bus="grid",
        carrier="solar",
        p_nom_extendable=True,
        p_nom_min=0.0,
        p_nom_max=500.0,        # MW
        capital_cost=60_000.0,  # £/MW
        marginal_cost=0.0,
        p_max_pu=cf,
    )

    # ------------------------------------------------------------------
    # Gas — extendable, expensive peaker, zero capital cost
    # ------------------------------------------------------------------
    n.add(
        "Generator",
        "gas",
        bus="grid",
        carrier="gas",
        p_nom_extendable=True,
        p_nom_min=0.0,
        p_nom_max=500.0,        # MW
        capital_cost=0.0,
        marginal_cost=200.0,    # £/MWh
    )

    # ------------------------------------------------------------------
    # Battery — extendable 6-hour storage
    # state_of_charge_initial: absolute MWh, NOT a fraction of p_nom_opt
    # ------------------------------------------------------------------
    n.add(
        "StorageUnit",
        "battery",
        bus="grid",
        carrier="battery",
        p_nom_extendable=True,
        p_nom_min=0.0,
        p_nom_max=300.0,        # MW power capacity
        max_hours=6.0,          # e_nom = 6 × p_nom MWh
        capital_cost=25.0,      # £/MW
        marginal_cost=0.0,
        efficiency_store=0.95,
        efficiency_dispatch=0.95,
        standing_loss=0.0,
        # Cycling flags — the only difference between runs
        cyclic_state_of_charge=cyclic,
        cyclic_state_of_charge_per_period=cyclic_per_period,
        state_of_charge_initial_per_period=soc_initial_per_period,
        state_of_charge_initial=soc_initial_mwh,  # MWh absolute
    )

    return n


# ---------------------------------------------------------------------------
# Run optimisation
# ---------------------------------------------------------------------------

def run(n: pypsa.Network, label: str, warning_store: list[str]) -> dict:
    """Optimise, capture warnings, return results dict."""
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

    su = n.storage_units
    soc = n.storage_units_t.state_of_charge["battery"]
    sns = n.snapshots

    period1_sns = sns[sns.get_level_values(0) == 2025]
    period2_sns = sns[sns.get_level_values(0) == 2030]
    period1_last = period1_sns[-1]
    period2_first = period2_sns[0]

    return {
        "label": label,
        "warnings": list(warning_store),
        "p_nom_opt_mw": float(su.loc["battery", "p_nom_opt"]),
        "e_nom_opt_mwh": float(su.loc["battery", "p_nom_opt"] * su.loc["battery", "max_hours"]),
        "solar_p_nom_opt_mw": float(n.generators.loc["solar", "p_nom_opt"]),
        "gas_p_nom_opt_mw": float(n.generators.loc["gas", "p_nom_opt"]),
        "objective": float(n.objective),
        "soc_period1_last_mwh": float(soc.loc[period1_last]),
        "soc_period2_first_mwh": float(soc.loc[period2_first]),
        "period1_last_snapshot": str(period1_last),
        "period2_first_snapshot": str(period2_first),
        "soc_trajectory": {
            str(k): round(float(v), 3) for k, v in soc.items()
        },
    }


# ---------------------------------------------------------------------------
# Print results
# ---------------------------------------------------------------------------

def print_results(r: dict) -> None:
    sep = "=" * 64
    print(f"\n{sep}")
    print(f"  RUN {r['label']}")
    print(sep)

    print("\n--- Warnings emitted during optimisation ---")
    pypsa_warnings = [
        w for w in r["warnings"]
        if "StorageUnit" in w or "cyclic" in w.lower() or "initial" in w.lower()
    ]
    if pypsa_warnings:
        for w in pypsa_warnings:
            print(f"  WARNING: {w}")
    else:
        print("  (none relevant to cycling flags)")

    print("\n--- SOC at period boundary ---")
    print(
        f"  Last snapshot of period 1  {r['period1_last_snapshot']}: "
        f"{r['soc_period1_last_mwh']:.3f} MWh"
    )
    print(
        f"  First snapshot of period 2 {r['period2_first_snapshot']}: "
        f"{r['soc_period2_first_mwh']:.3f} MWh"
    )
    print(
        "  NOTE: SOC at period-2 first snapshot is NOT necessarily equal to "
        "period-1 last snapshot.\n"
        "  With CP=True, period-2's first snapshot links back to period-2's "
        "own last snapshot\n"
        "  (within-period wrap via roll_within_periods), not to period 1."
    )

    print("\n--- Optimised capacities ---")
    print(f"  Battery  p_nom_opt : {r['p_nom_opt_mw']:.2f} MW")
    print(f"  Battery  e_nom_opt : {r['e_nom_opt_mwh']:.2f} MWh")
    print(
        f"  NOTE: state_of_charge_initial=50 MWh is an absolute energy value, "
        f"not a fraction of {r['p_nom_opt_mw']:.1f} MW p_nom_opt."
    )
    print(f"  Solar    p_nom_opt : {r['solar_p_nom_opt_mw']:.2f} MW")
    print(f"  Gas      p_nom_opt : {r['gas_p_nom_opt_mw']:.2f} MW")

    print("\n--- Total system cost (objective) ---")
    print(f"  {r['objective']:,.2f}")

    print("\n--- SOC trajectory — period 1 (first and last 3 snapshots) ---")
    items = [(k, v) for k, v in r["soc_trajectory"].items() if "2025" in k]
    for ts, val in items[:3]:
        print(f"  {ts}  {val:.3f} MWh")
    print("  ...")
    for ts, val in items[-3:]:
        print(f"  {ts}  {val:.3f} MWh")

    print("\n--- SOC trajectory — period 2 (first and last 3 snapshots) ---")
    items2 = [(k, v) for k, v in r["soc_trajectory"].items() if "2030" in k]
    for ts, val in items2[:3]:
        print(f"  {ts}  {val:.3f} MWh")
    print("  ...")
    for ts, val in items2[-3:]:
        print(f"  {ts}  {val:.3f} MWh")


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

def compare(a: dict, b: dict) -> None:
    print("\n" + "=" * 64)
    print("  COMPARISON  Run A (C only)  vs  Run B (CP + IP)")
    print("=" * 64)
    print(f"  {'Metric':<40} {'Run A':>10} {'Run B':>10}")
    print(f"  {'-'*40} {'-'*10} {'-'*10}")
    rows = [
        ("Battery p_nom_opt (MW)",   "p_nom_opt_mw"),
        ("Battery e_nom_opt (MWh)",  "e_nom_opt_mwh"),
        ("Solar p_nom_opt (MW)",     "solar_p_nom_opt_mw"),
        ("Gas p_nom_opt (MW)",       "gas_p_nom_opt_mw"),
        ("SOC period-1 last (MWh)",  "soc_period1_last_mwh"),
        ("SOC period-2 first (MWh)", "soc_period2_first_mwh"),
        ("Objective",                "objective"),
    ]
    for name, key in rows:
        va, vb = a[key], b[key]
        print(f"  {name:<40} {va:>10.2f} {vb:>10.2f}")
    print(f"\n  Cycling-related warnings Run A : {len([w for w in a['warnings'] if 'StorageUnit' in w or 'cyclic' in w.lower()])}")
    print(f"  Cycling-related warnings Run B : {len([w for w in b['warnings'] if 'StorageUnit' in w or 'cyclic' in w.lower()])}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    print("\nRun A — global cycling only (C=True, CP=False, IP=False)")
    n_a = build_network(
        cyclic=True,
        cyclic_per_period=False,
        soc_initial_per_period=False,
        soc_initial_mwh=0.0,
    )
    results_a = run(n_a, "A", captured_warnings["A"])
    print_results(results_a)

    print(
        "\nRun B — per-period cycling "
        "(C=False, CP=True, IP=True, soc_initial=50 MWh absolute)"
    )
    n_b = build_network(
        cyclic=False,
        cyclic_per_period=True,
        soc_initial_per_period=True,
        soc_initial_mwh=50.0,   # 50 MWh absolute — NOT a fraction of p_nom_opt
    )
    results_b = run(n_b, "B", captured_warnings["B"])
    print_results(results_b)

    compare(results_a, results_b)

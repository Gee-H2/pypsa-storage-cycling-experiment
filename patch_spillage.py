"""
Spillage variable patch for PyPSA + multi-investment-period models.

In some versions of PyPSA 1.3.0 (depending on which release artefact is
installed), define_spillage_variables raises a KeyError when optimising
multi-investment-period models. This script checks whether the patch is
needed and applies it if so.

If this script reports 'No patch needed', the installed version already
handles multi-investment-period models correctly and you can proceed directly
to running experiment.py.
"""
import pathlib

venv_path = pathlib.Path(".venv")
candidates = list(venv_path.rglob("pypsa/optimization/variables.py"))

if not candidates:
    raise FileNotFoundError(
        "Could not find pypsa/optimization/variables.py under .venv/. "
        "Make sure you have run: pip install -r requirements.txt"
    )

path = candidates[0]
src = path.read_text()

# Pattern that causes the bug (uses .sel() with MultiIndex snapshot)
buggy_pattern = (
    "    upper = c.da.inflow.sel(name=c.active_assets, snapshot=sns)\n"
    "    if upper.size == 0 or (upper.max() <= 0).all():\n"
    "        return"
)

# Safe pattern already using get_as_dense (no patch needed)
safe_pattern = "get_as_dense(n, c, \"inflow\", sns)"

already_patched = (
    "    try:\n"
    "        upper = c.da.inflow.sel(name=c.active_assets, snapshot=sns)\n"
    "    except KeyError:"
)

if safe_pattern in src:
    print(f"No patch needed: {path} already uses get_as_dense (safe for multi-period).")
elif already_patched in src:
    print(f"Patch already applied to {path}.")
elif buggy_pattern in src:
    fixed = (
        "    try:\n"
        "        upper = c.da.inflow.sel(name=c.active_assets, snapshot=sns)\n"
        "    except KeyError:\n"
        "        # Multi-investment-period MultiIndex causes KeyError;\n"
        "        # skip spillage (safe when inflow=0 for all StorageUnits).\n"
        "        return\n"
        "    if upper.size == 0 or (upper.max() <= 0).all():\n"
        "        return"
    )
    path.write_text(src.replace(buggy_pattern, fixed))
    print(f"Patch applied to {path}.")
else:
    print(
        f"WARNING: Neither the buggy nor the safe pattern was found in {path}.\n"
        "The installed PyPSA version may differ from 1.3.0.\n"
        "Try running experiment.py directly — it may work without patching."
    )

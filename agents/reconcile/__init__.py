"""Migration reconciliation as an in-process built-in toolset (``builtin:reconcile``).

A model compiles mapping rules into a :class:`~agents.reconcile.spec.ReconcileSpec`
once; code compares every row of a migrated CSV with the source table read
through the agent's ARC-1 server. See ``tools.py``.
"""

BUILTIN_RECONCILE_URL = "builtin:reconcile"
__all__ = ["BUILTIN_RECONCILE_URL"]

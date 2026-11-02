"""The data catalogue graph (design addendum 2026-11-01 (j), part 1).

One catalogue of what agents can reach in SAP, as nodes (a source, its
entities and fields, its functions) and edges (navigation, the same object in
two services), with the admin's hints (label, purpose, not for, synonyms,
example questions) and optional embedding vectors. It is built from the
OData catalogue (`agents/odata`) for the sources attached to an enabled
agent, and searched per agent: the ``find_data_source`` tool sees the calling
agent's own sources only, filtered before anything is scored.

Kept light on purpose: ``agents.db`` imports ``models`` at its end so the
tables are always on ``Base.metadata``; the other modules are imported where
they are used.
"""

"""The one error a refused database configuration raises.

Its own module so that :mod:`agents.db` and :mod:`agents.pg_schema` can both
raise it without importing each other (``agents.db`` imports
``agents.pg_schema`` while it is itself being imported). ``agents.db``
re-exports it: ``from agents.db import DatabaseConfigError`` keeps working.
"""

from __future__ import annotations


class DatabaseConfigError(RuntimeError):
    """The environment does not say which database to use, or names one that
    cannot be used. Raised while :mod:`agents.db` is imported or by its
    ``init_db``, so the app does not start; the message never repeats a
    credential, a URL or a value of a table."""

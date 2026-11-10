"""``builtin:hana``: on-premise SAP HANA monitoring views as an in-process
toolset, read through ``hdbcli`` over the Cloud Connector.

Not to be confused with ``agents/hana_hdi.py``, which deploys this app's own
registry schema into an HDI container: nothing here touches the registry
database. The entry of an agent is read by ``agents/hana/config.py``.
"""

BUILTIN_HANA_URL = "builtin:hana"

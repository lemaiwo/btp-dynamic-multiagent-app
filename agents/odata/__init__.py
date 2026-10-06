"""OData services as an in-process built-in toolset (``builtin:odata``).

Agents reach OData V2/V4 services of an SAP back end through BTP
destinations. What an agent may call is not discovered at run time: it is a
catalogue an admin curates (``models.py``), so a service, an entity set or a
field that is not in the catalogue does not exist for the model.
"""

BUILTIN_ODATA_URL = "builtin:odata"

__all__ = ["BUILTIN_ODATA_URL"]

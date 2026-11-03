"""Analytical queries over SAP's InA protocol as an in-process built-in toolset.

Agents read BW / embedded-analytics queries of an SAP back end through BTP
destinations. What an agent may run is not discovered at run time: an admin
registers each query (``models.py``) and a ``Describe`` reads its dimensions,
measures and variables from SAP once, so a query that is not registered does
not exist for the model.
"""

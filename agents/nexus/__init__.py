"""Logging of model calls to an agent register reached through a destination.

When a BTP destination for the register exists, every model call of the app
is reported to it as a request/response pair (who called, which agent, which
model, token counts and status; prompt and answer only when the operator
switches that on), and the app's agents are registered there so the register
can assign each one a use case. Without the destination the feature is off
and nothing is sent. Reporting never blocks or fails a model call: records go
through a bounded queue and a failed send is dropped with a log line.
"""

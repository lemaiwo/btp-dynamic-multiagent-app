# Configuring SAP Alert Notification Service for scheduled agent runs

How to get an email when a scheduled agent run finishes.

## Status: what works today

**Nothing in this app emits ANS events yet.** Increment 1 (merged on
`feat/scheduled-agent-runs`) added the runs themselves — the endpoint, the
`JobRun` record and the admin Job Runs tab. Event emission is increment 2 and
the BTP scheduler wiring is increment 3. See
`docs/superpowers/specs/2026-08-19-scheduled-agent-runs-design.md`.

You can do the ANS-side setup now; it is independent of our code.

## Two paths, and why you probably want both

| | Path A — scheduler-emitted | Path B — app-emitted |
|---|---|---|
| **Code needed** | none | `agents/ans.py` (increment 2) |
| **Available after** | increment 3 (scheduler wiring) | increment 2 |
| **Event type** | `JobSchedulerJobExecution` | `AgentRunCompleted` (ours) |
| **Tells you** | the run succeeded or failed | success/degraded/failed, plus a one-line summary and a link to the full report |
| **Knows about `degraded`?** | no — only success/error | yes |

Path A is free: the Job Scheduling Service posts the event itself, no app code
involved. Because our runs use the **asynchronous** job contract, the status it
reports is the one our app sends back via Update Run Log — so an ANS "error"
genuinely means the run failed, not merely that the HTTP trigger was accepted.

What Path A cannot do is carry the report. Its event has the job name, schedule
id, status, timestamp and error details — no summary, no link. A `degraded` run
(the agent worked but did not check every configured source) reports as success,
because that is what our callback reports at the job level.

**Recommendation:** turn on Path A when increment 3 lands, as a floor — it tells
you the nightly job ran at all, including the case where the app is down and
never reports anything. Add Path B for the actual report email.

## Prerequisites

Verified present in `infrabel-app-acc-cf / AI` on 2026-08-19:

```
cf marketplace -e alert-notification
   plan       free or paid
   standard   paid
   free       free
```

Both plans allow posting custom events, so the `free` plan is enough for Path B.

## Common setup (both paths)

ANS is configured as **Actions** (where a notification goes) → **Conditions**
(which events match) → **Subscriptions** (which conditions trigger which
actions). All three live in the ANS instance dashboard, not in this app.

1. **Create the service instance**

   ```bash
   cf create-service alert-notification free pydantic-agent-ans
   ```

   Or in the cockpit: **Services → Instances and Subscriptions → Create**.

2. **Create an Action** — in the ANS dashboard, **Actions → Create**, type
   **Email**. Add each recipient address. Recipients must confirm the
   subscription by email before anything is delivered; an unconfirmed address
   silently receives nothing, which is the most common reason a correctly
   configured alert appears not to work.

3. **Create a Condition** — see the path-specific filters below.

4. **Create a Subscription** linking the condition to the action, and
   **activate** it. A subscription left inactive is the second most common
   cause of silence.

## Path A — scheduler-emitted events

Available once increment 3 creates the BTP job. Nothing to build.

**Condition:** match `eventType` equals `JobSchedulerJobExecution`. Narrow it
further on the job name if you run several scheduled agents and want them routed
to different people.

**Enable per job**, either by toggling **Success** / **Error** under *SAP Alert
Notification Service Events* in the Job Scheduling dashboard, or via the REST
API when the job is created:

```json
POST /scheduler/jobs
{
  "name": "daily-sap-check",
  "action": "https://<app-host>/api/agents/daily-check/run",
  "active": true,
  "httpMethod": "POST",
  "ansConfig": { "onSuccess": true, "onError": true },
  "schedules": [{ "active": true, "cron": "* * * * 3 17 23" }]
}
```

`ansConfig` takes `onSuccess` and `onError`, both boolean. Cloud Foundry only —
this integration does not exist on Kyma.

## Path B — app-emitted custom events (increment 2)

This is the one that carries the report. When increment 2 lands, the app will
post one custom event per completed run to the ANS **producer** API, using the
OAuth credentials from the `alert-notification` service binding.

**Planned event contract** — treat this as the design intent, not as shipped
behaviour:

| Field | Value |
|---|---|
| `eventType` | `AgentRunCompleted` |
| `resource.resourceName` | the agent's name |
| `resource.resourceType` | `agent-run` |
| `severity` | `INFO` success · `WARNING` degraded · `ERROR` failed |
| `category` | `NOTIFICATION` for success/degraded · `ALERT` for failed |
| `subject` | agent name + status |
| `body` | the run's one-line summary + a link to `{PUBLIC_BASE_URL}/runs/{id}` |

**Condition:** match `eventType` equals `AgentRunCompleted`. To route by
outcome, add a second condition on `severity` — for example a `WARNING`/`ERROR`
condition wired to an on-call action, and an `INFO` condition wired to the
regular distribution list. That severity routing is ANS configuration, not
something to build into the app.

The full report is deliberately **not** in the event. ANS events are small, so
the email carries a summary and a link, and the report itself is served by the
app at `/runs/{id}` behind the approuter — recipients need an XSUAA login and
the `user` scope to open it.

### Details to confirm at implementation time

I could not verify these from public documentation and did not want to write
guesses into a setup guide:

- **Exact length limits on `subject` and `body`.** They are small. The
  implementation will truncate defensively regardless; confirm the real numbers
  against the Cloud Foundry Producer API spec on
  [SAP Business Accelerator Hub](https://api.sap.com/api/cf_producer_api/overview)
  before relying on a particular body size.
- **The complete `severity` and `category` enums.** `INFO`, `WARNING`,
  `NOTIFICATION` and `ALERT` are confirmed in SAP's own examples; the mapping
  above uses `ERROR` for failed runs, which should be checked against the spec.
- **The producer endpoint URL.** Read it from the service binding's credentials
  rather than hardcoding a landscape-specific host — that is what the
  implementation will do.

## Testing the setup before any of this exists

You do not need our code to prove the ANS half works. Create the instance and a
service key, get a token from the binding's OAuth credentials, and post a
hand-written event with `eventType: AgentRunCompleted`. If the email arrives,
your action, condition and subscription are correct, and increment 2 only has to
produce the same shape.

Worth doing in that order — it separates "ANS is misconfigured" from "the app
isn't sending", which are otherwise hard to tell apart from an empty inbox.

## Deployment prerequisite

Independently of ANS: `PUBLIC_BASE_URL` (or `A2A_PUBLIC_URL`) must be set in
`mta.yaml` to the approuter URL. Both currently ship empty, and scheduled runs
fail without one — the report link in the email is built from it, and the OAuth
client resolution needs it too.

## Sources

- [SAP Alert Notification service for SAP BTP](https://help.sap.com/docs/ALERT_NOTIFICATION)
- [Cloud Foundry Producer API](https://api.sap.com/api/cf_producer_api/overview)
- [Getting Started with SAP Alert Notification Service](https://developers.sap.com/tutorials/alert-notification-service.html)
- Job Scheduling ↔ ANS integration (`ansConfig`, `JobSchedulerJobExecution`):
  [SAP BTP Job Scheduling Service docs](https://help.sap.com/docs/job-scheduling)

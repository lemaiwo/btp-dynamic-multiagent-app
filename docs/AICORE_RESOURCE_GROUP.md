# Deploying to a dedicated AI Core resource group

SAP AI Core meters consumption per **resource group**. A landscape that has to
account for its own spend — Elia, for instance — gets a dedicated group instead
of sharing the tenant-wide `default`. This describes how to point a deployment
at one, and what the deploy does about creating it.

## What is already wired

`AICORE_RESOURCE_GROUP` is the only switch. No application code reads it: the
SAP SDK does. `ai_core_sdk/credentials.py` resolves every credential as
`AICORE_<NAME>`, and resolves the resource group *independently of the others*,
with the environment variable taking precedence over the bound `aicore` service
in `VCAP_SERVICES`. Both model paths in `agents/shared.py` build their client
through `get_proxy_client("gen-ai-hub")` — the OpenAI-compatible one directly,
the Bedrock one through `Session()` — so the `AI-Resource-Group` header follows
for every LLM call either way.

## Setting the group for a landscape

`mta.yaml` exposes it as the `aicore-resource-group` parameter, defaulting to
`default`. Override it in an extension descriptor rather than by editing the
descriptor itself, so each landscape keeps its own value:

```yaml
# elia.mtaext
_schema-version: "3.2"
ID: pydantic-agent-elia
extends: pydantic-agent

parameters:
  aicore-resource-group: elia-agent
```

```bash
cf deploy mta_archives/pydantic-agent_2.7.0.mtar -e elia.mtaext
```

## What the deploy hook does

`mta.yaml` runs `scripts/ensure_aicore_setup.py` as a `before-start` task hook.
It **creates the resource group if it is missing** and waits until it reaches
`PROVISIONED`. It is idempotent, and it is a no-op when the group is `default`,
which always exists.

It creates the group and nothing else. Model deployments are deliberately out
of scope: they bill real money and take minutes to reach `RUNNING`, neither of
which belongs in an implicit deploy step.

The script also runs on its own, against a local `.env`:

```bash
python scripts/ensure_aicore_setup.py
```

| Variable | Default | Effect |
| --- | --- | --- |
| `AICORE_RESOURCE_GROUP` | `default` | The group to ensure. `default` is a no-op. |
| `AICORE_ENSURE_STRICT` | `true` | `false` downgrades a failure to a warning so the deploy proceeds. |
| `AICORE_ENSURE_TIMEOUT` | `300` | Seconds to wait for `PROVISIONED`. |

## Deploying the models — the part you still have to do

**Deployments are scoped to a resource group.** A newly created group is empty,
and `available_models()` in `agents/shared.py` discovers models by querying the
deployments in the *current* group. Point a landscape at a fresh group and the
app starts with no models at all: discovery returns nothing, the static
`DEFAULT_AVAILABLE_MODELS` fallback kicks in, and every call fails against a
deployment that is not there. Any agent pinned to a model name absent from the
new group breaks the same way.

So switching group is a migration, not a config flip. After the first deploy:

```bash
AICORE_RESOURCE_GROUP=elia-agent python scripts/deploy_claude.py
AICORE_RESOURCE_GROUP=elia-agent python scripts/list_deployments.py
```

Then confirm each configured agent's model actually appears in the second
command's output before treating the landscape as live.

## When it fails

**403 or 401 on create.** Creating a resource group needs an `aicore` service
key with admin scope. If the landscape's key is deliberately restricted, create
the group once in the AI Core cockpit and set `AICORE_ENSURE_STRICT=false` so
the hook stops failing the deploy.

**400 on create.** `ResourceGroupsClient.create` documents a 3–10 character
limit on the id. The script warns rather than refuses when the name falls
outside that, since the server is the authority and the limit has moved before.

**Quota.** Group creation and deployment quota are separate. A group can exist
and still refuse a model deployment.

**`hooks` rejected by the deploy service.** Hooks need `_schema-version: "3.2"`,
which `mta.yaml` now declares. The phase identifiers also vary across deploy
service versions; both the standard and blue-green `before-start` phases are
listed so either deploy strategy triggers the task.

## Orchestration

The app does not use AI Core's orchestration service. It calls
`foundation-models` deployments through the GenAI Hub proxy, so nothing needs
enabling there for the app to run in a new group. If a landscape mandates
orchestration for policy reasons, that is a separate deployment in the group
and a change to how `agents/shared.py` builds its models.

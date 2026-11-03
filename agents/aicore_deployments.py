"""SAP AI Core deployments of embedding models: which exist, and making one.

The calls behind the embeddings panel of the UI5 admin's Settings page
(design addendum l, point 3): the embedding models the ``foundation-models``
scenario offers (``list_models``), the embedding deployments of the resource
group (``list_deployments``) and a new deployment of one model (``deploy``:
a configuration ``<model>-embeddings``, then a deployment). The REST calls
are the ones ``scripts/deploy_claude.py`` makes, async and with httpx, and
nothing waits for a deployment: it reaches RUNNING in minutes, and the
status endpoint shows it PENDING meanwhile.

**Credentials** resolve the way ``gen_ai_hub`` resolves them for the model
calls (``AICoreV2Client.from_env``): the ``AICORE_*`` variables when any of
the four is set, else the ``aicore`` binding in ``VCAP_SERVICES``, never a
mix of the two. So a deployment is made in the tenant the embeddings are
sent to; a deployment somewhere else would be billed and never used. Only a
client-secret binding is read: an x509 binding (``certurl``, no
``clientsecret``) is ``aicore_unavailable``. The client-credentials token is
cached until a minute before it expires; an answer without a usable
``expires_in`` is used for that call only.

**Failures** are an :class:`AICoreError` with one of four fixed codes
(``ERROR_CODES``). The log line names the step and the status or the
exception class: never a credential, a URL or a response body (the body of
an error status is not even read). httpx's own request line names the URL,
which holds no credential here; ``app.py`` raises that logger to WARNING.

**What is written, and billed.** Only ``deploy`` writes, and it assumes
ONE app instance (as the startup sweep in ``app.py`` does): two deploys in
one process take turns, two instances could both create. An existing
RUNNING, PENDING or UNKNOWN deployment of the model is answered instead
(``created: False``); a status that cannot be read counts as UNKNOWN, and a
fresh deployment whose details do not name its model yet counts under the
model its configuration is named after (``<model>-embeddings``, the name
this module writes). The deployment list is read page by page and must add
up to the ``count`` AI Core states, or nothing is created: a RUNNING
deployment on a page that was not read would be deployed a second time. A
create is sent once: the only repeat is after a 401, which AI Core answers
before it processes anything. When a deployment create fails or its answer
is lost, the list is read once more: a deployment of the model that has
appeared is the one this call made and is answered (``created: True``);
otherwise ``deploy_failed``. The configuration of such a failed create is
not made again by the next call: a configuration of the same name,
executable and bindings is reused.

Standard library and httpx only: no ``agents.*`` import, no database. The
module functions share one :class:`AICoreDeployments` per process
(``default_client``, a FastAPI dependency as it is), which holds the token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

SCENARIO_ID = "foundation-models"
DEFAULT_RESOURCE_GROUP = "default"
REQUEST_TIMEOUT_SECONDS = 30.0
TOKEN_MARGIN_SECONDS = 60.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# A list is read in pages of PAGE_SIZE, at most MAX_PAGES of them.
PAGE_SIZE = 100
MAX_PAGES = 100
# Live states, in the order deploy() prefers an existing deployment.
LIVE_STATUSES = ("RUNNING", "PENDING", "UNKNOWN")
CONFIGURATION_SUFFIX = "-embeddings"

ERROR_MESSAGES = {
    "aicore_unavailable": "SAP AI Core could not be reached or did not answer as expected.",
    "token_failed": "SAP AI Core did not issue a token for the app's credentials.",
    "model_unknown": "The foundation-models scenario offers no embedding model of that name.",
    "deploy_failed": "SAP AI Core did not confirm the deployment; it may still appear in the list.",
}
ERROR_CODES = frozenset(ERROR_MESSAGES)

_ENV_KEYS = (("client_id", "AICORE_CLIENT_ID"), ("client_secret", "AICORE_CLIENT_SECRET"),
             ("auth_url", "AICORE_AUTH_URL"), ("base_url", "AICORE_BASE_URL"))
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
# What AI Core sends back reaches the admin UI and log lines (the deploy
# route names the model): a value outside these forms is dropped, not shown.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_STATUS = re.compile(r"[A-Z_]{1,32}")


class AICoreError(Exception):
    """A refused or failed AI Core call: ``code`` is one of ``ERROR_CODES``,
    ``message`` its fixed text. What went wrong is in the log only."""

    def __init__(self, code: str) -> None:
        if code not in ERROR_MESSAGES:
            raise ValueError("unknown AI Core error code")
        super().__init__(code)
        self.code = code
        self.message = ERROR_MESSAGES[code]


@dataclass(frozen=True, repr=False)
class AICoreCredentials:
    """The service key of one AI Core tenant. ``source`` says where it was
    read (``env`` or ``binding``)."""

    client_id: str
    client_secret: str
    auth_url: str
    base_url: str
    source: str = "env"

    def __repr__(self) -> str:
        # Id, secret and URLs stay out of every repr, traceback and log line.
        return f"AICoreCredentials(source={self.source!r})"

    __str__ = __repr__


def _unusable(detail: str) -> AICoreError:
    logger.warning("AI Core: no usable credentials (%s)", detail)
    return AICoreError("aicore_unavailable")


def _checked_url(raw: str, suffix: str, name: str) -> str:
    """``raw`` without a trailing slash and with ``suffix`` once, as the SDK
    writes ``/oauth/token`` and ``/v2``. The client secret goes to the token
    URL, so it is https (plain http only on the loopback, for local work) and
    carries no user info. ``name`` names the setting in the log, never its
    value."""
    url = raw.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that is no number
    except ValueError:
        raise _unusable(f"{name} is not a URL") from None
    host = parts.hostname or ""
    if "@" in parts.netloc:
        raise _unusable(f"{name} carries user info")
    if not host:
        raise _unusable(f"{name} has no host")
    if parts.scheme != "https" and not (parts.scheme == "http" and host in _LOOPBACK):
        raise _unusable(f"{name} is not an https URL")
    if parts.query or parts.fragment:
        raise _unusable(f"{name} has a query or a fragment")
    return url if url.endswith(suffix) else url + suffix


def _from_binding(vcap: str | None) -> AICoreCredentials:
    raw = (vcap or "").strip()
    if not raw:
        raise _unusable("no AICORE_* variables and no VCAP_SERVICES")
    try:
        services = json.loads(raw)
    except (ValueError, RecursionError):
        raise _unusable("VCAP_SERVICES is not JSON") from None
    if not isinstance(services, dict):
        raise _unusable("VCAP_SERVICES is not a JSON object")
    binding = next((service for group in services.values() if isinstance(group, list)
                    for service in group
                    if isinstance(service, dict) and service.get("label") == "aicore"), None)
    if binding is None:
        raise _unusable("no aicore binding")
    creds = binding.get("credentials")
    if not isinstance(creds, dict):
        raise _unusable("the aicore binding has no credentials object")
    urls = creds.get("serviceurls")
    values = {"clientid": creds.get("clientid"), "clientsecret": creds.get("clientsecret"),
              "url": creds.get("url"),
              "serviceurls.AI_API_URL": urls.get("AI_API_URL") if isinstance(urls, dict) else None}
    missing = [key for key, value in values.items()
               if not isinstance(value, str) or not value.strip()]
    if missing:
        raise _unusable("the aicore binding has no " + ", ".join(missing))
    return AICoreCredentials(
        client_id=values["clientid"].strip(), client_secret=values["clientsecret"].strip(),
        auth_url=_checked_url(values["url"], "/oauth/token", "the binding's url"),
        base_url=_checked_url(values["serviceurls.AI_API_URL"], "/v2", "the binding's AI_API_URL"),
        source="binding")


def resolve_credentials(environ: Mapping[str, str] | None = None) -> AICoreCredentials:
    """The credentials of the tenant the app's model calls go to (see the
    module docstring), or ``aicore_unavailable``."""
    env = os.environ if environ is None else environ
    if (env.get("AICORE_SERVICE_KEY") or "").strip():
        # The SDK reads this one before everything else, so the model calls
        # use it; answering from another source here would deploy into a
        # tenant the embeddings are not sent to.
        raise _unusable("AICORE_SERVICE_KEY is set, which this module does not read; "
                        "give the key as the AICORE_* variables instead")
    values = {field: (env.get(name) or "").strip() for field, name in _ENV_KEYS}
    if not any(values.values()):
        return _from_binding(env.get("VCAP_SERVICES"))
    missing = [name for field, name in _ENV_KEYS if not values[field]]
    if missing:
        raise _unusable("the AICORE_* variables are incomplete, missing " + ", ".join(missing))
    return AICoreCredentials(
        client_id=values["client_id"], client_secret=values["client_secret"],
        auth_url=_checked_url(values["auth_url"], "/oauth/token", "AICORE_AUTH_URL"),
        base_url=_checked_url(values["base_url"], "/v2", "AICORE_BASE_URL"),
        source="env")


def resource_group(environ: Mapping[str, str] | None = None) -> str:
    """``AICORE_RESOURCE_GROUP``, else the tenant's ``default`` group (as
    ``scripts/ensure_aicore_setup.py`` reads it)."""
    env = os.environ if environ is None else environ
    return (env.get("AICORE_RESOURCE_GROUP") or "").strip() or DEFAULT_RESOURCE_GROUP


def _is_name(value: Any) -> bool:
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def _is_embedding(value: Any) -> bool:
    # ``embed``, not ``embedding``: Bedrock and NVIDIA name theirs
    # ``amazon--titan-embed-text``, ``nvidia--...-nv-embedqa-...``.
    return _is_name(value) and "embed" in value.lower()


def _deployed_model(entry: dict) -> str | None:
    """The embedding model of a deployment, or ``None``.

    Where ``scripts/deploy_claude.py`` finds it; when the details name no
    model (a deployment just created has none yet), the model of a
    configuration this module named, ``<model>-embeddings``. A model the
    details do name wins over the configuration's name.
    """
    details = entry.get("details")
    details = details if isinstance(details, dict) else {}
    resources = details.get("resources")
    backend = resources.get("backend_details") if isinstance(resources, dict) else None
    model = backend.get("model") if isinstance(backend, dict) else None
    name = (model.get("name") if isinstance(model, dict) else None) or details.get("modelName")
    if _is_name(name):
        return name if _is_embedding(name) else None
    configuration = entry.get("configurationName")
    if isinstance(configuration, str) and configuration.endswith(CONFIGURATION_SUFFIX):
        named = configuration[:-len(CONFIGURATION_SUFFIX)]
        if _is_embedding(named):
            return named
    return None


def _live(deployments: list[dict[str, str]], model: str) -> dict[str, str] | None:
    """The deployment of ``model`` that is live, RUNNING before PENDING
    before UNKNOWN, or ``None``."""
    live = [d for d in deployments if d["model"] == model and d["status"] in LIVE_STATUSES]
    return min(live, key=lambda d: LIVE_STATUSES.index(d["status"])) if live else None


def _declares(executable: dict, parameter: str) -> bool:
    params = executable.get("parameters")
    return isinstance(params, list) and any(
        isinstance(p, dict) and p.get("name") == parameter for p in params)


def find_executable(executables: list[dict], model: str, listed: str | None) -> dict | None:
    """The executable to deploy ``model`` with. First the rule of
    ``scripts/deploy_claude.py`` (``find_executable_for_model``): the one
    whose ``modelName`` parameter lists the model in its ``enum``. Else the
    one the model list names for the model, when the scenario has it. (The
    script's Claude rule, ``aws-bedrock``, does not apply: only embedding
    models are deployed here.)"""
    for executable in executables:
        params = executable.get("parameters")
        for param in params if isinstance(params, list) else ():
            if not isinstance(param, dict) or param.get("name") != "modelName":
                continue
            constraints = param.get("constraints")
            allowed = constraints.get("enum") if isinstance(constraints, dict) else None
            if isinstance(allowed, list) and model in allowed and _is_name(executable.get("id")):
                return executable
    if listed is None:
        return None
    return next((ex for ex in executables if ex.get("id") == listed), None)


def _configuration(model: str, executable: dict) -> dict[str, Any]:
    bindings = [{"key": "modelName", "value": model}]
    # SAP's generative AI hub configurations bind modelVersion as well; an
    # executable that does not declare the parameter would refuse it.
    if _declares(executable, "modelVersion"):
        bindings.append({"key": "modelVersion", "value": "latest"})
    return {"name": f"{model}{CONFIGURATION_SUFFIX}", "executableId": executable["id"],
            "scenarioId": SCENARIO_ID, "parameterBindings": bindings,
            "inputArtifactBindings": []}


def _bindings(value: Any) -> tuple[tuple[str, str], ...] | None:
    """Parameter bindings in a form that compares, or ``None`` when they are
    not a list of string pairs (which then matches nothing)."""
    if not isinstance(value, list):
        return None
    pairs = []
    for binding in value:
        key = binding.get("key") if isinstance(binding, dict) else None
        val = binding.get("value") if isinstance(binding, dict) else None
        if not isinstance(key, str) or not isinstance(val, str):
            return None
        pairs.append((key, val))
    return tuple(sorted(pairs))


def _same_configuration(entry: dict, wanted: dict[str, Any]) -> bool:
    return (entry.get("name") == wanted["name"]
            and entry.get("executableId") == wanted["executableId"]
            and entry.get("scenarioId", SCENARIO_ID) == SCENARIO_ID
            and _bindings(entry.get("parameterBindings")) == _bindings(wanted["parameterBindings"])
            and not entry.get("inputArtifactBindings")
            and _is_name(entry.get("id")))


def _incomplete(step: str, detail: str) -> AICoreError:
    logger.warning("AI Core: %s failed (%s)", step, detail)
    return AICoreError("aicore_unavailable")


@dataclass(frozen=True)
class _Offered:
    model: str
    versions: tuple[str, ...]
    executable_id: str | None


class _Failed(Exception):
    """A request that failed; ``detail`` is the status or a fixed phrase."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _resources(data: dict, step: str, code: str = "aicore_unavailable") -> list[dict]:
    resources = data.get("resources")
    if not isinstance(resources, list):
        logger.warning("AI Core: %s failed (no resources list in the answer)", step)
        raise AICoreError(code)
    return [entry for entry in resources if isinstance(entry, dict)]


class AICoreDeployments:
    """The AI Core calls of one tenant, with its token cache.

    ``credentials`` default to :func:`resolve_credentials`, read at the first
    call (building the client never fails); ``group`` to
    :func:`resource_group`, read at every call. ``transport`` is for tests.
    """

    def __init__(self, credentials: AICoreCredentials | None = None, *,
                 group: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._credentials = credentials
        self._group = group
        self._transport = transport
        self._clock = clock
        self._token: tuple[str, float] | None = None
        self._token_lock = asyncio.Lock()
        self._deploy_lock = asyncio.Lock()

    @property
    def group(self) -> str:
        return self._group or resource_group()

    def _resolved(self) -> AICoreCredentials:
        if self._credentials is None:
            self._credentials = resolve_credentials()
        return self._credentials

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=REQUEST_TIMEOUT_SECONDS,
                                 follow_redirects=False)

    async def _send(self, http: httpx.AsyncClient, method: str, url: str, *, step: str,
                    code: str, retry_401: bool = False, **kwargs: Any) -> dict | None:
        """One request and its JSON object answer, or ``code``. ``None`` only
        for a 401 when ``retry_401``. httpx's timeouts are per read, so the
        whole request also has one deadline: an answer that drips in is
        held to it."""
        deadline = asyncio.timeout(REQUEST_TIMEOUT_SECONDS)
        try:
            async with deadline:
                async with http.stream(method, url, **kwargs) as response:
                    status = response.status_code
                    if status == 401 and retry_401:
                        return None
                    if not 200 <= status < 300:
                        raise _Failed(f"HTTP {status}")
                    body = bytearray()
                    async for piece in response.aiter_bytes():
                        body += piece
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise _Failed("answer too large")
        except _Failed as failed:
            detail = failed.detail
        except TimeoutError:
            detail = (f"deadline of {REQUEST_TIMEOUT_SECONDS:g} s" if deadline.expired()
                      else "TimeoutError")
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            detail = type(exc).__name__
        else:
            try:
                data = json.loads(bytes(body))
            except (ValueError, RecursionError):
                detail = "answer is not JSON"
            else:
                if isinstance(data, dict):
                    return data
                detail = "answer is not a JSON object"
        logger.warning("AI Core: %s failed (%s)", step, detail)
        raise AICoreError(code)

    async def _bearer(self, http: httpx.AsyncClient, creds: AICoreCredentials, *,
                      stale: str | None = None) -> str:
        """A token from the cache, else a new one. ``stale`` is a token AI
        Core just refused: it is not handed out again."""
        async with self._token_lock:
            now = self._clock()
            if self._token is not None and now < self._token[1] and self._token[0] != stale:
                return self._token[0]
            self._token = None
            data = await self._send(
                http, "POST", creds.auth_url, step="token request", code="token_failed",
                data={"grant_type": "client_credentials"},
                auth=(creds.client_id, creds.client_secret))
            token = data.get("access_token") if data is not None else None
            if not isinstance(token, str) or not token:
                logger.warning("AI Core: token request failed (no access token in the answer)")
                raise AICoreError("token_failed")
            lifetime = data.get("expires_in")
            if (isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool)
                    and math.isfinite(lifetime) and lifetime > TOKEN_MARGIN_SECONDS):
                self._token = (token, now + lifetime - TOKEN_MARGIN_SECONDS)
            return token

    async def _call(self, http: httpx.AsyncClient, creds: AICoreCredentials, method: str,
                    path: str, *, step: str, code: str, **kwargs: Any) -> dict:
        """One AI API call under the resource group; after a 401 once more
        with a new token (the 401 came before anything was processed)."""
        url = creds.base_url + path
        token = await self._bearer(http, creds)
        headers = {"Authorization": f"Bearer {token}", "AI-Resource-Group": self.group}
        data = await self._send(http, method, url, step=step, code=code, retry_401=True,
                                headers=headers, **kwargs)
        if data is not None:
            return data
        token = await self._bearer(http, creds, stale=token)
        headers["Authorization"] = f"Bearer {token}"
        data = await self._send(http, method, url, step=step, code=code, headers=headers,
                                **kwargs)
        assert data is not None  # without retry_401 a 401 raises
        return data

    async def _offered(self, http: httpx.AsyncClient, creds: AICoreCredentials) -> list[_Offered]:
        data = await self._call(http, creds, "GET", f"/lm/scenarios/{SCENARIO_ID}/models",
                                step="model list", code="aicore_unavailable")
        offered: dict[str, _Offered] = {}
        for entry in _resources(data, "model list"):
            name = entry.get("model")
            if not _is_embedding(name) or name in offered:
                continue
            versions = entry.get("versions")
            names = [v.get("name") for v in versions if isinstance(v, dict)] \
                if isinstance(versions, list) else []
            executable = entry.get("executableId")
            offered[name] = _Offered(
                model=name,
                versions=tuple(dict.fromkeys(
                    n for n in names if isinstance(n, str) and _VERSION.fullmatch(n))),
                executable_id=executable if _is_name(executable) else None)
        return sorted(offered.values(), key=lambda m: m.model)

    async def _list_all(self, http: httpx.AsyncClient, creds: AICoreCredentials, path: str, *,
                        step: str, params: dict[str, str]) -> list[dict]:
        """Every entry of an AI API list, read in pages of ``PAGE_SIZE``.

        Fails closed (``aicore_unavailable``): ``count``, a required field of
        the list answer, must be the same on every page, the pages must add
        up to it and no id may come twice. Otherwise the list moved while it
        was read, or the server ignored ``$skip``, and an entry may be
        missing: for the deployment list that is a RUNNING deployment that
        ``deploy`` would create a second time.
        """
        entries: list[dict] = []
        seen: set[str] = set()
        total: int | None = None
        received = 0
        for _ in range(MAX_PAGES):
            page_params: dict[str, Any] = {**params, "$top": PAGE_SIZE}
            if received:
                page_params["$skip"] = received
            data = await self._call(http, creds, "GET", path, step=step,
                                    code="aicore_unavailable", params=page_params)
            count, resources = data.get("count"), data.get("resources")
            if (not isinstance(count, int) or isinstance(count, bool) or count < 0
                    or not isinstance(resources, list)):
                raise _incomplete(step, "no count or no resources list in the answer")
            if total is None:
                total = count
            elif count != total:
                raise _incomplete(step, "the list changed while it was read")
            received += len(resources)
            for entry in resources:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("id")
                if isinstance(key, str):
                    if key in seen:
                        raise _incomplete(step, "an entry came on two pages")
                    seen.add(key)
                entries.append(entry)
            if received > total:
                raise _incomplete(step, "more entries than the count")
            if received == total:
                return entries
            if not resources:
                raise _incomplete(step, "the pages do not add up to the count")
        raise _incomplete(step, f"more than {MAX_PAGES} pages")

    async def _deployments(self, http: httpx.AsyncClient,
                           creds: AICoreCredentials) -> list[dict[str, str]]:
        entries = await self._list_all(http, creds, "/lm/deployments", step="deployment list",
                                       params={"scenarioId": SCENARIO_ID})
        out = []
        for entry in entries:
            deployment_id, model = entry.get("id"), _deployed_model(entry)
            if not _is_name(deployment_id) or model is None:
                continue
            status = entry.get("status")
            out.append({"model": model,
                        "status": status if isinstance(status, str) and _STATUS.fullmatch(status)
                        else "UNKNOWN",
                        "deployment_id": deployment_id})
        out.sort(key=lambda d: (d["model"], d["deployment_id"]))
        return out

    async def list_models(self) -> list[dict[str, Any]]:
        """``[{model, versions}]``: the scenario's embedding models (the name
        holds ``embed``, in any case), by name."""
        creds = self._resolved()
        async with self._http() as http:
            offered = await self._offered(http, creds)
        return [{"model": m.model, "versions": list(m.versions)} for m in offered]

    async def list_deployments(self) -> list[dict[str, str]]:
        """``[{model, status, deployment_id}]``: the resource group's
        deployments of embedding models, in any status, by model and id."""
        creds = self._resolved()
        async with self._http() as http:
            return await self._deployments(http, creds)

    async def _configuration_id(self, http: httpx.AsyncClient, creds: AICoreCredentials,
                                model: str, executable: dict) -> str:
        """The id of the configuration to deploy: one of the same name,
        executable and bindings that a failed deploy left, else a new one."""
        wanted = _configuration(model, executable)
        for entry in await self._list_all(http, creds, "/lm/configurations",
                                          step="configuration list",
                                          params={"scenarioId": SCENARIO_ID}):
            if _same_configuration(entry, wanted):
                logger.info("AI Core: configuration %s of %s reused", entry["id"], model)
                return entry["id"]
        created = await self._call(http, creds, "POST", "/lm/configurations",
                                   step="configuration create", code="deploy_failed",
                                   json=wanted)
        configuration_id = created.get("id")
        if not _is_name(configuration_id):
            logger.warning("AI Core: configuration create failed (no id in the answer)")
            raise AICoreError("deploy_failed")
        return configuration_id

    async def _create_deployment(self, http: httpx.AsyncClient, creds: AICoreCredentials,
                                 model: str, configuration_id: str) -> dict[str, str]:
        """``{deployment_id, status}`` of the deployment just created. A
        create that failed or whose answer was lost is checked against the
        list once: what appeared since the list was read under the lock is
        this call's deployment (one app instance assumed)."""
        try:
            created = await self._call(http, creds, "POST", "/lm/deployments",
                                       step="deployment create", code="deploy_failed",
                                       json={"configurationId": configuration_id})
            deployment_id, status = created.get("id"), created.get("status")
            if not _is_name(deployment_id):
                logger.warning("AI Core: deployment create failed (no id in the answer)")
                raise AICoreError("deploy_failed")
        except AICoreError as failed:
            if failed.code != "deploy_failed":
                raise  # a token that could not be renewed: the create was refused
            try:
                appeared = _live(await self._deployments(http, creds), model)
            except AICoreError:
                appeared = None
            if appeared is None:
                raise
            logger.warning("AI Core: deployment %s of %s found after an unconfirmed create",
                           appeared["deployment_id"], model)
            return {"deployment_id": appeared["deployment_id"], "status": appeared["status"]}
        if not isinstance(status, str) or not _STATUS.fullmatch(status):
            status = "PENDING"
        logger.info("AI Core: deployment %s of %s created (status %s)",
                    deployment_id, model, status)
        return {"deployment_id": deployment_id, "status": status}

    async def deploy(self, model: str) -> dict[str, Any]:
        """Deploys ``model`` unless it is: ``{deployment_id, status, created}``.

        ``model_unknown`` for a name the scenario does not offer as an
        embedding model, before anything is created; ``deploy_failed`` when
        no executable takes it or a create failed (see the module docstring).
        """
        if not _is_embedding(model):
            raise AICoreError("model_unknown")
        creds = self._resolved()
        async with self._deploy_lock, self._http() as http:
            offered = {m.model: m for m in await self._offered(http, creds)}
            if model not in offered:
                raise AICoreError("model_unknown")
            live = _live(await self._deployments(http, creds), model)
            if live is not None:
                return {"deployment_id": live["deployment_id"], "status": live["status"],
                        "created": False}
            listing = await self._call(http, creds, "GET",
                                       f"/lm/scenarios/{SCENARIO_ID}/executables",
                                       step="executable list", code="aicore_unavailable")
            executable = find_executable(_resources(listing, "executable list"), model,
                                         offered[model].executable_id)
            if executable is None:
                logger.warning("AI Core: deployment of %s not made (no executable of the "
                               "scenario takes it)", model)
                raise AICoreError("deploy_failed")
            configuration_id = await self._configuration_id(http, creds, model, executable)
            made = await self._create_deployment(http, creds, model, configuration_id)
        return {**made, "created": True}


_default: AICoreDeployments | None = None


def default_client() -> AICoreDeployments:
    """The process's client, built at the first use; it keeps the token."""
    global _default
    if _default is None:
        _default = AICoreDeployments()
    return _default


def reset() -> None:
    """Tests only: drops the process's client with its credentials and token."""
    global _default
    _default = None


async def list_models() -> list[dict[str, Any]]:
    """:meth:`AICoreDeployments.list_models` of the process's client."""
    return await default_client().list_models()


async def list_deployments() -> list[dict[str, str]]:
    """:meth:`AICoreDeployments.list_deployments` of the process's client."""
    return await default_client().list_deployments()


async def deploy(model: str) -> dict[str, Any]:
    """:meth:`AICoreDeployments.deploy` of the process's client (it bills)."""
    return await default_client().deploy(model)

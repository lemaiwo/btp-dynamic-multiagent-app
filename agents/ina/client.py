"""The InA client of the built-in: one registered query, read through its destination.

One client serves one registered query (``agents/ina/models.py``) for one
call. It is handed an ``httpx.AsyncClient`` built by
``agents.destination_auth.destination_http_client(..., connectivity=...)``,
so the host and the credential are decided per request by
``DestinationAuth`` -- an OnPremise destination (SIA is one) goes through the
connectivity proxy -- and this module only ever sends the two relative paths
of the protocol below.

The protocol (design addendum (k), part 2 point 2, and what SIA parsed for
``scripts/probe_ina.py``):

* ``GET /sap/bw/ina/GetServerInfo`` with ``X-CSRF-Token: Fetch``. Its answer
  names the services and their capabilities; the capability names of the
  ``Analytics`` service go back to SAP in every later request. ICF hands out
  the CSRF token and the session cookie with it, and refuses a POST without
  them (403).
* ``POST /sap/bw/ina/GetResponse`` with the token and the cookies: a
  ``Metadata`` request for :meth:`InaClient.describe`, and for
  :meth:`InaClient.run` three ``Analytics`` requests on one query instance
  (``DataSource.InstanceId``, ``ina`` and 16 random hex digits per run), as
  SIA answered them on 2026-11-04: a VariableSubmit when the run has
  variables (SIA takes values only so), the read, and a Close that releases
  the instance. All carry ``Options: ["StatefulServer"]``: the InA state
  lives in the SAP session the cookie names. The cookies an answer sets join
  the session for the next request of the run and for later calls; the read
  asks for the text attribute of each row dimension the submit answer named
  (``protocol.text_attributes``). The Close is best effort: sent also after
  a failed submit or read, never the run's failure, logged by exception
  class only, and not sent once the run's deadline is spent.

Whose session it is. The token and the cookies ARE a SAP session of the
identity the destination signed in as, so they live in the
:class:`InaSessionStore` (``agents.odata.session.CsrfSessionStore`` with its
key per destination and signed-in user, and its rule that an entry is served
only to the request that carries the same token), never in the shared HTTP
client, whose cookie jar stores nothing (``NoCookieJar``). The capability
list is stored next to the session it came with. ``GetServerInfo`` is sent
once per (destination, user) and session lifetime; a 403 on a POST drops the
session, fetches a new one and sends the request once more -- for a run the
whole sequence from the submit on, since the instance lived in the refused
session; an InA request only reads, so a repeat changes nothing -- and a
second 403 is ``csrf_failed`` (or ``forbidden`` when SAP did not say
``Required``).

What reaches the caller. Every failure is an :class:`InaError` with a code
of the closed list in ``errors.py`` and a fixed text; SAP's own words pass
only as the cut, masked text of an InA error message, and of a result only
what was asked (``protocol.py`` builds the requests and reads the answers,
default-deny). No body, header, URL, token, cookie or exception text leaves
this module, and a log line names the step and the class or the status only.

The caps and every argument check (``protocol.plan_run``) refuse before
anything is sent; a missing mandatory variable is ``variable_required``. One
deadline (:data:`CALL_DEADLINE_SECONDS`) covers each describe and the whole
sequence of a run, Close included.

Reads only. Nothing here changes data in SAP, and no call is recorded in the
OData write audit. Never imports ``agents.db``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, TypeVar

import httpx
from pydantic import ValidationError

from agents.destination import DestinationError
from agents.destination_auth import (
    PROXY_REFUSED_TEXT,
    DestinationRefused,
    DestinationUserRequired,
    destination_http_client,
    went_through_proxy,
)
from agents.odata.session import (
    SESSION_CACHE_MAX,
    CsrfSession,
    CsrfSessionStore,
    NoCookieJar,
    cookies_from_response,
)

from .errors import InaError
from .models import InaDefinition

# The caps and the wire format are the protocol module's; they are named here
# too because a caller of the client should not need to know where they live.
from .protocol import (
    CLIENT_INFO,
    DEFAULT_TOP,
    DESCRIBE_EXPAND,
    FILTER_OPERATORS,
    MAX_CELLS,
    MAX_DIMENSIONS,
    MAX_FILTERS,
    MAX_MEASURES,
    MAX_TOP,
    TELL_THE_USER,
    UNREADABLE,
    InaDescription,
    RunPlan,
    analytics_capabilities,
    analytics_request,
    check_caps,
    close_request,
    data_source,
    describe_request,
    fits_name,
    plan_run,
    read_description,
    read_grid,
    sap_message_error,
    shown_name,
    text_attributes,
    variable_submit_request,
)

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

# What one answer may weigh before it is parsed: the body is read as a stream
# and refused beyond this.
MAX_RESPONSE_BYTES = 8_000_000
# A total deadline per request, on top of the HTTP client's per-operation
# timeout: a body that trickles in would otherwise hold the call for good.
CALL_DEADLINE_SECONDS = 120.0
# The most a Close may take of what is left of a run's deadline.
CLOSE_DEADLINE_SECONDS = 10.0
INSTANCE_PREFIX = "ina"
INFO_PATH = "/sap/bw/ina/GetServerInfo"
RESPONSE_PATH = "/sap/bw/ina/GetResponse"

_CSRF_TOKEN = re.compile(r"[\x21-\x7E]{1,512}")
PROXY_REFUSED_HINT = (
    "the connectivity proxy refused the request before it reached SAP. An administrator "
    "has to fix the connectivity setup: tell the user instead of retrying"
)


# -- the session store -----------------------------------------------------


class InaSessionStore(CsrfSessionStore):
    """The InA sessions: ``CsrfSessionStore`` plus the capability list each
    ``GetServerInfo`` answered with.

    The capabilities are kept by a digest of the session's token, so they
    belong to exactly the session they came with and are found again only
    through it; they are SAP's names, not a credential. Bounded like the
    sessions (``SESSION_CACHE_MAX``, least recently used first).
    """

    def __init__(self) -> None:
        super().__init__()
        self._capabilities: OrderedDict[str, tuple[str, ...]] = OrderedDict()

    @staticmethod
    def _digest(session: CsrfSession) -> str:
        return hashlib.sha256(session.token.encode("utf-8")).hexdigest()

    def remember_capabilities(self, session: CsrfSession, capabilities: list[str]) -> None:
        digest = self._digest(session)
        self._capabilities.pop(digest, None)
        self._capabilities[digest] = tuple(capabilities)
        while len(self._capabilities) > SESSION_CACHE_MAX:
            self._capabilities.popitem(last=False)

    def capabilities_of(self, session: CsrfSession) -> tuple[str, ...] | None:
        digest = self._digest(session)
        hit = self._capabilities.get(digest)
        if hit is not None:
            self._capabilities.move_to_end(digest)
        return hit


# -- the HTTP client ---------------------------------------------------------


def build_ina_http_client(
    resolver: Any,
    *,
    user_context: bool,
    server_key: str,
    connectivity: Any,
    transport: httpx.AsyncBaseTransport | None = None,
    proxy_transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """The HTTP client of a registered query's calls, for the agent tools
    and the admin Describe and Test alike: one way to route a query's
    credential.

    Built for the connectivity route (``destination_http_client(...,
    connectivity=...)``): an OnPremise destination goes through the proxy,
    and ``connectivity`` None says "no binding", so such a destination is
    refused with a text that names it. The cookie jar stores nothing: the
    SAP session of each identity lives in an :class:`InaSessionStore`,
    never in a client another identity may share. ``transport`` and
    ``proxy_transport`` are the test seams of the two sides.
    """
    http = destination_http_client(
        resolver,
        user_context=user_context,
        server_key=server_key,
        transport=transport,
        connectivity=connectivity,
        proxy_transport=proxy_transport,
    )
    http.cookies = NoCookieJar()  # type: ignore[assignment]
    return http


# -- the client ------------------------------------------------------------


@dataclass(repr=False)
class _Answer:
    """One answer as ``_exchange`` read it. Headers, body and cookies hold
    SAP's session and data, so the repr shows the status only."""

    status: int
    headers: httpx.Headers
    body: bytes
    cookies: dict[str, str]
    proxied: bool

    def __repr__(self) -> str:
        return f"_Answer(status={self.status!r}, proxied={self.proxied!r})"

    __str__ = __repr__


def _csrf_required(headers: httpx.Headers) -> bool:
    return headers.get("x-csrf-token", "").strip().lower() == "required"


@dataclass(repr=False)
class _Instance:
    """A run's query instance in the SAP session, and the session (a copy,
    with the cookies of the run's answers) a request that may have opened it
    went out with: the one its Close goes to. None while nothing was sent,
    and after a 403, which leaves nothing to close."""

    name: str
    session: CsrfSession | None = None

    def __repr__(self) -> str:
        return f"_Instance(name={self.name!r})"


class _Stale(Exception):
    """A 403 at a step of a run's first attempt: ``session`` is dropped and
    the whole sequence runs once more."""

    def __init__(self, session: CsrfSession) -> None:
        super().__init__("stale InA session")
        self.session = session


class InaClient:
    """``describe`` and ``run`` of one registered query through its destination.

    ``destination`` and ``user_context`` are the registration's identity and
    must be the ones the HTTP client's auth was built with (checked before
    every call); ``sessions`` is the app's :class:`InaSessionStore`;
    ``definition`` is the stored description, without which ``run``
    describes the query first.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        sessions: InaSessionStore,
        *,
        destination: str,
        user_context: bool,
        query: str,
        package: str = "",
        definition: InaDefinition | dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(sessions, InaSessionStore):
            raise TypeError("an InA client needs an InaSessionStore")
        self._http = http
        # The client is shared by every user of an agent: whatever jar it came
        # with, from here on it stores and sends no cookie by itself.
        if not isinstance(http.cookies.jar, NoCookieJar):
            http.cookies = NoCookieJar()  # type: ignore[assignment]
        self._sessions = sessions
        self._destination = destination
        self._user_context = user_context
        if not fits_name(query):
            raise InaError("query_unknown", "the registration names no usable InA query")
        self._query = query
        self._package = package if isinstance(package, str) else ""
        self._definition: InaDefinition | None = None
        if definition is not None:
            try:
                self._definition = InaDefinition.model_validate(
                    definition.model_dump() if isinstance(definition, InaDefinition) else definition
                )
            except ValidationError:
                # `from None`: the ValidationError carries the input in its repr.
                raise InaError(
                    "sap_error",
                    "the stored description of this query is not valid",
                    hint="an administrator has to describe the query again",
                ) from None

    def __repr__(self) -> str:
        # Names only: the auth behind `_http` holds the destination's credential.
        return (
            f"InaClient(destination={shown_name(self._destination)!r}, "
            f"user_context={self._user_context!r}, query={shown_name(self._query)!r})"
        )

    __str__ = __repr__

    @property
    def definition(self) -> InaDefinition | None:
        return self._definition

    # -- identity -----------------------------------------------------------
    def _session_key(self) -> Any:
        """The session key of the identity this request runs as.

        Fails closed when the pieces do not line up: the key must describe the
        identity the HTTP client's auth sends the request as, or one user's
        SAP session could travel with another credential.
        """
        if (
            not isinstance(self._destination, str)
            or not self._destination
            or not isinstance(self._user_context, bool)
        ):
            raise InaError("destination_error", "the query names no destination to read through")
        auth = self._http.auth
        if (
            getattr(auth, "user_context", None) is not self._user_context
            or getattr(auth, "destination_name", None) != self._destination
        ):
            raise InaError(
                "destination_error",
                "the connection of this query does not match its destination settings",
            )
        try:
            return self._sessions.key(self._destination, self._user_context)
        except DestinationUserRequired:
            raise _no_user() from None

    # -- HTTP ---------------------------------------------------------------
    async def _exchange(
        self, method: str, path: str, headers: dict[str, str], content: bytes | None
    ) -> _Answer:
        """One request; the answer with its body read up to the cap. The
        body of a 407 is never read. Every failure is an ``InaError``."""
        body = bytearray()
        deadline = asyncio.timeout(CALL_DEADLINE_SECONDS)
        try:
            async with deadline:
                async with self._http.stream(
                    method,
                    path,
                    headers={"Accept": "application/json", **headers},
                    content=content,
                    follow_redirects=False,
                ) as response:
                    status = response.status_code
                    answer = _Answer(
                        status=status,
                        headers=response.headers,
                        body=b"",
                        cookies=cookies_from_response(response),
                        proxied=went_through_proxy(response),
                    )
                    if status != 407:
                        async for chunk in response.aiter_bytes():
                            body += chunk
                            if len(body) > MAX_RESPONSE_BYTES:
                                raise InaError(
                                    "result_too_large",
                                    f"the answer of SAP is larger than {MAX_RESPONSE_BYTES} bytes",
                                    hint="group by fewer dimensions, filter, or lower top",
                                )
        except InaError:
            raise
        except TimeoutError:
            # Ours or not, the model is told the same; the log says which.
            logger.warning(
                "ina: %s %s did not finish in time (deadline %s)",
                method,
                path,
                "expired" if deadline.expired() else "not expired",
            )
            raise _timed_out() from None
        except DestinationUserRequired:
            raise _no_user() from None
        except DestinationError as exc:
            logger.warning("ina: the destination could not be used (%s)", type(exc).__name__)
            raise InaError(
                "destination_error",
                "the destination of this query could not be used",
                hint="an administrator has to check the destination and the connectivity "
                "setup; " + TELL_THE_USER,
                # A fixed text naming the destination and the rule, for the
                # admin routes; the model gets the message above.
                admin_text=exc.admin_text if isinstance(exc, DestinationRefused) else None,
            ) from None
        except httpx.TimeoutException as exc:
            logger.warning("ina: %s %s timed out (%s)", method, path, type(exc).__name__)
            raise _timed_out() from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # The exception text can carry the URL; only its type is logged.
            logger.warning("ina: %s %s failed (%s)", method, path, type(exc).__name__)
            raise InaError("unreachable", "SAP could not be reached") from None
        answer.body = bytes(body)
        return answer

    @staticmethod
    def _decode(answer: _Answer) -> dict[str, Any]:
        """The JSON object of an answer, or ``not_json``."""
        try:
            payload = json.loads(answer.body) if answer.body else None
        except ValueError:
            payload = None
        if payload is None:
            raise InaError(
                "not_json",
                f"SAP did not answer with JSON (HTTP {answer.status})",
                hint="a sign-in page instead of data usually means the destination's "
                "credential was not accepted",
                status=answer.status,
            )
        if not isinstance(payload, dict):
            raise InaError("sap_error", UNREADABLE, status=answer.status)
        return payload

    @staticmethod
    def _refusal(answer: _Answer) -> InaError:
        """The error of an answer of 300 or above."""
        status = answer.status
        if status == 407 and answer.proxied:
            return InaError(
                "proxy_refused", PROXY_REFUSED_TEXT, hint=PROXY_REFUSED_HINT, status=status
            )
        if status == 401:
            return InaError(
                "unauthorized",
                "SAP did not accept the destination's credential (HTTP 401)",
                hint="an administrator has to check the destination; " + TELL_THE_USER,
                status=status,
            )
        if status == 403:
            return InaError(
                "forbidden",
                "SAP refused the request (HTTP 403)",
                hint="the SAP user is not authorised for InA or for this query; " + TELL_THE_USER,
                status=status,
            )
        if 300 <= status < 400:
            return InaError(
                "sap_error",
                f"SAP answered with a redirect (HTTP {status}), which is not followed",
                hint="a redirect to a sign-in page usually means the destination's "
                "credential was not accepted",
                status=status,
            )
        if status != 407:
            try:
                payload = json.loads(answer.body) if answer.body else None
            except ValueError:
                payload = None
            error = sap_message_error(payload)
            if error is not None:
                error.status = status
                return error
        return InaError(
            "sap_error",
            f"HTTP {status} from the InA service",
            hint="InA (/sap/bw/ina) may not be active on this system" if status == 404 else None,
            status=status,
        )

    async def _fetch_session(self) -> CsrfSession:
        """``GetServerInfo`` as the caller: the CSRF token, the session
        cookies and the ``Analytics`` capabilities. Runs inside
        ``CsrfSessionStore.get``, in the caller's own context."""
        answer = await self._exchange("GET", INFO_PATH, {"X-CSRF-Token": "Fetch"}, None)
        if answer.status >= 300:
            raise self._refusal(answer)
        capabilities = analytics_capabilities(self._decode(answer))
        if capabilities is None:
            raise InaError(
                "sap_error",
                "SAP offers no InA Analytics service on this system",
                status=answer.status,
            )
        token = answer.headers.get("x-csrf-token", "")
        if not _CSRF_TOKEN.fullmatch(token) or token.lower() in ("required", "fetch"):
            raise InaError(
                "csrf_failed",
                "SAP did not hand out a CSRF token",
                hint=TELL_THE_USER,
                status=answer.status,
            )
        session = CsrfSession.fresh(token, answer.cookies)
        self._sessions.remember_capabilities(session, capabilities)
        return session

    async def _acquire(self, key: Any) -> tuple[CsrfSession, list[str]]:
        for _ in range(2):
            session = await self._sessions.get(key, self._fetch_session)
            capabilities = self._sessions.capabilities_of(session)
            if capabilities is not None:
                return session, list(capabilities)
            # The capabilities aged out of their table before the session did.
            self._sessions.drop(key, session)
        raise InaError("sap_error", "the capabilities of the InA service could not be read")

    async def _send(self, session: CsrfSession, body: dict[str, Any]) -> _Answer:
        headers = {"Content-Type": "application/json", "X-CSRF-Token": session.token}
        cookie = session.cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        content = json.dumps(body, allow_nan=False).encode("utf-8")
        return await self._exchange("POST", RESPONSE_PATH, headers, content)

    @staticmethod
    async def _guarded(work: Awaitable[_T]) -> _T:
        """``work``, with the destination's and the store's refusals as an
        ``InaError``."""
        try:
            return await work
        except DestinationUserRequired:
            raise _no_user() from None
        except InaError:
            raise
        except DestinationError as exc:
            # The store's own refusal: a key carried into another user's request.
            logger.warning("ina: the session could not be used (%s)", type(exc).__name__)
            raise InaError(
                "destination_error", "the SAP session of this query could not be used"
            ) from None

    def _accept(self, key: Any, session: CsrfSession, answer: _Answer) -> dict[str, Any]:
        """The JSON object of an answer, or the refusal it is or carries.

        The cookies it sets join the session: in the store, and in
        ``session``, the copy the run's next request is sent with. Only after
        a positive verdict (below 300, JSON): what a sign-in page sets is
        never sent with a later request.
        """
        if answer.status >= 300:
            raise self._refusal(answer)
        payload = self._decode(answer)
        if answer.cookies:
            cookies = {**session.cookies, **answer.cookies}
            self._sessions.update(key, session, cookies)
            session.cookies = cookies
        error = sap_message_error(payload)
        if error is not None:
            raise error
        return payload

    def _second_403(self, answer: _Answer) -> InaError:
        if _csrf_required(answer.headers):
            return InaError(
                "csrf_failed", "SAP refused the CSRF token twice", hint=TELL_THE_USER, status=403
            )
        return self._refusal(answer)

    async def _post(self, build: Callable[[list[str]], dict[str, Any]]) -> dict[str, Any]:
        """One ``GetResponse``, with the caller's session: the answer's JSON
        object, or the refusal it carries."""
        key = self._session_key()
        return await self._guarded(self._post_once(key, build))

    async def _post_once(
        self, key: Any, build: Callable[[list[str]], dict[str, Any]]
    ) -> dict[str, Any]:
        session, capabilities = await self._acquire(key)
        answer = await self._send(session, build(capabilities))
        if answer.status == 403:
            # Stale token or session: drop exactly the one that was sent,
            # fetch again and repeat once. A read changes nothing.
            self._sessions.drop(key, session)
            session, capabilities = await self._acquire(key)
            answer = await self._send(session, build(capabilities))
            if answer.status == 403:
                raise self._second_403(answer)
        return self._accept(key, session, answer)

    def _data_source(self) -> dict[str, str]:
        return data_source(self._query, self._package)

    # -- describe -----------------------------------------------------------
    async def describe(self) -> InaDescription:
        """Read the query's dimensions, measures and variables from SAP."""
        source = self._data_source()
        payload = await self._post(lambda capabilities: describe_request(capabilities, source))
        described = read_description(payload.get("Cube"))
        self._definition = described.definition
        return described

    # -- run ----------------------------------------------------------------
    async def run(
        self,
        measures: Any,
        group_by: Any = (),
        filters: Any = (),
        top: Any = DEFAULT_TOP,
        order: Any = (),
        variables: Any = None,
    ) -> dict[str, Any]:
        """One ``Analytics`` read: ``{columns, rows, units, row_count,
        truncated}``, ``truncated`` true when SAP had more than ``top`` rows
        (the rows beyond are not returned). Checked in full before anything
        is sent."""
        definition = self._definition
        if definition is None:
            # What fails without the description is refused before it is read.
            check_caps(
                measures=measures,
                group_by=group_by,
                filters=filters,
                top=top,
                order=order,
                variables=variables,
            )
            definition = (await self.describe()).definition
        plan = plan_run(
            definition,
            measures=measures,
            group_by=group_by,
            filters=filters,
            top=top,
            order=order,
            variables=variables,
        )
        key = self._session_key()
        instance = _Instance(INSTANCE_PREFIX + secrets.token_hex(8))
        ends_at = asyncio.get_running_loop().time() + CALL_DEADLINE_SECONDS
        deadline = asyncio.timeout_at(ends_at)
        try:
            try:
                async with deadline:
                    payload, plan = await self._guarded(self._sequence(key, plan, instance))
            except TimeoutError:
                logger.warning(
                    "ina: a run did not finish in time (deadline %s)",
                    "expired" if deadline.expired() else "not expired",
                )
                raise _timed_out() from None
        finally:
            await self._close(instance, ends_at)
        return read_grid(payload, plan)

    async def _sequence(
        self, key: Any, plan: RunPlan, instance: _Instance
    ) -> tuple[dict[str, Any], RunPlan]:
        """The submit (when the run has variables) and the read, on one
        instance in one session: the read's answer and the plan it was sent
        with. A 403 at either step drops the session that was sent and runs
        the whole sequence once more, in a new session: the instance lived in
        the refused one."""
        try:
            return await self._attempt(key, plan, instance, repeat=False)
        except _Stale as stale:
            self._sessions.drop(key, stale.session)
            return await self._attempt(key, plan, instance, repeat=True)

    async def _attempt(
        self, key: Any, plan: RunPlan, instance: _Instance, *, repeat: bool
    ) -> tuple[dict[str, Any], RunPlan]:
        session, capabilities = await self._acquire(key)
        source = data_source(self._query, self._package, instance.name)
        if plan.variables:
            submitted = await self._step(
                key, session, instance, variable_submit_request(capabilities, source, plan), repeat
            )
            # The read asks for the text attributes the submit answer names.
            plan = replace(plan, texts=text_attributes(submitted.get("Cube"), plan.group_by))
        payload = await self._step(
            key, session, instance, analytics_request(capabilities, source, plan), repeat
        )
        return payload, plan

    async def _step(
        self,
        key: Any,
        session: CsrfSession,
        instance: _Instance,
        body: dict[str, Any],
        repeat: bool,
    ) -> dict[str, Any]:
        # Set before sending: a request that left may have opened the instance.
        instance.session = session
        answer = await self._send(session, body)
        if answer.status == 403:
            instance.session = None
            if repeat:
                raise self._second_403(answer)
            raise _Stale(session)
        return self._accept(key, session, answer)

    async def _close(self, instance: _Instance, ends_at: float) -> None:
        """Release the run's query instance, best effort: never raised, a
        failure logged by exception class (and HTTP status) only. Not sent
        while the caller is being cancelled or once the run's deadline is
        spent; otherwise within what is left of it."""
        session = instance.session
        task = asyncio.current_task()
        if session is None or (task is not None and task.cancelling()):
            return
        left = ends_at - asyncio.get_running_loop().time()
        if left <= 0:
            logger.warning("ina: the query instance was not closed (no time left)")
            return
        try:
            async with asyncio.timeout(min(left, CLOSE_DEADLINE_SECONDS)):
                answer = await self._send(session, close_request(instance.name))
            if answer.status >= 300:
                raise self._refusal(answer)
            error = sap_message_error(self._decode(answer))
            if error is not None:
                raise error
        except Exception as exc:  # noqa: BLE001 - a Close never fails the run
            status = getattr(exc, "status", None)
            logger.warning(
                "ina: the query instance was not closed (%s%s)",
                type(exc).__name__,
                f", HTTP {status}" if isinstance(status, int) else "",
            )


def _timed_out() -> InaError:
    return InaError(
        "timeout",
        "SAP did not answer in time",
        hint="ask for fewer dimensions, a narrower filter or a lower top",
    )


def _no_user() -> InaError:
    return InaError(
        "user_token_required",
        "this query runs as the signed-in user, and this run has no signed-in user",
        hint="start the agent from the chat, or have an administrator register the query "
        "to run as a technical user",
    )


__all__ = [
    "CALL_DEADLINE_SECONDS",
    "CLIENT_INFO",
    "CLOSE_DEADLINE_SECONDS",
    "DEFAULT_TOP",
    "DESCRIBE_EXPAND",
    "FILTER_OPERATORS",
    "INFO_PATH",
    "MAX_CELLS",
    "MAX_DIMENSIONS",
    "MAX_FILTERS",
    "MAX_MEASURES",
    "MAX_RESPONSE_BYTES",
    "MAX_TOP",
    "RESPONSE_PATH",
    "InaClient",
    "InaDescription",
    "InaSessionStore",
    "build_ina_http_client",
]

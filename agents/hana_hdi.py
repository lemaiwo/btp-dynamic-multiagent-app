"""The app's tables in an SAP HANA Cloud HDI container.

An ``hdi-shared`` binding carries two users. The runtime user (``user``),
which the SQLAlchemy engine connects as, may read and write rows and nothing
else: any DDL fails. Tables, indexes and constraints exist only as
*design-time artifacts* that the design-time user (``hdi_user``) writes into
the container and has HDI *make*. So ``create_all`` and the
``_ensure_column`` chain of :func:`agents.db.init_db` cannot run on HANA;
this module is what runs instead.

* :func:`artifacts` turns the SQLAlchemy models into the artifact files. The
  models stay the one description of the schema: nothing is committed as an
  ``.hdbtable`` file, and there is no Node deployer module.
* :func:`deploy` writes those files and makes them, through HDI's SQL API
  (``<schema>#DI.WRITE`` / ``MAKE``), with the synchronous ``hdbcli`` driver
  in a thread, under the container lock. HDI compares each artifact with
  what it deployed before and migrates the table itself (a new column is
  added, a dropped one is removed); an artifact that is deployed but no
  longer generated is *undeployed*, which drops that object.

What a transaction does and does not do here (measured on a container): the
container lock (``#DI.LOCK``) lives in the client's transaction, so the
connection must not autocommit (hdbcli's default) or the lock is gone the
moment it is taken. But HDI's procedures commit their own work: a ROLLBACK
takes back neither a WRITE or DELETE in the design-time file system nor a
successful MAKE. A MAKE is atomic in itself (a refused artifact leaves
every deployed object as it was), the file system is not. So a deploy never
relies on rollback: it reads both the deployed state and the file system
each time and is written to succeed from whatever an interrupted or failed
deploy left behind.

"The models are the schema" would make an OLDER version of the app
destructive: started against a container a newer version deployed (a
rollback, or an old instance restarting during a rolling deploy), its
smaller artifact set would drop the newer tables and columns with their
rows. On Postgres old code simply runs on a superset schema. Two guards make
HANA behave the same:

* **Schema generation.** :data:`HANA_SCHEMA_GENERATION` counts the artifact
  sets this code base has shipped; ``hana_schema_history.json`` pins the
  digest of each (a unit test fails when the models change without a new
  generation). A deploy records in the container its generation, the
  digest of its files and whether its make finished ("not made" before the
  make, "made" after it). A container whose record is a HIGHER generation,
  made, with the digest of what is deployed, is left exactly as it is:
  nothing written, nothing made, one WARNING, and the app starts on the
  newer schema. A higher generation that is not made (its make failed or
  was interrupted) or whose schema is not what is deployed refuses the
  start: an older version must neither deploy over it nor run on it. The
  guard fails closed: a record that is there and unreadable, or a listing
  that fails, is an error, never "no record"; and a record is never
  replaced by a lower generation.
* **No table drop by default.** Whatever the generations say, a deploy that
  would undeploy an ``.hdbtable`` is refused before anything is written
  unless ``HANA_HDI_ALLOW_DROP`` is exactly ``true``. Indexes and constraints
  are undeployed without it.

Because the models are the only DDL on HANA, a new NOT NULL column needs a
``server_default``: HDI adds the column to a table that already has rows.

What HDI insists on (all enforced by :func:`artifacts`):

* an ``.hdbtable`` is ``COLUMN TABLE <name> (...)`` with no named unique
  constraint and no foreign key inside;
* a unique constraint is an ``.hdbindex`` (``UNIQUE INDEX``), like every
  other index. HANA allows several NULLs in a unique index, so the partial
  ``api_slug`` indexes of the other databases are plain unique ones here;
* a foreign key is an ``.hdbconstraint``;
* ``DATETIME`` is spelled ``TIMESTAMP``.

Nothing here logs or raises a password, the certificate or a connection
URL: a failure names HDI's own messages (which describe artifacts) or the
driver's error code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, MetaData, PrimaryKeyConstraint, Table, UniqueConstraint
from sqlalchemy.schema import (
    Constraint,
    CreateColumn,
    CreateIndex,
    ForeignKeyConstraint,
)

logger = logging.getLogger(__name__)

# Every artifact lives in this one folder of the container's file system.
ROOT = "src/"
HDICONFIG = f"{ROOT}.hdiconfig"
HDINAMESPACE = f"{ROOT}.hdinamespace"

# Artifact suffix -> the HDI build plugin that makes it.
PLUGINS = {
    "hdbtable": "com.sap.hana.di.table",
    "hdbindex": "com.sap.hana.di.index",
    "hdbconstraint": "com.sap.hana.di.constraint",
}
# A table before its indexes before the constraints that reference it. HDI
# orders a make by dependency itself; this is only the order of the answer.
_SUFFIX_ORDER = {suffix: n for n, suffix in enumerate(PLUGINS)}

# What a container schema name may look like before it is written into a
# statement (``"<schema>#DI"`` cannot be a bound parameter).
_SCHEMA_RE = re.compile(r"[A-Za-z0-9_]{1,120}")
# How many of HDI's error messages a failure repeats.
_MAX_MESSAGES = 20
_MAX_MESSAGE_CHARS = 500

# How many artifact sets this code base has shipped. Raise it by one with
# every change of the models (or of the generator) that changes an artifact,
# then run scripts/hana_schema_history.py to pin the new digest. Never reuse
# or lower a number: a container remembers the highest one it was given.
HANA_SCHEMA_GENERATION = 2
HISTORY_FILE = Path(__file__).with_name("hana_schema_history.json")
# Where a container keeps the generation it holds: a file in its design-time
# file system, outside the made folder, so it needs no build plugin. It is
# read and written under the container lock by the design-time user (which
# cannot read a table). Content: ``{"generation": n, "made": bool, "digest":
# state_digest of the files}``. ``made`` is false from before a make until
# it succeeded: HDI commits every call itself, so there is no transaction
# that could keep a record and the schema it describes in step.
GENERATION_FILE = "meta/generation.json"
# HDI's message codes when a LIST names a path that does not exist: the file
# itself, and the summary rows that come with it. Only this exact answer
# means "this container has no record"; any other error is a failure.
_FILE_NOT_FOUND = 8212757
_NOT_FOUND_COMPANIONS = frozenset({8212757, 8212760, 8211562, 8212764, 8214188})
# The same for a FOLDER that does not exist, which is what a container nobody
# deployed to answers for ``src/`` in both file systems (LIST and
# LIST_DEPLOYED differ in two of the four codes). Read off a never-used
# container: a folder exists only through the files below it.
_FOLDER_NOT_FOUND = 8212760
_FOLDER_NOT_FOUND_COMPANIONS = frozenset(
    {8212760, 8212764, 8211562, 8214188, 8211563, 8214191}
)
# The only value that lets a deploy undeploy a table.
ALLOW_DROP_ENV = "HANA_HDI_ALLOW_DROP"
# How long a deploy waits for another one that holds the container lock
# (another instance starting at the same moment). It has to fit inside the
# platform's start timeout, 60 s on Cloud Foundry unless a landscape raises
# it: an instance that waits longer is killed before it can report anything.
# Measured on a container: a first deploy of the whole schema from empty
# 3.0 s, a redeploy with a change 3-5 s, the check of a current container
# about 1 s. 30 s is several first deploys, and leaves the waiting instance
# the rest of the minute for its own check and the app's start.
LOCK_WAIT_MS = 30_000
# hdbcli's code for "lock wait timeout exceeded".
_LOCK_TIMEOUT_CODE = 131


class HdiError(RuntimeError):
    """The container could not be brought to the generated schema."""


@dataclass(frozen=True)
class HdiCredentials:
    """How to reach a container's design-time API. Never shown: ``repr``
    names the fields that are set, not their values."""

    host: str
    port: int
    schema: str
    user: str
    password: str = field(repr=False)
    certificate: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "HdiCredentials(<hidden>)"


def credentials_from_binding(credentials: Mapping[str, Any]) -> HdiCredentials:
    """The design-time user of an ``hdi-shared`` binding or service key.

    ``HdiError`` naming the missing field when the binding has none: a
    ``schema`` plan binding, for one, has a runtime user only and cannot
    create a table.
    """
    missing = [
        key
        for key in ("host", "port", "schema", "hdi_user", "hdi_password")
        if not credentials.get(key)
    ]
    if missing:
        raise HdiError(
            "the SAP HANA binding has no " + ", ".join(missing) + ": the tables can "
            "only be created through an HDI container (service plan hdi-shared)"
        )
    return HdiCredentials(
        host=str(credentials["host"]),
        port=int(credentials["port"]),
        schema=str(credentials["schema"]),
        user=str(credentials["hdi_user"]),
        password=str(credentials["hdi_password"]),
        certificate=str(credentials["certificate"]) if credentials.get("certificate") else None,
    )


# ---------------------------------------------------------------------------
# Models -> design-time artifacts
# ---------------------------------------------------------------------------
def _dialect() -> Any:
    # Imported here: the module is imported by agents.db on every database,
    # and only a HANA deployment needs the dialect at run time.
    from sqlalchemy_hana.dialect import HANAHDBCLIDialect

    return HANAHDBCLIDialect()


def _column(column: Any, dialect: Any) -> str:
    spec = str(CreateColumn(column).compile(dialect=dialect)).strip()
    if isinstance(column.type, DateTime):
        # The dialect writes DATETIME, which HDI's table plugin refuses.
        spec = re.sub(r"\bDATETIME\b", "TIMESTAMP", spec, count=1)
    return spec


def _table(table: Table, dialect: Any) -> str:
    quote = dialect.identifier_preparer.quote
    lines = [_column(column, dialect) for column in table.columns]
    key = [quote(column.name) for column in table.primary_key.columns]
    if key:
        lines.append(f"PRIMARY KEY ({', '.join(key)})")
    return f"COLUMN TABLE {quote(table.name)} (\n\t" + ",\n\t".join(lines) + "\n)\n"


def _foreign_key_name(constraint: ForeignKeyConstraint) -> str:
    if constraint.name:
        return str(constraint.name)
    columns = "_".join(column.name for column in constraint.columns)
    return f"fk_{constraint.table.name}_{columns}"


def artifacts(metadata: MetaData) -> dict[str, str]:
    """``{path: content}`` of every design-time file for ``metadata``.

    Deterministic: the same models give the same paths and bytes, in the
    same order, so HDI sees no change between two starts of one version.
    ``HdiError`` for a model construct there is no artifact for (an unnamed
    unique constraint, a CHECK constraint, two objects of one name): the
    unit tests then fail at once instead of a deployment.
    """
    dialect = _dialect()
    quote = dialect.identifier_preparer.quote
    files: dict[str, str] = {}

    def add(name: str, suffix: str, content: str) -> None:
        path = f"{ROOT}{name}.{suffix}"
        if path in files:
            raise HdiError(f"two schema objects share the artifact {path}")
        files[path] = content if content.endswith("\n") else content + "\n"

    for table in metadata.sorted_tables:
        add(table.name, "hdbtable", _table(table, dialect))
        for index in table.indexes:
            if not index.name:
                raise HdiError(f"an index on {table.name} has no name")
            ddl = str(CreateIndex(index).compile(dialect=dialect)).strip()
            add(str(index.name), "hdbindex", ddl.removeprefix("CREATE "))
        for constraint in table.constraints:
            if isinstance(constraint, PrimaryKeyConstraint):
                continue  # inside the table artifact
            if isinstance(constraint, UniqueConstraint):
                if not constraint.name:
                    raise HdiError(f"a unique constraint on {table.name} has no name")
                columns = ", ".join(quote(column.name) for column in constraint.columns)
                add(
                    str(constraint.name),
                    "hdbindex",
                    f"UNIQUE INDEX {quote(constraint.name)} ON {quote(table.name)} ({columns})",
                )
            elif isinstance(constraint, ForeignKeyConstraint):
                name = _foreign_key_name(constraint)
                columns = ", ".join(quote(column.name) for column in constraint.columns)
                referred = ", ".join(quote(e.column.name) for e in constraint.elements)
                tail = f" ON DELETE {constraint.ondelete}" if constraint.ondelete else ""
                add(
                    name,
                    "hdbconstraint",
                    f"CONSTRAINT {quote(name)} ON {quote(table.name)} "
                    f"FOREIGN KEY ({columns}) REFERENCES "
                    f"{quote(constraint.referred_table.name)} ({referred}){tail}",
                )
            elif isinstance(constraint, Constraint):
                raise HdiError(
                    f"{type(constraint).__name__} on {table.name} has no HDI artifact"
                )

    files[HDICONFIG] = (
        json.dumps(
            {"file_suffixes": {s: {"plugin_name": p} for s, p in PLUGINS.items()}},
            indent=2,
        )
        + "\n"
    )
    # No namespace prefix: the objects carry the plain table names the
    # models use, whatever the folder.
    files[HDINAMESPACE] = json.dumps({"name": "", "subfolder": "ignore"}) + "\n"
    return dict(sorted(files.items(), key=_artifact_order))


def _artifact_order(item: tuple[str, str]) -> tuple[int, str]:
    path = item[0]
    suffix = path.rsplit(".", 1)[-1]
    # The two configuration files first, then tables, indexes, constraints.
    return (_SUFFIX_ORDER.get(suffix, -1), path)


# ---------------------------------------------------------------------------
# Schema generations
# ---------------------------------------------------------------------------
def state_digest(hashes: Mapping[str, str]) -> str:
    """One SHA-256 over ``{path: sha256 of the file}``: the identity of a
    set of artifact files. Computable from the files (:func:`schema_digest`)
    and from what HDI lists as deployed, which is how a container's record
    is checked against the schema it claims to describe."""
    digest = hashlib.sha256()
    for path in sorted(hashes):
        digest.update(f"{path}\0{hashes[path].lower()}\0".encode())
    return digest.hexdigest()


def schema_digest(files: Mapping[str, str]) -> str:
    """The :func:`state_digest` of an artifact set (paths and contents)."""
    return state_digest({
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in files.items()
    })


def load_history(path: Path = HISTORY_FILE) -> dict[int, str]:
    """``{generation: digest}`` of every artifact set shipped so far."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {int(generation): str(digest) for generation, digest in raw.items()}


# ---------------------------------------------------------------------------
# Deploy through the HDI SQL API
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeployResult:
    """What one deploy did.

    ``changed`` is False when nothing was made: the container already held
    exactly the generated files, or (``newer_generation`` set) it holds the
    schema of a newer app version and was left alone. ``undeployed`` are the
    artifacts whose objects were dropped.
    """

    deployed: tuple[str, ...]
    undeployed: tuple[str, ...]
    changed: bool
    newer_generation: int | None = None


@dataclass(frozen=True)
class _ResultSet:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]

    def column(self, name: str) -> list[Any]:
        at = self.columns.index(name)
        return [row[at] for row in self.rows]


def _scrub(text: str, credentials: HdiCredentials) -> str:
    """``text`` without a credential value, should HDI ever echo one, and
    without the container's schema name, which its messages do carry: an
    error text travels into logs and reports, and names no landscape."""
    for secret in (credentials.password, credentials.certificate):
        if secret:
            text = text.replace(secret, "***")
    return text.replace(credentials.schema, "<container>")


def _connect(credentials: HdiCredentials) -> Any:
    from hdbcli import dbapi

    options: dict[str, Any] = {}
    if credentials.certificate:
        # The binding's own CA instead of the system trust store.
        options["sslTrustStore"] = credentials.certificate
    connection = dbapi.connect(
        address=credentials.host,
        port=credentials.port,
        user=credentials.user,
        password=credentials.password,
        # Always: there is no unencrypted or unvalidated way in.
        encrypt=True,
        sslValidateCertificate=True,
        **options,
    )
    # hdbcli connects with autocommit ON, and the container lock lives in
    # the client's transaction: with autocommit it is released the moment it
    # is taken and two deploys run into each other (measured). So: one
    # transaction, ended by the commit or rollback that ends the deploy.
    connection.setautocommit(False)
    return connection


def _call(cursor: Any, statement: str) -> list[_ResultSet]:
    """Run one HDI procedure; every result set it answers with."""
    cursor.execute(statement)
    sets: list[_ResultSet] = []
    while True:
        description = cursor.description
        if description:
            sets.append(
                _ResultSet(
                    tuple(str(d[0]).upper() for d in description),
                    tuple(tuple(row) for row in cursor.fetchall()),
                )
            )
        if not cursor.nextset():
            return sets


def _errors(sets: list[_ResultSet], credentials: HdiCredentials) -> list[str]:
    """HDI's ERROR messages. A failed procedure does not raise: it answers
    with a message table, and a row of severity ERROR is the failure."""
    found: list[str] = []
    for result in sets:
        if "SEVERITY" not in result.columns or "MESSAGE" not in result.columns:
            continue
        paths = result.column("PATH") if "PATH" in result.columns else [None] * len(result.rows)
        for severity, message, path in zip(
            result.column("SEVERITY"), result.column("MESSAGE"), paths, strict=True
        ):
            if str(severity).upper() == "ERROR":
                # Scrubbed as HDI sent it, before the whitespace is folded.
                text = " ".join(_scrub(str(message), credentials).split())
                text = text[:_MAX_MESSAGE_CHARS]
                found.append(f"{path}: {text}" if path else text)
    return found


def _checked(step: str, sets: list[_ResultSet], credentials: HdiCredentials) -> list[_ResultSet]:
    errors = _errors(sets, credentials)
    if errors:
        shown = errors[:_MAX_MESSAGES]
        more = len(errors) - len(shown)
        text = "; ".join(shown) + (f"; and {more} more" if more > 0 else "")
        raise HdiError(f"HDI {step} failed: {text}")
    return sets


# The temporary tables the procedures take their arguments from. All are
# created before the container lock is taken: creating one is DDL, and DDL
# commits, which would end the transaction and with it the lock.
_TEMP_TABLES = {
    "#DEPLOYED_IN": "TT_FILESFOLDERS",
    "#WORK_IN": "TT_FILESFOLDERS",
    "#GENERATION_IN": "TT_FILESFOLDERS",
    "#WRITTEN": "TT_FILESFOLDERS_CONTENT",
    "#FINISHED": "TT_FILESFOLDERS_CONTENT",
    "#DELETED": "TT_FILESFOLDERS",
    "#MADE": "TT_FILESFOLDERS",
    "#UNDEPLOYED": "TT_FILESFOLDERS",
    "#PATH_PARAMETERS": "TT_FILESFOLDERS_PARAMETERS",
}


# The OUT parameters of each procedure (return code, request id, messages,
# and a result table for the ones that list or read).
_OUT = {
    "LOCK": "?, ?, ?", "WRITE": "?, ?, ?", "DELETE": "?, ?, ?", "MAKE": "?, ?, ?",
    "READ": "?, ?, ?, ?", "LIST": "?, ?, ?, ?", "LIST_DEPLOYED": "?, ?, ?, ?",
}


def _fill(cursor: Any, name: str, rows: list[tuple[Any, ...]]) -> None:
    if rows:
        marks = ", ".join("?" for _ in rows[0])
        cursor.executemany(f"INSERT INTO {name} VALUES ({marks})", rows)


def _listing(step: str, sets: list[_ResultSet]) -> dict[str, str]:
    """``{path: sha256}`` of the files (not folders) a LIST call answered."""
    for result in sets:
        if "PATH" in result.columns and "SHA256" in result.columns:
            return {
                str(path): str(digest or "").lower()
                for path, digest in zip(
                    result.column("PATH"), result.column("SHA256"), strict=True
                )
                if not str(path).endswith("/")
            }
    raise HdiError(f"HDI {step} answered without a file list")


@dataclass(frozen=True)
class _Record:
    """What a container says about the schema it holds."""

    generation: int
    made: bool
    digest: str


def _record_bytes(generation: int, made: bool, digest: str) -> bytes:
    return json.dumps(
        {"generation": generation, "made": made, "digest": digest}, sort_keys=True
    ).encode("utf-8")


def _parse_record(sets: list[_ResultSet]) -> _Record:
    """The record a READ answered with. A record that is there and cannot be
    read is an ``HdiError``, never "no record": taking it for none would let
    an older app version deploy over a newer schema."""
    for result in sets:
        if "PATH" not in result.columns or "CONTENT" not in result.columns:
            continue
        for path, content in zip(result.column("PATH"), result.column("CONTENT"), strict=True):
            if str(path) != GENERATION_FILE:
                continue
            try:
                data = json.loads(bytes(content).decode("utf-8"))
                generation, made, digest = data["generation"], data["made"], data["digest"]
            except Exception:  # noqa: BLE001 -- whatever it is, it is not a record
                break
            if (
                type(generation) is int and generation > 0 and type(made) is bool
                and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                return _Record(generation, made, digest)
            break
    raise HdiError(
        f"the container's schema record ({GENERATION_FILE}) cannot be read. Nothing "
        "was deployed: without it this app version cannot tell whether the container "
        "holds a newer schema. Start the newest app version that ran on this "
        "container, or have the record repaired"
    )


def _error_codes(sets: list[_ResultSet]) -> set[int]:
    """The message codes of the ERROR rows of an answer (-1 for a row
    without one, which then matches no known answer)."""
    codes: set[int] = set()
    for result in sets:
        if "SEVERITY" in result.columns and "MESSAGE_CODE" in result.columns:
            codes.update(
                int(code) if code is not None else -1
                for severity, code in zip(
                    result.column("SEVERITY"), result.column("MESSAGE_CODE"), strict=True
                )
                if str(severity).upper() == "ERROR"
            )
    return codes


def _folder_listing(
    step: str, sets: list[_ResultSet], credentials: HdiCredentials
) -> dict[str, str]:
    """``{path: sha256}`` below a folder, or ``{}`` when the folder does not
    exist. A container nobody deployed to has no ``src/`` at all, and HDI
    answers that with ERROR rows, not with an empty list. "Does not exist"
    is only HDI's folder-not-found answer, recognised by its message codes
    with nothing listed; any other ERROR row is raised, so a failing listing
    is never read as an empty container."""
    codes = _error_codes(sets)
    if codes and _FOLDER_NOT_FOUND in codes and codes <= _FOLDER_NOT_FOUND_COMPANIONS:
        if not _listing(step, sets):
            return {}
    return _listing(step, _checked(step, sets, credentials))


def _record_exists(sets: list[_ResultSet], credentials: HdiCredentials) -> bool:
    """Whether the LIST of the record file found it. "Not there" is only
    HDI's file-not-found answer, recognised by its message codes; any other
    ERROR row is raised, so a failing LIST is never read as an empty
    container."""
    codes = _error_codes(sets)
    found = GENERATION_FILE in _listing("LIST", sets)
    if not codes:
        if found:
            return True
        raise HdiError("HDI LIST neither found the schema record nor said it is missing")
    if not found and _FILE_NOT_FOUND in codes and codes <= _NOT_FOUND_COMPANIONS:
        return False
    _checked("LIST", sets, credentials)
    raise HdiError("HDI LIST of the schema record failed")


def deploy_files(
    credentials: HdiCredentials,
    files: Mapping[str, str],
    *,
    generation: int,
    allow_drop: bool = False,
    connect: Callable[[HdiCredentials], Any] = _connect,
) -> DeployResult:
    """Bring the container to exactly ``files``; blocking (see :func:`deploy`).

    Everything happens under HDI's container lock (``<schema>#DI.LOCK``),
    which is what serialises two app instances that start together: the
    second waits up to :data:`LOCK_WAIT_MS` for the first and then reads a
    container that is current. Under the lock:

    1. The container's record is read (generation, made or not, digest of
       the files). A record that exists and cannot be read, or a listing
       that fails for any reason but "no such file", is an ``HdiError``:
       the guard fails closed. A HIGHER generation whose make finished and
       whose digest is what is deployed: nothing at all is done
       (``newer_generation``). A higher generation that is not made, or
       whose schema is not what is deployed: ``HdiError``, nothing changed;
       an older version neither deploys nor runs on a half-made schema.
    2. Same files deployed already (paths and SHA-256): nothing is made;
       the record is brought to "this generation, made", and files a failed
       deploy left in the file system are removed.
    3. Otherwise the record is written as "this generation, not made",
       the files are written and made, and the record becomes "made";
       what is deployed below :data:`ROOT` and not in ``files`` is
       undeployed -- but an ``.hdbtable`` only with ``allow_drop``, else
       the deploy is refused before anything is written.

    HDI commits each of its calls itself, so a failure is not undone by the
    rollback that follows (it only releases the lock). The deployed objects
    are safe all the same -- a make is all or nothing -- and the file system
    may be left changed; step 3 therefore deletes exactly the files that
    are there and not generated, and undeploys exactly what is deployed and
    not generated, whatever the two look like. ``HdiError`` carries HDI's
    messages. Nothing is retried: a refused artifact is refused again, and
    the lock already covers the one case that waiting solves.
    """
    if not _SCHEMA_RE.fullmatch(credentials.schema):
        raise HdiError("the HDI container schema name is not a plain identifier")
    if any(not path.startswith(ROOT) or path.endswith("/") for path in files):
        raise HdiError(f"every artifact must be a file below {ROOT}")
    if type(generation) is not int or generation < 1:
        raise HdiError("the schema generation must be a positive integer")
    api = f'"{credentials.schema}#DI"'
    wanted = {path: content.encode("utf-8") for path, content in files.items()}
    try:
        connection = connect(credentials)
    except Exception as exc:  # noqa: BLE001 -- the driver's text names the host
        code = getattr(exc, "errorcode", None)
        raise HdiError(
            "could not connect to the HDI container as its design-time user "
            f"({type(exc).__name__}, code {code})"
        ) from None
    try:
        cursor = connection.cursor()
        for name, like in _TEMP_TABLES.items():
            cursor.execute(f"CREATE LOCAL TEMPORARY COLUMN TABLE {name} LIKE _SYS_DI.{like}")
        _fill(cursor, "#DEPLOYED_IN", [(ROOT,)])
        _fill(cursor, "#WORK_IN", [(ROOT,)])
        _fill(cursor, "#GENERATION_IN", [(GENERATION_FILE,)])
        connection.commit()  # the lock below is taken in a transaction of its own

        def call(step: str, arguments: str) -> list[_ResultSet]:
            statement = (
                f"CALL {api}.{step}({arguments}_SYS_DI.T_NO_PARAMETERS, {_OUT[step]})"
            )
            return _checked(step, _call(cursor, statement), credentials)

        def folder(step: str, arguments: str) -> dict[str, str]:
            statement = (
                f"CALL {api}.{step}({arguments}_SYS_DI.T_NO_PARAMETERS, {_OUT[step]})"
            )
            return _folder_listing(step, _call(cursor, statement), credentials)

        try:
            call("LOCK", f"{LOCK_WAIT_MS}, ")
        except HdiError:
            raise
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "errorcode", None) == _LOCK_TIMEOUT_CODE:
                raise HdiError(
                    "another deployment held the HDI container lock for more than "
                    f"{LOCK_WAIT_MS // 1000} s"
                ) from None
            raise

        # A file that is not there is an ERROR row of READ; LIST says first.
        recorded: _Record | None = None
        if _record_exists(_call_raw(cursor, api, "#GENERATION_IN"), credentials):
            recorded = _parse_record(call("READ", "#GENERATION_IN, "))
        deployed = folder("LIST_DEPLOYED", "#DEPLOYED_IN, ")
        if recorded is not None and recorded.generation > generation:
            # A newer app version's container. Run on it only when it really
            # holds that version's schema: its make finished, and what is
            # deployed is what that make recorded.
            if not recorded.made or state_digest(deployed) != recorded.digest:
                raise HdiError(
                    f"the container holds an unfinished or changed deployment of "
                    f"schema generation {recorded.generation}; this app version is "
                    f"generation {generation}. Nothing was deployed and the app does "
                    f"not start on a schema nobody finished: start the version of "
                    f"generation {recorded.generation} (or newer) to complete it"
                )
            connection.rollback()
            return DeployResult(
                tuple(wanted), (), changed=False, newer_generation=recorded.generation
            )

        stale = sorted(path for path in deployed if path not in wanted)
        same = not stale and all(
            deployed.get(path) == hashlib.sha256(content).hexdigest()
            for path, content in wanted.items()
        )
        dropped = [path for path in stale if path.endswith(".hdbtable")]
        if dropped and not allow_drop:
            names = [Path(path).stem for path in dropped]
            more = f" and {len(names) - 5} more" if len(names) > 5 else ""
            raise HdiError(
                f"this deployment would drop the table(s) {', '.join(names[:5])}{more} "
                f"with every row. Refused: set {ALLOW_DROP_ENV}=true for one start "
                "if that is intended"
            )

        # The design-time file system can differ from what is deployed: a
        # failed make leaves the files it wrote and misses the ones it
        # deleted. So: delete what is there and not generated (HDI refuses
        # to delete a file that is not there), undeploy what is deployed
        # and not generated.
        work = folder("LIST", "#WORK_IN, ")
        leftover = sorted(path for path in work if path not in wanted)
        _fill(cursor, "#DELETED", [(path,) for path in leftover])
        digest = state_digest({p: hashlib.sha256(c).hexdigest() for p, c in wanted.items()})
        finished = _Record(generation, True, digest)
        if same:
            # The schema is this generation's already; at most the record
            # (a container from before records, a make that finished without
            # its record, two generations with equal files) and the file
            # system need bringing up to date.
            if recorded != finished:
                _fill(cursor, "#WRITTEN", [
                    ("meta/", None), (GENERATION_FILE, _record_bytes(generation, True, digest)),
                ])
                call("WRITE", "#WRITTEN, ")
            if leftover:
                call("DELETE", "#DELETED, ")
            connection.commit()
            return DeployResult(tuple(wanted), (), changed=False)

        # The record first, as "not made": from here on the container is no
        # older app version's to deploy to or to run on, whatever happens to
        # the make below. It becomes "made" only after the make succeeded.
        _fill(cursor, "#WRITTEN", [
            ("meta/", None), (GENERATION_FILE, _record_bytes(generation, False, digest)),
            (ROOT, None), *wanted.items(),
        ])
        _fill(cursor, "#MADE", [(path,) for path in wanted])
        _fill(cursor, "#UNDEPLOYED", [(path,) for path in stale])
        _fill(cursor, "#FINISHED", [
            (GENERATION_FILE, _record_bytes(generation, True, digest)),
        ])
        call("WRITE", "#WRITTEN, ")
        if leftover:
            call("DELETE", "#DELETED, ")
        call("MAKE", "#MADE, #UNDEPLOYED, #PATH_PARAMETERS, ")
        call("WRITE", "#FINISHED, ")
        connection.commit()
        return DeployResult(tuple(wanted), tuple(stale), changed=True)
    except HdiError:
        _quietly(connection.rollback)
        raise
    except Exception as exc:  # noqa: BLE001 -- never the driver's own text
        _quietly(connection.rollback)
        code = getattr(exc, "errorcode", None)
        raise HdiError(
            f"the HDI deployment failed ({type(exc).__name__}, code {code})"
        ) from None
    finally:
        _quietly(connection.close)


def _call_raw(cursor: Any, api: str, paths: str) -> list[_ResultSet]:
    """LIST of ``paths`` without the error check: a path that does not exist
    is an ERROR row there, and for the generation file that is an answer."""
    return _call(cursor, f"CALL {api}.LIST({paths}, _SYS_DI.T_NO_PARAMETERS, ?, ?, ?, ?)")


def _quietly(action: Callable[[], Any]) -> None:
    try:
        action()
    except Exception:  # noqa: BLE001 -- the first failure is the one to report
        logger.debug("HDI connection cleanup failed", exc_info=False)


def allow_drop_from_environment() -> bool:
    """``HANA_HDI_ALLOW_DROP`` is exactly ``true``; anything else is no."""
    return os.environ.get(ALLOW_DROP_ENV, "") == "true"


async def deploy(credentials: HdiCredentials, metadata: MetaData) -> DeployResult:
    """Generate the artifacts of ``metadata`` and deploy them as this code's
    :data:`HANA_SCHEMA_GENERATION`.

    ``hdbcli`` is a blocking driver and a first make takes seconds, so the
    work runs in a thread and the event loop stays free. Idempotent, also
    for two app instances starting together (see :func:`deploy_files`).
    """
    files = artifacts(metadata)
    result = await asyncio.to_thread(
        deploy_files,
        credentials,
        files,
        generation=HANA_SCHEMA_GENERATION,
        allow_drop=allow_drop_from_environment(),
    )
    if result.newer_generation is not None:
        logger.warning(
            "HDI: the container holds schema generation %d, this app version is "
            "generation %d. Nothing was deployed; the app runs on the newer schema",
            result.newer_generation, HANA_SCHEMA_GENERATION,
        )
    elif result.changed:
        logger.info(
            "HDI: %d artifacts deployed as generation %d, %d undeployed",
            len(result.deployed), HANA_SCHEMA_GENERATION, len(result.undeployed),
        )
        for path in result.undeployed:
            # Dropping an object is worth a line of its own in the log.
            logger.warning("HDI: undeployed %s (no longer part of the models)", path)
    else:
        logger.info("HDI: the container already holds the %d artifacts", len(result.deployed))
    return result

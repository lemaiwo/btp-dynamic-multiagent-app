"""Go/no-go probe for InA (``/sap/bw/ina``) on an OnPremise destination.

Run inside the app container (it needs the destination and connectivity
bindings), as the technical user of the destination:

    cf ssh pydantic-agent -c 'cd /home/vcap/app; unset PYTHONHOME; \\
        export LD_LIBRARY_PATH=/home/vcap/deps/0/lib:/home/vcap/deps/0/python/lib \\
        PYTHONPATH=/home/vcap/deps/0/lib/python3.13/site-packages; \\
        /home/vcap/deps/0/bin/python3 scripts/probe_ina.py --destination <name>'

(the buildpack's profile script does not work in an ssh shell; a version of
this file that is not in the droplet yet is piped in with ``cf ssh
pydantic-agent -c 'cat > /home/vcap/app/scripts/probe_ina_x.py' <
scripts/probe_ina.py`` and removed afterwards).

Three steps, one line each: ``GetServerInfo`` (is InA there, which services
and how many capabilities it reports), a metadata catalogue request (how many
queries it lists and the first names; ``--skip-catalog`` leaves it out), and a
metadata request for one query when ``--query`` names it (how many dimensions
and measures). Prints statuses, counts, keys and object NAMES, never a
credential, a token, a URL or a value of the data. Reuses the binding,
destination and proxy helpers of ``probe_odata_connectivity.py``; nothing of
the app is imported.

Recording an exchange (``--dump DIR``, added 2026-11-04 for the first real
SIA exchange): the RAW JSON of ``GetServerInfo`` (``server_info.json``), of
the query's describe answer (``describe.json``) and, with ``--run``, of one
``Analytics`` read (``<label>.request.json`` and ``<label>.response.json``,
``--label`` defaults to ``run``) is written to DIR; stdout still shows only
statuses, sizes and counts. ``--run`` sends the read the app's
``agents.ina.protocol.analytics_request`` builds (mirrored here, not
imported) on a fresh ``InstanceId``: the first measure of the describe
answer (or ``--measure``), no filter, ``top=1`` (``RowTo`` 2), the
dimensions of ``--group-by DIM`` (repeatable) on Rows, each listing its key
(and its text, once a submit answer named one) and hiding its totals, and
every ``--variable NAME=VALUE`` (repeatable) as a single-value variable in
the form ``plan_run`` sends. Switches for SAP's own InA client's form:
``--variable-form sap`` names each value by the key of the variable's
dimension (as the describe answer states a value), ``--structure NAME``
puts the real measure structure on Columns, ``--submit separate`` sends the
app's ``variable_submit_request`` before the read, on the same instance, and
takes the text attributes from its answer (``batch`` sends the two as one
``{"Batch": [...]}`` instead), ``--top N`` sets the rows asked for and
``--close`` releases the instance afterwards (firefly's ``Close`` action).
On SIA (2026-11-04) the read alone, also with both mandatory variables and
in either variable form, was refused ("all mandatory variables need
values"); the batch was answered with the submit's ``Cube`` alone, no grid;
``--submit separate --variable-form sap --structure <real name>`` was
answered with a grid (run 5); with Rows the read needed each row's key and
text as ``Attributes`` and the totals hidden (runs 6 to 8): the sequence the
app sends since 2.37.1 (``agents/ina/client.py``).

``--order NAME=asc|desc`` (repeatable; added 2026-11-05) sorts the run by
the measure or a ``--group-by`` dimension with the ``Sort`` ``plan_run``
builds (in the submit and the read), and the read then prints one ``ORDER``
line: whether the first column came back in descending or ascending order,
the rows and ``TupleCount`` / ``TupleCountTotal``, never a value.
``--sort-form`` tries other readings of a measure sort (``structure``: the
measure structure as its ``Dimension``, 2.37.1's form; ``rows``: the first
row dimension). On SIA (2026-11-05) the default form, a measure without
``Dimension`` as SAP's own client writes it, gave the rows in descending
value order with the full ``TupleCountTotal``, and a ``MemberKey`` sort of
the dimension descending key order.

A run answer holds DATA: DIR must lie under the system temp directory
(``tempfile.gettempdir()``, ``/tmp`` in the container; anything else is
refused), is created ``0700`` and every file ``0600``; ``--label`` is
``[A-Za-z0-9_-]``, at most 40 characters. Copy the files back with ``cf ssh pydantic-agent -c 'cat
/tmp/<dir>/<file>'`` into the local, gitignored ``docs/evidence/`` and
delete them in the container. A fixture made from a dump is sanitised first
(session ids, and every data value and member text of a grid, replaced), as
``tests/fixtures/ina/describe_sia.json`` is. Example::

    python scripts/probe_ina.py --destination <name> --skip-catalog \\
        --query 2CCMMPURORDQUERY --dump /tmp/ina --run --submit separate \\
        --variable-form sap --structure ELTUIDSTRCMMPURORDQUERY \\
        --variable '!CDS_P_P_DISPLAYCURRENCY=EUR' \\
        --variable '!CDS_P_P_DATEFUNCTION=YEARTODATE' --label run-5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_odata_connectivity import (  # noqa: E402
    ProbeError,
    _find_destination,
    _token,
    build_request_url,
    describe_bindings,
    destination_headers,
    proxy_headers,
    read_bindings,
)

INA_INFO = "/sap/bw/ina/GetServerInfo"
INA_RESPONSE = "/sap/bw/ina/GetResponse"
MAX_NAMES = 25
MAX_BODY = 4 * 1024 * 1024


CLIENT_INFO = {"Version": "1.0", "Identifier": "btp-dynamic-multiagent-probe",
               "Component": "probe"}


def _envelope(metadata: dict[str, Any], capabilities: list[str]) -> dict[str, Any]:
    body = {"ClientInfo": CLIENT_INFO, "Metadata": {"Capabilities": capabilities,
                                                   "Context": "Analytics", **metadata},
            "Options": ["StatefulServer"]}
    return body


def _catalog_requests(capabilities: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Catalogue requests to try, most likely first: the ``$$DataSource$$``
    catalogue (what SAC sends), then a wildcard query list."""
    return [
        ("catalog", _envelope({
            "Expand": ["Cubes"],
            "DataSource": {"ObjectName": "$$DataSource$$", "PackageName": "", "Type": "Catalog"},
            "Language": "EN",
        }, capabilities)),
        ("catalog-notype", _envelope({
            "Expand": ["Cubes"],
            "DataSource": {"ObjectName": "$$DataSource$$", "Type": "Catalog"},
            "Language": "EN",
        }, capabilities)),
        ("query-wildcard", _envelope({
            "Expand": ["Cubes"],
            "DataSource": {"ObjectName": "*", "Type": "Query"},
            "Language": "EN",
        }, capabilities)),
    ]


def _query_request(name: str, capabilities: list[str]) -> dict[str, Any]:
    return _envelope({
        "DataSource": {"ObjectName": name, "Type": "Query"},
        "Expand": ["Cube", "Axis?Name=Rows", "Axis?Name=Columns", "Axis?Name=Filter",
                   "Variables", "DynamicFilter"],
        "Language": "EN",
    }, capabilities)


def _name(value: Any) -> str:
    text = str(value) if value is not None else ""
    return "".join(ch for ch in text if ch.isprintable())[:80]


def _cubes(payload: Any) -> list[str]:
    """The query names of a catalogue answer, whatever nesting the server used."""
    found: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 8 or len(found) >= 500:
            return
        if isinstance(node, dict):
            if "Cubes" in node and isinstance(node["Cubes"], list):
                for cube in node["Cubes"]:
                    if isinstance(cube, dict):
                        name = cube.get("ObjectName") or cube.get("Name")
                        if name:
                            found.append(_name(name))
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(payload, 0)
    return found


def _count(payload: Any, key: str) -> int:
    total = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal total
        if depth > 8:
            return
        if isinstance(node, dict):
            if key in node and isinstance(node[key], list):
                total += len(node[key])
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(payload, 0)
    return total


async def _send(
    client: httpx.AsyncClient, method: str, url: str, headers: dict[str, str],
    body: Any | None, csrf: dict[str, str],
) -> tuple[int, str, Any, bytes]:
    """One call through the proxy client. ICF wants a CSRF token on a POST:
    the GET fetches it (``X-CSRF-Token: Fetch``, the session cookie stays in
    the client), the POST sends it back; ``csrf`` carries it between calls."""
    sent = dict(headers)
    if method == "GET":
        sent["X-CSRF-Token"] = "Fetch"
        response = await client.get(url, headers=sent)
        token = response.headers.get("x-csrf-token", "")
        if token and token.lower() != "required":
            csrf["token"] = token
    else:
        if csrf.get("token"):
            sent["X-CSRF-Token"] = csrf["token"]
        response = await client.post(url, headers=sent, json=body)
    ctype = response.headers.get("content-type", "").split(";")[0].strip()[:60]
    payload: Any = None
    if len(response.content) <= MAX_BODY and "json" in ctype:
        try:
            payload = response.json()
        except ValueError:
            payload = None
    return response.status_code, ctype, payload, response.content


LABEL = re.compile(r"[A-Za-z0-9_-]{1,40}")
DUMP_REFUSED = "--dump must name a directory under the system temp directory"


def _label(value: str) -> str:
    """``--label``: it becomes part of a file name."""
    if not LABEL.fullmatch(value):
        raise argparse.ArgumentTypeError("--label takes 1 to 40 of A-Z a-z 0-9 _ -")
    return value


def _dump_dir(directory: str) -> Path:
    """The dump directory, which must lie under the system temp directory:
    an answer holds a session ticket or data and is never written next to
    the code or into a synced folder by mistake."""
    root = Path(tempfile.gettempdir()).resolve()
    target = Path(directory).resolve()
    if target == root or root not in target.parents:
        raise ProbeError(DUMP_REFUSED)
    return target


def _dump(directory: str, name: str, raw: bytes) -> None:
    """Write ``raw`` as it came to ``directory/name`` (directory 0700, file
    0600); print name and size only."""
    if not directory:
        return
    target = _dump_dir(directory)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    fd = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, raw)
    finally:
        os.close(fd)
    print(f"DUMP {name} bytes={len(raw)}")


def _first_measure(cube: Any) -> str:
    """The first measure of a describe answer's ``Cube``, read as the app's
    ``read_description`` reads it: the members of the measure structure
    (``DimensionType`` 2 or the name ``Measures``), ``<structure>.KEY`` else
    ``Name``."""
    dimensions = cube.get("Dimensions") if isinstance(cube, dict) else None
    for raw in dimensions if isinstance(dimensions, list) else []:
        if not isinstance(raw, dict) or not isinstance(raw.get("Members"), list):
            continue
        kind = raw.get("DimensionType")
        if not ((isinstance(kind, int) and not isinstance(kind, bool) and kind == 2)
                or raw.get("Name") == "Measures"):
            continue
        structure = raw.get("Name") if isinstance(raw.get("Name"), str) else ""
        for member in raw["Members"]:
            if isinstance(member, dict):
                name = member.get(f"{structure}.KEY", member.get("Name"))
                if isinstance(name, str) and name:
                    return name
    return ""


def _variable_entry(name: str, value: str, field: str = "") -> dict[str, Any]:
    """One single-value variable as ``plan_run`` sends it (``FieldName`` the
    variable's name), or with ``field`` in the form SAP's describe answer
    states a variable's value (``FieldName`` the key of the variable's
    dimension, ``IsExcluding`` false)."""
    element: dict[str, Any] = {"Comparison": "=", "Low": value, "IsExcluding": False}
    return {"Name": name, "Values": {"Selection": {"SetOperand": {
        "FieldName": field or name, "Elements": [element]}}}}


def _variable_fields(cube: Any) -> dict[str, str]:
    """``<DimensionReference.Name>.KEY`` per variable of a describe answer."""
    raw = cube.get("Variables") if isinstance(cube, dict) else None
    out: dict[str, str] = {}
    for variable in raw if isinstance(raw, list) else []:
        ref = variable.get("DimensionReference") if isinstance(variable, dict) else None
        if isinstance(ref, dict) and isinstance(ref.get("Name"), str):
            out[str(variable.get("Name"))] = f"{ref['Name']}.KEY"
    return out


HIDE_TOTALS = [{"Result": "Members", "Visibility": "Visible"},
               {"Result": "Total", "Visibility": "Hidden"}]


def _row(name: str, texts: dict[str, str] | None, submit: bool) -> dict[str, Any]:
    """A Rows entry: the read lists the key (``<dim>.KEY``) and the text the
    submit answer named and hides totals (firefly's
    ``QInAField.exportComponentWithStructure``; a dimension's
    ``ResultSetFields`` was ignored by SIA, run 7); the submit keeps the
    plain form."""
    entry: dict[str, Any] = {"Name": name, "Axis": "Rows"}
    if not submit:
        names = [f"{name}.KEY", *([texts[name]] if texts and texts.get(name) else [])]
        entry["Attributes"] = [{"Name": n, "Obtainability": "Always"} for n in names]
        entry["ResultStructure"] = [dict(x) for x in HIDE_TOTALS]
    return entry


SORT_FORMS = ("measure", "structure", "rows")


def _sort(order: list[tuple[str, str]], measure: str, structure: str,
          group_by: list[str], form: str = "measure") -> list[dict[str, Any]]:
    """The ``Sort`` of a run as ``plan_run`` builds it: the measure as
    firefly's ``QInASort.exportMeasureSorting`` writes it (``SortType``
    ``Measure``, ``MeasureName``, ``Direction``, no ``Dimension``), any other
    name as a row dimension by its key (``SortType`` ``MemberKey`` with its
    ``Dimension``). ``form`` tries the other readings of a measure sort:
    ``structure`` names the measure structure as its ``Dimension`` (what the
    app sent up to 2.37.1), ``rows`` the first row dimension."""
    out: list[dict[str, Any]] = []
    for name, direction in order:
        wire = "Desc" if direction == "desc" else "Asc"
        if name != measure:
            out.append({"Dimension": name, "SortType": "MemberKey", "Direction": wire})
            continue
        entry: dict[str, Any] = {"SortType": "Measure", "MeasureName": name, "Direction": wire}
        if form == "structure":
            entry["Dimension"] = structure
        elif form == "rows" and group_by:
            entry["Dimension"] = group_by[0]
        out.append(entry)
    return out


def _run_request(capabilities: list[str], query: str, measure: str,
                 variables: list[tuple[str, str]], top: int = 1,
                 structure: str = "Measures",
                 fields: dict[str, str] | None = None,
                 group_by: list[str] | None = None,
                 instance: str = "",
                 texts: dict[str, str] | None = None,
                 submit: bool = False,
                 order: list[tuple[str, str]] | None = None,
                 sort_form: str = "measure") -> dict[str, Any]:
    """The read ``analytics_request`` builds for one measure, the dimensions
    of ``group_by`` on Rows, no filter, the ``Sort`` of ``order`` -- or with
    ``submit`` the ``variable_submit_request`` (mirrored from
    ``agents/ina/protocol.py``)."""
    feature: dict[str, Any] = {
        "ResultFormat": "Version2",
        "ResultEncoding": "None",
        "SubSetDescription": {"RowFrom": 0, "RowTo": top + 1,
                              "ColumnFrom": 0, "ColumnTo": -1},
        "UseDefaultAttributeKey": False,
    }
    if submit:
        feature["MaxResultRecords"] = top + 1
    definition: dict[str, Any] = {
        "Dimensions": [*(_row(name, texts, submit) for name in group_by or []), {
            "Name": structure,
            "Axis": "Columns",
            "Members": [{"Name": measure, "Visibility": "Visible"}],
        }],
        "ResultSetFeatureRequest": feature,
    }
    if order:
        definition["Sort"] = _sort(order, measure, structure, group_by or [], sort_form)
    if variables:
        definition["Variables"] = [_variable_entry(n, v, (fields or {}).get(n, ""))
                                   for n, v in variables]
    source = {"ObjectName": query, "Type": "Query"}
    if instance:
        source["InstanceId"] = instance
    analytics: dict[str, Any] = {
        "Capabilities": capabilities,
        "DataSource": source,
        "Definition": definition,
        "Language": "EN",
    }
    if submit:
        analytics["ProcessingDirectives"] = {"ProcessingStep": "VariableSubmit"}
    return {"ClientInfo": CLIENT_INFO, "Analytics": analytics, "Options": ["StatefulServer"]}


def _texts(cube: Any, group_by: list[str]) -> dict[str, str]:
    """The text attribute of each of ``group_by`` a submit answer's ``Cube``
    names, as ``agents.ina.protocol.text_attributes`` takes it (one printable
    line of at most 128 characters, ``<dim>.``, not the key)."""
    raw = cube.get("Dimensions") if isinstance(cube, dict) else None
    out: dict[str, str] = {}
    for dim in raw if isinstance(raw, list) else []:
        name = dim.get("Name") if isinstance(dim, dict) else None
        text = dim.get("TextAttribute") if isinstance(dim, dict) else None
        if (isinstance(name, str) and name in group_by and name not in out
                and isinstance(text, str) and 0 < len(text) <= 128 and text.isprintable()
                and text.startswith(f"{name}.")
                and text not in (f"{name}.KEY", dim.get("KeyAttribute"))):
            out[name] = text
    return out


def _batch_with_submit(body: dict[str, Any], instance: str) -> dict[str, Any]:
    """``{"Batch": [submit, read]}``: a copy of the read with
    ``ProcessingDirectives.ProcessingStep = VariableSubmit`` first, then the
    read, both on one ``InstanceId`` -- the stateful BW variable flow of SAP's
    own InA client (firefly, ``sap/sac/df/firefly/ff4400.olap.providers``)."""
    read = json.loads(json.dumps(body))
    read["Analytics"]["DataSource"]["InstanceId"] = instance
    submit = json.loads(json.dumps(read))
    submit["Analytics"]["ProcessingDirectives"] = {"ProcessingStep": "VariableSubmit"}
    return {"Batch": [submit, read]}


def _close_request(instance: str) -> dict[str, Any]:
    """firefly's ``Close`` of a query instance (``InAQMgrShutdownAction``),
    as ``agents.ina.protocol.close_request`` builds it."""
    return {"Analytics": {"Actions": [{"Type": "Close", "DataSources": [instance]}]},
            "Options": ["StatefulServer"]}


def _grids(payload: Any) -> list[int]:
    """Grids per answer: one entry, or one per element of a ``Batch``."""
    answers = payload.get("Batch") if isinstance(payload, dict) and "Batch" in payload \
        else [payload]
    out = []
    for answer in answers if isinstance(answers, list) else []:
        grids = answer.get("Grids") if isinstance(answer, dict) else None
        out.append(len(grids) if isinstance(grids, list) else 0)
    return out


def _parse_order(values: list[str]) -> list[tuple[str, str]]:
    """``--order NAME=asc|desc`` (repeatable), in the order given."""
    out: list[tuple[str, str]] = []
    for item in values:
        name, sep, direction = item.partition("=")
        if not sep or not name or direction not in ("asc", "desc"):
            raise ProbeError("--order takes NAME=asc or NAME=desc")
        out.append((name, direction))
    return out


def _seen_order(payload: Any) -> str:
    """How the first column of the read's grid is ordered (``desc``,
    ``asc``, ``unordered``, ``-`` without numbers) and the row counts of its
    Rows axis -- never a value."""
    grids = payload.get("Grids") if isinstance(payload, dict) else None
    grid = grids[0] if isinstance(grids, list) and grids and isinstance(grids[0], dict) else {}
    cells = grid.get("Cells") if isinstance(grid.get("Cells"), dict) else {}
    raw = cells.get("Values")
    values = raw.get("Values") if isinstance(raw, dict) else raw
    sizes = grid.get("CellArraySizes")
    width = sizes[1] if isinstance(sizes, list) and len(sizes) == 2 \
        and isinstance(sizes[1], int) and sizes[1] > 0 else 1
    column = [v for v in (values if isinstance(values, list) else [])[::width]
              if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(column) < 2:
        verdict = "-"
    elif all(a >= b for a, b in zip(column, column[1:])):
        verdict = "desc"
    elif all(a <= b for a, b in zip(column, column[1:])):
        verdict = "asc"
    else:
        verdict = "unordered"
    rows = next((a for a in grid.get("Axes") or [] if isinstance(a, dict)
                 and a.get("Type") == "Rows"), {})
    return (f"order={verdict} rows={len(column)} tuple_count={_name(rows.get('TupleCount'))} "
            f"tuple_count_total={_name(rows.get('TupleCountTotal'))}")


def _parse_variables(values: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in values:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise ProbeError("--variable takes NAME=VALUE")
        out.append((name, value))
    return out


def _messages(payload: Any) -> str:
    """Number and type of an answer's messages (the texts are in the dump)."""
    if isinstance(payload, dict) and isinstance(payload.get("Batch"), list):
        return "|".join(_messages(a) for a in payload["Batch"])
    raw = payload.get("Messages") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return "none"
    return ";".join(f"{_name(m.get('Number'))}/{_name(m.get('Type'))}"
                    for m in raw[:10] if isinstance(m, dict)) or "none"


def _top_keys(payload: Any) -> str:
    if isinstance(payload, dict):
        return ",".join(_name(k) for k in list(payload)[:12])
    return type(payload).__name__


async def run(args: argparse.Namespace, environ: dict[str, str]) -> int:
    try:
        if args.dump:
            _dump_dir(args.dump)  # refused before anything is sent
        cfg = read_bindings(environ.get("VCAP_SERVICES", ""))
        describe_bindings(cfg)
        async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as client:
            dest = await _find_destination(client, cfg.destination, args.destination, None)
            app_token = await _token(
                client, cfg.connectivity.oauth, {"grant_type": "client_credentials"},
                "connectivity token",
            )
        print(f"DESTINATION proxy_type={_name(dest.proxy_type)} "
              f"authentication={_name(dest.authentication)} "
              f"location_id={'yes' if dest.location_id else 'no'}")
        if dest.proxy_type.lower() != "onpremise":
            raise ProbeError("this probe routes through the connectivity proxy: "
                             "the destination must be OnPremise")
        proxy_url = f"http://{cfg.connectivity.proxy_host}:{cfg.connectivity.proxy_port}"
        csrf: dict[str, str] = {}
        proxied = httpx.AsyncClient(
            proxy=proxy_url, timeout=args.timeout, trust_env=False, follow_redirects=False
        )
        headers = {
            **proxy_headers("technical", app_token=app_token, user_jwt=None, exchanged=None,
                            location_id=dest.location_id),
            **destination_headers(dest, "technical"),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        # 1. GetServerInfo
        url = build_request_url(dest.url, INA_INFO, dest.queries)
        status, ctype, payload, raw = await _send(proxied, "GET", url, headers, None, csrf)
        _dump(args.dump, "server_info.json", raw)
        services = payload.get("Services") if isinstance(payload, dict) else None
        names = [_name(s.get("Service")) for s in services if isinstance(s, dict)] \
            if isinstance(services, list) else []
        caps = sum(len(s.get("Capabilities") or []) for s in services if isinstance(s, dict)) \
            if isinstance(services, list) else 0
        analytics = next((s for s in (services or []) if isinstance(s, dict)
                          and s.get("Service") == "Analytics"), {})
        capabilities = [c.get("Capability") if isinstance(c, dict) else str(c)
                        for c in (analytics.get("Capabilities") or [])]
        capabilities = [c for c in capabilities if isinstance(c, str)]
        verdict = "PASS" if status == 200 and isinstance(payload, dict) else "FAIL"
        print(f"{verdict} GetServerInfo: HTTP {status} {ctype} keys={_top_keys(payload)} "
              f"services={names} capabilities={caps} csrf={'yes' if csrf.get('token') else 'no'}")
        if verdict == "FAIL":
            return 1
        if not args.skip_catalog:
            await _catalogue(proxied, dest, headers, csrf, capabilities)
        url = build_request_url(dest.url, INA_RESPONSE, dest.queries)
        # 3. one query's metadata
        cube: Any = None
        if args.query and not args.no_describe:
            status, ctype, payload, raw = await _send(
                proxied, "POST", url, headers, _query_request(args.query, capabilities), csrf)
            _dump(args.dump, "describe.json", raw)
            verdict = "PASS" if status == 200 and isinstance(payload, dict) else "FAIL"
            print(f"{verdict} query {_name(args.query)}: HTTP {status} {ctype} "
                  f"keys={_top_keys(payload)} dimensions={_count(payload, 'Dimensions')} "
                  f"measures={_count(payload, 'Members')} variables={_count(payload, 'Variables')}"
                  f" messages={_messages(payload)}")
            cube = payload.get("Cube") if isinstance(payload, dict) else None
        # 4. one Analytics read, as the app's run sends it
        if args.query and args.run:
            measure = args.measure or _first_measure(cube)
            if not measure:
                raise ProbeError("no measure: describe the query or pass --measure")
            instance = "probe" + os.urandom(8).hex()
            group_by = args.group_by or []
            request: dict[str, Any] = {
                "top": args.top,
                "structure": args.structure or "Measures",
                "fields": _variable_fields(cube) if args.variable_form == "sap" else None,
                "group_by": group_by,
                "instance": instance,
                "order": _parse_order(args.order or []),
                "sort_form": args.sort_form,
            }
            variables = _parse_variables(args.variable or [])
            body = _run_request(capabilities, args.query, measure, variables, **request)
            if args.submit == "batch":
                body = _batch_with_submit(body, instance)
            elif args.submit == "separate":
                # The submit alone first, on the InstanceId the read then uses;
                # the read asks for the texts its answer names.
                submit = _run_request(capabilities, args.query, measure, variables,
                                      submit=True, **request)
                _dump(args.dump, f"{args.label}.submit.request.json",
                      json.dumps(submit, indent=1).encode("utf-8"))
                status, ctype, payload, raw = await _send(
                    proxied, "POST", url, headers, submit, csrf)
                _dump(args.dump, f"{args.label}.submit.response.json", raw)
                cube_out = payload.get("Cube") if isinstance(payload, dict) else None
                body = _run_request(capabilities, args.query, measure, variables,
                                    texts=_texts(cube_out, group_by), **request)
                state = cube_out.get("ServerState") if isinstance(cube_out, dict) else None
                shown = _name(state.get("State")) if isinstance(state, dict) else "-"
                print(f"{'PASS' if status == 200 else 'FAIL'} submit {_name(args.label)}: "
                      f"HTTP {status} {ctype} bytes={len(raw)} keys={_top_keys(payload)} "
                      f"server_state={shown} messages={_messages(payload)}")
            _dump(args.dump, f"{args.label}.request.json",
                  json.dumps(body, indent=1).encode("utf-8"))
            status, ctype, payload, raw = await _send(proxied, "POST", url, headers, body, csrf)
            _dump(args.dump, f"{args.label}.response.json", raw)
            grids = _grids(payload)
            ok = status == 200 and bool(grids) and grids[-1] > 0
            print(f"{'PASS' if ok else 'FAIL'} run {_name(args.label)}: HTTP {status} {ctype} "
                  f"bytes={len(raw)} keys={_top_keys(payload)} grids={grids} "
                  f"variables={len(args.variable or [])} messages={_messages(payload)}")
            if args.order:
                print(f"ORDER {_name(args.label)}: {_seen_order(payload)}")
            if args.close:
                close = _close_request(instance)
                _dump(args.dump, f"{args.label}.close.request.json",
                      json.dumps(close, indent=1).encode("utf-8"))
                status, ctype, payload, raw = await _send(
                    proxied, "POST", url, headers, close, csrf)
                _dump(args.dump, f"{args.label}.close.response.json", raw)
                print(f"{'PASS' if status == 200 else 'FAIL'} close {_name(args.label)}: "
                      f"HTTP {status} {ctype} bytes={len(raw)} keys={_top_keys(payload)} "
                      f"messages={_messages(payload)}")
        await proxied.aclose()
        return 0
    except ProbeError as exc:
        print(f"ERROR: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - the text may hold a URL or a token
        print(f"ERROR: unexpected {type(exc).__name__}")
        return 2


async def _catalogue(
    proxied: httpx.AsyncClient, dest: Any, headers: dict[str, str], csrf: dict[str, str],
    capabilities: list[str],
) -> None:
    """Steps 1b and 2: GetCatalog and the catalogue requests."""
    # 1b. GetCatalog: the catalogue endpoint next to GetResponse
    url = build_request_url(dest.url, "/sap/bw/ina/GetCatalog", dest.queries)
    status, ctype, payload, _raw = await _send(proxied, "GET", url, headers, None, csrf)
    cat_cubes = _cubes(payload)
    print(f"{'PASS' if status == 200 else 'FAIL'} GetCatalog: HTTP {status} {ctype} "
          f"keys={_top_keys(payload)} cubes={len(cat_cubes)} first={cat_cubes[:MAX_NAMES]}"
          f" size={len(json.dumps(payload)) if payload is not None else 0}")
    # 2. catalogue of queries: the first shape that lists something wins
    url = build_request_url(dest.url, INA_RESPONSE, dest.queries)
    for label, body in _catalog_requests(capabilities):
        status, ctype, payload, _raw = await _send(proxied, "POST", url, headers, body, csrf)
        cubes = _cubes(payload)
        ok = status == 200 and isinstance(payload, dict) and bool(cubes)
        verdict = "PASS" if ok else "FAIL"
        print(f"{verdict} catalogue[{label}]: HTTP {status} {ctype} "
              f"keys={_top_keys(payload)} queries={len(cubes)} first={cubes[:MAX_NAMES]}")
        if isinstance(payload, dict) and payload.get("Messages"):
            for m in payload["Messages"][:3]:
                if isinstance(m, dict):
                    print(f"  message number={_name(m.get('Number'))} "
                          f"type={_name(m.get('Type'))} "
                          f"text={_name(m.get('Text'))[:300]}")
        if ok:
            break


def main() -> int:
    parser = argparse.ArgumentParser(prog="probe_ina.py",
                                     description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--destination", required=True, help="name of the OnPremise destination")
    parser.add_argument("--query", default="", help="an analytical query name to describe")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--skip-catalog", action="store_true",
                        help="leave out GetCatalog and the catalogue requests")
    parser.add_argument("--dump", default="",
                        help="directory for the raw JSON of the answers (holds data: /tmp only)")
    parser.add_argument("--run", action="store_true",
                        help="send one Analytics read of --query (first measure, top=1)")
    parser.add_argument("--no-describe", action="store_true",
                        help="send the run without the describe first (needs --measure)")
    parser.add_argument("--measure", default="", help="the measure of the run")
    parser.add_argument("--variable", action="append", metavar="NAME=VALUE",
                        help="a single-value variable of the run (repeatable)")
    parser.add_argument("--label", default="run", type=_label,
                        help="file name stem of the run's dump ([A-Za-z0-9_-], at most 40)")
    parser.add_argument("--group-by", action="append", metavar="DIMENSION",
                        help="a dimension of the run on Rows (repeatable)")
    parser.add_argument("--top", type=int, default=1, help="rows asked for (default 1)")
    parser.add_argument("--close", action="store_true",
                        help="release the query instance afterwards")
    parser.add_argument("--variable-form", choices=("app", "sap"), default="app",
                        help="app: FieldName the variable name (as plan_run sends it); "
                        "sap: the key of the variable's dimension, as the describe states it")
    parser.add_argument("--submit", choices=("none", "batch", "separate"), default="none",
                        help="separate: the VariableSubmit, then the read, on one InstanceId "
                        "(the app's sequence); batch: both as one Batch [VariableSubmit, read]")
    parser.add_argument("--structure", default="",
                        help="name of the measure structure on Columns (default Measures, "
                        "as analytics_request sends it)")
    parser.add_argument("--order", action="append", metavar="NAME=asc|desc",
                        help="sort the run by the measure or a --group-by dimension "
                        "(repeatable); the read prints how its rows came back ordered")
    parser.add_argument("--sort-form", choices=SORT_FORMS, default="measure",
                        help="measure: a measure sort as plan_run sends it (no Dimension); "
                        "structure: with the measure structure as Dimension; rows: with "
                        "the first --group-by dimension as Dimension")
    args = parser.parse_args()
    return asyncio.run(run(args, dict(os.environ)))


if __name__ == "__main__":
    sys.exit(main())

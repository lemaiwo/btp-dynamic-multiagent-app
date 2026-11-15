"""Register OData services of the SAP system (destination SAP_USER, as the
signed-in user) in the catalogue, read-only, and attach them to a chat agent.

    python3 test/odata_agent.py /sap/opu/odata/IWBEP/GWSAMPLE_BASIC [more service paths]

Every entity set of each service is enabled for list and get with all its
fields readable; nothing can be written (no create/update/delete, no
function imports, Allow writes off). The agent is "sap-odata-reader".
"""
import re
import sys

import stack_api

AGENT = "sap-odata-reader"
DESTINATION = "SAP_USER"


def slug(path):
    name = re.sub(r"[^a-z0-9]+", "-", path.rstrip("/").split("/")[-1].lower()).strip("-")
    return name[:60]


def definition(preview):
    names = {e["name"] for e in preview["entity_sets"]}
    sets = []
    for entity in preview["entity_sets"]:
        keys = [k["name"] for k in entity["keys"]]
        fields, seen = [], set()
        for field in entity["fields"]:
            seen.add(field["name"])
            fields.append({
                "name": field["name"], "type": field.get("type") or "Edm.String",
                "label": (field.get("label") or "")[:120], "selectable": True,
                "filterable": bool((field.get("declared") or {}).get("filterable")),
                "writable": False,
            })
        if not fields or any(k not in seen for k in keys):
            continue  # nothing an agent could read by key
        sets.append({
            "name": entity["name"], "title": (entity.get("label") or entity["name"])[:120],
            "keys": [{"name": k} for k in keys], "operations": ["list", "get"], "fields": fields,
            "navigations": [n for n in entity.get("navigations", []) if n["target"] in names],
        })
    kept = {s["name"] for s in sets}
    for s in sets:
        s["navigations"] = [n for n in s["navigations"] if n["target"] in kept]
    return {"entity_sets": sets, "operations": []}


def main(paths):
    api = stack_api.Api()
    services = []
    for path in paths:
        status, preview, headers = api.call("POST", "/admin/api/odata/metadata", {
            "destination": DESTINATION, "service_path": path, "odata_version": "v2",
            "user_context": True})
        if status != 200:
            print("FAIL %s: metadata HTTP %s (%s)" % (path, status, headers.get("x-odata-error")))
            continue
        name = slug(path)
        body = {
            "name": name, "title": path.rstrip("/").split("/")[-1],
            "purpose": "Read the data of %s" % path.rstrip("/").split("/")[-1],
            "destination": DESTINATION, "user_context": True, "odata_version": "v2",
            "service_path": path, "enabled": True, "definition": definition(preview),
        }
        status, answer, _ = api.call("POST", "/admin/api/odata/services", body)
        if status == 409:
            status, answer, _ = api.call("PUT", "/admin/api/odata/services/" + name, body)
        if status not in (200, 201):
            print("FAIL %s: save HTTP %s %s" % (name, status, str((answer or {}).get("detail"))[:300]))
            continue
        services.append(name)
        print("PASS service '%s': %d entity sets, read-only" % (name, len(body["definition"]["entity_sets"])))
    if not services:
        raise SystemExit(1)

    agent = {
        "name": AGENT,
        "description": "Reads business data from the SAP system through its OData services, "
                       "as the signed-in user.",
        "instructions": "You answer questions about data in the SAP system. Search the OData "
                        "catalogue first (search_operations), then read with execute_operation "
                        "using exactly the names the search returned. Say which service and "
                        "entity set an answer comes from. You cannot change data.",
        "mcp_servers": [{"url": "builtin:odata", "auth_mode": "destination",
                         "oauth": {"services": services, "allow_write": False}}],
        "enabled": True, "expose_chat": True,
    }
    _, agents, _ = api.call("GET", "/admin/api/agents")
    existing = [a for a in (agents if isinstance(agents, list) else agents.get("agents", []))
                if a["name"] == AGENT]
    if existing:
        status, answer, _ = api.call("PUT", "/admin/api/agents/%s" % existing[0]["id"], agent)
    else:
        status, answer, _ = api.call("POST", "/admin/api/agents", agent)
    if status not in (200, 201):
        raise SystemExit("FAIL agent: HTTP %s %s" % (status, str((answer or {}).get("detail"))[:300]))
    status, _, _ = api.call("POST", "/admin/api/reload", {})
    print("PASS agent '%s' with %d service(s); registry reload HTTP %s" % (AGENT, len(services), status))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    main(sys.argv[1:])

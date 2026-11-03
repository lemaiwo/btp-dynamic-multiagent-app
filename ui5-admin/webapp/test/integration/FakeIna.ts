import type { InaDefinition, InaQuery, InaQuerySummary, InaUsedBy } from "com/agent/admin/service/inaTypes";
import type { ReloadOutcome } from "com/agent/admin/service/types";

type Json = (body: unknown, status?: number, headers?: Record<string, string>) => Promise<Response>;

/** `QUERY_NAME_RE` (agents/ina/models.py) and `DESTINATION_NAME_RE` (agents/odata/models.py). */
const QUERY_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$/;
const DESTINATION_NAME_RE = /^[A-Za-z0-9_.-]{1,200}$/;
const QUERY_NAME_PATTERN = "^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$";
const DESTINATION_PATTERN = "^[A-Za-z0-9_.-]{1,200}$";

/** The fields of `InaQueryPayload`, in its order: the order of a refusal's problems. */
const PAYLOAD_KEYS = [
    "name", "title", "purpose", "not_for", "destination", "user_context", "query", "package", "definition",
    "metadata_fetched_at", "enabled"
];

/** `LOC_FIELD_RE` of agents/loc_fields.py: a key that may be repeated in a refusal. */
const LOC_FIELD_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
/** What pydantic's `strip_whitespace` drops: Unicode White_Space. */
const STRIP_RE = /^[\t\n\v\f\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t\n\v\f\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g;
/** `one_line` and `one_line_strict` of agents/loc_fields.py, by Unicode category. */
const NOT_ONE_LINE_RE = /[\p{Cc}\p{Zl}\p{Zp}]/u;
const NOT_ONE_LINE_STRICT_RE = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u;

/** The fixed texts of the routes (agents/db.py, agents/ina/protocol.py). */
export const NOT_FOUND = "Query not found";
export const NAME_TAKEN = "name: already exists";
export const NAME_FIXED = "name: cannot be changed; register the query under the new name instead";
export const UNKNOWN_IN_SAP = "SAP does not know the InA query of this registration";

type Body = Record<string, unknown>;

function isObject(value: unknown): value is Body {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The `/admin/api/ina/...` routes of the fake backend (design addendum (k),
 * part 2 point 4), apart from `FakeBackend` so neither file grows past its
 * size. Two queries: `flight-revenue` (described, the signed-in user, named
 * by gmail-agent) and `cost-centre-actuals` (not described, the technical
 * user, switched off, named by no agent). `sap` is what SAP describes for an
 * InA object name; a query it does not hold is `query_unknown`.
 */
export default class FakeIna {

    public queries: InaQuery[] = [];
    /** What `Describe` reads from SAP, by the query's InA name. */
    public sap: Record<string, InaDefinition> = {};
    private stamp = 0;
    private nextId = 1;

    public reset(): void {
        this.stamp = Date.parse("2026-11-01T08:00:00Z");
        this.nextId = 1;
        this.sap = {
            ZFLIGHT_REV_Q001: {
                dimensions: [
                    { name: "0CALMONTH", label: "Calendar month" },
                    { name: "ZCARRID", label: "Airline", key_field: "ZCARRID_KEY" }
                ],
                measures: [{ name: "ZREVENUE", label: "Revenue", unit: "EUR" }],
                variables: []
            },
            "0CCA_C11_Q0001": {
                dimensions: [{ name: "0COSTCENTER", label: "Cost centre" }, { name: "0CALMONTH", label: "Calendar month" }],
                measures: [{ name: "0AMOUNT", label: "Amount", unit: "EUR" }, { name: "0QUANTITY", label: "Quantity" }],
                variables: [{ name: "ZV_YEAR", label: "Fiscal year", mandatory: true, type: "single" }]
            }
        };
        const gmail: InaUsedBy = { agent_id: 101, agent: "gmail-agent", enabled: true, expose_api: false, api_slug: null };
        this.queries = [
            this.record({
                name: "flight-revenue", title: "Flight revenue", purpose: "Revenue per airline and month",
                not_for: "Plan values", destination: "S4_ODATA_USER", user_context: true, query: "ZFLIGHT_REV_Q001",
                package: "ZFLIGHT", enabled: true
            }, JSON.parse(JSON.stringify(this.sap.ZFLIGHT_REV_Q001)) as InaDefinition, [gmail]),
            this.record({
                name: "cost-centre-actuals", title: "Cost centre actuals", purpose: "Actual costs per cost centre",
                not_for: "", destination: "S4_ODATA_TECH", user_context: false, query: "0CCA_C11_Q0001",
                package: "", enabled: false
            }, null, [])
        ];
    }

    /** The answer of a `PUT` made from the version before the last save. */
    public static changedElsewhere(name: string): string {
        return `Query '${name}' was changed since it was loaded; reload it and save again`;
    }

    /** A new stamp, never one a query already has. */
    private nextStamp(): string {
        this.stamp += 1000;
        return new Date(this.stamp).toISOString().replace(".000Z", "+00:00");
    }

    private record(fields: Body, definition: InaDefinition | null, usedBy: InaUsedBy[]): InaQuery {
        const at = this.nextStamp();
        return {
            id: this.nextId++, name: String(fields.name), title: String(fields.title), purpose: String(fields.purpose),
            not_for: String(fields.not_for ?? ""), destination: String(fields.destination),
            user_context: fields.user_context === true, query: String(fields.query), package: String(fields.package ?? ""),
            enabled: fields.enabled !== false, described: definition !== null, definition,
            metadata_fetched_at: definition ? at : null, created_at: at, updated_at: at, used_by: usedBy
        };
    }

    /** The list row: the record without its definition. */
    private static summary(query: InaQuery): InaQuerySummary {
        const { definition, reloaded, reload_failed, ...row } = query;
        void [definition, reloaded, reload_failed];
        return row;
    }

    private static copy(query: InaQuery): InaQuery {
        return JSON.parse(JSON.stringify(query)) as InaQuery;
    }

    /** Answers an `ina/...` route, or `undefined` for any other path. */
    public handle(path: string, method: string, body: unknown, json: Json, reload: ReloadOutcome): Promise<Response> | undefined {
        if (!/^ina\//.test(path)) {
            return undefined;
        }
        const sub = path.replace(/^ina\//, "");
        if (sub === "queries" && method === "GET") {
            const sorted = this.queries.slice().sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
            return json(sorted.map(FakeIna.summary));
        }
        if (sub === "queries" && method === "POST") {
            return this.create(body, json);
        }
        const one = /^queries\/([^/]+)(\/describe|\/test)?$/.exec(sub);
        if (!one) {
            return json({ detail: "Not Found" }, 404);
        }
        const name = decodeURIComponent(one[1]);
        const action = one[2] ?? "";
        // A name that cannot be a query name is the same 404 as an unknown one.
        const stored = QUERY_NAME_RE.test(name) ? this.queries.filter((q) => q.name === name)[0] : undefined;
        if (action === "" && method === "PUT") {
            return this.update(name, stored, body, json, reload);
        }
        if (!stored) {
            return json({ detail: NOT_FOUND }, 404);
        }
        if (action === "" && method === "GET") {
            return json(FakeIna.copy(stored));
        }
        if (action === "" && method === "DELETE") {
            if (stored.used_by.length) {
                const agents = stored.used_by.map((u) => `'${typeof u === "string" ? u : u.agent}'`).join(", ");
                return json({ detail: `Query '${name}' is used by agent(s) ${agents}` }, 409);
            }
            this.queries.splice(this.queries.indexOf(stored), 1);
            return Promise.resolve(new Response(null, {
                status: 204,
                headers: {
                    "X-Ina-Reloaded": String(reload.reloaded === true),
                    "X-Ina-Reload-Failed": String(reload.reload_failed === true)
                }
            }));
        }
        if (action && method === "POST") {
            const extra = isObject(body) ? Object.keys(body)[0] : undefined;
            if (extra !== undefined) {
                return json({ detail: `${FakeIna.loc(extra)}: Extra inputs are not permitted` }, 422);
            }
            return action === "/describe" ? this.describe(stored, json) : this.test(stored, json);
        }
        return json({ detail: "Method Not Allowed" }, 405);
    }

    private create(body: unknown, json: Json): Promise<Response> {
        const problems = FakeIna.problems(body);
        if (problems.length) {
            return json({ detail: problems.join("; ") }, 422);
        }
        const payload = body as Body;
        if (this.queries.some((q) => q.name === payload.name)) {
            return json({ detail: NAME_TAKEN }, 409);
        }
        const created = this.record(FakeIna.stripped(payload), null, []);
        if (isObject(payload.definition)) {
            created.definition = payload.definition as unknown as InaDefinition;
            created.described = true;
            // A pair: a definition without its stamp is stamped now.
            created.metadata_fetched_at = typeof payload.metadata_fetched_at === "string"
                ? payload.metadata_fetched_at : created.updated_at;
        }
        this.queries.push(created);
        return json(FakeIna.copy(created), 201);
    }

    private update(name: string, stored: InaQuery | undefined, body: unknown, json: Json, reload: ReloadOutcome): Promise<Response> {
        const { expected_updated_at: expected, ...payload } = isObject(body) ? body : {};
        const problems = FakeIna.problems(isObject(body) ? payload : body);
        if (expected !== undefined && expected !== null && typeof expected !== "string") {
            problems.push("expected_updated_at: Input should be a valid string");
        }
        if (problems.length) {
            return json({ detail: problems.join("; ") }, 422);
        }
        if (!stored) {
            return json({ detail: NOT_FOUND }, 404);
        }
        if (payload.name !== name) {
            return json({ detail: NAME_FIXED }, 409);
        }
        // Exact comparison of the text the GET answered; left out or null: not checked.
        if (typeof expected === "string" && expected !== stored.updated_at) {
            return json({ detail: FakeIna.changedElsewhere(name) }, 409);
        }
        const fields = FakeIna.stripped(payload);
        Object.assign(stored, {
            title: fields.title, purpose: fields.purpose, not_for: fields.not_for ?? "", destination: fields.destination,
            user_context: fields.user_context === true, query: fields.query, package: fields.package ?? "",
            enabled: fields.enabled !== false, updated_at: this.nextStamp()
        });
        // `definition` and `metadata_fetched_at` are a pair: an absent key
        // keeps both, null clears both, a definition without a stamp is stamped now.
        if ("definition" in payload) {
            stored.definition = isObject(payload.definition) ? payload.definition as unknown as InaDefinition : null;
            stored.described = stored.definition !== null;
            stored.metadata_fetched_at = stored.definition === null ? null
                : typeof payload.metadata_fetched_at === "string" ? payload.metadata_fetched_at : stored.updated_at;
        }
        return json({ ...FakeIna.copy(stored), reloaded: reload.reloaded === true, reload_failed: reload.reload_failed === true });
    }

    /** Reads SAP and stores what it describes; the stored row moves with it. */
    private describe(stored: InaQuery, json: Json): Promise<Response> {
        const read = this.sap[stored.query];
        if (!read) {
            return json({ detail: UNKNOWN_IN_SAP }, 502, { "X-Ina-Error": "query_unknown" });
        }
        stored.definition = JSON.parse(JSON.stringify(read)) as InaDefinition;
        stored.described = true;
        stored.updated_at = this.nextStamp();
        stored.metadata_fetched_at = stored.updated_at;
        const names = [...read.dimensions, ...read.measures, ...read.variables].map((member) => member.name);
        return json({
            dimensions: read.dimensions.length, measures: read.measures.length, variables: read.variables.length,
            names, truncated: false, metadata_fetched_at: stored.metadata_fetched_at
        });
    }

    /** One read of the first measure: outcome and column names, never a value. */
    private test(stored: InaQuery, json: Json): Promise<Response> {
        // Not described yet, the run reads the description from SAP first.
        const read = this.sap[stored.query];
        if (!read) {
            return json({ detail: UNKNOWN_IN_SAP }, 502, { "X-Ina-Error": "query_unknown" });
        }
        const first = (stored.definition ?? read).measures[0];
        return json({ ok: true, status: 200, duration_ms: 87, columns: first ? [first.name] : [] });
    }

    /** Title and purpose as the server stores them: without the space around. */
    private static stripped(payload: Body): Body {
        return { ...payload, title: String(payload.title).replace(STRIP_RE, ""), purpose: String(payload.purpose).replace(STRIP_RE, "") };
    }

    private static loc(key: string): string {
        return LOC_FIELD_RE.test(key) ? key : "<unknown field>";
    }

    /** `validate_ina_query`: `loc: msg` per problem, in the order of the model, never a value. */
    private static problems(body: unknown): string[] {
        if (!isObject(body)) {
            return ["ina_query: expected an object"];
        }
        const problems: string[] = [];
        const text = (key: string, opts: { required?: boolean; strip?: boolean; min?: number; max: number; strict?: boolean }) => {
            if (!(key in body)) {
                if (opts.required) {
                    problems.push(`${key}: Field required`);
                }
                return;
            }
            const raw = body[key];
            if (typeof raw !== "string") {
                problems.push(`${key}: Input should be a valid string`);
                return;
            }
            const value = opts.strip ? raw.replace(STRIP_RE, "") : raw;
            const length = Array.from(value).length;
            if (opts.min && length < opts.min) {
                problems.push(`${key}: String should have at least ${opts.min} character${opts.min > 1 ? "s" : ""}`);
            } else if (length > opts.max) {
                problems.push(`${key}: String should have at most ${opts.max} characters`);
            } else if ((opts.strict ? NOT_ONE_LINE_STRICT_RE : NOT_ONE_LINE_RE).test(value)) {
                const what = opts.strict ? "control or format characters" : "control characters";
                problems.push(`${key}: Value error, ${key} must be one line of text without ${what}`);
            }
        };
        const pattern = (key: string, re: RegExp, shown: string) => {
            if (!(key in body)) {
                problems.push(`${key}: Field required`);
            } else if (typeof body[key] !== "string") {
                problems.push(`${key}: Input should be a valid string`);
            } else if (!re.test(body[key] as string)) {
                problems.push(`${key}: String should match pattern '${shown}'`);
            }
        };
        const flag = (key: string) => {
            if (key in body && typeof body[key] !== "boolean") {
                problems.push(`${key}: Input should be a valid boolean`);
            }
        };
        pattern("name", QUERY_NAME_RE, QUERY_NAME_PATTERN);
        text("title", { required: true, strip: true, min: 1, max: 120 });
        text("purpose", { required: true, strip: true, min: 1, max: 200 });
        text("not_for", { max: 200 });
        pattern("destination", DESTINATION_NAME_RE, DESTINATION_PATTERN);
        flag("user_context");
        text("query", { required: true, min: 1, max: 128, strict: true });
        text("package", { max: 64 });
        FakeIna.definitionProblems(body.definition, problems);
        if (body.metadata_fetched_at !== undefined && body.metadata_fetched_at !== null
            && typeof body.metadata_fetched_at !== "string") {
            problems.push("metadata_fetched_at: Input should be a valid datetime");
        }
        flag("enabled");
        Object.keys(body).filter((key) => PAYLOAD_KEYS.indexOf(key) === -1)
            .forEach((key) => problems.push(`${FakeIna.loc(key)}: Extra inputs are not permitted`));
        return problems;
    }

    /** The shape of an `InaDefinition`, lightly: three lists of members with distinct names. */
    private static definitionProblems(definition: unknown, problems: string[]): void {
        if (definition === undefined || definition === null) {
            return;
        }
        if (!isObject(definition)) {
            problems.push("definition: Input should be an object");
            return;
        }
        ["dimensions", "measures", "variables"].forEach((list) => {
            const members = definition[list];
            if (members === undefined) {
                return;
            }
            if (!Array.isArray(members)) {
                problems.push(`definition.${list}: Input should be a valid list`);
                return;
            }
            const names = members.map((member: unknown) => (isObject(member) ? member.name : undefined));
            const bad = names.findIndex((name) => typeof name !== "string" || name === "");
            const repeat = names.findIndex((name, i) => names.indexOf(name) !== i);
            if (bad !== -1) {
                problems.push(`definition.${list}.${bad}.name: Field required`);
            } else if (repeat !== -1) {
                problems.push(`definition.${list}: Value error, entry ${repeat} repeats the name of an earlier entry; `
                    + "names must be distinct");
            }
        });
    }
}

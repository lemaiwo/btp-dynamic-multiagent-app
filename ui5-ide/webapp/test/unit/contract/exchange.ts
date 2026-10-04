import { validate, type SchemaDoc } from "./validate";

/**
 * One call between the app and the backend judged against the backend's
 * route map (`x-routes` in `test/contract/ide-api.schema.json`, exported by
 * `scripts/export_ide_schema.py` from the FastAPI app): the request body
 * against the route's `request` model, a 2xx answer against its `response`
 * model (a list when `list`, an event stream when `stream`, no body when
 * `response` is null), a refusal that carries a `code` against `ErrorOut`.
 */

/** One `x-routes` entry. */
export interface RouteSpec {
    response: string | null;
    request: string | null;
    list: boolean;
    stream: boolean;
}

export interface ContractDoc extends SchemaDoc {
    "x-routes": Record<string, RouteSpec>;
}

/** What went over the wire, as the observer around `window.fetch` saw it. */
export interface Exchange {
    method: string;
    /** Below `backend/`, no query, no trailing slash. */
    path: string;
    /** The `x-routes` key the call matched ("POST /sessions/{sid}/approve"), undefined for none. */
    route?: string;
    /** The request body as sent (a string), undefined when none was sent. */
    requestBody?: string;
    status: number;
    contentType: string;
    text: string;
}

export interface Verdict {
    /** What the answer was validated as ("SessionOut[]", "sse", "-" for no body, "ErrorOut", "" for nothing). */
    model: string;
    /** The request model the body was validated against, "" for none. */
    requestModel: string;
    /** Event names of an event stream, in order. */
    events: string[];
    errors: string[];
}

/**
 * The event names the server emits (agents/ide: `emit(...)` calls framed by
 * `sse.format_event`). Closed: any other name in a stream fails the test,
 * so a new event needs a decision here (and a model below if it has one).
 */
export const SSE_EVENTS: readonly string[] = [
    "run", "text", "tool", "plan", "file", "artifact", "finding", "approval_required", "comments", "usage", "error", "done"
];

/**
 * SSE events whose data has a model in `$defs` (the data, or one field of
 * it). The others (`run`, `text`, `file`, `artifact`, `comments`, `usage`,
 * `error`, `done`) are plain dicts on the server with no exported model.
 */
export const FRAME_MODELS: Readonly<Record<string, { model: string; field?: string }>> = {
    tool: { model: "ToolEventOut" },
    finding: { model: "FindingOut" },
    approval_required: { model: "ApprovalOut" },
    plan: { model: "TodoOut[]", field: "todos" }
};

/** `event:`/`data:` frames of an event-stream body. */
export function parseFrames(text: string): [string, unknown][] {
    return text.split("\n\n").filter((b) => b.trim()).map((block) => {
        const event = /^event: (.*)$/m.exec(block)?.[1] ?? "";
        const raw = /^data: (.*)$/m.exec(block)?.[1] ?? "null";
        let data: unknown;
        try {
            data = JSON.parse(raw) as unknown;
        } catch {
            data = raw;
        }
        return [event, data];
    });
}

function parse(text: string): { ok: boolean; value?: unknown } {
    try {
        return { ok: true, value: JSON.parse(text) as unknown };
    } catch {
        return { ok: false };
    }
}

function requiredOf(doc: SchemaDoc, model: string): string[] {
    return (doc.$defs[model]?.required ?? []) as string[];
}

/** Judges one exchange; `errors` is empty when both sides keep the contract. */
export function checkExchange(doc: ContractDoc, ex: Exchange): Verdict {
    const verdict: Verdict = { model: "", requestModel: "", events: [], errors: [] };
    const key = ex.route;
    const spec = key ? doc["x-routes"][key] : undefined;
    if (!key || !spec) {
        verdict.errors.push(`${ex.method} ${ex.path}: no x-routes entry (${key ?? "no fake route"})`);
        return verdict;
    }

    // The request side.
    if (ex.requestBody !== undefined) {
        const body = parse(ex.requestBody);
        if (!body.ok) {
            verdict.errors.push("request: the body is not JSON");
        } else if (spec.request) {
            verdict.requestModel = spec.request;
            verdict.errors.push(...validate(doc, spec.request, body.value).map((e) => `request ${e}`));
        } else if (!(body.value && typeof body.value === "object" && !Array.isArray(body.value)
            && Object.keys(body.value).length === 0)) {
            // A route without a body parameter: FastAPI ignores what is sent, so an
            // empty object (IdeService.streamReport sends `{}`) is tolerated, anything else is not.
            verdict.errors.push("request: a body where the route takes none");
        }
    } else if (spec.request && requiredOf(doc, spec.request).length) {
        verdict.errors.push(`request: no body, but ${spec.request} requires ${requiredOf(doc, spec.request).join(", ")}`);
    }

    // The response side.
    if (ex.status < 200 || ex.status >= 300) {
        const body = ex.text ? parse(ex.text) : { ok: false };
        if (body.ok && body.value && typeof body.value === "object" && "code" in (body.value as object)) {
            verdict.model = "ErrorOut";
            verdict.errors.push(...validate(doc, "ErrorOut", body.value));
        }
        return verdict;
    }
    const isStream = ex.contentType.toLowerCase().startsWith("text/event-stream");
    if (spec.stream) {
        verdict.model = "sse";
        if (!isStream) {
            verdict.errors.push("JSON where an event stream was expected");
            return verdict;
        }
        for (const [event, data] of parseFrames(ex.text)) {
            verdict.events.push(event);
            if (!SSE_EVENTS.includes(event)) {
                verdict.errors.push(`unknown event ${JSON.stringify(event)}`);
                continue;
            }
            const frame = FRAME_MODELS[event];
            if (frame) {
                const value = frame.field ? (data as Record<string, unknown> | null)?.[frame.field] : data;
                verdict.errors.push(...validate(doc, frame.model, value).map((e) => `${event} frame ${e}`));
            }
        }
        return verdict;
    }
    if (isStream) {
        verdict.errors.push("an event stream where JSON was expected");
        return verdict;
    }
    if (!spec.response) {
        verdict.model = "-";
        if (ex.text) {
            verdict.errors.push("a body where none was expected");
        }
        return verdict;
    }
    verdict.model = spec.list ? `${spec.response}[]` : spec.response;
    const body = parse(ex.text);
    if (!body.ok) {
        verdict.errors.push("the answer is not JSON");
        return verdict;
    }
    verdict.errors.push(...validate(doc, verdict.model, body.value));
    return verdict;
}

/**
 * The two-way difference between the routes the fake serves and the
 * backend's `x-routes`, minus `excluded` (x-routes entries the UI never
 * calls). An exclusion that is served by the fake or missing from x-routes
 * is stale and reported too.
 */
export function routeDiff(fakeRoutes: readonly string[], xRoutes: readonly string[], excluded: readonly string[]): {
    notInXRoutes: string[]; notServed: string[]; staleExclusions: string[];
} {
    return {
        notInXRoutes: fakeRoutes.filter((r) => !xRoutes.includes(r)),
        notServed: xRoutes.filter((r) => !fakeRoutes.includes(r) && !excluded.includes(r)),
        staleExclusions: excluded.filter((r) => fakeRoutes.includes(r) || !xRoutes.includes(r))
    };
}

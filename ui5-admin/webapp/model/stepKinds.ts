import type {
    ConditionConfig, ConditionOp, ConditionRule, ConditionSource, HttpConfig,
    PythonConfig, StepAction, StepConfig, StepKind, TransformConfig, WorkflowStep
} from "../service/types";

/**
 * Deterministic workflow step kinds, as the editor sees them.
 *
 * Mirrors `agents/step_kinds.py`: the kinds, the condition operators, the
 * HTTP methods, and the shape of each kind's config. The server validates
 * every config again on save; this module only converts between the wire
 * shape (`WorkflowStep.config`) and a flat, binding-friendly editor copy
 * (`UiStepConfig`) -- a JSONModel cannot bind an `<Input>` to
 * `config/regex/pattern` while `regex` is null, and a condition rule is
 * easier to edit as one flat row than as `{when: {...}, then: {...}}`.
 *
 * Pure functions, so the round trip is unit-testable without a view.
 */

export const STEP_KINDS: StepKind[] = ["agent", "condition", "transform", "http", "python"];

export const CONDITION_SOURCES: ConditionSource[] = ["text", "item", "json"];

export const CONDITION_OPS: ConditionOp[] = [
    "contains", "not_contains", "equals", "not_equals", "matches",
    "not_matches", "gt", "lt", "is_empty", "not_empty"
];

export const STEP_ACTIONS: StepAction[] = ["continue", "stop"];

export const HTTP_METHODS: HttpConfig["method"][] = ["GET", "POST", "PUT", "PATCH", "DELETE"];

/** One condition rule as a flat editor row. */
export interface UiRule {
    source: ConditionSource;
    field: string;
    op: ConditionOp;
    value: string;
    case_sensitive: boolean;
    action: StepAction;
    output: string;
}

/** Every kind's fields in one flat object, so a single `cfg` path per step
 * row can back four alternative forms. Fields of the other kinds are simply
 * ignored when mapping back to the wire. */
export interface UiStepConfig {
    /** The kind this cfg was built for; `onStepKindChange` rebuilds it when
     * the kind changes so a stale form never leaks into the payload. */
    kind: StepKind;
    // condition
    rules: UiRule[];
    else_action: StepAction;
    else_output: string;
    // transform
    extract_json: string;
    regex_pattern: string;
    regex_replace: string;
    regex_flags: string;
    template: string;
    truncate: string;
    // http
    destination: string;
    method: HttpConfig["method"];
    path: string;
    query_text: string;
    headers_text: string;
    body: string;
    content_type: string;
    http_timeout: number;
    expect_status_text: string;
    // python
    code: string;
    py_timeout: number;
}

export function emptyRule(): UiRule {
    return {
        source: "text", field: "", op: "contains", value: "", case_sensitive: false,
        action: "continue", output: ""
    };
}

export function emptyUiConfig(kind: StepKind): UiStepConfig {
    return {
        kind,
        rules: [],
        else_action: "continue",
        else_output: "",
        extract_json: "",
        regex_pattern: "",
        regex_replace: "",
        regex_flags: "",
        template: "",
        truncate: "",
        destination: "",
        method: "GET",
        path: "/",
        query_text: "",
        headers_text: "",
        body: "",
        content_type: "application/json",
        http_timeout: 30,
        expect_status_text: "",
        code: "",
        py_timeout: 10
    };
}

function asString(value: unknown, fallback = ""): string {
    return value === undefined || value === null ? fallback : String(value);
}

function jsonText(value: unknown): string {
    if (!value || typeof value !== "object" || !Object.keys(value as object).length) {
        return "";
    }
    return JSON.stringify(value, null, 2);
}

/** The editor copy of a stored config. Unknown or missing fields fall back
 * to the empty form, so an older row (or a hand-edited import) still opens. */
export function uiConfigFromWire(kind: StepKind, config: StepConfig | undefined): UiStepConfig {
    const cfg = emptyUiConfig(kind);
    const c = (config || {}) as Record<string, unknown>;
    if (kind === "condition") {
        const rules = Array.isArray(c.rules) ? (c.rules as Partial<ConditionRule>[]) : [];
        cfg.rules = rules.map((r) => {
            const when = (r.when || {}) as Partial<ConditionRule["when"]>;
            const then = (r.then || {}) as Partial<ConditionRule["then"]>;
            return {
                source: when.source || "text",
                field: asString(when.field),
                op: when.op || "contains",
                value: asString(when.value),
                case_sensitive: when.case_sensitive === true,
                action: then.action || "continue",
                output: asString(then.output)
            };
        });
        const els = (c.else || {}) as Partial<ConditionConfig["else"]>;
        cfg.else_action = els.action || "continue";
        cfg.else_output = asString(els.output);
    } else if (kind === "transform") {
        const t = c as Partial<TransformConfig>;
        cfg.extract_json = asString(t.extract_json);
        cfg.regex_pattern = asString(t.regex?.pattern);
        cfg.regex_replace = asString(t.regex?.replace);
        cfg.regex_flags = asString(t.regex?.flags);
        cfg.template = asString(t.template);
        cfg.truncate = t.truncate ? String(t.truncate) : "";
    } else if (kind === "http") {
        const h = c as Partial<HttpConfig>;
        cfg.destination = asString(h.destination);
        cfg.method = (asString(h.method, "GET").toUpperCase() as HttpConfig["method"]);
        cfg.path = asString(h.path, "/");
        cfg.query_text = jsonText(h.query);
        cfg.headers_text = jsonText(h.headers);
        cfg.body = asString(h.body);
        cfg.content_type = asString(h.content_type, "application/json");
        cfg.http_timeout = Number(h.timeout_seconds) || 30;
        cfg.expect_status_text = Array.isArray(h.expect_status) ? h.expect_status.join(",") : "";
    } else if (kind === "python") {
        const p = c as Partial<PythonConfig>;
        cfg.code = asString(p.code);
        cfg.py_timeout = Number(p.timeout_seconds) || 10;
    }
    return cfg;
}

function parseJsonObject(text: string, what: string): Record<string, string> {
    const raw = (text || "").trim();
    if (!raw) {
        return {};
    }
    let value: unknown;
    try {
        value = JSON.parse(raw);
    } catch {
        throw new Error(`${what} must be a JSON object.`);
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error(`${what} must be a JSON object.`);
    }
    const out: Record<string, string> = {};
    Object.keys(value as object).forEach((k) => {
        out[k] = asString((value as Record<string, unknown>)[k]);
    });
    return out;
}

/** The wire config for `kind` from its editor copy. Throws an Error whose
 * message names the field when a free-text JSON field does not parse; the
 * caller prefixes the step position. */
export function wireConfigFromUi(kind: StepKind, cfg: UiStepConfig | undefined): StepConfig {
    const c = cfg || emptyUiConfig(kind);
    if (kind === "condition") {
        const config: ConditionConfig = {
            rules: (c.rules || []).map((r) => ({
                when: {
                    source: r.source, field: (r.field || "").trim(), op: r.op,
                    value: r.value || "", case_sensitive: r.case_sensitive === true
                },
                then: { action: r.action, output: r.output || "" }
            })),
            else: { action: c.else_action || "continue", output: c.else_output || "" }
        };
        return config as unknown as StepConfig;
    }
    if (kind === "transform") {
        const truncate = parseInt(c.truncate, 10);
        const config: TransformConfig = {
            extract_json: (c.extract_json || "").trim(),
            regex: c.regex_pattern
                ? { pattern: c.regex_pattern, replace: c.regex_replace || "", flags: (c.regex_flags || "").trim() }
                : null,
            template: c.template || "",
            truncate: truncate > 0 ? truncate : null
        };
        return config as unknown as StepConfig;
    }
    if (kind === "http") {
        const expect = (c.expect_status_text || "")
            .split(",")
            .map((s) => parseInt(s.trim(), 10))
            .filter((n) => n > 0);
        const config: HttpConfig = {
            destination: (c.destination || "").trim(),
            method: c.method || "GET",
            path: (c.path || "").trim() || "/",
            query: parseJsonObject(c.query_text, "Query"),
            headers: parseJsonObject(c.headers_text, "Headers"),
            body: c.body || "",
            content_type: (c.content_type || "").trim() || "application/json",
            timeout_seconds: Number(c.http_timeout) || 30
        };
        if (expect.length) {
            config.expect_status = expect;
        }
        return config as unknown as StepConfig;
    }
    if (kind === "python") {
        const config: PythonConfig = { code: c.code || "", timeout_seconds: Number(c.py_timeout) || 10 };
        return config as unknown as StepConfig;
    }
    return {};
}

/** The kind of a stored step; rows written before kinds existed have none. */
export function kindOf(step: Pick<WorkflowStep, "kind">): StepKind {
    return step.kind && STEP_KINDS.indexOf(step.kind) > -1 ? step.kind : "agent";
}

/** A one-line description of a deterministic step for lists and the flow
 * preview: what it does, not how. Empty for an agent step. */
export function stepSummary(step: Pick<WorkflowStep, "kind" | "config">): string {
    const kind = kindOf(step);
    const c = (step.config || {}) as Record<string, unknown>;
    if (kind === "condition") {
        const n = Array.isArray(c.rules) ? (c.rules as unknown[]).length : 0;
        return `${n} rule${n === 1 ? "" : "s"}`;
    }
    if (kind === "transform") {
        const bits: string[] = [];
        if (c.extract_json) { bits.push(`json ${String(c.extract_json)}`); }
        if (c.regex) { bits.push("regex"); }
        if (c.template) { bits.push("template"); }
        if (c.truncate) { bits.push(`≤${String(c.truncate)}`); }
        return bits.join(", ") || "pass-through";
    }
    if (kind === "http") {
        return `${asString(c.method, "GET").toUpperCase()} ${asString(c.destination)}${asString(c.path, "/")}`;
    }
    if (kind === "python") {
        const first = asString(c.code).split("\n").map((l) => l.trim()).filter(Boolean)[0] || "";
        return first.length > 40 ? first.slice(0, 40) + "…" : first;
    }
    return "";
}

// --- step list ---
/** The icon the Steps list shows per kind. */
export const KIND_ICONS: Record<StepKind, string> = {
    agent: "sap-icon://person-placeholder",
    condition: "sap-icon://decision",
    transform: "sap-icon://edit",
    http: "sap-icon://cloud",
    python: "sap-icon://source-code"
};

export function stepKindIcon(kind: string | undefined): string {
    return KIND_ICONS[kindOf({ kind: kind as StepKind })];
}

/** How many characters a list summary may run to before it is cut. */
export const SUMMARY_MAX = 80;

/** The first non-blank line of `text`, cut to `max` characters with an
 * ellipsis. Empty for blank text. */
export function firstLine(text: string | undefined | null, max = SUMMARY_MAX): string {
    const line = String(text ?? "").split("\n").map((l) => l.trim()).filter(Boolean)[0] || "";
    return line.length > max ? line.slice(0, max - 1).trimEnd() + "…" : line;
}

/** The one-line summary the Steps list shows for a row while it is being
 * edited: the first line of an agent step's instructions, or the kind's
 * summary built from the *editor* copy of its config (`cfg`), which is what
 * the operator is typing into -- `config` is only refreshed on save. A
 * JSON field that does not parse yet falls back to the stored config, so a
 * half-typed header never blanks the row. */
export function stepListSummary(step: Pick<WorkflowStep, "kind" | "instructions" | "config"> & { cfg?: UiStepConfig }): string {
    const kind = kindOf(step);
    if (kind === "agent") {
        return firstLine(step.instructions);
    }
    let config: StepConfig | undefined = step.config;
    if (step.cfg) {
        try {
            config = wireConfigFromUi(kind, step.cfg);
        } catch {
            // keep the stored config
        }
    }
    return firstLine(stepSummary({ kind, config }));
}

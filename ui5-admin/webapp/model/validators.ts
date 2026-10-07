import type { AuthMode, DeepConfig, McpServer, OAuthClient, WorkflowStep } from "../service/types";
import { BUILTINS, DESTINATION_MAILBOX_URLS, DESTINATION_USER_CONTEXT_URLS } from "./builtins";
import { isRemoteUrl } from "./remoteUrl";
import odataEntry from "./odataEntry";

// --- odata ---
/** `SERVICE_NAME_RE` in agents/odata/models.py: the slug of a catalogue service. */
const ODATA_SERVICE_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$/;
/** `MAX_ODATA_ENTRY_SERVICES` in agents/db.py. */
const MAX_ODATA_ENTRY_SERVICES = 50;

// --- destinations ---
/** What a BTP destination may be called. Mirrors `_DESTINATION_NAME_RE` in
 * `agents/admin.py`. */
const DESTINATION_NAME_RE = /^[A-Za-z0-9_.-]{1,200}$/;

// --- sharepoint ---
/** The `site` pin: `<tenant>.sharepoint.com:/<1-4 path segments>`. Mirrors
 * `check_pins` in `agents/sharepoint_views.py`, which is authoritative. */
const SHAREPOINT_SITE_RE = /^[a-z0-9][a-z0-9-]{0,62}\.sharepoint\.com:(\/[A-Za-z0-9._-]{1,128}){1,4}$/;
/** A control or format character, or a line / paragraph separator: what
 * `_text` in `agents/sharepoint_views.py` refuses in a one-line value. */
const NOT_ONE_LINE_RE = /[\p{C}\p{Zl}\p{Zp}]/u;
/** `MAX_KINDS` and `MAX_KIND_CHARS` in `agents/sharepoint_views.py`. */
const SHAREPOINT_MAX_KINDS = 10;
const SHAREPOINT_MAX_KIND_CHARS = 60;
/** `RUN_KEYS` in `agents/sharepoint_views.py`: the fields of a calendar run,
 * which a looked-up column must not be named like. */
const SHAREPOINT_RUN_KEYS = ["member", "team", "kind", "status", "from", "to"];

function isPlainObject(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A configured name as the server reads it (`_text`: composed, trimmed), or
 * undefined when it is no one-line string of 1 to `limit` characters. */
function configuredName(value: unknown, limit: number): string | undefined {
    if (typeof value !== "string") {
        return undefined;
    }
    const text = value.normalize("NFC").trim();
    return text && text.length <= limit && !NOT_ONE_LINE_RE.test(text) ? text : undefined;
}

/**
 * A pin the server stores as it was given (`_exact`): refused unless it
 * already is the checked form. `label` starts the message; the value is
 * never part of it.
 */
function exactPinError(value: string, label: string): string {
    if (NOT_ONE_LINE_RE.test(value)) {
        return `${label} must be one line without control characters.`;
    }
    if (value.trim() !== value || value.normalize("NFC") !== value) {
        return `${label} must have no leading or trailing whitespace and use `
            + "composed (NFC) characters.";
    }
    return "";
}

/**
 * Client-side mirrors of the Pydantic rules in `agents/admin.py`.
 *
 * These exist so admins see inline field errors instead of a 422 toast. The
 * server remains authoritative: anything these miss is still rejected there and
 * surfaced through `AdminError.fieldErrors`.
 *
 * `MCP_URL_ALLOWLIST` is deliberately NOT mirrored here — it is read from the
 * environment at request time and belongs to the server alone.
 */

/**
 * Closed set; an unknown `builtin:` value is a typo, not an extension point.
 * Taken from the catalog in `./builtins` so the dropdown and the validator
 * cannot disagree about what exists.
 */
const BUILTIN_URLS: readonly string[] = BUILTINS.map((b) => b.url);

/**
 * Config keys a built-in may carry on `auth_mode: "none"`.
 *
 * Mirrors `_BUILTIN_PUBLIC_KEYS` in `agents/db.py`. A public data source needs
 * no credential, so the only thing worth storing is how to narrow it — and the
 * whitelist is what stops `none` becoming a general-purpose place to stash
 * settings on any server.
 */
const BUILTIN_PUBLIC_KEYS = ["min_score", "lookback"] as const;

export default {

    BUILTIN_URLS,

    BUILTIN_PUBLIC_KEYS,

    /** True for `builtin:teams`, whose target is a pinned team, not a mailbox. */
    isTeams(url: string): boolean {
        return (url || "").trim().replace(/\/+$/, "").toLowerCase() === "builtin:teams";
    },

    /** Returns an error message, or an empty string when the config is valid. */
    validateTeams(oauth: OAuthClient | undefined, authMode: AuthMode): string {
        if (authMode !== "oauth2" && authMode !== "app_only" && authMode !== "destination") {
            return "Teams requires auth mode 'oauth2' (as the signed-in user), "
                + "'app_only' (read-only, as the application) or 'destination'.";
        }
        if (oauth && "dcr" in oauth && oauth.dcr === true) {
            return "Teams cannot use dynamic registration: Microsoft Entra ID does "
                + "not offer it.";
        }
        const cfg = (oauth || {}) as Exclude<OAuthClient, { dcr: true }>;
        if (!(cfg.team || "").trim()) {
            return "Teams requires a team ID: the one team this agent may read is "
                + "pinned here, never chosen by the agent.";
        }
        if (authMode === "app_only" && cfg.allow_send === true) {
            return "Teams cannot post as the application: Graph does not allow it. "
                + "Use oauth2 to post as the signed-in user, or turn sending off.";
        }
        if (authMode === "destination" && cfg.allow_send === true && cfg.user_context !== true) {
            return "Teams cannot post through a destination without acting as the "
                + "signed-in user: the destination's app-level credential is an "
                + "application token, and Graph does not allow application posts. "
                + "Turn 'Act as signed-in user' on, or turn sending off.";
        }
        return "";
    },

    // --- sharepoint ---
    /** True for `builtin:sharepoint`, whose target is one pinned workbook. */
    isSharePoint(url: string): boolean {
        return (url || "").trim().replace(/\/+$/, "").toLowerCase() === "builtin:sharepoint";
    },

    /**
     * The rules of a `builtin:sharepoint` entry that can be said while the
     * field is on screen. Mirrors `_validate_sharepoint` in `agents/admin.py`;
     * what is inside a view is the server's alone. Returns an error message,
     * or an empty string when the config is valid.
     */
    validateSharePoint(oauth: OAuthClient | undefined, authMode: AuthMode): string {
        if (authMode !== "app_only" && authMode !== "destination") {
            return "SharePoint requires auth mode 'destination' or 'app_only': it "
                + "reads the pinned workbook as the application.";
        }
        const cfg = (oauth || {}) as Record<string, unknown>;
        // As typed: a pin is stored and sent in exactly the form that was
        // checked, so nothing is trimmed here before it is looked at.
        const text = (value: unknown): string => (typeof value === "string" ? value : "");
        if (cfg.user_context === true) {
            return "SharePoint has no signed-in user to act as; turn 'Act as "
                + "signed-in user' off.";
        }
        if (!SHAREPOINT_SITE_RE.test(text(cfg.site))) {
            return "Site must be <tenant>.sharepoint.com:/sites/<site>.";
        }
        const library = text(cfg.library);
        if (!library.trim()) {
            return "SharePoint requires the document library's name.";
        }
        const libraryError = exactPinError(library, "Library");
        if (libraryError) {
            return libraryError;
        }
        const path = text(cfg.path);
        if (!/\.xlsx$/i.test(path.trim())) {
            return "Path must be the path of an .xlsx file below the library root.";
        }
        const pathError = exactPinError(path, "Path");
        if (pathError) {
            return pathError;
        }
        const views = cfg.views;
        if (!isPlainObject(views) || Object.keys(views).length === 0) {
            return "SharePoint requires at least one view.";
        }
        return this.validateSharePointViews(views);
    },

    /**
     * The rules of a calendar view that pin what a sheet's label text may
     * put in a result. Mirrors `_calendar` in `agents/sharepoint_views.py`:
     * a view with a kind label lists its `kinds`, `conflict` names two of
     * them, and a looked-up column is not named like a run field. Every
     * other rule of a view is the server's alone. The messages name the
     * field and the rule, never a value or a view.
     */
    validateSharePointViews(views: Record<string, unknown>): string {
        for (const view of Object.values(views)) {
            if (!isPlainObject(view) || view.kind !== "calendar") {
                continue;
            }
            let kinds: string[] = [];
            if (isPlainObject(view.labels) && "kind" in view.labels) {
                const raw = view.kinds;
                const names = Array.isArray(raw)
                    ? raw.map((item) => configuredName(item, SHAREPOINT_MAX_KIND_CHARS))
                    : [];
                if (names.length === 0 || names.length > SHAREPOINT_MAX_KINDS
                    || names.some((name) => name === undefined)) {
                    return `Views: 'kinds' of a calendar view with a kind label must be a list of 1 to `
                        + `${SHAREPOINT_MAX_KINDS} names, each one line of at most `
                        + `${SHAREPOINT_MAX_KIND_CHARS} characters.`;
                }
                kinds = names as string[];
                if (new Set(kinds).size !== kinds.length) {
                    return "Views: 'kinds' must not repeat a value.";
                }
            } else if ("kinds" in view) {
                return "Views: 'kinds' is only allowed with labels.kind.";
            }
            const lookup = view.lookup;
            if (isPlainObject(lookup) && Array.isArray(lookup.add) && lookup.add.some(
                (column) => typeof column === "string" && SHAREPOINT_RUN_KEYS.indexOf(
                    column.normalize("NFC").trim().toLowerCase()) > -1)) {
                return "Views: 'lookup.add' must not add a column named like a run field "
                    + `(${SHAREPOINT_RUN_KEYS.join(", ")}).`;
            }
            const conflict = view.conflict;
            if (isPlainObject(conflict) && kinds.length) {
                for (const key of ["kind", "against"]) {
                    const name = configuredName(conflict[key], 64);
                    if (name !== undefined && kinds.indexOf(name) === -1) {
                        return `Views: 'conflict.${key}' must be one of kinds.`;
                    }
                }
            }
        }
        return "";
    },

    // --- destinations ---
    /** The per-built-in rules of destination mode. Mirrors
     * `_validate_destination_config` in `agents/admin.py`. */
    validateDestinationBuiltin(cfg: Exclude<OAuthClient, { dcr: true }>, url: string): string {
        const key = (url || "").trim().replace(/\/+$/, "").toLowerCase();
        if (!DESTINATION_NAME_RE.test((cfg.destination || "").trim())) {
            return "The destination name may only contain letters, digits, '_', '.' "
                + "and '-' (up to 200 characters).";
        }
        if (isRemoteUrl(key)) {
            // A remote MCP server: name and user context are all it has, and
            // either setting of the switch is valid (`_validate_destination_config`
            // returns early for a non-builtin url the same way).
            return "";
        }
        const userContext = cfg.user_context === true;
        if (userContext && DESTINATION_USER_CONTEXT_URLS.indexOf(key) === -1) {
            return "This toolset has no signed-in user to act as; turn 'Act as "
                + "signed-in user' off.";
        }
        if (DESTINATION_MAILBOX_URLS.indexOf(key) > -1 && !userContext
            && !(cfg.mailbox || "").trim()) {
            return "A destination with an app-level credential identifies no user: "
                + "name the mailbox, or turn 'Act as signed-in user' on.";
        }
        return "";
    },

    // --- odata ---
    /**
     * The rules of a `builtin:odata` entry. Mirrors `_validate_odata_entry`
     * in `agents/admin.py`: destination mode, at least one service, only
     * `services` and `allow_write`, and `allow_write` a real boolean. The
     * messages name the field, never a value.
     */
    validateODataEntry(oauth: OAuthClient | undefined, authMode: AuthMode): string {
        if (authMode !== "destination") {
            return "OData services requires auth mode 'destination': every catalogue "
                + "service is reached through the BTP destination it names.";
        }
        const cfg = (oauth || {}) as Record<string, unknown>;
        // `has_client_secret: false` is the server's own echo on every stored
        // block (`_redact_servers`); it accepts it back, as this does.
        const stray = Object.keys(cfg).filter((k) => k !== "services" && k !== "allow_write"
            && !(k === "has_client_secret" && cfg[k] === false));
        if (stray.length) {
            return "An OData services entry holds only the services and 'Allow writes': "
                + "the destination and the identity belong to each catalogue service.";
        }
        if (cfg.allow_write !== undefined && typeof cfg.allow_write !== "boolean") {
            return "'Allow writes' must be on or off; any other value does not open writes.";
        }
        const services = cfg.services;
        if (!Array.isArray(services) || services.length === 0) {
            return "Select at least one OData service.";
        }
        if (services.length > MAX_ODATA_ENTRY_SERVICES) {
            return `At most ${MAX_ODATA_ENTRY_SERVICES} OData services per agent.`;
        }
        if (services.some((name) => typeof name !== "string" || !ODATA_SERVICE_NAME_RE.test(name))) {
            return "An OData service name is not valid (lower-case letters, digits and '-').";
        }
        if (new Set(services).size !== services.length) {
            return "An OData service is listed twice.";
        }
        return "";
    },

    /** True when this url may carry a public config block on `none`. */
    carriesPublicConfig(url: string): boolean {
        return (BUILTIN_URLS as readonly string[])
            .indexOf((url || "").trim().toLowerCase()) > -1;
    },

    /** Returns an error message, or an empty string when the url is valid. */
    validateServerUrl(url: string, authMode: AuthMode): string {
        const value = (url || "").trim().replace(/\/+$/, "");
        if (!value) {
            return "A server URL is required.";
        }
        if (value.toLowerCase().startsWith("builtin")) {
            return (BUILTIN_URLS as readonly string[]).indexOf(value.toLowerCase()) > -1
                ? ""
                : `Unknown built-in toolset. Known: ${BUILTIN_URLS.join(", ")}.`;
        }
        if (authMode === "none") {
            return /^https?:\/\//.test(value)
                ? ""
                : "URL must start with http:// or https://.";
        }
        return value.startsWith("https://")
            ? ""
            : "URL must use https:// (set auth mode to 'none' for public servers).";
    },

    /** Returns an error message, or an empty string when the config is valid. */
    validateOAuth(oauth: OAuthClient | undefined, authMode: AuthMode, url = ""): string {
        if (odataEntry.isODataUrl(url)) {
            // Its own rules and none of the destination ones below: this
            // entry names no destination.
            return this.validateODataEntry(oauth, authMode);
        }
        if (this.isTeams(url)) {
            const teamsError = this.validateTeams(oauth, authMode);
            if (teamsError) {
                return teamsError;
            }
        }
        if (this.isSharePoint(url)) {
            const sharePointError = this.validateSharePoint(oauth, authMode);
            if (sharePointError) {
                return sharePointError;
            }
        }
        if (authMode === "app_only") {
            if (!oauth || ("dcr" in oauth && oauth.dcr === true)) {
                return oauth
                    ? "App-only auth cannot use dynamic registration: a client "
                      + "registered on the fly holds no admin-consented permissions."
                    : "App-only auth requires a client ID and secret.";
            }
            const app = oauth as Exclude<OAuthClient, { dcr: true }>;
            if (!(app.client_id || "").trim()) {
                return "App-only auth requires a client ID.";
            }
            if (!(app.token_url || "").trim() && !(app.uaa_url || "").trim()) {
                return "App-only auth requires a token URL (no authorize URL: "
                    + "nobody signs in).";
            }
            const isBuiltin = (url || "").trim().toLowerCase().startsWith("builtin:");
            if (isBuiltin && !this.isTeams(url) && !this.isSharePoint(url)
                && !(app.mailbox || "").trim()) {
                return "App-only auth requires a mailbox: the token identifies no "
                    + "user, so there is no 'me' to fall back to.";
            }
            return "";
        }
        if (authMode === "destination") {
            if (!oauth) {
                return "A destination server requires a destination name.";
            }
            if ("dcr" in oauth && oauth.dcr === true) {
                return "A destination server cannot use dynamic registration: the "
                    + "destination already holds the target's credential.";
            }
            const dest = oauth as Exclude<OAuthClient, { dcr: true }>;
            if (!(dest.destination || "").trim()) {
                return "A destination server requires a destination name: it names "
                    + "the BTP destination holding the target's URL and credential.";
            }
            if ((dest.client_id || "").trim() || (dest.client_secret || "").trim()) {
                return "A destination server stores no credential of its own. Keep "
                    + "the secret in the destination, where it can be rotated "
                    + "without touching this app.";
            }
            const builtinError = this.validateDestinationBuiltin(dest, url);
            if (builtinError) {
                return builtinError;
            }
            // Caught here as well as server-side so the dialog says what is
            // wrong while the field is still on screen. A URL is the mistake
            // worth naming: it would aim the destination's credential at a
            // host the destination never mentioned.
            const apiBase = (dest.api_base || "").trim();
            if (apiBase && (apiBase.includes("://") || apiBase.startsWith("//"))) {
                return "The API base path is a path, not a URL: the host comes "
                    + "from the destination. Try /rest/api/2 or /api/2.";
            }
            if (apiBase && !apiBase.startsWith("/")) {
                return "The API base path must start with '/', e.g. /rest/api/2.";
            }
            // Same cap as the server, said while the field is still on screen.
            // The limit is about a paste accident becoming a query nobody can
            // read in a run record, not about anything Jira refuses.
            const countValues = (v: string): number => {
                const seen = new Set<string>();
                v.split(",").forEach((p) => {
                    const t = p.trim();
                    if (t) { seen.add(t); }
                });
                return seen.size;
            };
            if (countValues(dest.labels || "") > 20) {
                return "At most 20 comma-separated labels.";
            }
            if (countValues(dest.status || "") > 20) {
                return "At most 20 comma-separated statuses.";
            }
            return "";
        }
        if (authMode !== "oauth2") {
            // A built-in on `none` is the one case where a config block is
            // legitimate without a credential: the source is public and the
            // only settings are how to narrow it. Anything outside the
            // whitelist is still an error, and still says so.
            const publicBuiltin = authMode === "none" && this.carriesPublicConfig(url);
            const set = Object.keys(oauth || {}).filter((k) => {
                const v = (oauth as Record<string, unknown>)[k];
                return k !== "has_client_secret" && v !== "" && v !== undefined && v !== false;
            });
            if (!set.length) {
                return "";
            }
            if (!publicBuiltin) {
                return "OAuth configuration is only valid when auth mode is 'oauth2'.";
            }
            const stray = set.filter(
                (k) => (BUILTIN_PUBLIC_KEYS as readonly string[]).indexOf(k) === -1
            );
            if (stray.length) {
                return `A public built-in stores no credential. Remove: ${stray.join(", ")}.`;
            }
            const score = String((oauth as Record<string, unknown>).min_score ?? "").trim();
            if (score && !(Number(score) >= 0 && Number(score) <= 10)) {
                return "The minimum CVSS score must be a number between 0 and 10.";
            }
            return "";
        }
        if (!oauth) {
            return "An oauth2 server requires a client ID, or enable dynamic registration.";
        }
        if ("dcr" in oauth && oauth.dcr === true) {
            return "";
        }
        const manual = oauth as Exclude<OAuthClient, { dcr: true }>;
        if (!(manual.client_id || "").trim()) {
            return "An oauth2 server requires a client ID, or enable dynamic registration.";
        }
        const hasUaa = !!(manual.uaa_url || "").trim();
        const hasBoth = !!(manual.authorize_url || "").trim() && !!(manual.token_url || "").trim();
        return hasUaa || hasBoth
            ? ""
            : "Provide a UAA URL, or both an authorize URL and a token URL.";
    },

    /**
     * Validates a whole server list.
     *
     * Keyed by index; the pseudo-index -1 carries list-level errors so a form
     * can show "at least one server is required" without a control to attach to.
     */
    validateServers(servers: McpServer[]): Record<number, string> {
        const errors: Record<number, string> = {};
        if (!servers || servers.length === 0) {
            errors[-1] = "At least one MCP server or built-in toolset is required.";
            return errors;
        }
        const seen = new Set<string>();
        servers.forEach((server, index) => {
            const urlError = this.validateServerUrl(server.url, server.auth_mode);
            if (urlError) {
                errors[index] = urlError;
                return;
            }
            const oauthError = this.validateOAuth(server.oauth, server.auth_mode, server.url);
            if (oauthError) {
                errors[index] = oauthError;
                return;
            }
            const key = (server.url || "").trim().replace(/\/+$/, "").toLowerCase();
            if (seen.has(key)) {
                errors[index] = key === odataEntry.ODATA_URL
                    ? "An agent has one OData services entry; add the services to the existing one."
                    : "This is a duplicate URL; each server may appear once.";
                return;
            }
            seen.add(key);
        });
        return errors;
    }
};

// --- step kinds ---
/**
 * Client-side mirror of the per-step rules in `validate_workflow_parts`
 * (agents/db.py) and the config models in agents/step_kinds.py, for the
 * workflow editor: an agent is required only for kind "agent", the fan-out
 * step must be an agent, timeouts have the server's bounds, a python step
 * needs code, an http step needs a destination and a confined relative path.
 *
 * Keyed by step index in the submitted list; the message is what the server
 * would say, minus the position prefix the caller adds. The server remains
 * authoritative -- python code, say, is only compiled there.
 */
export function validateWorkflowSteps(steps: WorkflowStep[]): Record<number, string> {
    const errors: Record<number, string> = {};
    const kinds: string[] = ["agent", "condition", "transform", "http", "python"];
    const ops: string[] = [
        "contains", "not_contains", "equals", "not_equals", "matches",
        "not_matches", "gt", "lt", "is_empty", "not_empty"
    ];
    steps.forEach((step, index) => {
        const kind = step.kind || "agent";
        const timeout = Number(step.step_timeout_seconds);
        if (kinds.indexOf(kind) === -1) {
            errors[index] = `Unknown step kind '${kind}'.`;
            return;
        }
        if (!(timeout >= 10 && timeout <= 1800)) {
            errors[index] = "The step timeout must be between 10 and 1800 seconds.";
            return;
        }
        if (kind === "agent") {
            if (!(step.agent_name || "").trim()) {
                errors[index] = "An agent step must name an agent.";
            }
            return;
        }
        if (step.fan_out) {
            errors[index] = `The fan-out step must be an agent step; this is a ${kind} step.`;
            return;
        }
        const c = (step.config || {}) as Record<string, unknown>;
        if (kind === "python") {
            if (!String(c.code || "").trim()) {
                errors[index] = "A python step needs code that assigns `output`.";
                return;
            }
            const t = Number(c.timeout_seconds);
            if (!(t >= 1 && t <= 60)) {
                errors[index] = "A python step's timeout must be between 1 and 60 seconds.";
            }
            return;
        }
        if (kind === "http") {
            if (!String(c.destination || "").trim()) {
                errors[index] = "An http step needs a destination name.";
                return;
            }
            const path = String(c.path || "").trim();
            // Templates are rendered before the request; probe the static
            // shape with placeholders blanked, as agents/step_kinds.py does.
            const probe = path.replace(/\{\{[^}]*\}\}/g, "x");
            if (probe.includes("://") || probe.startsWith("//") || probe.includes("\\")) {
                errors[index] = "The http path is a path, not a URL: the host comes from the destination.";
                return;
            }
            if (!probe.startsWith("/")) {
                errors[index] = "The http path must start with '/'.";
                return;
            }
            if (probe.includes("?") || probe.includes("#")) {
                errors[index] = "Put query parameters in the query field, not in the path.";
                return;
            }
            if (probe.split("/").some((s) => s === ".." || s === ".")) {
                errors[index] = "The http path must not contain '.' or '..' segments.";
                return;
            }
            const t = Number(c.timeout_seconds);
            if (!(t >= 1 && t <= 600)) {
                errors[index] = "An http step's timeout must be between 1 and 600 seconds.";
            }
            return;
        }
        if (kind === "transform") {
            const truncate = c.truncate;
            if (truncate !== null && truncate !== undefined && !(Number(truncate) >= 1)) {
                errors[index] = "Truncate must be a positive number of characters.";
                return;
            }
            const regex = c.regex as { pattern?: string; flags?: string } | null | undefined;
            if (regex && !(regex.pattern || "").length) {
                errors[index] = "A regex needs a pattern.";
                return;
            }
            if (regex && /[^imsx]/.test(regex.flags || "")) {
                errors[index] = "Regex flags may only be i, m, s or x.";
            }
            return;
        }
        // condition
        const rules = Array.isArray(c.rules) ? (c.rules as { when?: { op?: string } }[]) : [];
        const bad = rules.findIndex((r) => ops.indexOf(String(r.when?.op || "")) === -1);
        if (bad > -1) {
            errors[index] = `Rule ${bad + 1} has an unknown operator.`;
        }
    });
    return errors;
}

// --- deep agents -------------------------------------------------------------

/** Same bounds as `agents.deep.DeepConfig` (pydantic `ge`/`le`). */
export const DEEP_MAX_SUBAGENTS = 20;
export const DEEP_MAX_DEPTH = 3;

/**
 * Validates the deep-agent panel. Keyed by field name so the controller can
 * attach each message to its control; empty when everything is in range.
 * Only the numbers can be wrong: the switches are booleans and the prompt is
 * free text.
 */
export function validateDeep(deep: DeepConfig | undefined): Record<string, string> {
    const errors: Record<string, string> = {};
    if (!deep) {
        return errors;
    }
    const isInt = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v);
    if (!isInt(deep.max_subagents) || deep.max_subagents < 1 || deep.max_subagents > DEEP_MAX_SUBAGENTS) {
        errors.max_subagents = `Concurrent sub-agents must be a whole number from 1 to ${DEEP_MAX_SUBAGENTS}.`;
    }
    if (!isInt(deep.subagent_max_depth) || deep.subagent_max_depth < 1 || deep.subagent_max_depth > DEEP_MAX_DEPTH) {
        errors.subagent_max_depth = `Sub-agent depth must be a whole number from 1 to ${DEEP_MAX_DEPTH}.`;
    }
    return errors;
}

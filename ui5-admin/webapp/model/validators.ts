import type { AuthMode, McpServer, OAuthClient } from "../service/types";

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

/** Closed set; an unknown `builtin:` value is a typo, not an extension point. */
const BUILTIN_URLS = [
    "builtin:gmail",
    "builtin:outlook",
    "builtin:jira",
    "builtin:sapnotes",
] as const;

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
            if (isBuiltin && !(app.mailbox || "").trim()) {
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
                errors[index] = "This is a duplicate URL; each server may appear once.";
                return;
            }
            seen.add(key);
        });
        return errors;
    }
};

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
const BUILTIN_URLS = ["builtin:gmail", "builtin:outlook"] as const;

export default {

    BUILTIN_URLS,

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
        if (authMode !== "oauth2") {
            const hasConfig = !!oauth && Object.keys(oauth).some((k) => {
                const v = (oauth as Record<string, unknown>)[k];
                return k !== "has_client_secret" && v !== "" && v !== undefined && v !== false;
            });
            return hasConfig
                ? "OAuth configuration is only valid when auth mode is 'oauth2'."
                : "";
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

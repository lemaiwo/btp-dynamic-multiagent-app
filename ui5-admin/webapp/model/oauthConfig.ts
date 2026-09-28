import type { AuthMode, McpServer } from "../service/types";
import validators from "./validators";
import { findBuiltin } from "./builtins";

/**
 * Turns the server dialog's flat form model into the config block the API
 * stores, per auth mode and per built-in.
 *
 * Kept out of the controller so the rules can be unit-tested: an admin who
 * reopens a server and presses OK must get back exactly what was stored, and
 * a key dropped here disappears with no message. Mirrors the per-mode key
 * lists in `agents/db.py` (`_OAUTH_KEYS`, `_CC_KEYS`, `_TEAMS_OAUTH2_KEYS`,
 * `_DEST_KEYS`, `_SLACK_DEST_KEYS`, `BUILTIN_PUBLIC_KEYS`) and the fields
 * `OAuthClientPayload` in `agents/admin.py` accepts. Whatever the dialog
 * fragment shows for a mode and url must be listed here, or it is lost on OK.
 */

/** Client fields shared by every manual (non-DCR) oauth2 server. */
const OAUTH2_CLIENT_KEYS = ["client_secret", "uaa_url", "authorize_url", "token_url", "scope"];

/**
 * Client-credentials fields. No authorize_url: nobody visits a browser. The
 * built-in-specific keys (`mailbox`, `lookback`, `recipients`, `team`,
 * `channels`) are kept for every app-only url, as `_CC_KEYS` does server-side.
 */
const APP_ONLY_KEYS = ["client_secret", "uaa_url", "token_url", "scope", "mailbox", "lookback",
    "recipients", "team", "channels"];

/**
 * Built-in keys kept next to the client fields on oauth2, per url. Each entry
 * must agree with what the fragment shows for that url on oauth2:
 * - teams pins its team and channels on either mode and keeps its window and
 *   posting switch on oauth2 too (`agents/teams_tools.py`);
 * - outlook reads `allow_send`, `recipients` and `lookback` regardless of
 *   mode (`outlook_toolset` in `agents/outlook_tools.py`), so an oauth2
 *   mailbox keeps its send config across a re-save.
 */
const OAUTH2_BUILTIN_KEYS: Record<string, string[]> = {
    "builtin:teams": ["team", "channels", "lookback"],
    "builtin:outlook": ["lookback", "recipients"]
};

/** Built-ins whose `allow_send` switch is shown on oauth2. */
const OAUTH2_ALLOW_SEND_URLS = ["builtin:teams", "builtin:outlook"];

function builtinKey(url: string): string {
    return findBuiltin(url)?.url ?? "";
}

function copyNonBlank(raw: Record<string, unknown>, keys: string[], out: Record<string, unknown>): void {
    keys.forEach((key) => {
        const value = String(raw[key] ?? "").trim();
        if (value) {
            out[key] = value;
        }
    });
}

export default {

    OAUTH2_BUILTIN_KEYS,

    /** The built-in keys `cleanOAuth` keeps on oauth2 for this url. */
    oauth2BuiltinKeys(url: string): string[] {
        return (OAUTH2_BUILTIN_KEYS[builtinKey(url)] ?? []).slice();
    },

    /** True when the send switch is kept (and shown) for this url and mode. */
    keepsAllowSend(url: string, authMode: AuthMode): boolean {
        const key = builtinKey(url);
        if (authMode === "app_only") {
            // Teams cannot post as the application; the validator says so
            // before this is ever sent, and the fragment hides the switch.
            return key !== "builtin:teams";
        }
        if (authMode === "oauth2") {
            return OAUTH2_ALLOW_SEND_URLS.indexOf(key) > -1;
        }
        return authMode === "destination" && key === "builtin:slack";
    },

    /** Drops blank fields so the server sees the same shape `to_config()` builds. */
    cleanOAuth(
        raw: Record<string, unknown>, authMode: AuthMode = "oauth2", url = ""
    ): McpServer["oauth"] {
        const key = builtinKey(url);
        if (authMode === "none") {
            // Whitelisted, not "everything that isn't blank": this block goes
            // to a server with no credential in it, and it must stay that way
            // even if the dialog model still holds fields from another mode.
            const out: Record<string, unknown> = {};
            copyNonBlank(raw, validators.BUILTIN_PUBLIC_KEYS.slice(), out);
            return (Object.keys(out).length ? out : undefined) as McpServer["oauth"];
        }
        if (authMode === "destination" && key === "builtin:slack") {
            // Slack keeps a channel pin and a posting switch instead of
            // Jira's filters. Only ever sent as `true`, as for app-only.
            const slack: Record<string, unknown> = {
                destination: String(raw.destination ?? "").trim(),
                channels: String(raw.channels ?? "").trim(),
                lookback: String(raw.lookback ?? "").trim()
            };
            if (raw.allow_send === true) {
                slack.allow_send = true;
            }
            return slack as McpServer["oauth"];
        }
        if (authMode === "destination") {
            return {
                destination: String(raw.destination ?? "").trim(),
                project: String(raw.project ?? "").trim(),
                status: String(raw.status ?? "").trim(),
                lookback: String(raw.lookback ?? "").trim(),
                api_base: String(raw.api_base ?? "").trim(),
                labels: String(raw.labels ?? "").trim(),
                allow_comment: raw.allow_comment === true
            } as McpServer["oauth"];
        }
        const appOnly = authMode === "app_only";
        // DCR is meaningless app-only: a client registered on the fly holds no
        // admin-consented application permissions, so its tokens reach nothing.
        if (!appOnly && raw.dcr === true) {
            const scope = String(raw.scope ?? "").trim();
            return scope ? { dcr: true, scope } : { dcr: true };
        }
        const out: Record<string, unknown> = { client_id: String(raw.client_id ?? "").trim() };
        copyNonBlank(
            raw,
            appOnly ? APP_ONLY_KEYS : OAUTH2_CLIENT_KEYS.concat(this.oauth2BuiltinKeys(url)),
            out
        );
        // Only ever sent as `true`. Omitting it when off keeps the stored
        // config identical to what a config file would carry, so an exported
        // agent does not gain a field it never asked for.
        if (this.keepsAllowSend(url, authMode) && raw.allow_send === true) {
            out.allow_send = true;
        }
        return out as McpServer["oauth"];
    }
};

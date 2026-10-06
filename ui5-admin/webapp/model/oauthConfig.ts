import type { AuthMode, McpServer } from "../service/types";
import validators from "./validators";
import { findBuiltin } from "./builtins";
import { isRemoteUrl } from "./remoteUrl";
import odataEntry from "./odataEntry";

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
const OAUTH2_ALLOW_SEND_URLS = ["builtin:teams", "builtin:outlook", "builtin:gmail"];

// --- destinations ---
/**
 * Keys kept on `destination` per built-in, next to `destination` itself.
 * Mirrors `_DEST_KEYS_BY_URL` in `agents/db.py`. Jira (the default below)
 * and Slack keep the shapes they always had.
 */
const DESTINATION_BUILTIN_KEYS: Record<string, string[]> = {
    "builtin:gmail": ["mailbox"],
    "builtin:outlook": ["mailbox", "lookback", "recipients"],
    "builtin:teams": ["team", "channels", "lookback"],
    "builtin:sapnotes": ["min_score", "lookback"],
    "builtin:sapnotedetail": [],
    "builtin:smtp": ["recipients", "from"]
};

/** Built-ins whose destination may act as the signed-in user. */
const DESTINATION_USER_CONTEXT_URLS = ["builtin:gmail", "builtin:outlook", "builtin:teams"];

/**
 * Built-ins that originate report mail and so keep a `theme` object (see
 * `MailTheme` in `agents/mail_render.py`) on whatever mode they run. Mirrors
 * `_MAIL_THEME_URLS` in `agents/db.py` and `agents/admin.py`.
 */
const MAIL_THEME_URLS = ["builtin:smtp", "builtin:outlook"];

function isPlainObject(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

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

    /** The built-in keys `cleanOAuth` keeps on destination for this url. */
    destinationBuiltinKeys(url: string): string[] | undefined {
        const keys = DESTINATION_BUILTIN_KEYS[builtinKey(url)];
        return keys ? keys.slice() : undefined;
    },

    /**
     * True for a remote MCP server (an http(s) url), as opposed to a
     * `builtin:` toolset. The shared {@link isRemoteUrl} helper.
     */
    isRemote(url: string): boolean {
        return isRemoteUrl(url);
    },

    /**
     * The starting value of "Act as signed-in user" for a NEW server: on for a
     * remote MCP url behind a destination, off everywhere else (built-ins keep
     * their own defaults). Never applied to a stored server.
     */
    defaultUserContext(authMode: AuthMode | string, url: string): boolean {
        return authMode === "destination" && this.isRemote(url);
    },

    /** True when this url's destination may act as the signed-in user. */
    supportsUserContext(url: string): boolean {
        return this.isRemote(url) || DESTINATION_USER_CONTEXT_URLS.indexOf(builtinKey(url)) > -1;
    },

    /**
     * True when the send switch is kept (and shown) for this url and mode.
     * On `destination`, `userContext` decides for Teams: an app-level
     * destination credential is an application token, and Graph refuses
     * application posts -- the same rule as app-only.
     */
    keepsAllowSend(url: string, authMode: AuthMode, userContext = false): boolean {
        const key = builtinKey(url);
        if (authMode === "app_only") {
            // Teams cannot post as the application; the validator says so
            // before this is ever sent, and the fragment hides the switch.
            return key !== "builtin:teams";
        }
        if (authMode === "oauth2") {
            return OAUTH2_ALLOW_SEND_URLS.indexOf(key) > -1;
        }
        if (authMode === "destination") {
            return key === "builtin:slack" || key === "builtin:outlook" || key === "builtin:smtp"
                || (key === "builtin:teams" && userContext === true);
        }
        return false;
    },

    /** True when this url sends report mail and so takes a mail theme. */
    supportsMailTheme(url: string): boolean {
        return MAIL_THEME_URLS.indexOf(builtinKey(url)) > -1;
    },

    /**
     * The "Mail theme (JSON)" textarea as a theme object. Blank is no theme.
     * Only the JSON shape is checked here; the keys and values (hex colours,
     * an https logo, lengths) are the server's `MailTheme.from_config`, whose
     * 422 names the key.
     */
    parseMailTheme(text: string): { theme?: Record<string, unknown>; error: string } {
        const source = (text || "").trim();
        if (!source) {
            return { error: "" };
        }
        let parsed: unknown;
        try {
            parsed = JSON.parse(source);
        } catch (e) {
            return { error: `Mail theme is not valid JSON: ${(e as Error).message}` };
        }
        if (!isPlainObject(parsed)) {
            return { error: "Mail theme must be a JSON object, e.g. {\"band\": \"#1f3348\"}." };
        }
        return { theme: parsed, error: "" };
    },

    /** A stored theme as the textarea shows it; blank when there is none. */
    formatMailTheme(theme: unknown): string {
        return isPlainObject(theme) && Object.keys(theme).length
            ? JSON.stringify(theme, null, 2)
            : "";
    },

    /** Drops blank fields so the server sees the same shape `to_config()` builds. */
    cleanOAuth(
        raw: Record<string, unknown>, authMode: AuthMode = "oauth2", url = ""
    ): McpServer["oauth"] {
        const out = this.cleanOAuthFields(raw, authMode, url) as Record<string, unknown> | undefined;
        // The theme rides along on every mode a mail built-in runs, except
        // DCR, which the server stores as `{dcr, scope}` only.
        if (out && out.dcr !== true && this.supportsMailTheme(url)
            && isPlainObject(raw.theme) && Object.keys(raw.theme).length) {
            out.theme = Object.assign({}, raw.theme);
        }
        return out as McpServer["oauth"];
    },

    cleanOAuthFields(
        raw: Record<string, unknown>, authMode: AuthMode = "oauth2", url = ""
    ): McpServer["oauth"] {
        const key = builtinKey(url);
        if (key === odataEntry.ODATA_URL) {
            // --- odata --- First, and whatever the mode: this entry is the
            // agent-side write switch. Exactly the services and `allow_write`
            // as a real boolean (true only for exactly `true`); no
            // destination, no user_context, nothing left from another type.
            return odataEntry.clean(raw) as McpServer["oauth"];
        }
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
        const destinationKeys = this.destinationBuiltinKeys(url);
        if (authMode === "destination" && destinationKeys) {
            // --- destinations ---
            // The built-ins that gained destination mode later: the name,
            // their own pinned keys, and the two switches only ever as `true`.
            const out: Record<string, unknown> = {
                destination: String(raw.destination ?? "").trim()
            };
            copyNonBlank(raw, destinationKeys, out);
            const userContext = this.supportsUserContext(url) && raw.user_context === true;
            if (userContext) {
                out.user_context = true;
            }
            if (this.keepsAllowSend(url, authMode, userContext) && raw.allow_send === true) {
                out.allow_send = true;
            }
            return out as McpServer["oauth"];
        }
        if (authMode === "destination" && this.isRemote(url)) {
            // --- destinations: a remote MCP server ---
            // The destination holds the URL and the credential, so its name
            // and whose credential it hands back are all there is. Exactly
            // what `_clean_destination` in agents/db.py stores: losing
            // `user_context` on a re-save would quietly swap every caller's
            // identity for the destination's technical user.
            const remote: Record<string, unknown> = {
                destination: String(raw.destination ?? "").trim()
            };
            if (raw.user_context === true) {
                remote.user_context = true;
            }
            return remote as McpServer["oauth"];
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

import type { AuthMode } from "../service/types";

/**
 * The built-in toolsets, as the server dialog offers them.
 *
 * Mirrors `_FACTORIES` in `agents/builtins.py`: the set is closed, so an entry
 * here without a factory there is a save the server refuses. `authModes` is
 * the list of modes the built-in's factory can actually build with: some are
 * refused by `McpServerPayload` in `agents/admin.py` at save time, others
 * (gmail, outlook, sapnotes) are accepted there and only fail at reload, when
 * the registry logs the error and drops the agent while the UI still shows it
 * configured. Leaving `authModes` out means the server does not restrict it.
 * `defaultAuthMode` is what the dialog selects when the entry is picked.
 */
export interface BuiltinToolset {
    url: string;
    /** i18n key of the name shown in the toolset dropdown. */
    titleKey: string;
    /** i18n key of the one-line explanation shown under the dropdown. */
    descriptionKey: string;
    defaultAuthMode: AuthMode;
    authModes?: AuthMode[];
}

// --- destinations ---
// Every built-in can also run through a BTP destination (`destination`): the
// destination holds the URL and credential, and with "Act as signed-in user"
// (`user_context`) it is resolved with the user's JWT and acts as that user.
// See agents/destination_auth.py and the table in agents/builtins.py.
export const BUILTINS: BuiltinToolset[] = [
    // gmail_toolset refuses app_only (Google app-only access needs domain-wide
    // delegation) and reads a per-user token, or a destination -- see
    // agents/gmail_tools.py.
    {
        url: "builtin:gmail", titleKey: "builtinGmail", descriptionKey: "builtinGmailDesc",
        defaultAuthMode: "oauth2", authModes: ["oauth2", "destination"]
    },
    // outlook_toolset builds a per-user, an app-only or a destination-backed
    // Graph client -- see build_http_client in agents/outlook_tools.py.
    {
        url: "builtin:outlook", titleKey: "builtinOutlook", descriptionKey: "builtinOutlookDesc",
        defaultAuthMode: "oauth2", authModes: ["oauth2", "app_only", "destination"]
    },
    {
        url: "builtin:teams", titleKey: "builtinTeams", descriptionKey: "builtinTeamsDesc",
        defaultAuthMode: "oauth2", authModes: ["oauth2", "app_only", "destination"]
    },
    {
        url: "builtin:slack", titleKey: "builtinSlack", descriptionKey: "builtinSlackDesc",
        defaultAuthMode: "destination", authModes: ["destination"]
    },
    {
        url: "builtin:jira", titleKey: "builtinJira", descriptionKey: "builtinJiraDesc",
        defaultAuthMode: "destination", authModes: ["destination"]
    },
    // NVD is public: sapnotes_toolset's config block is the credential-free
    // `none` shape (min_score, lookback); a destination only adds a proxy URL
    // and an apiKey header.
    {
        url: "builtin:sapnotes", titleKey: "builtinSapNotes", descriptionKey: "builtinSapNotesDesc",
        defaultAuthMode: "none", authModes: ["none", "destination"]
    },
    // The session cookie is stored here (`session`) or carried by a
    // destination as URL.headers.Cookie (`destination`).
    {
        url: "builtin:sapnotedetail", titleKey: "builtinSapNoteDetail", descriptionKey: "builtinSapNoteDetailDesc",
        defaultAuthMode: "session", authModes: ["session", "destination"]
    }
];

/** Built-ins whose destination may act as the signed-in user. Mirrors
 * `_DEST_USER_CONTEXT_URLS` in agents/db.py. */
export const DESTINATION_USER_CONTEXT_URLS = ["builtin:gmail", "builtin:outlook", "builtin:teams"];

/** Built-ins that read a mailbox and therefore need one named when the
 * destination's credential is app-level (no user context). */
export const DESTINATION_MAILBOX_URLS = ["builtin:gmail", "builtin:outlook"];

/** Every auth mode, in the order the dialog lists them. */
export const ALL_AUTH_MODES: AuthMode[] = ["jwt", "none", "oauth2", "app_only", "destination", "session"];

/** The i18n key naming each auth mode in the dropdown. */
export const AUTH_MODE_TEXT_KEYS: Record<AuthMode, string> = {
    jwt: "authJwt",
    none: "authNone",
    oauth2: "authOauth2",
    app_only: "authAppOnly",
    destination: "authModeDestination",
    session: "authSession"
};

/** The catalog entry for a url, or undefined for a remote MCP server. */
export function findBuiltin(url: string): BuiltinToolset | undefined {
    const key = (url || "").trim().replace(/\/+$/, "").toLowerCase();
    return BUILTINS.find((b) => b.url === key);
}

/**
 * The auth modes the server accepts for a url.
 *
 * A remote MCP server cannot use `destination` or `session`: the server
 * refuses both for anything that is not a built-in. `session` is also
 * refused for every built-in but the one that reads a session cookie, which
 * the catalog already says through its `authModes`.
 */
export function authModesFor(url: string): AuthMode[] {
    const builtin = findBuiltin(url);
    if (builtin?.authModes) {
        return builtin.authModes.slice();
    }
    return builtin
        ? ALL_AUTH_MODES.filter((m) => m !== "session")
        : ALL_AUTH_MODES.filter((m) => m !== "destination" && m !== "session");
}

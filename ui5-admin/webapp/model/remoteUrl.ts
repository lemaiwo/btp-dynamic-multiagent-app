/**
 * True for a remote MCP server url (http:// or https://, any case, surrounding
 * blanks ignored), as opposed to a `builtin:` toolset or an unfinished value.
 *
 * The one place the admin decides "remote": `oauthConfig.isRemote` (and with it
 * `cleanOAuth` and `supportsUserContext`), `validators.validateDestinationBuiltin`
 * and the server dialog's user_context switch (`formatter.userContextVisible`)
 * all go through it, so the switch is shown exactly where the value is kept.
 * Mirrors the remote branch of `_clean_destination` in `agents/db.py`.
 */
export function isRemoteUrl(url: string | null | undefined): boolean {
    return /^https?:\/\//i.test((url || "").trim());
}

# SAP note detail — Phase 2

`builtin:sapnotedetail` resolves the fixing support-package level for a SAP
security note. It exists to narrow the `UNKNOWN` bucket described in
[`docs/SAP_SECURITY_NOTES.md`](SAP_SECURITY_NOTES.md#8-known-limits): NVD
gives a note number and a CVSS score but never the support package that fixes
it, and that field lives only in SAP's backbone, behind a private endpoint.

Spec: `docs/superpowers/specs/2026-09-10-sap-note-detail-design.md`

---

## 1. What this adds

`get_note_details` fetches `me.sap.com/backend/raw/sapnotes/Detail` for a list
of note numbers and returns, per note, `support_packages` — the software
component and support-package/patch level that resolves it — plus `validity`
(which releases the note applies to). That is the one field a digest needs to
turn "this note is UNKNOWN for your system" into "this note needs support
package X on component Y", and it is the whole scope of Phase 2. Rewiring the
digest workflow to consume it is a separate piece of work; this delivers the
toolset only.

## 2. The credential

The session has to belong to a **dialog user**: an S-user or SAP Universal ID
that can complete an interactive login at `accounts.sap.com`.

**It cannot be the technical communication user in `SAP_NOTES_USER`.** That
account exists for machine-to-machine API calls and has no dialog logon —
pointing this at it does not degrade gracefully, it simply fails the login.
Use a real person's S-user (or a dedicated dialog account set up for this
purpose), configured as `SAP_DIALOG_USER` / `SAP_DIALOG_PWD` in `.env`, never
in the database.

## 3. Why runs are attended

The captured session is a browser cookie with a lifetime of hours, and this
report is a monthly job. By the time a scheduled run is due, the stored
session is always stale — there is no credential SAP offers for this endpoint
that survives unattended for a month. So the operating rhythm is always the
same two steps in order: **refresh the session, then trigger the run.** There
is no "set it and forget it" mode for this toolset; see Known limits below.

## 4. One-time setup

Playwright drives the login, and it is **deliberately absent from
`requirements.txt`** — a Chromium download does not belong in the Cloud
Foundry buildpack for an app that otherwise ships no browser. Install it only
on the operator machine that will run the refresh:

```bash
pip install playwright
playwright install chromium
```

Then set the dialog credential in `.env` (see `.env.example`):

```
SAP_DIALOG_USER=
SAP_DIALOG_PWD=
```

## 5. Refreshing

```bash
python scripts/sap_session.py refresh --base-url https://your-app-url --token <admin-token>
```

`--token` defaults to `ADMIN_TOKEN` in `.env` when omitted, but that variable
is not pre-declared in `.env.example` — set it yourself. It is just a bearer
token carrying the `$XSAPPNAME.admin` scope, the same kind any other
`/admin/api/...` call needs. Locally, with no `VCAP_SERVICES` binding, admin
routes are open and everyone is treated as an admin (see the README's "Local
auth" note), so any non-blank string satisfies the flag. On a deployed BTP
instance there is no shortcut through the browser: the approuter authenticates
`/admin` by session cookie and attaches the XSUAA JWT only on its own hop to
the backend, so it never appears in the browser's own dev tools. What you
need instead is a real XSUAA access token for a user in the **Agent
Administrator** role collection, obtained through your landscape's normal
XSUAA token-issuance path against the app's bound `uaa-service` (the same
authorization_code exchange that powers interactive `/admin` sign-in) — this
project ships no separate script to mint one standalone. Add `--headful` if
the account is expected to hit MFA — a visible browser window lets you finish
the challenge by hand; see Known limits for when this applies.

This logs in at `accounts.sap.com`, captures the resulting `me.sap.com`
session cookie, and POSTs it straight to the running app's
`POST /admin/api/sessions/builtin:sapnotedetail`. The cookie is never printed
to the console and never written to disk — it goes over HTTPS to the app and
nowhere else.

`--ttl-hours` (default 12, must be 1-48) controls how long the app treats the
stored session as valid. It exists because that default is the **upstream
project's cache TTL**, not a measured SAP session lifetime — nobody has
established how long a real `me.sap.com` session actually survives. If real
sessions die sooner than the TTL says, the credentials panel reports `valid`
and the workflow's preflight check passes for a window after SAP has already
dropped the session — the exact failure both mechanisms exist to prevent.
Lower it if you observe sessions going dead before the reported expiry.

`capture` (used to obtain a note for local testing, not part of the
operator's monthly refresh) writes its raw response under `tmp/` by default
— gitignored, because it is a live SAP session's output. Promoting a capture
to the tracked fixture (`tests/fixtures/sapnote_detail.json`) is a deliberate
step, never automatic: scrub it first — drop `Actions` entirely, and check
for any `token=`/`auth=`/`key=`/`sid=` query parameter, any `*.sap.corp`
host, any email address, and any S-user id — before it is committed.

## 6. Checking it took

Open `/admin`, find the agent using `builtin:sapnotedetail`, and look at its
credentials panel. `valid` with an expiry timestamp means the refresh landed;
`expired` (or no entry at all) means a scheduled or manual run will refuse to
start — the workflow's preflight check names the agent and says its
credential needs re-authorizing before the run proceeds.

## 7. Known limits and troubleshooting

- **The login is a two-step form, not one.** `accounts.sap.com` shows the
  username field and a `Continue` button first, with **no password field on
  that page at all** — filling one in there does nothing, and the password
  step only appears after `Continue` is submitted. This is a code comment in
  `scripts/sap_session.py` (`login()`), not enforced anywhere else. If SAP
  changes this form, `refresh`/`capture` will hang until `--timeout` expires
  with no clearer symptom than "did not reach me.sap.com" — that is the two-
  step assumption breaking, and the fix is to update the selectors in
  `login()`, not to suspect the credential.
- **Symptom: login appears to succeed but every note comes back
  `unavailable` / the raw `Detail` call returns ~723 bytes of HTML instead of
  JSON.** This looks exactly like a bad password, but is almost always the
  cookie-scoping trap: the captured browser session holds **two different
  cookies both named `JSESSIONID`** — one for `me.sap.com`, one for
  `accounts.sap.com` (the identity provider). Concatenating every `sap.com`
  cookie into one header sends the name twice, and `me.sap.com` reads the
  identity provider's session instead of its own, returning an anonymous
  bootstrap page indistinguishable from an auth failure. The fix already
  lives in code — `cookies_for_host()` in `scripts/sap_session.py` filters to
  cookies actually scoped to `me.sap.com` before serializing the header — so
  this should not recur through the shipped script, but it is the first
  thing to suspect if a *hand-rolled* cookie capture (e.g. copied by hand
  from browser dev tools) produces the same symptom.
- **Private API.** `me.sap.com/backend/raw/sapnotes/Detail` has no public
  contract, is not documented by SAP, and can change shape without notice.
  The upstream project this approach is based on warns explicitly to check
  use against SAP's Terms of Service — treat that warning as applying here
  too.
- **No MFA was challenged during development of this toolset.** The login
  automated fully headless for the account tested. That is a property of
  *that account's* settings, not a guarantee for every dialog user — some
  S-users and Universal IDs have MFA enforced, in which case headless login
  will hang and time out. `--headful` exists for exactly that case.
- **`unavailable` means unknown, not "not affected."** A note that could not
  be read — because the session died mid-run, the shape changed, or the note
  itself is inaccessible — is reported as `unavailable`. It must never be
  read as "this note doesn't apply."
- **A session that dies mid-run truncates the report.** `get_note_details`
  stops fetching further notes the moment the session goes bad and reports
  the rest as `unavailable`; the digest is expected to say the run was cut
  short, not present a partial list as complete.
- **The fixing level is unreliable from a different field than you'd guess.**
  It comes from `SupportPackagePatch`, not the plain `SupportPackage` field —
  the latter was empty on every note sampled while building this. Related:
  the `CVSS` value this private API returns is not trustworthy on its own
  (observed as 10.0 on one note and 0 on another with no clear pattern); NVD
  remains the source of truth for scoring, and this toolset is not used for
  it.

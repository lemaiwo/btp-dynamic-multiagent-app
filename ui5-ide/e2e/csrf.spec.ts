import { test, expect } from "@playwright/test";
import { byId, collectErrors, ensureTarget, field, openApp } from "./support/backend";

// The approuter's CSRF handshake, which does not exist locally (no approuter):
// page.route plays the approuter on top of the real backend. GET /me with
// `X-CSRF-Token: Fetch` answers a token in the response header; a state-
// changing call is refused once with `403` + `X-CSRF-Token: Required` (the
// token expired), which must make the UI fetch a new token and retry once.
// Every request that passes is continued to the real backend (route.fallback),
// so the session is really created.
const TARGET = "DEMO_CSRF";

test("CSRF: the first POST carries the fetched token; a Required refusal fetches a new one and retries once", async ({ page, request }) => {
    await ensureTarget(request, TARGET);
    const fetches: string[] = [];
    const posts: (string | null)[] = [];
    let issued = 0;

    await page.route("**/backend/me", async (route) => {
        if ((route.request().headers()["x-csrf-token"] ?? "").toLowerCase() !== "fetch") {
            return route.fallback();
        }
        const token = `csrf-token-${++issued}`;
        fetches.push(token);
        const response = await route.fetch();
        await route.fulfill({ response, headers: { ...response.headers(), "x-csrf-token": token } });
    });
    await page.route("**/backend/sessions", async (route) => {
        if (route.request().method() !== "POST") {
            return route.fallback();
        }
        const token = route.request().headers()["x-csrf-token"] ?? null;
        posts.push(token);
        if (token === "csrf-token-1") {
            // The approuter refuses before the backend sees the call.
            return route.fulfill({ status: 403, headers: { "x-csrf-token": "Required" }, body: "" });
        }
        return route.fallback();
    });

    // The refusal is logged by the browser as a failed resource (403).
    const errors = collectErrors(page, [403]);
    await openApp(page);
    // GET calls never fetch a token.
    expect(fetches).toEqual([]);

    const title = `CSRF ${Date.now()}`;
    await byId(page, "worklistNewButton").click();
    await field(page, "newSessionTitle").fill(title);
    await byId(page, "newSessionTarget").click();
    await page.locator(".sapMSelectList li[data-sap-ui]").filter({ hasText: new RegExp(`^${TARGET}$`) }).click();
    await byId(page, "newSessionCreate").click();
    await expect(byId(page, "sessionTitle")).toHaveText(title);

    // One token fetched for the first POST, refused once, a second fetch, one retry with the new token.
    expect(fetches).toEqual(["csrf-token-1", "csrf-token-2"]);
    expect(posts).toEqual(["csrf-token-1", "csrf-token-2"]);
    const sessions = await (await request.get("/backend/sessions")).json() as { title: string }[];
    expect(sessions.filter((s) => s.title === title)).toHaveLength(1);

    // The token is cached: the next state-changing call reuses it without fetching again.
    await byId(page, "backToWorklist").click();
    const second = `${title} again`;
    await byId(page, "worklistNewButton").click();
    await field(page, "newSessionTitle").fill(second);
    await byId(page, "newSessionTarget").click();
    await page.locator(".sapMSelectList li[data-sap-ui]").filter({ hasText: new RegExp(`^${TARGET}$`) }).click();
    await byId(page, "newSessionCreate").click();
    await expect(byId(page, "sessionTitle")).toHaveText(second);
    expect(fetches).toEqual(["csrf-token-1", "csrf-token-2"]);
    expect(posts).toEqual(["csrf-token-1", "csrf-token-2", "csrf-token-2"]);

    expect(errors).toEqual([]);
});

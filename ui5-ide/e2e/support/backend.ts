import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { expect, type APIRequestContext, type Locator, type Page, type Route } from "@playwright/test";

/**
 * Helpers the specs share. Every spec runs against the real Python backend
 * that playwright.config.ts starts on a throw-away SQLite file; the UI reaches
 * it through ui5-local.yaml's /backend proxy, the specs through `request`.
 *
 * No model and no ARC-1 exist locally, so the rows a model run or an SAP read
 * would leave are written straight into that SQLite file (`sql`), and only the
 * few calls that would need a model or SAP are answered with `page.route`
 * (each spec says which). Everything else the UI reads goes through the
 * backend.
 */
export const DB = path.join(__dirname, "..", "..", "_e2e_registry.db");

export function sql(statement: string): string {
    return execFileSync("sqlite3", ["-cmd", ".timeout 5000", DB, statement], { encoding: "utf8" }).trim();
}

export function quote(text: string): string {
    return `'${text.replace(/'/g, "''")}'`;
}

export function uuid(): string {
    return randomUUID();
}

/**
 * Console errors and page errors. `expected` lists the HTTP statuses a spec
 * provokes on purpose: the browser logs every non-2xx answer as an error.
 */
export function collectErrors(page: Page, expected: number[] = []): string[] {
    const errors: string[] = [];
    const allowed = expected.length ? new RegExp(`status of (${expected.join("|")})`) : null;
    page.on("console", (message) => {
        if (message.type() === "error" && !(allowed && allowed.test(message.text()))) {
            errors.push(message.text());
        }
    });
    page.on("pageerror", (error) => errors.push(error.message));
    return errors;
}

/** Opens the app at a hash (`""` = the worklist) and waits for the shell. */
export async function openApp(page: Page, hash = ""): Promise<void> {
    await page.goto(`/index.html${hash ? `#/${hash}` : ""}`);
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
}

/** A control's root element by its view-local id (UI5 marks roots with data-sap-ui). */
export function byId(page: Page, id: string) {
    return page.locator(`[data-sap-ui$="--${id}"]`);
}

/** The native input or textarea of an Input / TextArea / SearchField. */
export function field(page: Page, id: string) {
    return page.locator(`[id$="--${id}"][data-sap-ui]`).locator("input, textarea").first();
}

/**
 * A conventions target as the spec needs it. Locally there is no XSUAA, so
 * the caller is an admin: POST creates the target (PUT no longer does), PUT
 * sets the flag. A target left by an earlier spec of the same run is reused.
 */
export async function ensureTarget(request: APIRequestContext, target: string, nonProduction = false): Promise<void> {
    const created = await request.post("/backend/conventions", { data: { target, label: `${target} system`, package: "$TMP" } });
    expect([201, 409]).toContain(created.status());
    const put = await request.put(`/backend/conventions/${target}`, { data: { non_production: nonProduction } });
    expect(put.ok()).toBeTruthy();
}

export interface SessionJson {
    id: string; title: string; target: string; type: string; stage: string; status: string;
    open_comments: number; unresolved_comments: number; waiting: string | null;
    pins: Record<string, unknown>;
}

export async function createSession(
    request: APIRequestContext, title: string, target: string, type: "change" | "diagnose" = "change"
): Promise<SessionJson> {
    const created = await request.post("/backend/sessions", { data: { title, target, type } });
    expect(created.status()).toBe(201);
    return await created.json() as SessionJson;
}

export async function getSession(request: APIRequestContext, sid: string): Promise<SessionJson> {
    const response = await request.get(`/backend/sessions/${sid}`);
    expect(response.ok()).toBeTruthy();
    return await response.json() as SessionJson;
}

/** Moves a change session one stage on through the real approve route. */
export async function approve(request: APIRequestContext, sid: string): Promise<SessionJson> {
    const response = await request.post(`/backend/sessions/${sid}/approve`, { data: {} });
    expect(response.status(), await response.text()).toBe(200);
    return await response.json() as SessionJson;
}

/** A document as `submit_document` would store it. */
export function insertArtifact(
    sid: string, stage: string, kind: string, version: number, content: string, basedOn: Record<string, number> | null = null
): string {
    const id = uuid();
    sql(`INSERT INTO ide_artifacts (id, session_id, stage, kind, content, version, based_on_json)
         VALUES (${quote(id)}, ${quote(sid)}, ${quote(stage)}, ${quote(kind)}, ${quote(content)}, ${version},
                 ${basedOn ? quote(JSON.stringify(basedOn)) : "NULL"})`);
    return id;
}

export function insertMessage(sid: string, stage: string, role: "user" | "assistant", content: string, id = uuid()): string {
    sql(`INSERT INTO ide_messages (id, session_id, stage, role, content)
         VALUES (${quote(id)}, ${quote(sid)}, ${quote(stage)}, ${quote(role)}, ${quote(content)})`);
    return id;
}

/** A Server-Sent-Events body in the backend's framing. */
export function sseBody(frames: [string, unknown][]): string {
    return frames.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");
}

export function fulfillSse(route: Route, frames: [string, unknown][]): Promise<void> {
    return route.fulfill({ status: 200, contentType: "text/event-stream", body: sseBody(frames) });
}

export function fulfillJson(route: Route, body: unknown, status = 200): Promise<void> {
    return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

/**
 * Presses Save in the open comment popover and checks that it closes and stays
 * closed. The popover's afterClose may queue a reopen in a zero-delay timeout
 * (Session.controller `onCommentPopoverClosed`), so the check waits for the
 * afterClose event and two more macrotasks, then asks the control itself:
 * no fixed sleep.
 */
export async function saveCommentAndExpectClosed(page: Page): Promise<void> {
    const popover = byId(page, "commentPopover");
    await expect(popover).toBeVisible();
    const id = await popover.getAttribute("id");
    expect(id).toBeTruthy();
    await page.evaluate((pid) => {
        const w = window as unknown as Record<string, unknown> & { sap: { ui: { getCore(): { byId(id: string): { attachEventOnce(e: string, f: () => void): void } } } } };
        w.__e2ePopoverClosed = new Promise<void>((resolve) => w.sap.ui.getCore().byId(pid as string).attachEventOnce("afterClose", () => resolve()));
    }, id);
    await byId(page, "commentSave").click();
    const stillOpen = await page.evaluate(async (pid) => {
        const w = window as unknown as Record<string, unknown> & { sap: { ui: { getCore(): { byId(id: string): { isOpen(): boolean } } } } };
        await (w.__e2ePopoverClosed as Promise<void>);
        for (let i = 0; i < 2; i++) {
            await new Promise((resolve) => setTimeout(resolve, 0));
        }
        return w.sap.ui.getCore().byId(pid as string).isOpen();
    }, id);
    expect(stillOpen, "the comment popover opened again after Save").toBe(false);
    await expect(popover).toBeHidden();
}

/**
 * Clicks once the element has stopped moving. Right after the session page
 * loads, its columns still settle; a click whose mouseup lands after such a
 * shift hits another element and presses nothing.
 */
export async function clickWhenSettled(locator: Locator): Promise<void> {
    await locator.scrollIntoViewIfNeeded();
    let last = "";
    await expect.poll(async () => {
        const box = JSON.stringify(await locator.boundingBox());
        const settled = box === last;
        last = box;
        return settled;
    }, { intervals: [150] }).toBe(true);
    await locator.click();
}

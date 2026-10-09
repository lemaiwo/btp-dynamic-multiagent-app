import { execFileSync } from "node:child_process";
import * as path from "node:path";
import { test as base, expect, type APIRequestContext } from "@playwright/test";

const API = "http://127.0.0.1:7932/admin/api";

export interface SeedFixture { seed: (name: string) => Promise<void> }

export const test = base.extend<SeedFixture>({
    seed: async ({ request }: { request: APIRequestContext }, use) => {
        const seedAgent = async (name: string): Promise<void> => {
            const response = await request.post(`${API}/import`, {
                data: {
                    skills: [{
                        name: "e2e-skill",
                        description: "A skill used by the end-to-end tests",
                        content: "Instructions for the e2e skill."
                    }],
                    agents: [{
                        name,
                        description: "Created by the end-to-end tests",
                        instructions: "Do the e2e thing.",
                        mcp_servers: [{ url: "builtin:gmail", auth_mode: "none" }],
                        skills: ["e2e-skill"],
                        enabled: true,
                        expose_chat: true,
                        expose_api: false,
                        api_slug: "",
                        run_as_principal: "",
                        run_prompt: "",
                        run_timeout_seconds: 1800
                    }],
                    replace: false
                }
            });
            expect(response.ok()).toBeTruthy();
        };
        await use(seedAgent);
    }
});

// The throw-away database the e2e backend runs on (playwright.config.ts:
// DATABASE_URL=sqlite+aiosqlite:///./_e2e_registry.db, relative to ui5-admin/).
const E2E_DB = path.resolve(__dirname, "..", "_e2e_registry.db");

function sql(statement: string): void {
    execFileSync("sqlite3", ["-cmd", ".timeout 5000", E2E_DB, statement]);
}

/** Empties the run tables and the notification read markers of the e2e database. */
export function clearRunsAndMarkers(): void {
    sql("DELETE FROM job_runs; DELETE FROM workflow_runs; DELETE FROM admin_notification_state;");
}

// SQLite holds the app's timestamps as naive UTC text, e.g. "2026-10-09 13:20:09.330000".
const NOW_UTC = "strftime('%Y-%m-%d %H:%M:%f', 'now')";

/** Seeds one finished agent run, finished now. Names and ids must be plain identifiers. */
export function seedFinishedAgentRun(id: string, name: string, status = "success"): void {
    sql(`INSERT INTO job_runs (id, agent_id, agent_name, "trigger", status, started_at, finished_at,
         timeout_seconds, notified, created_by)
         VALUES ('${id}', 1, '${name}', 'manual', '${status}', ${NOW_UTC}, ${NOW_UTC}, 1800, 0, 'e2e');`);
}

/** Seeds one finished workflow run, finished now. */
export function seedFinishedWorkflowRun(id: string, name: string, status = "failed"): void {
    sql(`INSERT INTO workflow_runs (id, workflow_id, workflow_name, "trigger", status, started_at, finished_at,
         items_total, items_succeeded, items_failed, items_skipped, created_by)
         VALUES ('${id}', 1, '${name}', 'manual', '${status}', ${NOW_UTC}, ${NOW_UTC}, 0, 0, 0, 0, 'e2e');`);
}

export { expect };

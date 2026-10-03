import { defineConfig } from "@playwright/test";

/**
 * Two servers: the Python backend (the app reads /ide/api on start-up) and
 * the UI, whose ui5-local.yaml proxies /backend to the backend's /ide/api.
 *
 * The backend runs against a throw-away SQLite file and with
 * reuseExistingServer: false, so a developer's `python app.py` on the same
 * port fails the suite loudly instead of having it write into the real
 * database (the reasoning of ui5-admin/playwright.config.ts). No browsers are
 * downloaded: run with the local Chrome (`channel: "chrome"`).
 *
 * PYTHON overrides the interpreter; the default is the repo's POSIX venv.
 */
export default defineConfig({
    testDir: "./e2e",
    timeout: 60_000,
    fullyParallel: false,
    workers: 1,
    reporter: [["list"]],
    use: {
        baseURL: "http://localhost:8080",
        channel: "chrome",
        trace: "retain-on-failure"
    },
    webServer: [
        {
            command: `${process.env.PYTHON ?? "../.venv/bin/python"} ../app.py`,
            url: "http://127.0.0.1:7932/healthz",
            reuseExistingServer: false,
            timeout: 60_000,
            env: {
                DATABASE_URL: "sqlite+aiosqlite:///./_e2e_registry.db",
                MCP_URL_ALLOWLIST: "",
                PUBLIC_BASE_URL: "http://127.0.0.1:7932",
                // Start-up builds the orchestrator, which needs AI Core
                // settings to construct a client; no journey here runs a
                // model, so unreachable placeholders keep the suite
                // independent of a developer's .env (they win over it:
                // load_dotenv never overrides a set variable).
                AICORE_CLIENT_ID: "e2e",
                AICORE_CLIENT_SECRET: "e2e",
                AICORE_AUTH_URL: "http://127.0.0.1:9",
                AICORE_BASE_URL: "http://127.0.0.1:9/v2",
                AICORE_RESOURCE_GROUP: "default"
            }
        },
        {
            command: "npx ui5 serve --config ui5-local.yaml --port 8080",
            url: "http://localhost:8080/index.html",
            reuseExistingServer: true,
            timeout: 120_000
        }
    ]
});

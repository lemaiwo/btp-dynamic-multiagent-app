import { defineConfig } from "@playwright/test";

export default defineConfig({
    testDir: "./e2e",
    timeout: 60_000,
    fullyParallel: false,
    workers: 1,
    reporter: [["list"]],
    use: {
        baseURL: "http://localhost:8080",
        trace: "retain-on-failure"
    },
    webServer: [
        {
            // Windows's cmd.exe (which Playwright's webServer shells out
            // through) fails to resolve a relative executable path given
            // with forward slashes -- "'..' is not recognized as an
            // internal or external command" -- so the leading path needs
            // backslashes here even though the rest of the repo uses "/".
            command: "..\\.venv\\Scripts\\python.exe ..\\app.py",
            url: "http://127.0.0.1:7932/healthz",
            // Deliberately false (unlike the UI entry below): if a dev
            // backend from `python app.py` at the repo root is already
            // listening on this port, reusing it would point the suite at
            // the real `agents_registry.db` instead of the isolated
            // `_e2e_registry.db` below. false makes Playwright fail loudly
            // ("port already in use") instead of silently writing into the
            // developer's real database.
            reuseExistingServer: false,
            timeout: 60_000,
            env: {
                DATABASE_URL: "sqlite+aiosqlite:///./_e2e_registry.db",
                MCP_URL_ALLOWLIST: "",
                PUBLIC_BASE_URL: "http://127.0.0.1:7932"
            }
        },
        {
            command: "npm start",
            url: "http://localhost:8080/index.html",
            reuseExistingServer: true,
            timeout: 120_000
        }
    ]
});

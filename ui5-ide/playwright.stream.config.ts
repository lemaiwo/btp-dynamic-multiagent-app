import { defineConfig } from "@playwright/test";

/**
 * The real-stream e2e (e2e/real-stream.spec.ts): the browser drives the real
 * backend, runner and SSE with a scripted model in place of AI Core and a fake
 * SAP system in place of ARC-1 (../tests/e2e/ide_stream_server.py). No
 * page.route anywhere: every request the UI sends reaches the backend.
 *
 * Its own ports, so it never meets the default suite's servers: backend 7933,
 * UI 8081 (ui5-stream.yaml proxies /backend to 7933's /ide/api).
 *
 * The server sets its own environment (SQLite file anchored to the repo root,
 * placeholder AI Core values, no XSUAA, IDE_SEED=false, DEMO target seeded) and
 * deletes the SQLite file on start and in its lifespan on SIGTERM; Playwright's
 * default kill would skip that lifespan, hence `gracefulShutdown`.
 *
 * PYTHON overrides the interpreter; the default is the repo's POSIX venv.
 */
export default defineConfig({
    testDir: "./e2e",
    testMatch: "real-stream.spec.ts",
    timeout: 120_000,
    expect: { timeout: 15_000 },
    fullyParallel: false,
    workers: 1,
    retries: 0,
    reporter: [["list"]],
    use: {
        baseURL: "http://localhost:8081",
        channel: "chrome",
        viewport: { width: 1600, height: 1000 },
        trace: "retain-on-failure"
    },
    webServer: [
        {
            command: `exec ${process.env.PYTHON ?? "../.venv/bin/python"} ../tests/e2e/ide_stream_server.py`,
            url: "http://127.0.0.1:7933/healthz",
            reuseExistingServer: false,
            timeout: 60_000,
            gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 }
        },
        {
            command: "npx ui5 serve --config ui5-stream.yaml --port 8081",
            url: "http://localhost:8081/index.html",
            reuseExistingServer: false,
            timeout: 120_000,
            gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 }
        }
    ]
});

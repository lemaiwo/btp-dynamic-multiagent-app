import { rmSync } from "node:fs";
import path from "node:path";

/** Removes the throw-away SQLite database of the run (see playwright.config.ts). */
export default function globalTeardown(): void {
    for (const suffix of ["", "-journal", "-wal", "-shm"]) {
        rmSync(path.join(__dirname, "..", `_e2e_registry.db${suffix}`), { force: true });
    }
}

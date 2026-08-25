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

export { expect };

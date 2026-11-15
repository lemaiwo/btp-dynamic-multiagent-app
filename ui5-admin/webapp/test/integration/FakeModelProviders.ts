import type {
    ModelProviderKeySource, ModelProviderName, ModelProviderStatus, ModelProviderTestResult, ModelProvidersStatus
} from "com/agent/admin/service/modelProviderTypes";

type Json = (body: unknown, status?: number, headers?: Record<string, string>) => Promise<Response>;
type Body = Record<string, unknown>;

/** The refusals of agents/model_providers.py `validate`: `<field>: <rule>`, never a value. */
export const NOT_AN_OBJECT = "body: a JSON object";
export const UNKNOWN_KEY = "body: an unknown key";
export const ENABLED_NOT_BOOLEAN = "enabled: must be a boolean";
export const BAD_KEY = "api_key: printable characters without blanks, at most 400";
export const BAD_BASE_URL = "base_url: https://host[/path] without credentials, query or fragment";
export const TOO_MANY_MODELS = "models: a list of at most 30 model names";

/** The test's answer for a key the fake provider refuses (it starts with this). */
export const REFUSED_KEY_PREFIX = "sk-bad";

const ALLOWED_KEYS = ["enabled", "api_key", "base_url", "models"];
const MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:\-/@]{0,127}$/;
/** The provider's default list when no model is stored. */
const DEFAULT_MODELS: Record<ModelProviderName, string[]> = {
    anthropic: ["claude-sonnet-4-5", "claude-haiku-4-5"],
    openai: ["gpt-4.1", "gpt-4.1-mini"]
};

interface Setting {
    enabled: boolean;
    /** Held to tell `has_key` and the test's outcome; never answered. */
    apiKey: string | null;
    baseUrl: string | null;
    models: string[];
}

function isObject(value: unknown): value is Body {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The `/admin/api/model-providers` routes of the fake backend, apart from
 * `FakeBackend` so neither file grows. Keeps a setting per provider and,
 * like the server, NEVER answers a key.
 *
 * The fixture: Anthropic stored and on, with a stored key and one model;
 * OpenAI not stored, no key anywhere (`envKey` sets a key "from the
 * environment"). SAP AI Core is configured. A key starting with `sk-bad`
 * is refused by the test (401 `AuthenticationError`); `unreachable` makes
 * every test answer as a provider that cannot be reached.
 */
export default class FakeModelProviders {

    public settings: Partial<Record<ModelProviderName, Setting>> = {};
    /** A key of the environment per provider (`ANTHROPIC_API_KEY`, ...). */
    public envKey: Partial<Record<ModelProviderName, string>> = {};
    public aicoreConfigured = true;
    /** The test cannot reach the provider (`APIConnectionError`). */
    public unreachable = false;

    public reset(): void {
        this.settings = {
            anthropic: { enabled: true, apiKey: "sk-ant-stored", baseUrl: null, models: ["claude-sonnet-4-5"] }
        };
        this.envKey = {};
        this.aicoreConfigured = true;
        this.unreachable = false;
    }

    private keyOf(name: ModelProviderName): { key: string | null; source: ModelProviderKeySource } {
        const stored = this.settings[name]?.apiKey;
        if (stored) {
            return { key: stored, source: "setting" };
        }
        const env = this.envKey[name];
        return env ? { key: env, source: "env" } : { key: null, source: "none" };
    }

    private provider(name: ModelProviderName): ModelProviderStatus {
        const setting = this.settings[name];
        const { source } = this.keyOf(name);
        const enabled = setting ? setting.enabled : source !== "none";
        const models = setting && setting.models.length ? setting.models : DEFAULT_MODELS[name];
        return {
            provider: name,
            stored: !!setting,
            enabled,
            has_key: source !== "none",
            key_source: source,
            has_stored_key: !!setting?.apiKey,
            base_url: setting?.baseUrl ?? "",
            models: setting ? setting.models.slice() : [],
            effective_models: enabled && source !== "none" ? models.map((model) => `${name}:${model}`) : []
        };
    }

    public status(): ModelProvidersStatus {
        const providers = (["anthropic", "openai"] as ModelProviderName[]).map((name) => this.provider(name));
        const available = ([] as string[]).concat(...providers.map((p) => p.effective_models));
        return {
            providers,
            aicore_configured: this.aicoreConfigured,
            available_models: (this.aicoreConfigured ? ["gpt-4o"] : []).concat(available),
            default_model: this.aicoreConfigured ? "gpt-4o" : available[0] ?? ""
        };
    }

    /** Answers a `model-providers` route, or `undefined` for any other path. */
    public handle(path: string, method: string, body: unknown, json: Json,
        reload: { reloaded: boolean; reload_failed: boolean }): Promise<Response> | undefined {
        if (!/^model-providers(\/|$)/.test(path)) {
            return undefined;
        }
        const parts = path.split("/").slice(1);
        if (parts.length === 0 && method === "GET") {
            return json(this.status());
        }
        const name = parts[0];
        if (parts.length >= 1 && name !== "anthropic" && name !== "openai") {
            return json({ detail: "Unknown provider" }, 404);
        }
        if (parts.length === 1 && method === "PUT") {
            return this.save(name as ModelProviderName, body, json, reload);
        }
        if (parts.length === 2 && parts[1] === "test" && method === "POST") {
            return json(this.test(name as ModelProviderName));
        }
        return json({ detail: "Method Not Allowed" }, 405);
    }

    private test(name: ModelProviderName): ModelProviderTestResult {
        const { key } = this.keyOf(name);
        if (!key) {
            return { ok: false, error: "no_key" };
        }
        if (this.unreachable) {
            return { ok: false, status: null, error: "APIConnectionError" };
        }
        return key.indexOf(REFUSED_KEY_PREFIX) === 0 ? { ok: false, status: 401, error: "AuthenticationError" } : { ok: true };
    }

    /** `validate` of agents/model_providers.py, then the save; the first refusal only, as the server raises it. */
    private save(name: ModelProviderName, body: unknown, json: Json,
        reload: { reloaded: boolean; reload_failed: boolean }): Promise<Response> {
        if (!isObject(body)) {
            return json({ detail: NOT_AN_OBJECT }, 422);
        }
        if (Object.keys(body).some((key) => ALLOWED_KEYS.indexOf(key) === -1)) {
            return json({ detail: UNKNOWN_KEY }, 422);
        }
        if (typeof body.enabled !== "boolean") {
            return json({ detail: ENABLED_NOT_BOOLEAN }, 422);
        }
        const before = this.settings[name];
        let apiKey = before?.apiKey ?? null;
        if ("api_key" in body) {
            const key = body.api_key;
            if (key === null || key === "") {
                apiKey = null;
            } else if (typeof key !== "string" || key.length > 400 || !/^[\x21-\x7e]+$/.test(key)) {
                return json({ detail: BAD_KEY }, 422);
            } else {
                apiKey = key;
            }
        }
        let baseUrl = before?.baseUrl ?? null;
        if ("base_url" in body) {
            const url = body.base_url;
            if (url === null || url === "") {
                baseUrl = null;
            } else if (typeof url !== "string" || url.length > 500) {
                return json({ detail: "base_url: a URL of at most 500 characters" }, 422);
            } else if (!/^https:\/\/[^/@?#\s]+(\/[^?#\s]*)?$/.test(url)) {
                return json({ detail: BAD_BASE_URL }, 422);
            } else {
                baseUrl = url.replace(/\/+$/, "");
            }
        }
        const models: string[] = [];
        if (body.models !== undefined && body.models !== null) {
            if (!Array.isArray(body.models) || body.models.length > 30) {
                return json({ detail: TOO_MANY_MODELS }, 422);
            }
            for (let i = 0; i < body.models.length; i++) {
                const item = body.models[i];
                let model = typeof item === "string" ? item.trim() : "";
                if (model.toLowerCase().indexOf(`${name}:`) === 0) {
                    model = model.slice(name.length + 1);
                }
                if (!MODEL_RE.test(model)) {
                    return json({ detail: `models[${i}]: not a model name` }, 422);
                }
                if (models.indexOf(model) === -1) {
                    models.push(model);
                }
            }
        }
        this.settings[name] = { enabled: body.enabled, apiKey, baseUrl, models };
        return json({ ...this.status(), reloaded: reload.reloaded, reload_failed: reload.reload_failed });
    }
}

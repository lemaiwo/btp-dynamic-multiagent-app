/**
 * The shapes of the model provider routes (`/admin/api/model-providers`,
 * agents/model_provider_routes.py): the direct model providers Anthropic and
 * OpenAI. The API key is a secret: no answer ever carries it.
 *
 * Kept apart from `types.ts`, which is past the size a file may have.
 */

/** A direct model provider. */
export type ModelProviderName = "anthropic" | "openai";

/** Where the key in effect comes from. */
export type ModelProviderKeySource = "setting" | "env" | "none";

/** One provider of `GET model-providers`. */
export interface ModelProviderStatus {
    provider: ModelProviderName;
    /** A setting of this provider is stored (else the environment decides). */
    stored: boolean;
    enabled: boolean;
    /** A key is in effect, from the setting or the environment. */
    has_key: boolean;
    key_source: ModelProviderKeySource;
    /** A key is stored in the setting ("Remove stored key" can remove it). */
    has_stored_key: boolean;
    /** "" when none is stored. */
    base_url: string;
    /** The model names stored, bare (without the `<provider>:` prefix). */
    models: string[];
    /** The models on offer from this provider, as `<provider>:<model>`. */
    effective_models: string[];
}

/** `GET model-providers`: always both providers, Anthropic first. */
export interface ModelProvidersStatus {
    providers: ModelProviderStatus[];
    aicore_configured: boolean;
    available_models: string[];
    default_model: string;
}

/** What `PUT model-providers/{provider}` answers: the status plus the reload's outcome. */
export interface ModelProvidersSaved extends ModelProvidersStatus {
    reloaded?: boolean;
    reload_failed?: boolean;
}

/**
 * The body of `PUT model-providers/{provider}`. `api_key` is left OUT to
 * keep the stored key; `null` removes it. `base_url` `null` clears it.
 */
export interface ModelProviderSave {
    enabled: boolean;
    api_key?: string | null;
    base_url: string | null;
    models: string[];
}

/** `POST model-providers/{provider}/test`: is the key in effect accepted? */
export interface ModelProviderTestResult {
    ok: boolean;
    status?: number | null;
    /** `no_key`, or the class name of the failure (`AuthenticationError`, ...). */
    error?: string;
}

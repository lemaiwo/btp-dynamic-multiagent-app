import type {
    EmbeddingDeployment, EmbeddingModelOffer, EmbeddingRun, EmbeddingSetting, EmbeddingSource, EmbeddingsStatus
} from "com/agent/admin/service/dataCatalogTypes";

type Json = (body: unknown, status?: number, headers?: Record<string, string>) => Promise<Response>;
type Body = Record<string, unknown>;

/** The refusals of the routes (design addendum (l), point 2): `<field>: <rule>`, never a value. */
export const MODEL_NOT_DEPLOYED = "model: not a deployed embedding model";
export const MODEL_NOT_OFFERED = "model: not an embedding model AI Core offers";
export const RUN_IN_FLIGHT = "An embedding run is already in progress";
export const NO_MODEL = "No embedding model is in effect";
const NOT_AN_OBJECT = "body: expected a JSON object";

/** `LOC_FIELD_RE` of agents/loc_fields.py: a key that may be repeated in a refusal. */
const LOC_FIELD_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

function isObject(value: unknown): value is Body {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The `/admin/api/data-catalog/embeddings` routes of the fake backend
 * (design addendum (l), point 2), apart from `FakeBackend` so neither file
 * grows past its size. Answered before the catalogue fake, which takes
 * every other `data-catalog/...` path.
 *
 * The fixture: embeddings on, Model automatic, `text-embedding-3-small`
 * deployed and RUNNING (so discovery finds it), 20 of 24 nodes embedded
 * with it, one finished run. AI Core offers `text-embedding-3-large` and
 * `text-embedding-ada-002` as well.
 *
 * Scripted: a run answers `running` to the next two `GET`s (progress a
 * third, then two thirds) and is done at the third, with every node
 * embedded by the model in effect (the vectors of another model are
 * dropped); `runFails` makes it end with that error class instead and
 * keeps the vectors. A deploy answers `PENDING` and stays so until
 * `startDeployments()`.
 */
export default class FakeEmbeddings {

    public setting: EmbeddingSetting = { enabled: true, model: null };
    /** `AICORE_EMBEDDING_MODEL` of the landscape; null: not set. */
    public envModel: string | null = null;
    /** `DATA_CATALOG_EMBEDDINGS=0`: nothing is embedded, whatever the setting. */
    public switchedOff = false;
    public deployments: EmbeddingDeployment[] = [];
    /** The scenario's embedding models, deployed or not. */
    public offers: EmbeddingModelOffer[] = [];
    public nodes = 24;
    /** The stored vectors: of one model only. */
    public vectors: { model: string; count: number } | null = null;
    public lastRun: EmbeddingRun | null = null;
    public resourceGroup = "rg-agents";
    /** An error class: the next run ends failed with it. */
    public runFails: string | null = null;
    private run: { model: string; total: number; done: number; pollsLeft: number; startedAt: string } | null = null;
    private stamp = 0;
    private nextId = 2;

    public reset(): void {
        this.stamp = Date.parse("2026-11-03T07:00:00Z");
        this.nextId = 2;
        this.setting = { enabled: true, model: null };
        this.envModel = null;
        this.switchedOff = false;
        this.deployments = [{ model: "text-embedding-3-small", status: "RUNNING", deployment_id: "d-emb-1" }];
        this.offers = [
            { model: "text-embedding-3-large", versions: ["1"] },
            { model: "text-embedding-3-small", versions: ["1"] },
            { model: "text-embedding-ada-002", versions: ["2"] }
        ];
        this.nodes = 24;
        this.vectors = { model: "text-embedding-3-small", count: 20 };
        this.lastRun = {
            started_at: this.nextStamp(), finished_at: this.nextStamp(), model: "text-embedding-3-small", embedded: 20,
            error_class: null
        };
        this.resourceGroup = "rg-agents";
        this.runFails = null;
        this.run = null;
    }

    /** Every PENDING deployment reaches RUNNING, as AI Core's do after some minutes. */
    public startDeployments(): void {
        this.deployments.forEach((deployment) => {
            if (deployment.status === "PENDING") {
                deployment.status = "RUNNING";
            }
        });
    }

    private nextStamp(): string {
        this.stamp += 1000;
        return new Date(this.stamp).toISOString().replace(".000Z", "+00:00");
    }

    private isRunning(model: string): boolean {
        return this.deployments.some((deployment) => deployment.model === model && deployment.status === "RUNNING");
    }

    /** `resolve_model`: the landscape switch, the setting's switch, the
     *  setting's model (only with a RUNNING deployment), the variable, discovery. */
    private resolve(): { model: string | null; source: EmbeddingSource } {
        if (this.switchedOff || !this.setting.enabled) {
            return { model: null, source: "none" };
        }
        if (this.setting.model) {
            return { model: this.isRunning(this.setting.model) ? this.setting.model : null, source: "setting" };
        }
        if (this.envModel) {
            return { model: this.envModel, source: "env" };
        }
        const found = this.isRunning("text-embedding-3-large") ? "text-embedding-3-large"
            : this.deployments.filter((d) => d.status === "RUNNING" && d.model.indexOf("embedding") !== -1)
                .map((d) => d.model)[0];
        return found ? { model: found, source: "discovery" } : { model: null, source: "none" };
    }

    private status(): EmbeddingsStatus {
        const { model, source } = this.resolve();
        const vectors = model && this.vectors && this.vectors.model === model ? this.vectors.count : 0;
        const deployed = this.deployments.map((d) => d.model);
        return {
            setting: { ...this.setting }, effective_model: model, source, env_override: this.envModel !== null,
            deployed: this.deployments.map((d) => ({ ...d })),
            available: this.offers.filter((offer) => deployed.indexOf(offer.model) === -1)
                .map((offer) => ({ model: offer.model, versions: offer.versions.slice() })),
            nodes: this.nodes, vectors, stale: this.nodes - vectors, running: this.run !== null,
            progress: this.run ? { done: this.run.done, total: this.run.total } : null,
            last_run: this.lastRun ? { ...this.lastRun } : null, resource_group: this.resourceGroup
        };
    }

    /** A `GET` is a poll: it moves a run in flight on by one step. */
    private poll(): void {
        const run = this.run;
        if (!run) {
            return;
        }
        if (run.pollsLeft > 0) {
            run.pollsLeft -= 1;
            run.done = Math.floor(run.total * (2 - run.pollsLeft) / 3);
            return;
        }
        this.run = null;
        if (this.runFails) {
            this.lastRun = {
                started_at: run.startedAt, finished_at: this.nextStamp(), model: run.model, embedded: 0,
                error_class: this.runFails
            };
            this.runFails = null;
            return;
        }
        this.vectors = { model: run.model, count: this.nodes };
        this.lastRun = {
            started_at: run.startedAt, finished_at: this.nextStamp(), model: run.model, embedded: run.total,
            error_class: null
        };
    }

    /** Answers a `data-catalog/embeddings` route, or `undefined` for any other path. */
    public handle(path: string, method: string, body: unknown, json: Json): Promise<Response> | undefined {
        if (!/^data-catalog\/embeddings(\/|$)/.test(path)) {
            return undefined;
        }
        const sub = path.replace(/^data-catalog\/embeddings\/?/, "");
        if (sub === "" && method === "GET") {
            this.poll();
            return json(this.status());
        }
        if (sub === "" && method === "PUT") {
            return this.save(body, json);
        }
        if (sub === "run" && method === "POST") {
            return this.start(json);
        }
        if (sub === "deploy" && method === "POST") {
            return this.deploy(body, json);
        }
        return sub === "" || sub === "run" || sub === "deploy"
            ? json({ detail: "Method Not Allowed" }, 405)
            : json({ detail: "Not Found" }, 404);
    }

    private static loc(key: string): string {
        return LOC_FIELD_RE.test(key) ? key : "<unknown field>";
    }

    /** The problems of a body that may hold `keys` only, `loc: msg` each. */
    private static problems(body: Body, keys: string[]): string[] {
        return Object.keys(body).filter((key) => keys.indexOf(key) === -1)
            .map((key) => `${FakeEmbeddings.loc(key)}: Extra inputs are not permitted`);
    }

    private save(body: unknown, json: Json): Promise<Response> {
        if (!isObject(body)) {
            return json({ detail: NOT_AN_OBJECT }, 422);
        }
        const problems: string[] = [];
        if (!("enabled" in body)) {
            problems.push("enabled: Field required");
        } else if (typeof body.enabled !== "boolean") {
            problems.push("enabled: Input should be a valid boolean");
        }
        if (!("model" in body)) {
            problems.push("model: Field required");
        } else if (body.model !== null && typeof body.model !== "string") {
            problems.push("model: Input should be a valid string");
        }
        problems.push(...FakeEmbeddings.problems(body, ["enabled", "model"]));
        if (problems.length) {
            return json({ detail: problems.join("; ") }, 422);
        }
        const model = body.model as string | null;
        if (model !== null && !this.deployments.some((deployment) => deployment.model === model)) {
            return json({ detail: MODEL_NOT_DEPLOYED }, 422);
        }
        this.setting = { enabled: body.enabled as boolean, model };
        return json(this.status());
    }

    private start(json: Json): Promise<Response> {
        if (this.run) {
            return json({ detail: RUN_IN_FLIGHT }, 409);
        }
        const { model } = this.resolve();
        if (!model) {
            return json({ detail: NO_MODEL }, 409);
        }
        const vectors = this.vectors && this.vectors.model === model ? this.vectors.count : 0;
        this.run = { model, total: this.nodes - vectors, done: 0, pollsLeft: 2, startedAt: this.nextStamp() };
        return json({ running: true }, 202);
    }

    /** Idempotent: a RUNNING or PENDING deployment of the model is answered and nothing is made. */
    private deploy(body: unknown, json: Json): Promise<Response> {
        if (!isObject(body)) {
            return json({ detail: NOT_AN_OBJECT }, 422);
        }
        const problems = typeof body.model === "string" ? []
            : ["model" in body ? "model: Input should be a valid string" : "model: Field required"];
        problems.push(...FakeEmbeddings.problems(body, ["model"]));
        if (problems.length) {
            return json({ detail: problems.join("; ") }, 422);
        }
        const model = body.model as string;
        const existing = this.deployments.filter((deployment) => deployment.model === model
            && (deployment.status === "RUNNING" || deployment.status === "PENDING"))[0];
        if (existing) {
            return json({ deployment_id: existing.deployment_id, status: existing.status }, 202);
        }
        if (!this.offers.some((offer) => offer.model === model)) {
            return json({ detail: MODEL_NOT_OFFERED }, 422);
        }
        const created: EmbeddingDeployment = { model, status: "PENDING", deployment_id: `d-emb-${this.nextId++}` };
        this.deployments.push(created);
        return json({ deployment_id: created.deployment_id, status: created.status }, 202);
    }
}

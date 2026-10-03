import IdeService, { IdeError } from "com/agent/ide/service/IdeService";
import type { Approval, SseEvent } from "com/agent/ide/service/types";
import FakeBackend, { APPROVAL_EXPIRED, TARGET_NOT_NON_PRODUCTION } from "../integration/FakeBackend";

/** The slice of sinon-4 (shipped by UI5 at sap/ui/thirdparty/sinon-4) these tests use. */
interface SinonStub {
    resolves: (value: unknown) => SinonStub;
    returns: (value: unknown) => SinonStub;
    callCount: number;
    getCall: (n: number) => { args: unknown[] };
    restore: () => void;
}
interface SinonLike { stub: (obj: object, method: string) => SinonStub }

let sinon: SinonLike;

interface Ctx { fetchStub?: SinonStub }

QUnit.module("IdeService", {
    before: function () {
        return new Promise<void>((resolve) => {
            sap.ui.require(["sap/ui/thirdparty/sinon-4"], function (lib: SinonLike) {
                sinon = lib;
                resolve();
            });
        });
    },
    afterEach: function (this: Ctx) {
        this.fetchStub?.restore();
    }
});

function jsonResponse(status: number, body?: unknown): Response {
    return new Response(body === undefined ? null : JSON.stringify(body), {
        status, headers: { "Content-Type": "application/json" }
    });
}

/**
 * A text/event-stream response whose body the test feeds by hand. `push`
 * enqueues one raw chunk, `close` ends the stream; `cancelled` turns true
 * when the reader cancels it (what an abort must do).
 */
function streamResponse(): {
    response: Response; push: (text: string) => void; close: () => void; cancelled: () => boolean;
} {
    const encoder = new TextEncoder();
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    let cancelled = false;
    let closed = false;
    const body = new ReadableStream<Uint8Array>({
        start(c) { controller = c; },
        cancel() { cancelled = true; }
    });
    return {
        response: new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
        push: (text) => { if (!cancelled && !closed) { controller.enqueue(encoder.encode(text)); } },
        close: () => { if (!cancelled && !closed) { closed = true; controller.close(); } },
        cancelled: () => cancelled
    };
}

function stubFetch(ctx: Ctx, response: Response | Promise<Response>): SinonStub {
    ctx.fetchStub = sinon.stub(window, "fetch").returns(Promise.resolve(response));
    return ctx.fetchStub;
}

function callOf(stub: SinonStub, n = 0): { url: string; init: RequestInit } {
    const args = stub.getCall(n).args;
    return { url: args[0] as string, init: (args[1] ?? {}) as RequestInit };
}

// --- plain JSON routes ---------------------------------------------------------

QUnit.test("getMe calls the relative backend path", async function (this: Ctx, assert) {
    const stub = stubFetch(this, jsonResponse(200, { principal: "dev@example.com", is_admin: false, targets: ["dev"] }));

    const me = await new IdeService().getMe();

    const { url, init } = callOf(stub);
    assert.strictEqual(url, "backend/me", "relative path, no leading slash");
    assert.strictEqual(init.method ?? "GET", "GET", "uses GET");
    assert.deepEqual(me.targets, ["dev"], "returns the parsed body");
});

QUnit.test("createSession POSTs title and target as JSON", async function (this: Ctx, assert) {
    const stub = stubFetch(this, jsonResponse(201, { id: "s1", title: "t", target: "dev", stage: "chat", status: "idle" }));

    const session = await new IdeService().createSession("t", "dev");

    const { url, init } = callOf(stub);
    assert.strictEqual(url, "backend/sessions", "targets the collection");
    assert.strictEqual(init.method, "POST", "uses POST");
    assert.deepEqual(JSON.parse(init.body as string), { title: "t", target: "dev", type: "change" }, "sends the body");
    assert.strictEqual((init.headers as Record<string, string>)["Content-Type"], "application/json", "JSON content type");
    assert.strictEqual(session.id, "s1", "returns the created session");
});

QUnit.test("deleteSession resolves on 204 without parsing a body", async function (this: Ctx, assert) {
    const stub = stubFetch(this, jsonResponse(204));

    const result = await new IdeService().deleteSession("s 1");

    const { url, init } = callOf(stub);
    assert.strictEqual(url, "backend/sessions/s%201", "the id is URL-encoded");
    assert.strictEqual(init.method, "DELETE", "uses DELETE");
    assert.strictEqual(result, undefined, "nothing to return");
});

QUnit.test("file routes put the path in the query string, encoded", async function (this: Ctx, assert) {
    const stub = stubFetch(this, jsonResponse(200, { path: "src/CLAS/zcl_x.clas.abap", state: "read", origin_source: "", proposed_source: null, lint: [] }));

    await new IdeService().getFile("s1", "src/CLAS/zcl_x.clas.abap");

    assert.strictEqual(callOf(stub).url, "backend/sessions/s1/file?path=src%2FCLAS%2Fzcl_x.clas.abap", "path query encoded");
});

QUnit.test("listArtifacts passes the optional kind filter", async function (this: Ctx, assert) {
    const stub = stubFetch(this, jsonResponse(200, []));

    await new IdeService().listArtifacts("s1", "design");

    assert.strictEqual(callOf(stub).url, "backend/sessions/s1/artifacts?kind=design", "kind filter in the query");
});

QUnit.test("searchObjects sends target and q", async function (this: Ctx, assert) {
    const stub = stubFetch(this, jsonResponse(200, []));

    await new IdeService().searchObjects("dev", "ZCL_*");

    assert.strictEqual(callOf(stub).url, "backend/objects/search?target=dev&q=ZCL_*", "both parameters");
});

// --- errors -------------------------------------------------------------------

QUnit.test("a 409 gate refusal becomes an IdeError carrying the code", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(409, { detail: "Approve needs a design artifact.", code: "missing_artifact" }));

    try {
        await new IdeService().approve("s1");
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.ok(e instanceof IdeError, "an IdeError");
        const err = e as IdeError;
        assert.strictEqual(err.status, 409, "status kept");
        assert.strictEqual(err.code, "missing_artifact", "code kept");
        assert.strictEqual(err.message, "Approve needs a design artifact.", "server message shown");
    }
});

QUnit.test("an unknown gate code is still carried with the server message", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(409, { detail: "The stage changed meanwhile.", code: "stage_changed" }));

    try {
        await new IdeService().approve("s1");
        assert.ok(false, "should have thrown");
    } catch (e) {
        const err = e as IdeError;
        assert.strictEqual(err.code, "stage_changed", "code not filtered against a known list");
        assert.strictEqual(err.detail, "The stage changed meanwhile.", "message as sent");
    }
});

QUnit.test("a 404 becomes an IdeError without a code", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(404, { detail: "Session not found" }));

    try {
        await new IdeService().getSession("other");
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.ok(e instanceof IdeError, "an IdeError");
        assert.strictEqual((e as IdeError).status, 404, "status 404");
        assert.strictEqual((e as IdeError).code, undefined, "no code");
        assert.strictEqual((e as IdeError).detail, "Session not found", "detail");
    }
});

QUnit.test("a 422 validation array becomes field errors", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(422, { detail: [{ loc: ["body", "title"], msg: "too long" }] }));

    try {
        await new IdeService().createSession("x".repeat(500), "dev");
        assert.ok(false, "should have thrown");
    } catch (e) {
        const err = e as IdeError;
        assert.strictEqual(err.status, 422, "status 422");
        assert.deepEqual(err.fieldErrors, { title: "too long" }, "leading body dropped");
        assert.strictEqual(err.detail, "too long", "messages joined into detail");
    }
});

QUnit.test("a non-JSON error body falls back to the status text", async function (this: Ctx, assert) {
    stubFetch(this, new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }));

    try {
        await new IdeService().listSessions();
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as IdeError).status, 502, "status kept");
        assert.strictEqual((e as IdeError).detail, "Bad Gateway", "status text as detail");
    }
});

// --- streaming ------------------------------------------------------------------

QUnit.test("streamMessage POSTs the text and delivers events in order across chunk boundaries", async function (this: Ctx, assert) {
    const s = streamResponse();
    const stub = stubFetch(this, s.response);
    const events: SseEvent[] = [];

    const done = new IdeService().streamMessage("s1", "Explain ZCL_X", (e) => events.push(e));
    s.push('event: run\ndata: {"run_id":"r1","stage":"chat","message_id":"m1"}\n\nevent: te');
    s.push('xt\ndata: {"delta":"Hel"}\n\n: ping\n\nevent: text\ndata: {"delta":"lo"}\n');
    s.push('\nevent: done\ndata: {"message_id":"m2","stage":"chat","status":"idle"}\n\n');
    s.close();
    await done;

    const { url, init } = callOf(stub);
    assert.strictEqual(url, "backend/sessions/s1/messages", "the messages route");
    assert.strictEqual(init.method, "POST", "a POST, which is why this is not EventSource");
    assert.deepEqual(JSON.parse(init.body as string), { text: "Explain ZCL_X" }, "the text is sent");
    assert.strictEqual((init.headers as Record<string, string>).Accept, "text/event-stream", "asks for a stream");
    assert.deepEqual(events.map((e) => e.type), ["run", "text", "text", "done"], "in order, heartbeat dropped");
    assert.strictEqual(events.filter((e) => e.type === "text").map((e) => (e.data as { delta: string }).delta).join(""),
        "Hello", "the deltas concatenate");
});

QUnit.test("a multi-byte character split across chunks decodes intact", async function (this: Ctx, assert) {
    const encoder = new TextEncoder();
    const bytes = encoder.encode('event: text\ndata: {"delta":"été"}\n\nevent: done\ndata: {"message_id":"m","stage":"chat","status":"idle"}\n\n');
    const split = bytes.indexOf(0xc3) + 1; // inside the first two-byte sequence
    const body = new ReadableStream<Uint8Array>({
        start(c) { c.enqueue(bytes.slice(0, split)); c.enqueue(bytes.slice(split)); c.close(); }
    });
    stubFetch(this, new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } }));
    const events: SseEvent[] = [];

    await new IdeService().streamMessage("s1", "x", (e) => events.push(e));

    assert.deepEqual(events[0], { type: "text", data: { delta: "été" } }, "decoded as one string");
});

QUnit.test("streamRevise POSTs the feedback to the revise route", async function (this: Ctx, assert) {
    const s = streamResponse();
    const stub = stubFetch(this, s.response);

    const done = new IdeService().streamRevise("s1", "Use a CDS view", () => undefined);
    s.push('event: done\ndata: {"message_id":"m","stage":"design","status":"idle"}\n\n');
    s.close();
    await done;

    const { url, init } = callOf(stub);
    assert.strictEqual(url, "backend/sessions/s1/revise", "the revise route");
    assert.deepEqual(JSON.parse(init.body as string), { feedback: "Use a CDS view" }, "the feedback is sent");
});

QUnit.test("a refused stream (409) rejects with an IdeError and delivers no events", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(409, { detail: "A run is already in progress.", code: "run_in_progress" }));
    const events: SseEvent[] = [];

    try {
        await new IdeService().streamMessage("s1", "again", (e) => events.push(e));
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.ok(e instanceof IdeError, "an IdeError");
        assert.strictEqual((e as IdeError).code, "run_in_progress", "the gate code");
    }
    assert.strictEqual(events.length, 0, "no events");
});

QUnit.test("a 429 usage refusal keeps its code", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(429, { detail: "Request budget used up.", code: "usage_exhausted" }));

    try {
        await new IdeService().streamRevise("s1", "more", () => undefined);
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as IdeError).status, 429, "429");
        assert.strictEqual((e as IdeError).code, "usage_exhausted", "code");
    }
});

QUnit.test("an error frame in the stream is delivered as an event", async function (this: Ctx, assert) {
    const s = streamResponse();
    stubFetch(this, s.response);
    const events: SseEvent[] = [];

    const done = new IdeService().streamMessage("s1", "x", (e) => events.push(e));
    s.push('event: error\ndata: {"message":"Model unavailable","code":"model_error"}\n\n');
    s.push('event: done\ndata: {"message_id":"m","stage":"chat","status":"idle"}\n\n');
    s.close();
    await done;

    assert.deepEqual(events[0], { type: "error", data: { message: "Model unavailable", code: "model_error" } }, "error first");
    assert.strictEqual(events[1].type, "done", "done still last");
});

QUnit.test("a stream that ends without a done frame reports a synthetic error", async function (this: Ctx, assert) {
    const s = streamResponse();
    stubFetch(this, s.response);
    const events: SseEvent[] = [];

    const done = new IdeService().streamMessage("s1", "x", (e) => events.push(e));
    s.push('event: text\ndata: {"delta":"partial"}\n\n');
    s.close();
    await done;

    assert.deepEqual(events.map((e) => e.type), ["text", "error"], "an error follows the cut-off stream");
    assert.strictEqual((events[1].data as { code?: string }).code, "stream_incomplete", "with a stable code");
});

QUnit.test("aborting via AbortController stops the reader and later events are not delivered", async function (this: Ctx, assert) {
    const s = streamResponse();
    const stub = stubFetch(this, s.response);
    const controller = new AbortController();
    const events: SseEvent[] = [];

    const done = new IdeService().streamMessage("s1", "x", (e) => {
        events.push(e);
        controller.abort();
    }, controller.signal);
    s.push('event: text\ndata: {"delta":"first"}\n\n');
    await done;
    s.push('event: text\ndata: {"delta":"second"}\n\n');

    assert.strictEqual(callOf(stub).init.signal, controller.signal, "the signal is handed to fetch");
    assert.deepEqual(events.map((e) => (e.data as { delta: string }).delta), ["first"], "only the event before the abort");
    assert.ok(s.cancelled(), "the body stream was cancelled");
});

QUnit.test("aborting after later frames already arrived in the same chunk drops them", async function (this: Ctx, assert) {
    const s = streamResponse();
    stubFetch(this, s.response);
    const controller = new AbortController();
    const events: SseEvent[] = [];

    const done = new IdeService().streamMessage("s1", "x", (e) => {
        events.push(e);
        controller.abort();
    }, controller.signal);
    s.push('event: text\ndata: {"delta":"a"}\n\nevent: text\ndata: {"delta":"b"}\n\n');
    await done;

    assert.strictEqual(events.length, 1, "the second frame of the chunk is not delivered");
});

QUnit.test("a signal aborted before the call never reaches onEvent", async function (this: Ctx, assert) {
    const s = streamResponse();
    stubFetch(this, s.response);
    const controller = new AbortController();
    controller.abort();
    const events: SseEvent[] = [];

    s.push('event: text\ndata: {"delta":"a"}\n\n');
    await new IdeService().streamMessage("s1", "x", (e) => events.push(e), controller.signal);

    assert.strictEqual(events.length, 0, "nothing delivered");
});

// --- fix round 1: reader release, auth and session expiry --------------------------

QUnit.test("an onEvent handler that throws rejects the call and cancels the body", async function (this: Ctx, assert) {
    const s = streamResponse();
    stubFetch(this, s.response);

    const done = new IdeService().streamMessage("s1", "x", () => { throw new Error("handler bug"); });
    s.push('event: text\ndata: {"delta":"a"}\n\n');
    try {
        await done;
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as Error).message, "handler bug", "the handler's error propagates");
    }
    assert.ok(s.cancelled(), "the body stream was cancelled, so the connection is released");
});

QUnit.test("a stream that ends normally is not cancelled", async function (this: Ctx, assert) {
    const s = streamResponse();
    stubFetch(this, s.response);

    const done = new IdeService().streamMessage("s1", "x", () => undefined);
    s.push('event: done\ndata: {"message_id":"m","stage":"chat","status":"idle"}\n\n');
    s.close();
    await done;

    assert.notOk(s.cancelled(), "no cancel after a clean end");
});

QUnit.test("a 401 is an auth error", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(401, { detail: "Not authenticated" }));

    try {
        await new IdeService().getMe();
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.ok(e instanceof IdeError, "an IdeError");
        assert.strictEqual((e as IdeError).status, 401, "status 401");
        assert.ok((e as IdeError).isAuth, "isAuth set");
    }
});

QUnit.test("a 403 is an auth error", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(403, { detail: "Admin scope required" }));

    try {
        await new IdeService().listAllSessions();
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as IdeError).status, 403, "status 403");
        assert.ok((e as IdeError).isAuth, "isAuth set");
        assert.strictEqual((e as IdeError).detail, "Admin scope required", "server message kept");
    }
});

QUnit.test("a 409 is not an auth error", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(409, { detail: "busy", code: "run_in_progress" }));

    try {
        await new IdeService().approve("s1");
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.notOk((e as IdeError).isAuth, "isAuth not set");
    }
});

QUnit.test("an HTML 200 (the approuter's login page) is session_expired", async function (this: Ctx, assert) {
    stubFetch(this, new Response("<html><body>Sign in</body></html>", {
        status: 200, headers: { "Content-Type": "text/html; charset=utf-8" }
    }));

    try {
        await new IdeService().listSessions();
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.ok(e instanceof IdeError, "an IdeError, not a SyntaxError");
        assert.strictEqual((e as IdeError).code, "session_expired", "session_expired");
        assert.ok((e as IdeError).isAuth, "isAuth set");
    }
});

QUnit.test("a redirect (to the identity provider) is session_expired and is not followed", async function (this: Ctx, assert) {
    const redirect = { type: "opaqueredirect", status: 0, ok: false, statusText: "", headers: new Headers(), redirected: false } as unknown as Response;
    const stub = stubFetch(this, redirect);

    try {
        await new IdeService().getMe();
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as IdeError).code, "session_expired", "session_expired");
        assert.ok((e as IdeError).isAuth, "isAuth set");
    }
    assert.strictEqual(callOf(stub).init.redirect, "manual", "redirects are not followed");
});

QUnit.test("a 200 whose JSON body is malformed becomes an IdeError", async function (this: Ctx, assert) {
    stubFetch(this, new Response("{not json", { status: 200, headers: { "Content-Type": "application/json" } }));

    try {
        await new IdeService().listSessions();
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.ok(e instanceof IdeError, "an IdeError, not a SyntaxError");
        assert.strictEqual((e as IdeError).code, "invalid_response", "invalid_response");
        assert.notOk((e as IdeError).isAuth, "not an auth problem");
    }
});

QUnit.test("a rejected fetch becomes an IdeError with status 0", async function (this: Ctx, assert) {
    stubFetch(this, Promise.reject(new TypeError("Failed to fetch")));

    try {
        await new IdeService().getMe();
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.ok(e instanceof IdeError, "an IdeError");
        assert.strictEqual((e as IdeError).status, 0, "status 0");
        assert.strictEqual((e as IdeError).code, "network", "network");
    }
});

QUnit.test("a stream answered with the HTML login page is session_expired, no events", async function (this: Ctx, assert) {
    const stub = stubFetch(this, new Response("<html>Sign in</html>", { status: 200, headers: { "Content-Type": "text/html" } }));
    const events: SseEvent[] = [];

    try {
        await new IdeService().streamMessage("s1", "x", (e) => events.push(e));
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as IdeError).code, "session_expired", "session_expired");
        assert.ok((e as IdeError).isAuth, "isAuth set");
    }
    assert.strictEqual(events.length, 0, "nothing delivered");
    assert.strictEqual(callOf(stub).init.redirect, "manual", "redirects are not followed");
});

QUnit.test("a stream with a non-SSE content type is invalid_response", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(200, { unexpected: true }));
    const events: SseEvent[] = [];

    try {
        await new IdeService().streamRevise("s1", "x", (e) => events.push(e));
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as IdeError).code, "invalid_response", "invalid_response");
        assert.notOk((e as IdeError).isAuth, "not an auth problem");
    }
    assert.strictEqual(events.length, 0, "nothing delivered");
});

QUnit.test("a 401 on a stream is an auth error", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(401, { detail: "Not authenticated" }));

    try {
        await new IdeService().streamMessage("s1", "x", () => undefined);
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.ok((e as IdeError).isAuth, "isAuth set");
    }
});

QUnit.test("a final frame terminated by lone CRs is delivered at stream end", async function (this: Ctx, assert) {
    const s = streamResponse();
    stubFetch(this, s.response);
    const events: SseEvent[] = [];

    const done = new IdeService().streamMessage("s1", "x", (e) => events.push(e));
    s.push('event: done\rdata: {"message_id":"m","stage":"chat","status":"idle"}\r\r');
    s.close();
    await done;

    assert.deepEqual(events.map((e) => e.type), ["done"], "done delivered, no synthetic error");
});

// --- phase 1c: diagnose sessions (plan §1.2-1.3) -------------------------------------

QUnit.test("createSession sends the type when given", async function (this: Ctx, assert) {
    const stub = stubFetch(this, jsonResponse(201, {
        id: "s2", title: "t", target: "DEMO", stage: "investigate", status: "idle", type: "diagnose"
    }));

    const session = await new IdeService().createSession("t", "DEMO", "diagnose");

    assert.deepEqual(JSON.parse(callOf(stub).init.body as string), { title: "t", target: "DEMO", type: "diagnose" },
        "type is in the body");
    assert.strictEqual(session.type, "diagnose", "the session carries its type");
});

QUnit.test("createSession defaults the type to change", async function (this: Ctx, assert) {
    const stub = stubFetch(this, jsonResponse(201, { id: "s1", type: "change" }));

    await new IdeService().createSession("t", "DEMO");

    assert.strictEqual(JSON.parse(callOf(stub).init.body as string).type, "change", "change by default");
});

QUnit.test("decideApproval POSTs the decision to the approval", async function (this: Ctx, assert) {
    const stub = stubFetch(this, jsonResponse(200, {
        id: "ap 1", action: "trace_start", params: {}, status: "approved", created_at: "x",
        decided_at: "y", result: { trace_request_id: "T1" }, error_code: null
    }));

    const approval = await new IdeService().decideApproval("s1", "ap 1", "approve");

    const { url, init } = callOf(stub);
    assert.strictEqual(url, "backend/sessions/s1/approvals/ap%201", "the approval route, id encoded");
    assert.strictEqual(init.method, "POST", "uses POST");
    assert.deepEqual(JSON.parse(init.body as string), { decision: "approve" }, "the decision is sent");
    assert.strictEqual(approval.status, "approved", "returns the decided approval");
});

QUnit.test("an expired approval (410) maps to IdeError.code", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(410, { detail: "The approval expired.", code: "approval_expired" }));

    try {
        await new IdeService().decideApproval("s1", "a1", "deny");
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as IdeError).status, 410, "410");
        assert.strictEqual((e as IdeError).code, "approval_expired", "code kept");
        assert.notOk((e as IdeError).isAuth, "not an auth problem");
    }
});

QUnit.test("a 403 target_not_non_production keeps its code", async function (this: Ctx, assert) {
    stubFetch(this, jsonResponse(403, { detail: "Target is not non-production.", code: "target_not_non_production" }));

    try {
        await new IdeService().decideApproval("s1", "a1", "approve");
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as IdeError).status, 403, "403");
        assert.strictEqual((e as IdeError).code, "target_not_non_production", "code kept");
    }
});

QUnit.test("streamReport POSTs to the report route and parses artifact and done", async function (this: Ctx, assert) {
    const s = streamResponse();
    const stub = stubFetch(this, s.response);
    const events: SseEvent[] = [];

    const done = new IdeService().streamReport("s1", (e) => events.push(e));
    s.push('event: artifact\ndata: {"id":"a1","kind":"report","version":1}\n\n');
    s.push('event: done\ndata: {"message_id":"m1","stage":"investigate","status":"idle"}\n\n');
    s.close();
    await done;

    const { url, init } = callOf(stub);
    assert.strictEqual(url, "backend/sessions/s1/report", "the report route");
    assert.strictEqual(init.method, "POST", "a POST");
    assert.deepEqual(events, [
        { type: "artifact", data: { id: "a1", kind: "report", version: 1 } },
        { type: "done", data: { message_id: "m1", stage: "investigate", status: "idle" } }
    ], "artifact then done");
});

QUnit.test("finding and approval_required frames are delivered", async function (this: Ctx, assert) {
    const s = streamResponse();
    stubFetch(this, s.response);
    const events: SseEvent[] = [];

    const done = new IdeService().streamMessage("s1", "x", (e) => events.push(e));
    s.push('event: finding\ndata: {"id":"f1","kind":"dump","ref_id":"D1","title":"t"}\n\n');
    s.push('event: approval_required\ndata: {"id":"ap1","action":"trace_start","status":"pending"}\n\n');
    s.push('event: approval\ndata: {"id":"ap1","action":"trace_start","status":"approved"}\n\n');
    s.push('event: done\ndata: {"message_id":"m","stage":"investigate","status":"idle"}\n\n');
    s.close();
    await done;

    assert.deepEqual(events.map((e) => e.type), ["finding", "approval_required", "approval", "done"], "all known");
});

QUnit.test("finding and approval routes use the session paths", async function (this: Ctx, assert) {
    const stub = stubFetch(this, jsonResponse(200, []));
    const service = new IdeService();

    await service.listFindings("s1");
    stub.returns(Promise.resolve(jsonResponse(200, { finding: {}, detail: "" })));
    await service.getFinding("s1", "f 1");
    stub.returns(Promise.resolve(jsonResponse(200, { finding: {}, detail: "" })));
    await service.getFinding("s1", "f 1", true);
    stub.returns(Promise.resolve(jsonResponse(200, { file: {}, line: 3, hint: null })));
    await service.openFinding("s1", "f1");
    stub.returns(Promise.resolve(jsonResponse(200, [])));
    await service.listApprovals("s1");
    stub.returns(Promise.resolve(jsonResponse(201, { id: "s9", type: "change" })));
    const handed = await service.handover("s1");

    assert.deepEqual([0, 1, 2, 3, 4, 5].map((n) => `${callOf(stub, n).init.method ?? "GET"} ${callOf(stub, n).url}`), [
        "GET backend/sessions/s1/findings",
        "GET backend/sessions/s1/findings/f%201",
        "GET backend/sessions/s1/findings/f%201?refresh=true",
        "POST backend/sessions/s1/findings/f1/open",
        "GET backend/sessions/s1/approvals",
        "POST backend/sessions/s1/handover"
    ], "methods and paths");
    assert.strictEqual(handed.id, "s9", "handover returns the new session");
});

// --- phase 1c: the fake backend speaks the same contract ------------------------------

interface FakeCtx { fake: FakeBackend }

QUnit.module("IdeService against FakeBackend (diagnose)", {
    beforeEach: function (this: FakeCtx) {
        this.fake = new FakeBackend();
        this.fake.reset();
        this.fake.install();
    },
    afterEach: function (this: FakeCtx) {
        this.fake.restore();
    }
});

async function codeOf(call: Promise<unknown>): Promise<string> {
    try {
        await call;
        return "no error";
    } catch (e) {
        return `${(e as IdeError).status} ${(e as IdeError).code ?? ""}`;
    }
}

QUnit.test("only a non-production target offers and accepts diagnose sessions", async function (this: FakeCtx, assert) {
    const service = new IdeService();

    assert.deepEqual((await service.getMe()).diagnose_targets, [], "none flagged yet");
    assert.strictEqual(await codeOf(service.createSession("d", "dev-system", "diagnose")),
        "422 target_not_non_production", "refused");

    this.fake.allowDiagnose();
    assert.deepEqual((await service.getMe()).diagnose_targets, ["dev-system"], "offered once flagged");
    const session = await service.createSession("Slow order call", "dev-system", "diagnose");
    assert.strictEqual(session.type, "diagnose", "typed");
    assert.strictEqual(session.stage, "investigate", "in the single diagnose stage");
    assert.strictEqual((await service.createSession("c", "dev-system")).type, "change", "change by default");
    assert.strictEqual(await codeOf(service.approve(session.id)), "409 approve_not_allowed", "no stage gate");
    assert.strictEqual(await codeOf(service.streamRevise(session.id, "x", () => undefined)),
        "409 revise_not_allowed", "no revise");
});

QUnit.test("a scripted run emits findings and an approval, which the routes then serve", async function (this: FakeCtx, assert) {
    const service = new IdeService();
    // The decide and handover routes recheck the target's flag, as the server does.
    this.fake.allowDiagnose();
    const sid = this.fake.addSession("Dump in order class", [], [], "diagnose").id;
    this.fake.scriptRun({
        findings: [
            { kind: "dump", ref_id: "D1", title: "CX_SY_ZERODIVIDE", program: "ZCL_DEMO======================CP", include: "ZCL_DEMO======================CM001", line: 12 },
            { kind: "dump", ref_id: "D2", title: "MESSAGE_TYPE_X", program: "ZDEMO_REPORT", line: 7 },
            { kind: "gateway_error", ref_id: "G1", title: "HTTP 500" }
        ],
        approval: {}
    });
    const events: SseEvent[] = [];

    await service.streamMessage(sid, "Why does it dump?", (e) => events.push(e));

    assert.deepEqual(events.filter((e) => e.type === "finding").length, 3, "one finding frame each");
    const required = events.find((e) => e.type === "approval_required")?.data as Approval;
    assert.strictEqual(required.status, "pending", "a pending approval is asked for");
    assert.strictEqual(events[events.length - 1].type, "done", "done stays last");
    assert.notOk(events.some((e) => e.type === "artifact"), "a diagnose message run writes no artifact");

    const findings = await service.listFindings(sid);
    assert.deepEqual(findings.map((f) => f.ref_id), ["G1", "D2", "D1"], "newest first");
    const detail = await service.getFinding(sid, findings[1].id);
    assert.strictEqual(detail.finding.ref_id, "D2", "the finding");
    assert.ok(detail.detail.includes("DEVUSER01"), "with the stored detail, as SAP sent it");
    assert.notOk(detail.detail.includes("re-read"), "not read again");
    assert.ok((await service.getFinding(sid, findings[1].id, true)).detail.includes("re-read from SAP"), "a refresh reads it again");
    this.fake.sessions[this.fake.sessions.length - 1].session.masked = true;
    assert.strictEqual(await codeOf(service.getFinding(sid, findings[1].id)), "409 target_not_non_production",
        "a masked diagnose session (its target lost the flag) does not serve the stored detail");
    assert.strictEqual(await codeOf(service.getFinding(sid, findings[1].id, true)), "409 target_not_non_production",
        "nor reads it from SAP again");
    assert.strictEqual(await codeOf(service.openFinding(sid, findings[1].id)), "409 target_not_non_production", "nor a finding's source");
    this.fake.sessions[this.fake.sessions.length - 1].session.masked = false;

    const opened = await service.openFinding(sid, findings[1].id);
    assert.strictEqual(opened.file.path, "src/PROG/zdemo_report.prog.abap", "the program is opened");
    assert.strictEqual(opened.line, 7, "at the finding's line");
    const method = await service.openFinding(sid, findings[2].id);
    assert.strictEqual(method.file.object_name, "ZCL_DEMO", "a class pool opens the class");
    assert.strictEqual(method.line, null, "a method include has no mapped line");
    assert.ok(method.hint, "but a hint");
    assert.strictEqual(await codeOf(service.openFinding(sid, findings[0].id)), "422 no_source", "no program, no source");

    assert.deepEqual((await service.listApprovals(sid)).map((a) => a.id), [required.id], "the approval is listed");
    this.fake.failNext = { path: `sessions/${sid}/approvals/${required.id}`, ...APPROVAL_EXPIRED };
    assert.strictEqual(await codeOf(service.decideApproval(sid, required.id, "approve")), "410 approval_expired", "expired");
    this.fake.failNext = { path: `sessions/${sid}/approvals/${required.id}`, ...TARGET_NOT_NON_PRODUCTION };
    assert.strictEqual(await codeOf(service.decideApproval(sid, required.id, "approve")),
        "403 target_not_non_production", "target no longer non-production");
    const decided = await service.decideApproval(sid, required.id, "approve");
    assert.strictEqual(decided.status, "approved", "approved");
    assert.ok(decided.result?.trace_request_id, "with the armed trace request");
    assert.strictEqual(await codeOf(service.decideApproval(sid, required.id, "deny")),
        "409 approval_not_pending", "decided only once");
});

QUnit.test("report writes a report artifact and handover seeds a change session with it", async function (this: FakeCtx, assert) {
    const service = new IdeService();
    // The decide and handover routes recheck the target's flag, as the server does.
    this.fake.allowDiagnose();
    const sid = this.fake.addSession("Dump in order class", [], [], "diagnose").id;
    const changeSid = this.fake.addSession("A change").id;

    assert.strictEqual(await codeOf(service.handover(sid)), "409 missing_artifact", "no report yet");
    assert.strictEqual(await codeOf(service.streamReport(changeSid, () => undefined)), "409 not_diagnose", "diagnose only");
    assert.strictEqual(await codeOf(service.handover(changeSid)), "409 not_diagnose", "diagnose only");

    const events: SseEvent[] = [];
    await service.streamReport(sid, (e) => events.push(e));
    const artifact = events.find((e) => e.type === "artifact");
    assert.deepEqual(artifact && (artifact.data as { kind: string; version: number }).kind, "report", "a report artifact");
    assert.strictEqual(events[events.length - 1].type, "done", "done last");
    const report = (await service.listArtifacts(sid, "report"))[0];

    const next = await service.handover(sid);
    assert.strictEqual(next.type, "change", "a change session");
    assert.strictEqual(next.stage, "chat", "in chat");
    assert.strictEqual(next.title, "Change: Dump in order class", "titled after the diagnose session");
    const copied = await service.listArtifacts(next.id, "report");
    assert.strictEqual(copied.length, 1, "one report");
    assert.strictEqual(copied[0].version, 1, "as version 1");
    assert.strictEqual(copied[0].content, report.content, "with the report's content");
});

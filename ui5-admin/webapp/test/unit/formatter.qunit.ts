import formatter from "com/agent/admin/model/formatter";

QUnit.module("formatter");

QUnit.test("run status maps to a ValueState", function (assert) {
    assert.strictEqual(formatter.runStatusState("success"), "Success");
    assert.strictEqual(formatter.runStatusState("failed"), "Error");
    assert.strictEqual(formatter.runStatusState("interrupted"), "Warning");
    assert.strictEqual(formatter.runStatusState("running"), "Information");
});

QUnit.test("the legacy 'degraded' status still gets a colour", function (assert) {
    // No current code writes this, but stored rows carry it — half the runs in
    // the deployed acceptance database do. Without this it rendered neutral,
    // which is indistinguishable from a status the app does not understand.
    assert.strictEqual(formatter.runStatusState("degraded"), "Warning");
});

QUnit.test("an unknown status degrades to None rather than throwing", function (assert) {
    assert.strictEqual(formatter.runStatusState("bogus" as never), "None");
});

QUnit.test("duration is rendered from the two timestamps", function (assert) {
    assert.strictEqual(
        formatter.runDuration("2026-08-24T10:00:00", "2026-08-24T10:01:30"),
        "1m 30s"
    );
    assert.strictEqual(
        formatter.runDuration("2026-08-24T10:00:00", "2026-08-24T10:00:05"),
        "5s"
    );
});

QUnit.test("a still-running job has no duration", function (assert) {
    assert.strictEqual(formatter.runDuration("2026-08-24T10:00:00", null), "");
});

QUnit.test("a null timestamp renders as an em dash, not 'null'", function (assert) {
    assert.strictEqual(formatter.timestamp(null), "—");
});

QUnit.test("server summary counts servers and names the builtins", function (assert) {
    assert.strictEqual(
        formatter.serverSummary([{ url: "builtin:gmail", auth_mode: "none" }]),
        "builtin:gmail"
    );
    assert.strictEqual(
        formatter.serverSummary([
            { url: "https://a.hana.ondemand.com/mcp", auth_mode: "jwt" },
            { url: "https://b.hana.ondemand.com/mcp", auth_mode: "jwt" }
        ]),
        "2 MCP servers"
    );
    assert.strictEqual(formatter.serverSummary([]), "—");
});

QUnit.test("a server needing no token is neutral, whatever its token state", function (assert) {
    // app_only and destination servers are connected by configuration. Colouring
    // them would suggest an action nobody can take from this screen.
    assert.strictEqual(formatter.credentialState(false, "none"), "None");
    assert.strictEqual(formatter.credentialState(false, "valid"), "None");
});

QUnit.test("a live token reads as success, an expired one as an error", function (assert) {
    assert.strictEqual(formatter.credentialState(true, "valid"), "Success");
    assert.strictEqual(formatter.credentialState(true, "expired"), "Error");
});

QUnit.test("a refreshable token is success, not a warning", function (assert) {
    // The access token has expired but PerUserOAuth2Auth renews it silently, so
    // the connection works and nobody needs to do anything. Flagging it would
    // train admins to ignore the column.
    assert.strictEqual(formatter.credentialState(true, "refreshable"), "Success");
});

QUnit.test("never signing in is a warning, not an error", function (assert) {
    // Nothing is broken yet — the agent has simply not been connected.
    assert.strictEqual(formatter.credentialState(true, "none"), "Warning");
});

QUnit.test("an unknown token state degrades to a warning", function (assert) {
    assert.strictEqual(formatter.credentialState(true, "bogus" as never), "Warning");
});

QUnit.module("formatter.runSummary: a run table's one line per run");

QUnit.test("prefers the summary, falls back to the error, never shows null", function (assert) {
    assert.strictEqual(formatter.runSummary("3 mails triaged", null), "3 mails triaged", "summary wins");
    assert.strictEqual(formatter.runSummary("3 mails triaged", "ignored"), "3 mails triaged", "even when an error is also recorded");
    assert.strictEqual(formatter.runSummary(null, "No run-as principal is configured"), "No run-as principal is configured",
        "a failed preflight has no summary, so its error is the line");
    assert.strictEqual(formatter.runSummary("   ", "boom"), "boom", "a blank summary counts as none");
    assert.strictEqual(formatter.runSummary(null, null), "", "nothing at all renders empty, not 'null'");
    assert.strictEqual(formatter.runSummary(undefined, undefined), "", "same for undefined");
});

QUnit.module("formatter: a job run's activity and plan");

QUnit.test("an activity entry's status maps to a ValueState", function (assert) {
    assert.strictEqual(formatter.activityState("running"), "Information");
    assert.strictEqual(formatter.activityState("ok"), "Success");
    assert.strictEqual(formatter.activityState("error"), "Error");
    assert.strictEqual(formatter.activityState(undefined), "None", "a note has no status");
});

QUnit.test("a plan item's status maps to a ValueState", function (assert) {
    assert.strictEqual(formatter.todoState("completed"), "Success");
    assert.strictEqual(formatter.todoState("in_progress"), "Information");
    assert.strictEqual(formatter.todoState("pending"), "None");
    assert.strictEqual(formatter.todoState("bogus"), "None");
});

QUnit.test("an activity time shows the time of day, never 'null'", function (assert) {
    assert.strictEqual(formatter.timeOfDay(null), "—");
    assert.strictEqual(formatter.timeOfDay("not a date"), "—");
    assert.strictEqual(
        formatter.timeOfDay("2026-10-01T21:11:35+00:00"),
        new Date("2026-10-01T21:11:35+00:00").toLocaleTimeString()
    );
});

QUnit.module("formatter: a job run's token usage");

const USAGE = {
    input_tokens: 10000, output_tokens: 2345, cache_read_tokens: 1200,
    total_tokens: 12345, requests: 7
};
const PLAIN = "{0} total ({1} in, {2} out) · {3}";
const CACHED = "{0} total ({1} in, {2} out, {3} cached) · {4}";
const ONE = "{0} model request";
const MANY = "{0} model requests";

QUnit.test("no usage renders empty and hides the detail row", function (assert) {
    for (const value of [null, undefined, "12345", 12345, [], {}]) {
        assert.strictEqual(formatter.tokenTotal(value), "", `total of ${JSON.stringify(value)}`);
        assert.strictEqual(formatter.tokenBreakdown("in {0} · out {1}", value), "");
        assert.strictEqual(formatter.tokenDetail(PLAIN, CACHED, ONE, MANY, value), "");
        assert.notOk(formatter.hasTokenUsage(value));
    }
});

QUnit.test("a count that is not a finite, non-negative number is no usage", function (assert) {
    for (const bad of [NaN, Infinity, -1, "5", null]) {
        const usage = { ...USAGE, output_tokens: bad };
        assert.strictEqual(formatter.tokenTotal(usage), "", `output_tokens ${String(bad)}`);
        assert.notOk(formatter.hasTokenUsage(usage));
    }
});

QUnit.test("large numbers are grouped by the locale", function (assert) {
    assert.ok(formatter.hasTokenUsage(USAGE));
    assert.strictEqual(formatter.tokenTotal(USAGE), "12,345");
    assert.strictEqual(formatter.tokenTotal({ ...USAGE, total_tokens: 1234567890 }), "1,234,567,890");
    assert.strictEqual(formatter.tokenBreakdown("in {0} · out {1}", USAGE), "in 10,000 · out 2,345");
    assert.strictEqual(
        formatter.tokenDetail(PLAIN, CACHED, ONE, MANY, USAGE),
        "12,345 total (10,000 in, 2,345 out, 1,200 cached) · 7 model requests"
    );
});

QUnit.test("zero tokens still show, the cached part only when something was cached", function (assert) {
    const zero = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, total_tokens: 0, requests: 0 };
    assert.ok(formatter.hasTokenUsage(zero), "a run that used none is still a run with usage");
    assert.strictEqual(formatter.tokenTotal(zero), "0");
    assert.strictEqual(formatter.tokenDetail(PLAIN, CACHED, ONE, MANY, zero), "0 total (0 in, 0 out) · 0 model requests");
    assert.strictEqual(
        formatter.tokenDetail(PLAIN, CACHED, ONE, MANY, { ...USAGE, cache_read_tokens: 0, requests: 1 }),
        "12,345 total (10,000 in, 2,345 out) · 1 model request"
    );
});

QUnit.test("a workflow step's tokens: total with the in/out split, empty without usage", function (assert) {
    const STEP = "{0} tokens (in {1} · out {2})";
    assert.strictEqual(formatter.tokenStep(STEP, USAGE), "12,345 tokens (in 10,000 · out 2,345)");
    assert.strictEqual(formatter.tokenStep(STEP, null), "", "a deterministic step has no usage");
    assert.strictEqual(formatter.tokenStep(STEP, { ...USAGE, input_tokens: NaN }), "", "a bad count is no usage");
});

QUnit.module("formatter: a run's estimated cost in euros");

const COST = "≈ € {0}";
const COST_SMALL = "< € 0.01";
const COST_PARTLY = "{0} (partly priced)";
const COST_UNPRICED = "no price for this model";
const PRICED = { amount: 0.0421, currency: "EUR", complete: true };

QUnit.test("a priced run shows the amount with two decimals", function (assert) {
    assert.ok(formatter.hasCost(PRICED));
    assert.strictEqual(formatter.costText(COST, COST_SMALL, COST_PARTLY, COST_UNPRICED, PRICED), "≈ € 0.04");
    assert.strictEqual(
        formatter.costText(COST, COST_SMALL, COST_PARTLY, COST_UNPRICED, { ...PRICED, amount: 1234.567 }), "≈ € 1,234.57",
        "grouped and rounded by the locale"
    );
});

QUnit.test("an amount below a cent never reads as zero; a real zero does", function (assert) {
    assert.strictEqual(formatter.costText(COST, COST_SMALL, COST_PARTLY, COST_UNPRICED, { ...PRICED, amount: 0.003 }), "< € 0.01");
    assert.strictEqual(formatter.costText(COST, COST_SMALL, COST_PARTLY, COST_UNPRICED, { ...PRICED, amount: 0 }), "≈ € 0.00");
});

QUnit.test("a run with an unpriced model says partly priced", function (assert) {
    assert.strictEqual(
        formatter.costText(COST, COST_SMALL, COST_PARTLY, COST_UNPRICED, { ...PRICED, complete: false }), "≈ € 0.04 (partly priced)"
    );
    assert.strictEqual(
        formatter.costText(COST, COST_SMALL, COST_PARTLY, COST_UNPRICED, { amount: 0, currency: "EUR", complete: false }),
        "no price for this model", "no model priced: a text, never an amount"
    );
    assert.strictEqual(
        formatter.costText(COST, COST_SMALL, COST_PARTLY, COST_UNPRICED, { amount: 0.003, currency: "EUR", complete: false }),
        "< € 0.01 (partly priced)"
    );
});

QUnit.test("no cost or a malformed one shows nothing", function (assert) {
    const bad: unknown[] = [
        null, undefined, {}, [], "0.04", 0.04,
        { ...PRICED, amount: NaN }, { ...PRICED, amount: Infinity }, { ...PRICED, amount: -1 },
        { ...PRICED, amount: "0.04" }, { ...PRICED, currency: "USD" }, { ...PRICED, currency: undefined },
        { ...PRICED, complete: "true" }, { ...PRICED, complete: 1 }, { amount: 0.04, currency: "EUR" }
    ];
    for (const value of bad) {
        assert.strictEqual(formatter.costText(COST, COST_SMALL, COST_PARTLY, COST_UNPRICED, value), "", JSON.stringify(value) ?? "undefined");
        assert.notOk(formatter.hasCost(value));
    }
    assert.strictEqual(formatter.costText("", COST_SMALL, COST_PARTLY, COST_UNPRICED, PRICED), "", "no pattern: nothing");
});

import { errorText, gateErrorText, runErrorText } from "com/agent/ide/model/errorText";
import { IdeError } from "com/agent/ide/service/IdeService";
import ResourceModel from "sap/ui/model/resource/ResourceModel";

const bundle = new ResourceModel({ bundleName: "com.agent.ide.i18n.i18n" }).getResourceBundle() as unknown as {
    getText(key: string, args?: (string | number)[]): string;
};
const text = (key: string, args?: (string | number)[]): string => bundle.getText(key, args);

QUnit.module("errorText");

QUnit.test("424 user_token_required asks to reload, without the raw detail", function (assert) {
    const msg = errorText(new IdeError(424, "No user token", "user_token_required"), text);
    assert.strictEqual(msg, text("userTokenRequired"));
    assert.ok(/reload/i.test(msg), "tells the user to reload");
});

QUnit.test("502 sap_* shows SAP's message and points at the user mapping", function (assert) {
    const msg = errorText(new IdeError(502, "Logon failed for user", "sap_authentication_failed"), text);
    assert.strictEqual(msg, text("sapError", ["Logon failed for user"]));
    assert.ok(msg.includes("Logon failed for user"), "carries the SAP message");
    assert.ok(/user mapping/i.test(msg), "points at the SAP user mapping");
});

QUnit.test("409 shows the server's gate message as is", function (assert) {
    assert.strictEqual(
        errorText(new IdeError(409, "A run is already in progress.", "run_in_progress"), text),
        "A run is already in progress."
    );
});

QUnit.test("429 usage_exhausted has its own message", function (assert) {
    assert.strictEqual(
        errorText(new IdeError(429, "Cap of 200 reached", "usage_exhausted"), text),
        text("usageExhausted", ["Cap of 200 reached"])
    );
});

QUnit.test("401 and an expired session ask to reload; 403 names the missing role", function (assert) {
    assert.strictEqual(errorText(new IdeError(401, ""), text), text("sessionExpired"));
    assert.strictEqual(errorText(new IdeError(302, "x", "session_expired"), text), text("sessionExpired"));
    const forbidden = errorText(new IdeError(403, "Forbidden"), text);
    assert.strictEqual(forbidden, text("missingRole"));
    assert.ok(forbidden.includes("ABAP IDE Developer"), "names the role to ask for");
});

QUnit.test("anything else falls back to requestFailed with the detail", function (assert) {
    assert.strictEqual(errorText(new IdeError(500, "boom"), text), text("requestFailed", ["boom"]));
    assert.strictEqual(errorText(new Error("network down"), text), text("requestFailed", ["network down"]));
    assert.strictEqual(errorText("odd", text), text("requestFailed", ["odd"]));
});

QUnit.test("a SAP code wins over the HTTP status: a 403 sap_* is SAP's refusal, not a missing role", function (assert) {
    assert.strictEqual(
        errorText(new IdeError(403, "No authorization for S_DEVELOP", "sap_not_authorized"), text),
        text("sapError", ["No authorization for S_DEVELOP"])
    );
    assert.strictEqual(errorText(new IdeError(403, "Forbidden", "forbidden"), text), text("missingRole"));
});

QUnit.test("a 403 readonly_refused is the read-only guard, not a missing role", function (assert) {
    const msg = errorText(new IdeError(403, "Tool SAPWrite is not allowed.", "readonly_refused"), text);
    assert.strictEqual(msg, text("readOnlyRefused"));
    assert.notStrictEqual(text("readOnlyRefused"), "readOnlyRefused", "readOnlyRefused exists in i18n");
    assert.strictEqual(text("activityRefused"), "refused (read-only)", "the activity panel's label for a refused tool");
    assert.notStrictEqual(msg, text("missingRole"));
    assert.ok(/read-only IDE/.test(msg), "says the read-only IDE refused it");
    assert.strictEqual(
        gateErrorText(new IdeError(403, "x", "readonly_refused"), text), text("readOnlyRefused"), "same text on a gate call"
    );
});

QUnit.test("missingRole only for a 403 without a code or with code forbidden", function (assert) {
    assert.strictEqual(errorText(new IdeError(403, "Forbidden"), text), text("missingRole"));
    assert.strictEqual(errorText(new IdeError(403, "Forbidden", "forbidden"), text), text("missingRole"));
    assert.strictEqual(
        errorText(new IdeError(403, "Something else", "brand_new"), text), text("requestFailed", ["Something else"]),
        "an unknown 403 code is not called a missing role"
    );
});

QUnit.module("gateErrorText / runErrorText");

QUnit.test("known gate codes get their own i18n text; unknown codes show the server message", function (assert) {
    const cases: [string, string][] = [
        ["run_in_progress", "gateRunInProgress"], ["stage_done", "gateStageDone"],
        ["missing_artifact", "gateMissingArtifact"], ["no_proposals", "gateNoProposals"],
        ["revise_not_allowed", "gateReviseNotAllowed"], ["stage_changed", "gateStageChanged"],
        ["invalid_stage", "gateInvalidStage"], ["run_on_other_instance", "cancelOtherInstance"]
    ];
    for (const [code, key] of cases) {
        assert.strictEqual(gateErrorText(new IdeError(409, "server says", code), text), text(key), code);
        assert.notStrictEqual(text(key), key, `${key} exists in i18n`);
    }
    assert.strictEqual(gateErrorText(new IdeError(409, "Something new.", "brand_new"), text), "Something new.");
    assert.strictEqual(gateErrorText(new IdeError(401, ""), text), text("sessionExpired"), "falls back to errorText");
    assert.strictEqual(gateErrorText(new IdeError(429, "Cap", "usage_exhausted"), text), text("usageExhausted", ["Cap"]));
});

QUnit.test("stream error events: incomplete, timeout, failed, usage, token, SAP and unknown", function (assert) {
    assert.strictEqual(runErrorText({ message: "x", code: "stream_incomplete" }, text), text("streamIncomplete"));
    assert.strictEqual(runErrorText({ message: "took 600 s", code: "run_timeout" }, text), text("runTimeout", ["took 600 s"]));
    assert.strictEqual(runErrorText({ message: "Reference: r-9.", code: "run_failed" }, text), text("runFailed", ["Reference: r-9."]));
    assert.ok(text("runFailed", ["Reference: r-9."]).includes("r-9"), "the run reference is kept");
    assert.strictEqual(runErrorText({ message: "cap", code: "usage_exhausted" }, text), text("usageExhausted", ["cap"]));
    assert.strictEqual(runErrorText({ message: "no token", code: "user_token_required" }, text), text("userTokenRequired"));
    assert.strictEqual(runErrorText({ message: "logon", code: "sap_authentication_failed" }, text), text("sapError", ["logon"]));
    assert.strictEqual(runErrorText({ message: "Agent is missing." , code: "agent_missing" }, text), "Agent is missing.");
    assert.strictEqual(runErrorText({ message: "plain" }, text), "plain");
    assert.strictEqual(runErrorText({ message: "no", code: "readonly_refused" }, text), text("readOnlyRefused"));
});

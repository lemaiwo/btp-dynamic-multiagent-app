import { errorText, gateErrorText, runErrorText, runNoteText } from "com/agent/ide/model/errorText";
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
    assert.ok(forbidden.includes("\"ABAP IDE Developer\" role collection"), "names the role collection to ask for");
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
    assert.strictEqual(msg, "This action is not allowed in the ABAP Assistant (read-only).", "names the app as it is called (no \"IDE\")");
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

QUnit.test("target_not_non_production has its own sentence, whatever the status; run notes have theirs", function (assert) {
    const expected = text("targetNotNonProd");
    assert.strictEqual(errorText(new IdeError(403, "The target is not flagged non-production.", "target_not_non_production"), text), expected);
    assert.strictEqual(gateErrorText(new IdeError(409, "x", "target_not_non_production"), text), expected);
    assert.strictEqual(gateErrorText(new IdeError(409, "x", "not_diagnose"), text), text("gateNotDiagnose"));
    assert.strictEqual(runNoteText({ code: "no_diagnose_server", message: "server text" }, text),
        "No ARC-1 server of this agent matches the session target — diagnostics tools are unavailable.");
    assert.ok(runNoteText({ code: "conventions_unavailable", message: "server text" }, text).includes("conventions"));
    assert.strictEqual(runNoteText({ code: "other", message: "server text" }, text), "server text");
});

QUnit.test("a 424 is mapped by its code: only a missing user token asks to sign in again", function (assert) {
    const notConfigured = errorText(new IdeError(424, "No ARC-1 server is configured for target X", "arc1_not_configured"), text);
    assert.strictEqual(notConfigured, text("arc1NotConfigured"));
    assert.notOk(/reload|sign in/i.test(notConfigured), "signing in again does not help there");
    assert.ok(/administrator/i.test(notConfigured), "it points at the administrator");
    assert.strictEqual(errorText(new IdeError(424, "No user token"), text), text("userTokenRequired"), "a 424 without a code is the user token");
    assert.strictEqual(errorText(new IdeError(424, "Something else", "other_dependency"), text),
        text("requestFailed", ["Something else"]), "an unknown 424 code shows the server's detail");
    assert.strictEqual(runErrorText({ message: "not configured", code: "arc1_not_configured" }, text), text("arc1NotConfigured"),
        "the stream's error frame too");
    assert.strictEqual(gateErrorText(new IdeError(424, "x", "arc1_not_configured"), text), text("arc1NotConfigured"));
});

QUnit.test("csrf_failed asks to reload the page, never names a missing role (Task U7)", function (assert) {
    const err = new IdeError(403, "The server did not accept the request's security token.", "csrf_failed");
    assert.notOk(err.isAuth, "not isAuth");
    const msg = errorText(err, text);
    assert.strictEqual(msg, "The request was refused by the security check — reload the page.");
    assert.notStrictEqual(msg, text("missingRole"));
    assert.strictEqual(gateErrorText(err, text), msg, "the gate wording falls back to the same text");
});

QUnit.test("approve and request-changes refusals are worded per code, not by the server (U7 fix round)", function (assert) {
    for (const code of ["version_changed", "pin_conflict", "approve_not_allowed", "nothing_to_send", "stage_changed", "syntax_check_running"]) {
        const msg = gateErrorText(new IdeError(409, "server sentence", code), text);
        assert.notStrictEqual(msg, "server sentence", `${code} has its own text`);
        assert.ok(msg && !/^gate/.test(msg), `${code}: ${msg}`);
    }
});

QUnit.test("409 open_comments has the primary action's own sentence (Task U7)", function (assert) {
    const msg = gateErrorText(new IdeError(409, "Resolve or dismiss the open review comments first.", "open_comments"), text);
    assert.strictEqual(msg, "Resolve or dismiss the open review comments first.");
});

QUnit.test("gateErrorText: a 409 code that names an Object.prototype member is not a gate key", function (assert) {
    ["constructor", "toString", "__proto__", "hasOwnProperty"].forEach((code) => {
        const e = new IdeError(409, "The server's own sentence.", code);
        assert.strictEqual(gateErrorText(e, text), "The server's own sentence.", `${code}: the server's sentence, no crash`);
    });
});

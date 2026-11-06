import reconcileEntry from "com/agent/admin/model/reconcileEntry";
import oauthConfig from "com/agent/admin/model/oauthConfig";
import validators from "com/agent/admin/model/validators";
import { authModesFor, findBuiltin } from "com/agent/admin/model/builtins";
import type { AuthMode, OAuthClient } from "com/agent/admin/service/types";

const URL = "builtin:reconcile";

/** The block as the API stores and answers it: all four keys, as strings. */
function stored(over: Record<string, unknown> = {}): Record<string, unknown> {
    return {
        arc1_server: "https://arc1.example.com/mcp",
        target: "SRC_SYSTEM",
        batch_size: "500",
        mismatch_cap: "20000",
        ...over
    };
}

function check(oauth: Record<string, unknown> | undefined, mode: AuthMode = "none"): string {
    return validators.validateOAuth(oauth as OAuthClient | undefined, mode, URL);
}

QUnit.module("reconcileEntry — the catalog entry");

QUnit.test("the dropdown lists \"Migration reconciliation\" on auth mode none only", function (assert) {
    const entry = findBuiltin(URL);
    assert.ok(entry, "listed");
    assert.strictEqual(entry?.titleKey, "builtinReconcile");
    assert.strictEqual(entry?.descriptionKey, "builtinReconcileDesc");
    assert.strictEqual(entry?.defaultAuthMode, "none");
    assert.deepEqual(authModesFor(URL), ["none"]);
    assert.strictEqual(findBuiltin(" BUILTIN:Reconcile/ ")?.url, URL, "in any spelling");
    assert.strictEqual(validators.validateServerUrl(URL, "none"), "");
    assert.ok(validators.carriesPublicConfig(URL), "its block is sent on none");
});

QUnit.module("reconcileEntry — load and save");

QUnit.test("a stored entry saved unchanged posts the same four keys", function (assert) {
    const block = stored();
    const out = oauthConfig.cleanOAuth({ ...block }, "none", URL) as Record<string, unknown>;
    assert.deepEqual(out, block, "no key dropped, none added");
    assert.strictEqual(check(out), "", "and the validator lets it through");
});

QUnit.test("the dialog's other fields never ride along", function (assert) {
    // The dialog model of a new server holds every field of every type.
    const form = {
        ...stored(), dcr: false, client_id: "cid", client_secret: "sec", destination: "DEST",
        min_score: "9.0", lookback: "2d", user_context: true, allow_send: true, has_client_secret: true
    };
    const out = oauthConfig.cleanOAuth(form, "none", URL) as Record<string, unknown>;
    assert.deepEqual(Object.keys(out).sort(), ["arc1_server", "batch_size", "mismatch_cap", "target"]);
});

QUnit.test("the server's has_client_secret: false echo is not a stray key", function (assert) {
    assert.strictEqual(check(stored({ has_client_secret: false })), "");
    assert.strictEqual(check(stored({ has_client_secret: true })), reconcileEntry.RECONCILE_TEXTS.stray);
    assert.deepEqual(oauthConfig.cleanOAuth(stored({ has_client_secret: false }), "none", URL), {
        arc1_server: "https://arc1.example.com/mcp", target: "SRC_SYSTEM", batch_size: "500", mismatch_cap: "20000"
    });
});

QUnit.test("values are trimmed and a blank one is left out (the default)", function (assert) {
    const out = oauthConfig.cleanOAuth(stored({ target: "  ", batch_size: " 300 ", mismatch_cap: "" }), "none", URL);
    assert.deepEqual(out, { arc1_server: "https://arc1.example.com/mcp", batch_size: "300" });
    assert.strictEqual(oauthConfig.cleanOAuth({ arc1_server: "", target: "" }, "none", URL), undefined,
        "all blank: no block");
    assert.strictEqual(check(undefined), "");
});

QUnit.test("another built-in never keeps the four keys", function (assert) {
    const form = { ...stored(), min_score: "9.0" };
    const notes = oauthConfig.cleanOAuth(form, "none", "builtin:sapnotes") as Record<string, unknown>;
    assert.deepEqual(Object.keys(notes), ["min_score"]);
    assert.ok(validators.validateOAuth({ arc1_server: "https://a.example.com/mcp" } as unknown as OAuthClient,
        "none", "builtin:sapnotes") !== "", "and the validator refuses them there");
});

QUnit.module("reconcileEntry — the server's ranges");

QUnit.test("auth mode other than none is refused", function (assert) {
    for (const mode of ["destination", "oauth2", "jwt", "app_only"] as AuthMode[]) {
        assert.strictEqual(check(stored(), mode), reconcileEntry.RECONCILE_TEXTS.mode, mode);
    }
    assert.strictEqual(check(undefined, "jwt"), reconcileEntry.RECONCILE_TEXTS.mode, "also without a block");
});

QUnit.test("a key outside the four is refused without naming it", function (assert) {
    const msg = check(stored({ secret_thing: "x" }));
    assert.strictEqual(msg, reconcileEntry.RECONCILE_TEXTS.stray);
    assert.notOk(msg.includes("secret_thing"));
});

QUnit.test("the ARC-1 server: an https URL with a host, no user, query or fragment, at most 512", function (assert) {
    for (const ok of ["https://arc1.example.com/mcp", "https://arc1.example.com:8443/multi/mcp", " https://h.example.com "]) {
        assert.strictEqual(check(stored({ arc1_server: ok })), "", ok);
    }
    const long = "https://h.example.com/" + "a".repeat(491);
    assert.strictEqual(long.length, 513);
    for (const bad of ["http://arc1.example.com/mcp", "https://user:pw@arc1.example.com/mcp", "https://u@h.example.com/",
        "https://arc1.example.com/mcp?token=abc", "https://arc1.example.com/mcp#frag", "https://", "arc1.example.com",
        "https://h.example.com:99999/", long]) {
        const msg = check(stored({ arc1_server: bad }));
        assert.strictEqual(msg, reconcileEntry.RECONCILE_TEXTS.arc1Server, bad);
        assert.notOk(msg.includes(bad.trim()), "the value is never repeated");
    }
    assert.strictEqual(check(stored({ arc1_server: "https://h.example.com/" + "a".repeat(490) })), "", "512 is fine");
});

QUnit.test("the target: 1 to 64 letters, digits, '_', '.', '-' or '/'", function (assert) {
    for (const ok of ["ECC", "src.system-1_A", "SBQ/200", "a".repeat(64)]) {
        assert.strictEqual(check(stored({ target: ok })), "", ok);
    }
    for (const bad of ["two words", "a".repeat(65), "ä"]) {
        assert.strictEqual(check(stored({ target: bad })), reconcileEntry.RECONCILE_TEXTS.target, bad);
    }
});

QUnit.test("batch size 1..10000 and mismatch cap 1..200000, whole numbers", function (assert) {
    for (const ok of ["1", "200", "10000"]) {
        assert.strictEqual(check(stored({ batch_size: ok })), "", "batch " + ok);
    }
    for (const bad of ["0", "10001", "-1", "1.5", "1e3", "abc", "0000001"]) {
        assert.strictEqual(check(stored({ batch_size: bad })), reconcileEntry.RECONCILE_TEXTS.batchSize, "batch " + bad);
    }
    for (const ok of ["1", "50000", "200000"]) {
        assert.strictEqual(check(stored({ mismatch_cap: ok })), "", "cap " + ok);
    }
    for (const bad of ["0", "200001", "12345678", "1,000", "x"]) {
        assert.strictEqual(check(stored({ mismatch_cap: bad })), reconcileEntry.RECONCILE_TEXTS.mismatchCap, "cap " + bad);
    }
});

QUnit.test("no message holds a value", function (assert) {
    Object.values(reconcileEntry.RECONCILE_TEXTS).forEach((text) => {
        assert.notOk(/example|SRC_SYSTEM|https:\/\//.test(text), text);
    });
});

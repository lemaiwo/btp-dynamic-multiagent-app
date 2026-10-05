import odataDestinations, { type DestinationsState } from "com/agent/admin/model/odataDestinations";
import type { ODataDestination, ODataDestinationList } from "com/agent/admin/service/types";

QUnit.module("odataDestinations");

/** A translator that shows which key and arguments were asked for. */
function text(key: string, args?: (string | number)[]): string {
    return args && args.length ? `${key}(${args.join(",")})` : key;
}

function destination(name: string, over: Partial<ODataDestination> = {}): ODataDestination {
    return {
        name, description: "", type: "HTTP", proxy_type: "Internet", authentication: "BasicAuthentication",
        level: "subaccount", user_propagating: false, usable: true, reason: null, notes: [],
        shadows_subaccount: false, ...over
    };
}

function list(items: ODataDestination[], over: Partial<ODataDestinationList> = {}): ODataDestinationList {
    return { items, truncated: false, skipped: 0, warnings: [], ...over };
}

const USER = destination("S4_DEV_USER", {
    authentication: "PrincipalPropagation", user_propagating: true, proxy_type: "OnPremise",
    notes: ["on_premise"], description: "Development system"
});
const BASIC = destination("S4_DEV_BASIC", { level: "instance" });
const RFC = destination("S4_DEV_RFC", { type: "RFC", usable: false, reason: "not_http" });
const BAD_ONE = destination("S4 DEV", { usable: false, reason: "invalid_name" });
const BAD_TWO = destination("S4 DEV", { usable: false, reason: "invalid_name" });
const LIST = list([BAD_ONE, BAD_TWO, BASIC, RFC, USER]);

QUnit.test("an item's line says sign-in, network and level in words, then the description", function (assert) {
    assert.strictEqual(
        odataDestinations.describe(USER, text),
        "odataDestinationAuthUser · odataDestinationOnPremise · odataDestinationLevelSubaccount · Development system"
    );
    assert.strictEqual(
        odataDestinations.describe(BASIC, text),
        "odataDestinationAuthFixed · odataDestinationInternet · odataDestinationLevelInstance",
        "no description, no trailing separator"
    );
    assert.strictEqual(
        odataDestinations.describe(destination("X", { authentication: "NoAuthentication", proxy_type: "PrivateLink" }), text),
        "odataDestinationAuthNone · odataDestinationLevelSubaccount",
        "a network that is neither of the two is left out"
    );
    for (const authentication of ["", "other"]) {
        assert.strictEqual(
            odataDestinations.describe(destination("X", { authentication, proxy_type: "" }), text),
            "odataDestinationAuthUnknown · odataDestinationLevelSubaccount",
            `authentication "${authentication}" is not called a fixed account`
        );
    }
    assert.strictEqual(
        odataDestinations.describe(destination("X", { description: "  " }), text),
        "odataDestinationAuthFixed · odataDestinationInternet · odataDestinationLevelSubaccount",
        "a blank description is none"
    );
});

QUnit.test("only usable destinations are offered, each name once", function (assert) {
    assert.deepEqual(
        odataDestinations.choices(LIST, text).map((choice) => choice.name), ["S4_DEV_BASIC", "S4_DEV_USER"]
    );
    assert.strictEqual(
        odataDestinations.choices(LIST, text)[0].info,
        "odataDestinationAuthFixed · odataDestinationInternet · odataDestinationLevelInstance"
    );
    assert.strictEqual(odataDestinations.unusableCount(LIST), 3);
    assert.deepEqual(
        odataDestinations.choices(list([BASIC, destination("S4_DEV_BASIC", { description: "again" })]), text)
            .map((choice) => choice.info),
        ["odataDestinationAuthFixed · odataDestinationInternet · odataDestinationLevelInstance"],
        "the first of two with one name"
    );
    assert.deepEqual(odataDestinations.choices("loading", text), []);
    assert.deepEqual(odataDestinations.choices("unavailable", text), []);
    assert.strictEqual(odataDestinations.unusableCount("unavailable"), 0);
});

QUnit.test("an answer that is not a list of destinations is no list", function (assert) {
    assert.strictEqual(odataDestinations.read(undefined), "unavailable");
    assert.strictEqual(odataDestinations.read(null), "unavailable");
    assert.strictEqual(odataDestinations.read([]), "unavailable");
    assert.strictEqual(odataDestinations.read({ items: "x" }), "unavailable");
    assert.strictEqual(odataDestinations.read({ detail: "no" }), "unavailable");

    const read = odataDestinations.read({ items: [BASIC, null, { name: 3 }, { name: "" }, USER], truncated: 1 });
    assert.deepEqual(
        (read as ODataDestinationList).items.map((item) => item.name), ["S4_DEV_BASIC", "S4_DEV_USER"],
        "entries without a name are dropped"
    );
    assert.strictEqual((read as ODataDestinationList).truncated, true);
    assert.deepEqual((read as ODataDestinationList).warnings, [], "missing parts are empty");
});

QUnit.test("runs as the signed-in user through a fixed account is a mismatch, and the reverse", function (assert) {
    assert.strictEqual(odataDestinations.notice(LIST, "S4_DEV_BASIC", true), "fixedAccount");
    assert.strictEqual(odataDestinations.notice(LIST, "S4_DEV_BASIC", false), "");
    assert.strictEqual(odataDestinations.notice(LIST, "S4_DEV_USER", false), "needsUser");
    assert.strictEqual(odataDestinations.notice(LIST, "S4_DEV_USER", true), "");
    assert.strictEqual(odataDestinations.notice(LIST, "  S4_DEV_USER ", false), "needsUser", "as the server reads the name");
    assert.strictEqual(odataDestinations.notice(LIST, "s4_dev_user", false), "notListed", "names are case-sensitive");
});

QUnit.test("no mismatch without a list, and none for a name that is not offered", function (assert) {
    const states: DestinationsState[] = ["loading", "unavailable"];
    for (const state of states) {
        for (const userContext of [true, false]) {
            assert.strictEqual(odataDestinations.notice(state, "S4_DEV_BASIC", userContext), "", `${state}`);
            assert.strictEqual(odataDestinations.notice(state, "ANYTHING", userContext), "", `${state}`);
        }
    }
    assert.strictEqual(odataDestinations.notice(LIST, "", true), "", "an empty field is the required-field rule's");
    assert.strictEqual(odataDestinations.notice(LIST, "   ", false), "");
});

QUnit.test("a typed name is 'not listed' only against a complete list", function (assert) {
    assert.strictEqual(odataDestinations.notice(LIST, "S4_NEW", true), "notListed");
    assert.strictEqual(odataDestinations.notice(LIST, "S4_NEW", false), "notListed");
    assert.strictEqual(odataDestinations.notice(list([BASIC], { truncated: true }), "S4_NEW", false), "");
    assert.strictEqual(
        odataDestinations.notice(
            list([BASIC], { warnings: [{ code: "level_unavailable", level: "subaccount" }] }), "S4_NEW", false
        ), ""
    );
    assert.strictEqual(
        odataDestinations.notice(list([BASIC], { truncated: true }), "S4_DEV_BASIC", true), "fixedAccount",
        "what an incomplete list does hold is still judged"
    );
    assert.strictEqual(odataDestinations.notice(list([], { skipped: 2 }), "S4_NEW", false), "notListed",
        "skipped entries have no name a service could use");
    assert.strictEqual(odataDestinations.notice(LIST, "S4_DEV_RFC", false), "notUsable",
        "listed, but not an HTTP destination");
    assert.strictEqual(odataDestinations.notice(LIST, "S4 DEV", false), "notListed",
        "a shown name that is invalid is not the stored one");
});

QUnit.test("each notice has a state and a text; none is an error", function (assert) {
    assert.deepEqual(
        (["", "fixedAccount", "needsUser", "notListed", "notUsable"] as const).map((notice) => [
            odataDestinations.noticeState(notice), odataDestinations.noticeText(notice, text)
        ]),
        [
            ["None", ""],
            ["Warning", "odataDestinationFixedAccount"],
            ["Warning", "odataDestinationNeedsUser"],
            ["Information", "odataDestinationNotListed"],
            ["Warning", "odataDestinationNotUsable"]
        ]
    );
});

QUnit.test("the hint under the field", function (assert) {
    assert.strictEqual(odataDestinations.hint("loading", text), "");
    assert.strictEqual(odataDestinations.hint("unavailable", text), "odataDestinationsUnavailable");
    assert.strictEqual(odataDestinations.hint(list([BASIC, USER]), text), "", "a complete list needs no words");
    assert.strictEqual(odataDestinations.hint(list([BASIC], { truncated: true }), text), "odataDestinationsIncomplete");
    assert.strictEqual(
        odataDestinations.hint(list([BASIC], { warnings: [{ code: "level_unavailable" }] }), text),
        "odataDestinationsIncomplete"
    );
    assert.strictEqual(odataDestinations.hint(list([BASIC, RFC]), text), "odataDestinationsUnusableOne");
    assert.strictEqual(odataDestinations.hint(LIST, text), "odataDestinationsUnusableMany(3)");
    assert.strictEqual(
        odataDestinations.hint(list([RFC, BAD_ONE], { truncated: true }), text),
        "odataDestinationsIncomplete odataDestinationsUnusableMany(2)"
    );
    assert.strictEqual(odataDestinations.hint(list([]), text), "odataDestinationsNone",
        "an empty list is said, or the empty dropdown looks broken");
});

import ResourceBundle from "sap/base/i18n/ResourceBundle";
import inaQueries, {
    MAX_NOT_FOR, MAX_PACKAGE, MAX_PURPOSE, MAX_QUERY, MAX_TITLE, describedText, emptyForm, formOf, identityChange,
    rowsOf, sameForm, toInput, toUpdate, usedByNames, validateInput
} from "com/agent/admin/model/inaQueries";
import type { InaQueryForm } from "com/agent/admin/model/inaQueries";
import type { InaQuery, InaQuerySummary } from "com/agent/admin/service/inaTypes";

function summary(name: string, extra: Partial<InaQuerySummary> = {}): InaQuerySummary {
    return {
        name, title: `Title of ${name}`, purpose: "Revenue per airline and month", not_for: "",
        destination: "S4_ODATA_USER", user_context: true, query: "ZFLIGHT_REV_Q001", package: "",
        enabled: true, described: false, metadata_fetched_at: null, updated_at: "2026-11-03T10:00:00+00:00",
        used_by: [], ...extra
    };
}

function form(extra: Partial<InaQueryForm> = {}): InaQueryForm {
    return {
        name: "flight-revenue", title: "Flight revenue", purpose: "Revenue per airline and month",
        not_for: "", destination: "S4_ODATA_USER", user_context: true, query: "ZFLIGHT_REV_Q001",
        package: "", enabled: true, ...extra
    };
}

/** The one refusal of `field` for a form with `value` in it. */
function refusalOf(field: keyof InaQueryForm, value: unknown): string | undefined {
    return validateInput({ ...form(), [field]: value } as unknown as InaQueryForm)[field];
}

QUnit.module("inaQueries: the list");

QUnit.test("a row per query, in the order of the answer, with the agents by name", function (assert) {
    const rows = rowsOf([
        summary("flight-revenue", {
            package: "ZFLIGHT", described: true, metadata_fetched_at: "2026-11-02T09:00:00+00:00",
            used_by: [
                { agent_id: 7, agent: "gmail-agent", enabled: true, expose_api: false, api_slug: null },
                { agent_id: 9, agent: "btp-agent", enabled: false, expose_api: true, api_slug: "btp" }
            ]
        }),
        summary("cost-centre-actuals", { user_context: false, enabled: false, destination: "S4_ODATA_TECH" })
    ]);
    assert.deepEqual(rows, [
        {
            name: "flight-revenue", title: "Title of flight-revenue", query: "ZFLIGHT_REV_Q001", package: "ZFLIGHT",
            destination: "S4_ODATA_USER", userContext: true, usedBy: ["gmail-agent", "btp-agent"],
            described: true, describedAt: "2026-11-02T09:00:00+00:00", enabled: true
        },
        {
            name: "cost-centre-actuals", title: "Title of cost-centre-actuals", query: "ZFLIGHT_REV_Q001", package: "",
            destination: "S4_ODATA_TECH", userContext: false, usedBy: [],
            described: false, describedAt: null, enabled: false
        }
    ]);
    assert.deepEqual(rowsOf(undefined), [], "no answer is no rows");
    assert.deepEqual(rowsOf(null), []);
});

QUnit.test("the flags are strict: only the boolean true counts", function (assert) {
    const loose = summary("x", { user_context: "true", enabled: 1, described: "yes" } as unknown as Partial<InaQuerySummary>);
    const row = rowsOf([loose])[0];
    assert.strictEqual(row.userContext, false, "a string is not the signed-in user");
    assert.strictEqual(row.enabled, false);
    assert.strictEqual(row.described, false);
});

QUnit.test("without a described key, the stamp of the last Describe says it", function (assert) {
    const { described, ...older } = summary("x", { metadata_fetched_at: "2026-11-02T09:00:00+00:00" });
    void described;
    assert.strictEqual(rowsOf([older as InaQuerySummary])[0].described, true, "definition and stamp are a pair");
    const { described: unused, ...never } = summary("y");
    void unused;
    assert.strictEqual(rowsOf([never as InaQuerySummary])[0].described, false);
});

QUnit.test("the agents of used_by are read as objects or as names, each once", function (assert) {
    assert.deepEqual(usedByNames([
        { agent_id: 1, agent: "a", enabled: true, expose_api: false, api_slug: null }, "b", "a",
        { agent: 42 } as never, null as never
    ]), ["a", "b"], "a repeat and what names no agent are left out");
    assert.deepEqual(usedByNames(undefined), []);
});

QUnit.module("inaQueries: what was described");

QUnit.test("the counts of a definition, or that there is none yet", function (assert) {
    assert.deepEqual(describedText(null), { key: "inaNotDescribed" });
    assert.deepEqual(describedText(undefined), { key: "inaNotDescribed" });
    assert.deepEqual(describedText({
        dimensions: [{ name: "0CALMONTH", label: "Calendar month" }, { name: "ZCARRID", label: "Airline" }],
        measures: [{ name: "ZREVENUE", label: "Revenue", unit: "EUR" }],
        variables: []
    }), { key: "inaDescribedCounts", args: ["2", "1", "0"] });
    assert.deepEqual(describedText({ dimensions: [] } as never), { key: "inaDescribedCounts", args: ["0", "0", "0"] },
        "a list that is missing counts as empty");
});

QUnit.module("inaQueries: the form");

QUnit.test("a complete form is valid", function (assert) {
    assert.deepEqual(validateInput(form()), {});
    assert.deepEqual(validateInput(form({ not_for: "Plan values", package: "ZFLIGHT", user_context: false })), {});
});

QUnit.test("the name is the slug agents use", function (assert) {
    assert.strictEqual(refusalOf("name", ""), "inaErrNameRequired");
    ["Flight", "flight revenue", "-flight", "flight-", "a".repeat(65), " flight"].forEach((name) => {
        assert.strictEqual(refusalOf("name", name), "inaErrNameInvalid", JSON.stringify(name));
    });
    ["a", "0", "a-b", "a".repeat(64)].forEach((name) => {
        assert.strictEqual(refusalOf("name", name), undefined, JSON.stringify(name));
    });
});

QUnit.test("title and purpose are stripped first, then counted, then held to one line", function (assert) {
    assert.strictEqual(refusalOf("title", "   "), "inaErrTitleRequired", "only white space is no title");
    assert.strictEqual(refusalOf("title", "\u00a0\u3000"), "inaErrTitleRequired", "Unicode white space too");
    assert.strictEqual(refusalOf("title", `  ${"t".repeat(MAX_TITLE)}  `), undefined, "the space around is dropped");
    assert.strictEqual(refusalOf("title", "t".repeat(MAX_TITLE + 1)), "inaErrTitleTooLong");
    assert.strictEqual(refusalOf("title", "\u{1F600}".repeat(MAX_TITLE)), undefined,
        "characters are counted as the server counts them, not as UTF-16 units");
    assert.strictEqual(refusalOf("title", "Flight\trevenue"), "inaErrTitleOneLine");
    assert.strictEqual(refusalOf("title", "Flight\u2028revenue"), "inaErrTitleOneLine");
    assert.strictEqual(refusalOf("title", "Flight\u200brevenue"), undefined, "a format character is the admin's own text");
    assert.strictEqual(refusalOf("purpose", ""), "inaErrPurposeRequired");
    assert.strictEqual(refusalOf("purpose", "p".repeat(MAX_PURPOSE + 1)), "inaErrPurposeTooLong");
    assert.strictEqual(refusalOf("purpose", "line\none"), "inaErrPurposeOneLine");
});

QUnit.test("not for and the package may be blank; their length and line are checked as typed", function (assert) {
    assert.strictEqual(refusalOf("not_for", ""), undefined);
    assert.strictEqual(refusalOf("not_for", "n".repeat(MAX_NOT_FOR + 1)), "inaErrNotForTooLong");
    assert.strictEqual(refusalOf("not_for", `${"n".repeat(MAX_NOT_FOR)} `), "inaErrNotForTooLong",
        "not stripped: the space counts");
    assert.strictEqual(refusalOf("not_for", "a\rb"), "inaErrNotForOneLine");
    assert.strictEqual(refusalOf("package", ""), undefined);
    assert.strictEqual(refusalOf("package", "p".repeat(MAX_PACKAGE + 1)), "inaErrPackageTooLong");
    assert.strictEqual(refusalOf("package", "Z\u0085FLIGHT"), "inaErrPackageOneLine", "a C1 control character");
});

QUnit.test("the query's InA name is required, and no format character may hide in it", function (assert) {
    assert.strictEqual(refusalOf("query", ""), "inaErrQueryRequired");
    assert.strictEqual(refusalOf("query", "q".repeat(MAX_QUERY + 1)), "inaErrQueryTooLong");
    assert.strictEqual(refusalOf("query", "q".repeat(MAX_QUERY)), undefined);
    ["ZFLIGHT\u200bREV", "ZFLIGHT\u202eREV", "\ufeffZFLIGHT", "Z\nFLIGHT", "Z\u2029FLIGHT"].forEach((name) => {
        assert.strictEqual(refusalOf("query", name), "inaErrQueryOneLine", JSON.stringify(name));
    });
    ["0CALMONTH", "[ZREGION].[KEY]", "!V0001", "ZQ 001"].forEach((name) => {
        assert.strictEqual(refusalOf("query", name), undefined, `InA names are not EDM names: ${name}`);
    });
});

QUnit.test("the destination name and the two flags", function (assert) {
    assert.strictEqual(refusalOf("destination", ""), "inaErrDestinationRequired");
    assert.strictEqual(refusalOf("destination", "S4 DEV"), "inaErrDestinationInvalid");
    assert.strictEqual(refusalOf("destination", "S4_DEV.user-1"), undefined);
    assert.strictEqual(refusalOf("user_context", "true"), "inaErrBoolean", "strict, as the server's StrictBool");
    assert.strictEqual(refusalOf("enabled", 1), "inaErrBoolean");
    assert.strictEqual(refusalOf("user_context", undefined), undefined, "left out, the server's default applies");
});

QUnit.test("every problem of a form is named, one per field", function (assert) {
    const errors = validateInput({
        name: "", title: "", purpose: "", not_for: "", destination: "", user_context: false,
        query: "", package: "", enabled: true
    });
    assert.deepEqual(Object.keys(errors).sort(), ["destination", "name", "purpose", "query", "title"]);
});

QUnit.test("an empty form has the server's defaults", function (assert) {
    assert.deepEqual(emptyForm(), {
        name: "", title: "", purpose: "", not_for: "", destination: "", user_context: false,
        query: "", package: "", enabled: true
    }, "technical user and enabled, as InaQueryPayload");
});

QUnit.module("inaQueries: what is sent");

QUnit.test("a create sends exactly the payload fields, as typed", function (assert) {
    const typed = form({ title: "  Flight revenue ", query: " ZFLIGHT_REV_Q001" });
    const extra = { ...typed, describedAt: "x", busy: true } as InaQueryForm;
    assert.deepEqual(toInput(extra), {
        name: "flight-revenue", title: "  Flight revenue ", purpose: "Revenue per airline and month", not_for: "",
        destination: "S4_ODATA_USER", user_context: true, query: " ZFLIGHT_REV_Q001", package: "", enabled: true
    }, "no state of the page and no repair: the server strips what it strips");
    assert.notOk("definition" in toInput(typed), "no definition: a new query is described afterwards");
});

QUnit.test("an update carries the stamp it was loaded with and never the definition", function (assert) {
    const body = toUpdate(form(), "2026-11-03T10:00:00+00:00");
    assert.strictEqual(body.expected_updated_at, "2026-11-03T10:00:00+00:00");
    assert.notOk("definition" in body, "left out, the server keeps what Describe stored");
    assert.notOk("metadata_fetched_at" in body, "and its stamp, which is its pair");
    assert.deepEqual(Object.keys(body).sort(), [
        "destination", "enabled", "expected_updated_at", "name", "not_for", "package", "purpose", "query", "title",
        "user_context"
    ]);
    assert.strictEqual(toUpdate(form(), null).expected_updated_at, null, "null: not checked");
});

QUnit.test("a stored query becomes the form it is edited in", function (assert) {
    const stored: InaQuery = {
        ...summary("flight-revenue", { not_for: "Plan values", package: "ZFLIGHT", described: true }),
        definition: { dimensions: [], measures: [], variables: [] }, reloaded: false
    };
    assert.deepEqual(formOf(stored), {
        name: "flight-revenue", title: "Title of flight-revenue", purpose: "Revenue per airline and month",
        not_for: "Plan values", destination: "S4_ODATA_USER", user_context: true, query: "ZFLIGHT_REV_Q001",
        package: "ZFLIGHT", enabled: true
    });
    assert.deepEqual(toUpdate(formOf(stored), stored.updated_at), { ...toInput(formOf(stored)),
        expected_updated_at: "2026-11-03T10:00:00+00:00" }, "loaded and saved unchanged, nothing else is sent");
    assert.strictEqual(formOf({ ...stored, not_for: null, package: undefined } as unknown as InaQuery).not_for, "",
        "a missing text is blank");
});

QUnit.test("the default export carries the functions", function (assert) {
    assert.strictEqual(inaQueries.rowsOf, rowsOf);
    assert.strictEqual(inaQueries.validateInput, validateInput);
    assert.strictEqual(inaQueries.toUpdate, toUpdate);
    assert.strictEqual(inaQueries.describedText, describedText);
});

QUnit.test("every text key the model answers exists", async function (assert) {
    const bundle = await ResourceBundle.create({
        url: sap.ui.require.toUrl("com/agent/admin/i18n/i18n.properties"), async: true
    }) as ResourceBundle;
    const keys = new Set<string>();
    const add = (errors: Record<string, string | undefined>) => Object.values(errors).forEach((key) => key && keys.add(key));
    add(validateInput({
        name: "", title: "", purpose: "", not_for: "", destination: "", user_context: false, query: "", package: "",
        enabled: true
    }));
    add(validateInput({
        name: "A", title: "t".repeat(200), purpose: "p".repeat(300), not_for: "n".repeat(300), destination: "a b",
        user_context: "x", query: "q".repeat(200), package: "p".repeat(100), enabled: "x"
    } as unknown as InaQueryForm));
    add(validateInput({
        ...form(), title: "a\nb", purpose: "a\nb", not_for: "a\nb", query: "a\u200bb", package: "a\nb"
    }));
    keys.add(describedText(null).key);
    keys.add(describedText({ dimensions: [], measures: [], variables: [] }).key);
    assert.strictEqual(keys.size, 20, "the 18 refusals and the two texts of a definition were produced");
    keys.forEach((key) => assert.ok(bundle.hasText(key), key));
    const counts = bundle.getText("inaDescribedCounts", ["12", "4", "2"]) || "";
    ["12", "4", "2"].forEach((count) => assert.ok(counts.indexOf(count) > -1, `the counts name ${count}`));
});

QUnit.test("identityChange names what decides whom and what the agents call", function (assert) {
    assert.deepEqual(identityChange(form(), form()), {}, "nothing changed");
    assert.deepEqual(identityChange(form(), form({ title: "Other", purpose: "Other", package: "ZOTHER", enabled: false })), {},
        "texts, the package and the switch are no identity");
    assert.deepEqual(identityChange(form(), form({ user_context: false })), { runsAs: "technical" }, "to the technical user");
    assert.deepEqual(identityChange(form({ user_context: false }), form()), { runsAs: "user" }, "to the signed-in user");
    assert.deepEqual(identityChange(form(), form({ destination: "S4_ODATA_TECH", query: "ZOTHER_Q" })), {
        destination: { from: "S4_ODATA_USER", to: "S4_ODATA_TECH" },
        query: { from: "ZFLIGHT_REV_Q001", to: "ZOTHER_Q" }
    }, "the destination and the InA name, from and to, as typed");
    assert.deepEqual(identityChange(form(), form({ destination: "s4_odata_user" })),
        { destination: { from: "S4_ODATA_USER", to: "s4_odata_user" } }, "names are case-sensitive");
});

QUnit.test("sameForm compares every field of the form", function (assert) {
    assert.ok(sameForm(form(), form()), "equal");
    (Object.keys(form()) as (keyof InaQueryForm)[]).forEach((field) => {
        const other = { ...form(), [field]: typeof form()[field] === "boolean" ? !form()[field] : `${String(form()[field])}x` };
        assert.notOk(sameForm(form(), other as InaQueryForm), `${field} differs`);
    });
});

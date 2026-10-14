import {
    CEILING_MAX_FILES, CEILING_MAX_FILE_MB, CEILING_MAX_PDF_PAGES, INVOKE_MEDIA_TYPES,
    fromForm, mediaTypeOptions, toForm
} from "com/agent/admin/model/invokeSettings";
import type { InvokeForm } from "com/agent/admin/model/invokeSettings";

const MB = 1024 * 1024;

function blank(): InvokeForm {
    return { schemaText: "", mediaTypes: [], maxFileMb: "", maxFiles: "", maxPdfPages: "" };
}

QUnit.module("invokeSettings: loading");

QUnit.test("null schema and limits are a blank form", function (assert) {
    assert.deepEqual(toForm(null, null), blank());
    assert.deepEqual(toForm(undefined, undefined), blank(), "an older server sends neither");
});

QUnit.test("a stored schema is shown as indented JSON, the limits as text in MB", function (assert) {
    const form = toForm(
        { type: "object", required: ["a"] },
        { media_types: ["application/pdf"], max_file_bytes: 2 * MB, max_files: 3, max_pdf_pages: 40 });
    assert.deepEqual(JSON.parse(form.schemaText), { type: "object", required: ["a"] });
    assert.ok(form.schemaText.indexOf("\n") !== -1, "indented");
    assert.deepEqual(form.mediaTypes, ["application/pdf"]);
    assert.strictEqual(form.maxFileMb, "2");
    assert.strictEqual(form.maxFiles, "3");
    assert.strictEqual(form.maxPdfPages, "40");
});

QUnit.test("an untouched form sends back exactly what it loaded", function (assert) {
    const schema = { type: "object", properties: { x: { type: "string", maxLength: 3 } } };
    const limits = { max_file_bytes: 1000003, max_files: 2 };
    const parsed = fromForm(toForm(schema, limits));
    assert.deepEqual(parsed.errors, {});
    assert.deepEqual(parsed.result_schema, schema);
    assert.deepEqual(parsed.invoke_limits, limits, "an odd byte count survives the MB round trip");
});

QUnit.module("invokeSettings: what Save sends");

QUnit.test("a blank form sends null for both", function (assert) {
    const parsed = fromForm({ ...blank(), schemaText: "   \n " });
    assert.deepEqual(parsed, { result_schema: null, invoke_limits: null, errors: {} });
});

QUnit.test("filled limits are an object with the size in bytes; blank keys are left out", function (assert) {
    const parsed = fromForm({ ...blank(), maxFileMb: "2.5", maxPdfPages: " 7 " });
    assert.deepEqual(parsed.errors, {});
    assert.deepEqual(parsed.invoke_limits, { max_file_bytes: Math.round(2.5 * MB), max_pdf_pages: 7 });
});

QUnit.test("media types are sent in the options' order, once each", function (assert) {
    const parsed = fromForm({ ...blank(), mediaTypes: ["application/pdf", "image/png", "application/pdf"] });
    assert.deepEqual(parsed.invoke_limits, { media_types: ["image/png", "application/pdf"] });
});

QUnit.test("the options are the three known types plus any stored other", function (assert) {
    assert.deepEqual(mediaTypeOptions([]), INVOKE_MEDIA_TYPES);
    assert.deepEqual(mediaTypeOptions(["image/gif", "image/png"]), INVOKE_MEDIA_TYPES.concat(["image/gif"]));
});

QUnit.module("invokeSettings: refusals");

QUnit.test("schema text that is not JSON is refused with a fixed key, never the text", function (assert) {
    const parsed = fromForm({ ...blank(), schemaText: "{ total: SECRETVALUE }" });
    assert.deepEqual(parsed.errors, { result_schema: { key: "invokeSchemaInvalidJson" } });
    assert.strictEqual(JSON.stringify(parsed).indexOf("SECRETVALUE"), -1, "the text is not echoed");
});

QUnit.test("JSON that is not one object is refused", function (assert) {
    ["[]", "\"x\"", "null", "3"].forEach((text) => {
        assert.deepEqual(fromForm({ ...blank(), schemaText: text }).errors,
            { result_schema: { key: "invokeSchemaNotObject" } }, text);
    });
});

QUnit.test("numbers outside the server's ceilings or not numbers are refused, naming the ceiling", function (assert) {
    const parsed = fromForm({ ...blank(), maxFileMb: "26", maxFiles: "0", maxPdfPages: "abc" });
    assert.deepEqual(parsed.errors, {
        max_file_mb: { key: "invokeMaxFileMbInvalid", args: [String(CEILING_MAX_FILE_MB)] },
        max_files: { key: "invokeMaxFilesInvalid", args: [String(CEILING_MAX_FILES)] },
        max_pdf_pages: { key: "invokeMaxPdfPagesInvalid", args: [String(CEILING_MAX_PDF_PAGES)] }
    });
    assert.ok(fromForm({ ...blank(), maxFiles: "1.5" }).errors.max_files, "a fraction is no file count");
    assert.ok(fromForm({ ...blank(), maxFileMb: "-1" }).errors.max_file_mb, "a negative size");
    assert.deepEqual(fromForm({ ...blank(), maxFileMb: "25", maxFiles: "10", maxPdfPages: "100" }).errors, {},
        "the ceilings themselves are allowed");
});

QUnit.module("invokeSettings: the size in MB");

QUnit.test("10485760 bytes are shown as 10 and sent back unchanged", function (assert) {
    const form = toForm(null, { max_file_bytes: 10485760 });
    assert.strictEqual(form.maxFileMb, "10");
    assert.deepEqual(fromForm(form).invoke_limits, { max_file_bytes: 10485760 });
});

QUnit.test("1 byte is shown without exponent notation and an untouched form sends 1", function (assert) {
    const form = toForm(null, { max_file_bytes: 1 });
    assert.strictEqual(form.maxFileMb, "0.000001");
    assert.strictEqual(form.maxFileMb.indexOf("e"), -1, "no exponent");
    const parsed = fromForm(form);
    assert.deepEqual(parsed.errors, {}, "not refused");
    assert.deepEqual(parsed.invoke_limits, { max_file_bytes: 1 });
});

QUnit.test("an edited size is converted from the text, not the stored bytes", function (assert) {
    const form = { ...toForm(null, { max_file_bytes: 1000003 }), maxFileMb: "3" };
    assert.deepEqual(fromForm(form).invoke_limits, { max_file_bytes: 3 * MB });
});

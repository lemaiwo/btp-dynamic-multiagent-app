import odataCatalog from "./odataCatalog";
import type {
    InaDefinition, InaQuery, InaQueryInput, InaQuerySummary, InaQueryUpdate, InaUsedBy
} from "../service/inaTypes";

/**
 * The logic of the "Analytical queries" pages (InA queries,
 * `/admin/api/ina/...`), without a view: the list rows, what `Describe`
 * stored, and the create and edit form as the bodies the server takes.
 *
 * `validateInput` mirrors `InaQueryPayload` in agents/ina/models.py rule for
 * rule and adds none: the server stays the gate, and its 422 names the
 * field. Refusals are i18n keys of fixed texts, never a text that quotes
 * what was typed.
 */

/** `QUERY_NAME_RE` in agents/ina/models.py: the slug agents use. */
const QUERY_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$/;
/** `DESTINATION_NAME_RE` of agents/odata/models.py, which the InA gate imports. */
const DESTINATION_NAME_RE = /^[A-Za-z0-9_.-]{1,200}$/;

/** The server's limits, in characters (code points, as Python counts). */
export const MAX_TITLE = 120;
export const MAX_PURPOSE = 200;
export const MAX_NOT_FOR = 200;
export const MAX_QUERY = 128;
export const MAX_PACKAGE = 64;

// `one_line` (the record's own texts: control characters and the line and
// paragraph separators) and `one_line_strict` (the query's InA name: the
// invisible format characters as well) of agents/loc_fields.py, by the
// same Unicode categories.
const NOT_ONE_LINE_RE = /[\p{Cc}\p{Zl}\p{Zp}]/u;
const NOT_ONE_LINE_STRICT_RE = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u;

/** What the create dialog and the detail page edit: the payload without
 *  the description, which only `Describe` writes. */
export interface InaQueryForm {
    name: string;
    title: string;
    purpose: string;
    not_for: string;
    destination: string;
    /** On: calls run as the signed-in user; off: as the destination's technical user. */
    user_context: boolean;
    query: string;
    package: string;
    enabled: boolean;
}

export type InaField = keyof InaQueryForm;

/** Field -> i18n key of what is wrong with it. Empty means valid. */
export type InaErrors = Partial<Record<InaField, string>>;

/** A text of the page as an i18n key and its arguments. */
export interface InaText {
    key: string;
    args?: string[];
}

/** One row of the list. */
export interface InaQueryRow {
    name: string;
    title: string;
    query: string;
    package: string;
    destination: string;
    userContext: boolean;
    /** The agents that name the query, by name. */
    usedBy: string[];
    described: boolean;
    /** When `Describe` last read SAP; null: not yet. */
    describedAt: string | null;
    enabled: boolean;
}

/** The texts of the shared destination field (`DestinationField`) that
 *  name OData, in the words of a query: OData key -> InA key. The rest of
 *  that field's texts hold for both. */
export const DESTINATION_TEXTS: Readonly<Record<string, string>> = {
    odataDestinationFixedAccount: "inaDestinationFixedAccount",
    odataDestinationNeedsUser: "inaDestinationNeedsUser",
    odataDestinationNotListed: "inaDestinationNotListed",
    odataDestinationNotUsable: "inaDestinationNotUsable",
    odataDestinationsUnusableOne: "inaDestinationsUnusableOne",
    odataDestinationsUnusableMany: "inaDestinationsUnusableMany"
};

/** Characters as the server counts them: code points, not UTF-16 units. */
function length(value: string): number {
    return Array.from(value).length;
}

function textOf(value: unknown): string {
    return typeof value === "string" ? value : "";
}

/** The agent names of `used_by`, each once: objects of the record's
 *  `to_dict` or plain names; anything else names no agent. */
export function usedByNames(usedBy: readonly (InaUsedBy | string)[] | null | undefined): string[] {
    const names: string[] = [];
    (usedBy ?? []).forEach((entry) => {
        const name = typeof entry === "string" ? entry : entry && typeof entry.agent === "string" ? entry.agent : "";
        if (name && names.indexOf(name) === -1) {
            names.push(name);
        }
    });
    return names;
}

/** The list rows, in the order of the answer. The flags count only as the
 *  boolean `true`; without a `described` key, the stamp of the last
 *  `Describe` says it (the definition and its stamp are a pair). */
export function rowsOf(summaries: readonly InaQuerySummary[] | null | undefined): InaQueryRow[] {
    return (summaries ?? []).map((summary) => {
        const describedAt = typeof summary.metadata_fetched_at === "string" ? summary.metadata_fetched_at : null;
        return {
            name: summary.name,
            title: textOf(summary.title),
            query: textOf(summary.query),
            package: textOf(summary.package),
            destination: textOf(summary.destination),
            userContext: summary.user_context === true,
            usedBy: usedByNames(summary.used_by),
            described: "described" in summary ? summary.described === true : describedAt !== null,
            describedAt,
            enabled: summary.enabled === true
        };
    });
}

/** "Dimensions: n · Measures: n · Variables: n", or that `Describe` has not run. */
export function describedText(definition: InaDefinition | null | undefined): InaText {
    if (!definition) {
        return { key: "inaNotDescribed" };
    }
    const count = (list: unknown[] | undefined) => String(Array.isArray(list) ? list.length : 0);
    return {
        key: "inaDescribedCounts",
        args: [count(definition.dimensions), count(definition.measures), count(definition.variables)]
    };
}

/** A new query's form, with the server's defaults: the technical user, enabled. */
export function emptyForm(): InaQueryForm {
    return {
        name: "", title: "", purpose: "", not_for: "", destination: "", user_context: false,
        query: "", package: "", enabled: true
    };
}

/** A stored query as the form it is edited in. */
export function formOf(query: InaQuery | InaQuerySummary): InaQueryForm {
    return {
        name: query.name, title: textOf(query.title), purpose: textOf(query.purpose), not_for: textOf(query.not_for),
        destination: textOf(query.destination), user_context: query.user_context === true, query: textOf(query.query),
        package: textOf(query.package), enabled: query.enabled === true
    };
}

/** A required stripped text of at most `max` characters on one line. */
function strippedProblem(value: unknown, max: number, keys: [string, string, string]): string {
    const text = odataCatalog.serverStrip(textOf(value));
    if (!text) {
        return keys[0];
    }
    if (length(text) > max) {
        return keys[1];
    }
    return NOT_ONE_LINE_RE.test(text) ? keys[2] : "";
}

/** An optional text, not stripped, of at most `max` characters on one line. */
function plainProblem(value: unknown, max: number, keys: [string, string]): string {
    const text = textOf(value);
    if (length(text) > max) {
        return keys[0];
    }
    return NOT_ONE_LINE_RE.test(text) ? keys[1] : "";
}

/**
 * What the server would refuse in `form`, field by field, before it is
 * sent: as `InaQueryPayload` checks it, in its order, the first problem of
 * each field. Title and purpose are stripped first, as the server does;
 * every other text is checked as typed.
 */
export function validateInput(form: InaQueryForm): InaErrors {
    const errors: InaErrors = {};
    const set = (field: InaField, key: string): void => {
        if (key) {
            errors[field] = key;
        }
    };
    const name = textOf(form.name);
    set("name", !name ? "inaErrNameRequired" : QUERY_NAME_RE.test(name) ? "" : "inaErrNameInvalid");
    set("title", strippedProblem(form.title, MAX_TITLE,
        ["inaErrTitleRequired", "inaErrTitleTooLong", "inaErrTitleOneLine"]));
    set("purpose", strippedProblem(form.purpose, MAX_PURPOSE,
        ["inaErrPurposeRequired", "inaErrPurposeTooLong", "inaErrPurposeOneLine"]));
    set("not_for", plainProblem(form.not_for, MAX_NOT_FOR, ["inaErrNotForTooLong", "inaErrNotForOneLine"]));
    const destination = textOf(form.destination);
    set("destination", !destination ? "inaErrDestinationRequired"
        : DESTINATION_NAME_RE.test(destination) ? "" : "inaErrDestinationInvalid");
    // Strict, like the server's StrictBool: "true" or 1 is not a flag. This
    // one decides whose identity reaches SAP. Left out, the server takes its
    // default (technical user, enabled), so that is no error.
    (["user_context", "enabled"] as const).forEach((flag) => {
        if (form[flag] !== undefined && typeof form[flag] !== "boolean") {
            errors[flag] = "inaErrBoolean";
        }
    });
    const query = textOf(form.query);
    set("query", !query ? "inaErrQueryRequired" : length(query) > MAX_QUERY ? "inaErrQueryTooLong"
        : NOT_ONE_LINE_STRICT_RE.test(query) ? "inaErrQueryOneLine" : "");
    set("package", plainProblem(form.package, MAX_PACKAGE, ["inaErrPackageTooLong", "inaErrPackageOneLine"]));
    return errors;
}

/** The body of a create: exactly the payload fields, as typed (the server
 *  strips what it strips), and no description: a new query is described
 *  afterwards. */
export function toInput(form: InaQueryForm): InaQueryInput {
    return {
        name: form.name, title: form.title, purpose: form.purpose, not_for: form.not_for,
        destination: form.destination, user_context: form.user_context, query: form.query,
        package: form.package, enabled: form.enabled
    };
}

/** The body of a save: the payload and the `updated_at` the page loaded
 *  (null: not checked). Never the definition or its stamp: left out, the
 *  server keeps what `Describe` stored. */
export function toUpdate(form: InaQueryForm, expected_updated_at: string | null): InaQueryUpdate {
    return { ...toInput(form), expected_updated_at };
}

/** What a save changes about whom the agents act as and what they call:
 *  "Runs as", the destination and the InA name. Absent: unchanged. */
export interface InaIdentityChange {
    /** The identity after the save. */
    runsAs?: "user" | "technical";
    destination?: { from: string; to: string };
    query?: { from: string; to: string };
}

/** The identity change from the stored query `stored` to `form`. Names are
 *  compared as typed: destination names are case-sensitive. */
export function identityChange(stored: InaQueryForm, form: InaQueryForm): InaIdentityChange {
    const change: InaIdentityChange = {};
    if ((stored.user_context === true) !== (form.user_context === true)) {
        change.runsAs = form.user_context === true ? "user" : "technical";
    }
    if (stored.destination !== form.destination) {
        change.destination = { from: stored.destination, to: form.destination };
    }
    if (stored.query !== form.query) {
        change.query = { from: stored.query, to: form.query };
    }
    return change;
}

/** Whether two forms hold the same values, field by field. */
export function sameForm(a: InaQueryForm, b: InaQueryForm): boolean {
    return (Object.keys(emptyForm()) as InaField[]).every((field) => a[field] === b[field]);
}

export default {
    rowsOf, usedByNames, describedText, emptyForm, formOf, validateInput, toInput, toUpdate, identityChange, sameForm
};

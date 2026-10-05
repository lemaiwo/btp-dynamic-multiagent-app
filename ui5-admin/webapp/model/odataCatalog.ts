import type {
    ODataDefinition, ODataDuplicateRequest, ODataEntityOp, ODataEntitySet, ODataExampleQuery, ODataField,
    ODataNavigation, ODataOperation, ODataServiceInput, ODataUsedBy, ODataValueMeaning
} from "../service/types";

/**
 * Pure logic for the OData catalogue pages: what a service's definition adds
 * up to, the empty form, and the client-side mirror of the server's rules.
 *
 * No `sap.m` in here, so all of it is unit-testable. The server
 * (`agents/odata/models.py`) stays authoritative: whatever `validate` misses
 * is still refused there, and `serverErrors` turns that refusal into one
 * message per field.
 */

/** Entity-set operations in the server's order (`ENTITY_OPS`). */
export const ENTITY_OPS: readonly ODataEntityOp[] = ["list", "get", "create", "update", "delete"];

/** The entity-set operations an agent gets only with `allow_write` (`WRITE_OPS`). */
export const WRITE_OPS: readonly ODataEntityOp[] = ["create", "update", "delete"];

// Mirrors of SERVICE_NAME_RE and DESTINATION_NAME_RE in agents/odata/models.py.
const SERVICE_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$/;
const DESTINATION_NAME_RE = /^[A-Za-z0-9_.-]{1,200}$/;

// Mirror of EDM_NAME_RE in agents/odata/models.py.
const EDM_NAME_RE = /^[A-Za-z_][A-Za-z0-9_.]{0,127}$/;

/** The most entity sets a definition may hold (`MAX_ENTITY_SETS`). */
const MAX_ENTITY_SETS = 200;

/** The most fields an entity set may hold (`MAX_FIELDS`). */
const MAX_FIELDS = 500;
const MAX_LABEL = 120;
const MAX_HINT = 300;
const MAX_TYPE = 200;
const MAX_VALUE = 64;
const MAX_MEANING = 200;
const MAX_ENTITY_DESCRIPTION = 600;
const MAX_NAV_DESCRIPTION = 300;
const MAX_EXAMPLE_DESCRIPTION = 200;
const MAX_EXAMPLE_FILTER = 1000;
const MAX_EXAMPLE_SELECT = 128;
const MAX_EXAMPLE_ORDERBY = 300;

const MAX_TITLE = 120;
const MAX_PURPOSE = 200;
const MAX_NOT_FOR = 200;
const MAX_SERVICE_PATH = 512;

export interface ODataCounts {
    entitySets: number;
    operations: number;
}

export interface ODataFieldCount {
    selected: number;
    total: number;
}

/**
 * One row of the entity sets table: everything the table shows of an entity
 * set, worked out once when the entity set changes and not while the table
 * renders or the admin types. `index` is the entity set's position in the
 * definition, which a filtered table no longer shows by its row number.
 */
export interface ODataEntityRow {
    index: number;
    name: string;
    /** The business title, or the name when there is none. */
    title: string;
    /** The URL segment when it is not the name, else "". */
    path: string;
    description: string;
    described: boolean;
    list: boolean;
    get: boolean;
    create: boolean;
    update: boolean;
    delete: boolean;
    /** At least one operation is on: agents can find this entity set. */
    anyOperation: boolean;
    /** Fields an agent can read, and all fields. */
    selectable: number;
    total: number;
    /** It has navigations, is in use, and Get -- which following a
     *  navigation from here needs -- is off. */
    navigationHint: boolean;
    /** Why the last tick in this row was refused (a text), or "". */
    note: string;
    /** What a refused save said about this row (a text), or "". */
    error: string;
    /** Title and technical name, for the accessible name of the row's
     *  checkboxes: two entity sets can share a title, never a name. */
    label: string;
}

/** The write operations a save would newly open on one entity set. */
export interface ODataNewWrite {
    name: string;
    title: string;
    operations: ODataEntityOp[];
}

/** An operation (function import, action) a save would newly open as a write. */
export interface ODataNewOperation {
    name: string;
    /** The business title, or the name when there is none. */
    title: string;
}

/** The fields of one entity set a save would newly let agents write. */
export interface ODataNewFieldWrite {
    name: string;
    title: string;
    fields: string[];
}

/** Everything a save would newly let agents with `allow_write` do. */
export interface ODataPending {
    entitySets: ODataNewWrite[];
    operations: ODataNewOperation[];
    /** Fields that become writable for agents: marked Write on an entity
     *  set that has (or gets) Create or Update. */
    fields: ODataNewFieldWrite[];
}

/** Which fields the entity set dialog lists: all; those agents may read;
 *  those they may write; those with any tick; those with none; those
 *  marked as personal data. */
export type ODataFieldFilter = "all" | "read" | "write" | "ticked" | "unticked" | "personal";

/** Why an entity set cannot be removed: an i18n key and the operations
 *  (by title) it is about. */
export interface ODataRemovalBlocker {
    key: "odataRemoveEntityBound" | "odataRemoveEntityReturned";
    operations: string[];
}

/** One thing the server would refuse about an entity set: where (the
 *  server's `loc` inside the entity set, "" for the entity set as a whole),
 *  and an i18n key with its arguments. */
export interface ODataIssue {
    loc: string;
    key: string;
    args: string[];
}

/** Whether an agent could follow a navigation today, and why not. */
export interface ODataFollow {
    ok: boolean;
    key: string;
    args: string[];
}

/** What agents will not see of an example query, and why. */
export interface ODataExampleWarning {
    /** No agent sees the example at all. */
    dropped: boolean;
    key: string;
    args: string[];
}

/** A refused save, taken apart: see `serverRefusal`. */
export interface ODataRefusal {
    /** What the text says before its first `<loc>: `, or "". */
    lead: string;
    /** `loc` -> message. */
    byLoc: Record<string, string>;
    /** The server's "and n more" tail, or "". */
    more: string;
}

/** What is wrong with the entity set at `index`: an i18n key and its arguments. */
export interface ODataEntityProblem {
    index: number;
    key: string;
    args: string[];
}

/** The agents of a service, by whether their server entry allows writes. */
export interface ODataWriters {
    allowed: string[];
    others: string[];
}

/** The payload fields `validate` can report on. The first six have a text
 *  field in the form; the flags and the definition can only be wrong in a
 *  payload that did not come from the form (an imported file). */
export type ODataErrorField = "name" | "title" | "purpose" | "not_for" | "destination" | "service_path"
    | "user_context" | "enabled" | "definition";

/** What a save changes about whose identity reaches SAP, and through which
 *  destination. `null` twice: nothing of the kind. */
export interface ODataIdentityChange {
    /** The identity the service switches TO, or null when it stays. */
    runsAs: "user" | "technical" | null;
    destination: { from: string; to: string } | null;
}

/** The payload fields, in the order of `ODataServicePayload`. */
const PAYLOAD_FIELDS: readonly (keyof ODataServiceInput)[] = [
    "name", "title", "purpose", "not_for", "destination", "user_context", "odata_version",
    "service_path", "enabled", "definition", "metadata_fetched_at"
];

/** Field -> i18n key of what is wrong with it. Empty means valid. */
export type ODataErrors = Partial<Record<ODataErrorField, string>>;

/** C0 and C1 control characters and DEL, as the server counts them. */
function hasControlCharacter(value: string): boolean {
    for (let i = 0; i < value.length; i++) {
        const code = value.charCodeAt(i);
        if (code < 0x20 || (code >= 0x7f && code <= 0x9f)) {
            return true;
        }
    }
    return false;
}

// What the server strips from both ends of a title and a purpose: Unicode
// White_Space, as pydantic's `strip_whitespace` does. Not the same set as
// JS `trim()`, which leaves U+0085 (so the text would be refused for a
// control character the server drops) and takes U+FEFF (so a text of only
// that would be "missing" here and accepted there).
const SERVER_SPACE = "[\\t\\n\\v\\f\\r \\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const SERVER_STRIP_RE = new RegExp(`^${SERVER_SPACE}+|${SERVER_SPACE}+$`, "g");

/** `value` as the server stores a stripped text field. */
function serverStrip(value: string | undefined | null): string {
    return String(value ?? "").replace(SERVER_STRIP_RE, "");
}

/**
 * Whether `value` is one line as the server means it (`_one_line` in
 * agents/odata/models.py): no control character (Unicode category Cc, so no
 * tab, CR or LF) and no line or paragraph separator (Zl, Zp). These texts
 * are printed as one line each in an agent's instructions.
 */
function isOneLine(value: string): boolean {
    return !hasControlCharacter(value) && value.indexOf("\u2028") === -1 && value.indexOf("\u2029") === -1;
}

function nameProblem(name: string): string {
    if (!name) {
        return "odataErrNameRequired";
    }
    return SERVICE_NAME_RE.test(name) ? "" : "odataErrNameInvalid";
}

function destinationProblem(destination: string): string {
    if (!destination) {
        return "odataErrDestinationRequired";
    }
    return DESTINATION_NAME_RE.test(destination) ? "" : "odataErrDestinationInvalid";
}

/**
 * Whether `path` is a service path the server accepts. Mirrors
 * `confine_service_path` in agents/odata/urls.py rule for rule: at most 512
 * characters; no control character, whitespace (anywhere: the value is not
 * trimmed, the server does not trim it either), backslash, `://`, `?`, `#`
 * or `%`; a leading `/` and no trailing one; no empty segment (`//`), no
 * `..` anywhere and no `.` segment. The host always comes from the
 * destination, never from here, and `sap-client` belongs there as well.
 */
function isConfinedPath(path: string): boolean {
    return path.length > 0
        && path.length <= MAX_SERVICE_PATH
        && !hasControlCharacter(path)
        && !/\s/.test(path)
        && path.indexOf("\\") === -1
        && path.indexOf("://") === -1
        && path.charAt(0) === "/"
        && !/[?#%]/.test(path)
        && path.charAt(path.length - 1) !== "/"
        && path.indexOf("//") === -1
        && path.indexOf("..") === -1
        && path.split("/").indexOf(".") === -1;
}

// "<loc>: " at the start of one part of a refusal: a dotted path of field
// names and list positions, e.g. "definition.entity_sets.0.fields.1: ".
// A key the server does not repeat (it did not look like a field name) is
// "<unknown field>" there.
const SERVER_LOC_RE = /^((?:[A-Za-z_][A-Za-z0-9_]*|<unknown field>)(?:\.(?:[A-Za-z0-9_]+|<unknown field>))*): (.*)$/;
const VALUE_ERROR_PREFIX = "Value error, ";

function writeOpsOf(entitySet: ODataEntitySet): ODataEntityOp[] {
    const enabled = entitySet.operations ?? [];
    return WRITE_OPS.filter((op) => enabled.indexOf(op) !== -1);
}

/**
 * Whether calling `operation` is a write. THE rule, the server's
 * (`operation_changes_data` in agents/odata/search.py, `call_changes_data`
 * in agents/odata/client.py): a read is only what is marked
 * `changes_data: false` (exactly) AND is sent with GET. A missing flag does
 * not read as "only reads", and a POST is a write whatever the flag says.
 * The Write tag, the pending strip, the Save question and the duplicate
 * dialog all go through here, so they cannot disagree.
 */
function operationIsWrite(operation: Pick<ODataOperation, "changes_data" | "http_method">): boolean {
    return !(operation.changes_data === false && operation.http_method === "GET");
}

/** The operations of a definition an agent with `allow_write` can run as
 *  writes: enabled, and a write by `operationIsWrite`. */
function writeOperations(definition: ODataDefinition | undefined | null): ODataOperation[] {
    return (definition?.operations ?? []).filter((o) => o.enabled === true && operationIsWrite(o));
}

function operationTitle(operation: ODataOperation): string {
    return (operation.title ?? "").trim() || operation.name;
}

function newEntityWrites(
    stored: ODataDefinition | undefined | null, current: ODataDefinition | undefined | null, switchedOn: boolean
): ODataNewWrite[] {
    const before: Record<string, ODataEntityOp[]> = {};
    // A service that is switched on by this save had no write an agent
    // could run: every write of it is newly enabled, ticked now or not.
    (switchedOn ? [] : stored?.entity_sets ?? []).forEach((entitySet) => {
        // Two of a name cannot be saved; if they are there, either's writes count as stored.
        before[`=${entitySet.name}`] = (before[`=${entitySet.name}`] ?? []).concat(writeOpsOf(entitySet));
    });
    const added: ODataNewWrite[] = [];
    (current?.entity_sets ?? []).forEach((entitySet) => {
        const had = before[`=${entitySet.name}`] ?? [];
        const operations = writeOpsOf(entitySet).filter((op) => had.indexOf(op) === -1);
        if (operations.length) {
            added.push({ name: entitySet.name, title: titleOf(entitySet), operations });
        }
    });
    return added;
}

function newWriteOperations(
    stored: ODataDefinition | undefined | null, current: ODataDefinition | undefined | null, switchedOn: boolean
): ODataNewOperation[] {
    // What agents could already run as a write: by name, and nothing when
    // the stored service was switched off.
    const had = switchedOn ? [] : writeOperations(stored).map((o) => o.name);
    return writeOperations(current)
        .filter((o) => had.indexOf(o.name) === -1)
        .map((o) => ({ name: o.name, title: operationTitle(o) }));
}

function titleOf(entitySet: ODataEntitySet): string {
    return (entitySet.title ?? "").trim() || entitySet.name;
}

function firstDuplicate(names: string[]): string | undefined {
    return names.filter((name, i) => names.indexOf(name) !== i)[0];
}

/**
 * Why `op` cannot be on for `entitySet`, as an i18n key, or "". The three
 * per-operation rules of `EntitySetDef._consistent` (agents/odata/models.py),
 * in its order, and nothing more: Get, Update and Delete address one entity,
 * so they need a key; a read returns only selectable fields, so List and Get
 * need one; Create and Update send writable fields, so they need one.
 */
function operationRefusal(entitySet: ODataEntitySet, op: ODataEntityOp): string {
    const fields = entitySet.fields ?? [];
    if ((op === "get" || op === "update" || op === "delete") && !(entitySet.keys ?? []).length) {
        return "odataNeedsKey";
    }
    if ((op === "list" || op === "get") && !fields.some((f) => f.selectable === true)) {
        return "odataNeedsSelectable";
    }
    if ((op === "create" || op === "update") && !fields.some((f) => f.writable === true)) {
        return "odataNeedsWritable";
    }
    return "";
}

/** The first rule of the server the entity set breaks on its own (not
 *  counting its name being taken by another one), or undefined. */
function entitySetProblem(entitySet: ODataEntitySet): { key: string; args: string[] } | undefined {
    const fields = entitySet.fields ?? [];
    const keys = entitySet.keys ?? [];
    if (!EDM_NAME_RE.test(entitySet.name ?? "")) {
        return { key: "odataErrEntityName", args: [] };
    }
    // The server checks each field before the entity set as a whole.
    const hidden = fields.filter((f) => f.filterable === true && f.selectable !== true)[0];
    if (hidden) {
        return { key: "odataErrFilterNotSelectable", args: [hidden.name] };
    }
    const names = fields.map((f) => f.name);
    const field = firstDuplicate(names);
    if (field !== undefined) {
        return { key: "odataErrDuplicateField", args: [field] };
    }
    const navigation = firstDuplicate((entitySet.navigations ?? []).map((n) => n.name));
    if (navigation !== undefined) {
        return { key: "odataErrDuplicateNavigation", args: [navigation] };
    }
    const key = firstDuplicate(keys.map((k) => k.name));
    if (key !== undefined) {
        return { key: "odataErrDuplicateKey", args: [key] };
    }
    const stray = keys.filter((k) => names.indexOf(k.name) === -1)[0];
    if (stray) {
        return { key: "odataErrKeyNotField", args: [stray.name] };
    }
    for (const op of entitySet.operations ?? []) {
        const refusal = operationRefusal(entitySet, op);
        if (refusal) {
            return { key: refusal, args: [] };
        }
    }
    return undefined;
}

function entitySetRow(entitySet: ODataEntitySet, index: number): ODataEntityRow {
    const operations = entitySet.operations ?? [];
    const fields = entitySet.fields ?? [];
    const on = (op: ODataEntityOp): boolean => operations.indexOf(op) !== -1;
    const description = entitySet.description ?? "";
    return {
        index,
        name: entitySet.name,
        title: titleOf(entitySet),
        path: entitySet.path && entitySet.path !== entitySet.name ? entitySet.path : "",
        description,
        described: description.trim().length > 0,
        list: on("list"), get: on("get"), create: on("create"), update: on("update"), delete: on("delete"),
        anyOperation: ENTITY_OPS.some(on),
        selectable: fields.filter((f) => f.selectable === true).length,
        total: fields.length,
        navigationHint: (entitySet.navigations ?? []).length > 0 && ENTITY_OPS.some(on) && !on("get"),
        note: "",
        error: "",
        label: titleOf(entitySet) === entitySet.name ? entitySet.name : `${titleOf(entitySet)} ${entitySet.name}`
    };
}

function entityLabel(row: { name: string; title: string }): string {
    return row.title && row.title !== row.name ? `${row.title} (${row.name})` : row.name;
}

/**
 * The rows one part of a refusal is about, and its text for a row (with
 * where inside the entity set, when the location says). See `rowErrors`.
 */
function placement(loc: string, message: string, names: readonly string[]): { rows: number[]; text: string } {
    const match = ENTITY_LOC_RE.exec(loc);
    if (!match && loc !== "definition") {
        return { rows: [], text: message };
    }
    const named = NAMED_ENTITY_RE.exec(message)?.[1];
    const text = match?.[2] ? `${match[2]}: ${message}` : message;
    const index = match ? Number(match[1]) : -1;
    if (match && index < names.length && (named === undefined || names[index] === named)) {
        return { rows: [index], text };
    }
    const rows: number[] = [];
    if (named !== undefined) {
        names.forEach((name, i) => {
            if (name === named) {
                rows.push(i);
            }
        });
    }
    return { rows, text };
}

// "definition.entity_sets.<n>" and what follows it in a refusal's `loc`.
const ENTITY_LOC_RE = /^definition\.entity_sets\.(\d+)(?:\.(.+))?$/;
// The entity set a message of the server names (`who` in models.py).
const NAMED_ENTITY_RE = /entity set '([^']*)'/;

/** Whether agents can send fields of this entity set at all. */
function takesWrites(entitySet: ODataEntitySet): boolean {
    const operations = entitySet.operations ?? [];
    return operations.indexOf("create") !== -1 || operations.indexOf("update") !== -1;
}

/**
 * The fields that become writable for agents by saving `current` over
 * `stored`: marked Write, on an entity set that has Create or Update, and
 * not writable for agents before -- not marked, or marked on an entity set
 * that had neither Create nor Update (only now can an agent send it).
 * Matched by entity set and field name. Switching a disabled service on
 * does not list its fields again: its write operations are all named then,
 * and the fields were marked before.
 */
function newWritableFields(
    stored: ODataDefinition | undefined | null, current: ODataDefinition | undefined | null
): ODataNewFieldWrite[] {
    const before: Record<string, boolean> = {};
    (stored?.entity_sets ?? []).filter(takesWrites).forEach((entitySet) => {
        (entitySet.fields ?? []).filter((f) => f.writable === true).forEach((f) => {
            before[`${entitySet.name}\n${f.name}`] = true;
        });
    });
    const added: ODataNewFieldWrite[] = [];
    (current?.entity_sets ?? []).filter(takesWrites).forEach((entitySet) => {
        const fields = (entitySet.fields ?? [])
            .filter((f) => f.writable === true && !before[`${entitySet.name}\n${f.name}`])
            .map((f) => f.name);
        if (fields.length) {
            added.push({ name: entitySet.name, title: titleOf(entitySet), fields });
        }
    });
    return added;
}

/** "B = awaiting release; 05 = released" as value/meaning pairs. A part
 *  without "=" or with an empty side is left out: see `valueMeaningsProblem`. */
function parseValueMeanings(text: string | undefined | null): ODataValueMeaning[] {
    const pairs: ODataValueMeaning[] = [];
    String(text ?? "").split(";").forEach((part) => {
        const at = part.indexOf("=");
        const value = at === -1 ? "" : part.substring(0, at).trim();
        const meaning = at === -1 ? "" : part.substring(at + 1).trim();
        if (value && meaning) {
            pairs.push({ value, meaning });
        }
    });
    return pairs;
}

function formatValueMeanings(values: readonly ODataValueMeaning[] | undefined | null): string {
    return (values ?? []).map((pair) => `${pair.value} = ${pair.meaning}`).join("; ");
}

/**
 * Why `text` cannot be stored as value meanings, as an i18n key, or "":
 * every part between ";" must read "value = meaning", with a value of at
 * most 64 and a meaning of at most 200 characters (`ValueMeaning`).
 */
function valueMeaningsProblem(text: string | undefined | null): string {
    const parts = String(text ?? "").split(";").filter((part) => part.trim());
    if (parts.length !== parseValueMeanings(text).length) {
        return "odataErrMeaningsFormat";
    }
    return parseValueMeanings(text).some((pair) => (
        pair.value.length > MAX_VALUE || pair.meaning.length > MAX_MEANING || !isOneLine(pair.value) || !isOneLine(pair.meaning)
    )) ? "odataErrMeaningsLength" : "";
}

/** Whether the dialog lists `field` for the search text and the filter. */
function fieldMatches(
    field: Pick<ODataField, "name" | "label" | "selectable" | "filterable" | "writable" | "personal_data">,
    query: string | undefined | null, mode: ODataFieldFilter
): boolean {
    const ticked = field.selectable === true || field.filterable === true || field.writable === true;
    if ((mode === "ticked" && !ticked) || (mode === "unticked" && ticked)
        || (mode === "read" && field.selectable !== true) || (mode === "write" && field.writable !== true)
        || (mode === "personal" && field.personal_data !== true)) {
        return false;
    }
    const text = String(query ?? "").trim().toLowerCase();
    return !text || field.name.toLowerCase().indexOf(text) !== -1
        || (field.label ?? "").toLowerCase().indexOf(text) !== -1;
}

/**
 * Whether an agent could follow `navigation` from `entitySet` today
 * (agents/odata/tools.py): it needs Get on the entity set it starts from,
 * the target in the catalogue, and on the target List (a collection) or Get
 * (a single entity).
 */
function navigationFollow(
    entitySet: ODataEntitySet, navigation: ODataNavigation, definition: ODataDefinition | undefined | null
): ODataFollow {
    const target = navigation.target === entitySet.name
        ? entitySet : (definition?.entity_sets ?? []).filter((e) => e.name === navigation.target)[0];
    if (!target) {
        return { ok: false, key: "odataNavTargetMissing", args: [navigation.target] };
    }
    if ((entitySet.operations ?? []).indexOf("get") === -1) {
        return { ok: false, key: "odataNavNeedsGet", args: [] };
    }
    const needed: ODataEntityOp = navigation.collection === true ? "list" : "get";
    if ((target.operations ?? []).indexOf(needed) === -1) {
        return {
            ok: false, key: needed === "list" ? "odataNavTargetNeedsList" : "odataNavTargetNeedsGet",
            args: [entityLabel({ name: target.name, title: titleOf(target) })]
        };
    }
    return { ok: true, key: "odataNavFollowable", args: [] };
}

/**
 * What agents will not see of `example`, by the rule that hides it at run
 * time (`_examples_out` in agents/odata/search.py), or undefined when they
 * see all of it:
 *
 * - its description, filter or orderby mentions, as a word and whatever the
 *   case, a field that is neither readable nor a key: dropped for everyone;
 * - its select lists fields and none of them is readable: dropped;
 * - it mentions a field that is only writable: agents without "Allow
 *   writes" do not see it;
 * - its select lists a field that is not readable: that entry is left out.
 */
function exampleWarning(entitySet: ODataEntitySet, example: ODataExampleQuery): ODataExampleWarning | undefined {
    const fields = entitySet.fields ?? [];
    const keys = (entitySet.keys ?? []).map((k) => k.name.toLowerCase());
    const words: Record<string, boolean> = {};
    [example.description, example.filter, example.orderby].forEach((text) => {
        String(text ?? "").toLowerCase().split(/[^\p{L}\p{N}_]+/u).forEach((word) => {
            words[`=${word}`] = true;
        });
    });
    const mentioned = (candidates: ODataField[]): string[] => candidates
        .filter((f) => f.selectable !== true && keys.indexOf(f.name.toLowerCase()) === -1)
        .filter((f) => words[`=${f.name.toLowerCase()}`])
        .map((f) => f.name);
    const writeOnly = (f: ODataField): boolean => f.writable === true && takesWrites(entitySet);
    const hidden = mentioned(fields.filter((f) => !writeOnly(f)));
    if (hidden.length) {
        return { dropped: true, key: "odataExampleDropped", args: [hidden.join(", ")] };
    }
    const readable = fields.filter((f) => f.selectable === true).map((f) => f.name);
    const select = example.select ?? [];
    const unread = select.filter((name) => readable.indexOf(name) === -1);
    if (select.length && unread.length === select.length) {
        return { dropped: true, key: "odataExampleDroppedSelect", args: [unread.join(", ")] };
    }
    const partly = mentioned(fields.filter(writeOnly));
    if (partly.length) {
        return { dropped: false, key: "odataExampleWriteOnly", args: [partly.join(", ")] };
    }
    if (unread.length) {
        return { dropped: false, key: "odataExampleSelectTrimmed", args: [unread.join(", ")] };
    }
    return undefined;
}

/** The key fields an agent can address but not see: not readable while Get
 *  is on. Key NAMES are always shown to agents; key VALUES come back only
 *  for a key field with Read. */
function hiddenKeys(entitySet: ODataEntitySet): string[] {
    if ((entitySet.operations ?? []).indexOf("get") === -1) {
        return [];
    }
    const fields = entitySet.fields ?? [];
    return (entitySet.keys ?? []).map((k) => k.name).filter((name) => (
        !fields.some((f) => f.name === name && f.selectable === true)
    ));
}

/**
 * Everything the server would refuse about one entity set, in the order it
 * checks (`EntitySetDef` and what it holds, agents/odata/models.py): first
 * the values -- names, types, lengths, one-line texts, value meanings,
 * example queries -- and a filterable field that is not readable; then,
 * only when all of that is fine (the server does the same), the entity set
 * as a whole (`entitySetProblem`: duplicates, keys, what its operations
 * need). `otherNames`: the names of the other entity sets of the service.
 */
function entitySetIssues(entitySet: ODataEntitySet, otherNames: readonly string[] = []): ODataIssue[] {
    const issues: ODataIssue[] = [];
    const add = (loc: string, key: string, ...args: string[]): void => {
        issues.push({ loc, key, args });
    };
    const fields = entitySet.fields ?? [];
    if (!EDM_NAME_RE.test(entitySet.name ?? "")) {
        add("name", "odataErrEntityName");
    }
    const title = entitySet.title ?? "";
    if (title.length > MAX_TITLE) {
        add("title", "odataErrTitleTooLong");
    } else if (!isOneLine(title)) {
        add("title", "odataErrTitleOneLine");
    }
    if ((entitySet.description ?? "").length > MAX_ENTITY_DESCRIPTION) {
        add("description", "odataErrEntityDescriptionTooLong");
    }
    (entitySet.keys ?? []).forEach((key, i) => {
        if (!EDM_NAME_RE.test(key.name ?? "")) {
            add(`keys.${i}.name`, "odataErrKeyName", String(key.name));
        }
        if (!key.type || key.type.length > MAX_TYPE) {
            add(`keys.${i}.type`, "odataErrFieldType", String(key.name));
        }
    });
    if (fields.length > MAX_FIELDS) {
        add("fields", "odataErrTooManyFields", String(MAX_FIELDS));
    }
    fields.forEach((field, i) => {
        const loc = `fields.${i}`;
        const before = issues.length;
        if (!EDM_NAME_RE.test(field.name ?? "")) {
            add(`${loc}.name`, "odataErrFieldName", String(field.name));
        }
        if (!field.type || field.type.length > MAX_TYPE) {
            add(`${loc}.type`, "odataErrFieldType", field.name);
        }
        const label = field.label ?? "";
        if (label.length > MAX_LABEL) {
            add(`${loc}.label`, "odataErrFieldLabelTooLong", field.name);
        } else if (!isOneLine(label)) {
            add(`${loc}.label`, "odataErrFieldLabelOneLine", field.name);
        }
        if ((field.hint ?? "").length > MAX_HINT) {
            add(`${loc}.hint`, "odataErrFieldHintTooLong", field.name);
        }
        (field.values ?? []).forEach((pair, v) => {
            const bad = (text: string, max: number): boolean => !text || text.length > max || !isOneLine(text);
            if (bad(pair.value ?? "", MAX_VALUE)) {
                add(`${loc}.values.${v}.value`, "odataErrFieldValues", field.name);
            }
            if (bad(pair.meaning ?? "", MAX_MEANING)) {
                add(`${loc}.values.${v}.meaning`, "odataErrFieldValues", field.name);
            }
        });
        // Like the server: the field as a whole only when its values passed.
        if (issues.length === before && field.filterable === true && field.selectable !== true) {
            add(loc, "odataErrFilterNotSelectable", field.name);
        }
    });
    (entitySet.navigations ?? []).forEach((navigation, i) => {
        if (!EDM_NAME_RE.test(navigation.name ?? "")) {
            add(`navigations.${i}.name`, "odataErrNavigationName", String(navigation.name));
        }
        if (!EDM_NAME_RE.test(navigation.target ?? "")) {
            add(`navigations.${i}.target`, "odataErrNavigationName", String(navigation.name));
        }
        if ((navigation.description ?? "").length > MAX_NAV_DESCRIPTION) {
            add(`navigations.${i}.description`, "odataErrNavigationDescription", navigation.name);
        }
    });
    (entitySet.examples ?? []).forEach((example, i) => {
        const loc = `examples.${i}`;
        const number = String(i + 1);
        const description = example.description ?? "";
        if (!description) {
            add(`${loc}.description`, "odataErrExampleDescriptionRequired", number);
        } else if (description.length > MAX_EXAMPLE_DESCRIPTION) {
            add(`${loc}.description`, "odataErrExampleDescriptionTooLong", number);
        }
        if ((example.filter ?? "").length > MAX_EXAMPLE_FILTER) {
            add(`${loc}.filter`, "odataErrExampleFilterTooLong", number);
        }
        (example.select ?? []).forEach((name, n) => {
            if (String(name).length > MAX_EXAMPLE_SELECT) {
                add(`${loc}.select.${n}`, "odataErrExampleSelectTooLong", number);
            }
        });
        if ((example.orderby ?? "").length > MAX_EXAMPLE_ORDERBY) {
            add(`${loc}.orderby`, "odataErrExampleOrderbyTooLong", number);
        }
        const top: unknown = example.top;
        if (top !== null && top !== undefined && !(typeof top === "number" && Number.isInteger(top) && top >= 1)) {
            add(`${loc}.top`, "odataErrExampleTop", number);
        }
    });
    if (!issues.length) {
        const whole = entitySetProblem(entitySet);
        if (whole) {
            add("", whole.key, ...whole.args);
        } else if (otherNames.indexOf(entitySet.name) !== -1) {
            add("name", "odataErrDuplicateEntitySet", entitySet.name);
        }
    }
    return issues;
}

/** The operations (function imports, actions) bound to the entity set
 *  `name`: the server refuses a definition in which one of them names an
 *  entity set that is not there. */
function boundOperations(definition: ODataDefinition | undefined | null, name: string): string[] {
    return (definition?.operations ?? []).filter((o) => o.bound_to === name).map(operationTitle);
}

/**
 * What keeps the entity set `name` from being removed: the operations
 * bound to it, and the operations that return it. The server refuses a
 * definition in which `bound_to` or `returns.entity_set` of an operation
 * names an entity set that is not there (`ServiceDefinition._consistent`
 * in agents/odata/models.py). One entry per wording; an operation that is
 * bound to the entity set and returns it is named once, as bound.
 */
function removalBlockers(definition: ODataDefinition | undefined | null, name: string): ODataRemovalBlocker[] {
    const operations = definition?.operations ?? [];
    const returning = operations.filter((o) => o.bound_to !== name && o.returns?.entity_set === name).map(operationTitle);
    const blockers: ODataRemovalBlocker[] = [
        { key: "odataRemoveEntityBound", operations: boundOperations(definition, name) },
        { key: "odataRemoveEntityReturned", operations: returning }
    ];
    return blockers.filter((blocker) => blocker.operations.length > 0);
}


export default {

    ENTITY_OPS,
    WRITE_OPS,

    /** How many entity sets and operations a definition holds. */
    counts(definition: ODataDefinition | undefined | null): ODataCounts {
        return {
            entitySets: definition?.entity_sets?.length ?? 0,
            operations: definition?.operations?.length ?? 0
        };
    },

    /**
     * Whether the definition lets an agent with `allow_write` change data:
     * an entity set with create, update or delete, or an enabled operation
     * that is a write (`operationIsWrite`). That is the rule the server RUNS
     * calls by; the `has_write` flag in its answers is narrower (it leaves
     * out an enabled POST marked `changes_data: false`), so the detail page
     * shows its Write tag by this function and not by that flag.
     */
    hasWrite(definition: ODataDefinition | undefined | null): boolean {
        return (definition?.entity_sets ?? []).some((e) => writeOpsOf(e).length > 0)
            || writeOperations(definition).length > 0;
    },

    operationIsWrite,

    writeOperations,

    /**
     * What `allow_write` opens, one entry per entity set with a write
     * operation -- "Item text (create, update)" -- followed by the enabled
     * operations that are writes, by title.
     */
    writeSummary(definition: ODataDefinition | undefined | null): string[] {
        const entitySets = (definition?.entity_sets ?? [])
            .filter((e) => writeOpsOf(e).length > 0)
            .map((e) => `${titleOf(e)} (${writeOpsOf(e).join(", ")})`);
        return entitySets.concat(writeOperations(definition).map(operationTitle));
    },

    /** How many of an entity set's fields an agent can read, of how many. */
    fieldCount(entitySet: ODataEntitySet): ODataFieldCount {
        const fields = entitySet.fields ?? [];
        return { selected: fields.filter((f) => f.selectable).length, total: fields.length };
    },

    /** The business title of an entity set, or its technical name. */
    titleOf,

    /** The most entity sets a definition may hold. */
    MAX_ENTITY_SETS,

    operationRefusal,

    /** The table row of the entity set at `index` of its definition. */
    entitySetRow,

    /** One table row per entity set, in the definition's order. */
    entitySetRows(definition: ODataDefinition | undefined | null): ODataEntityRow[] {
        return (definition?.entity_sets ?? []).map(entitySetRow);
    },

    /**
     * The write operations that are on in `current` and were not in
     * `stored`, per entity set (matched by name; an entity set `stored` does
     * not have counts with all its writes). This is what a save newly lets
     * agents with `allow_write` do; switching a write off is not in here.
     * `switchedOn`: the save also switches the stored, disabled service on.
     */
    newWrites(
        stored: ODataDefinition | undefined | null, current: ODataDefinition | undefined | null,
        switchedOn = false
    ): ODataNewWrite[] {
        return newEntityWrites(stored, current, switchedOn);
    },

    /**
     * Everything saving `current` over `stored` newly lets agents with
     * `allow_write` do: the entity-set writes of `newWrites`, and the
     * enabled operations that are writes (`operationIsWrite`) and were not
     * such in `stored` (matched by name: absent, not enabled, or a read
     * there), and the fields that become writable for agents
     * (`newWritableFields`). `switchedOn`: the save also switches the
     * stored, disabled service on, which opens every write it has.
     */
    pendingWrites(
        stored: ODataDefinition | undefined | null, current: ODataDefinition | undefined | null,
        switchedOn = false
    ): ODataPending {
        return {
            entitySets: newEntityWrites(stored, current, switchedOn),
            operations: newWriteOperations(stored, current, switchedOn),
            fields: newWritableFields(stored, current)
        };
    },

    /** How many things `pending` holds: each ticked operation of each
     *  entity set, each function import or action, and each field that
     *  becomes writable. */
    pendingCount(pending: ODataPending): number {
        return pending.entitySets.reduce((sum, write) => sum + write.operations.length, 0) + pending.operations.length
            + (pending.fields ?? []).reduce((sum, write) => sum + write.fields.length, 0);
    },

    /** What is in `pending` and not in `other`, in the order of `pending`. */
    pendingMinus(pending: ODataPending, other: ODataPending): ODataPending {
        const entitySets: ODataNewWrite[] = [];
        pending.entitySets.forEach((write) => {
            const had = other.entitySets
                .filter((candidate) => candidate.name === write.name)
                .reduce((all: ODataEntityOp[], candidate) => all.concat(candidate.operations), []);
            const operations = write.operations.filter((op) => had.indexOf(op) === -1);
            if (operations.length) {
                entitySets.push({ name: write.name, title: write.title, operations });
            }
        });
        const names = other.operations.map((operation) => operation.name);
        const fields: ODataNewFieldWrite[] = [];
        (pending.fields ?? []).forEach((write) => {
            const had = (other.fields ?? [])
                .filter((candidate) => candidate.name === write.name)
                .reduce((all: string[], candidate) => all.concat(candidate.fields), []);
            const left = write.fields.filter((field) => had.indexOf(field) === -1);
            if (left.length) {
                fields.push({ name: write.name, title: write.title, fields: left });
            }
        });
        return {
            entitySets, fields,
            operations: pending.operations.filter((operation) => names.indexOf(operation.name) === -1)
        };
    },

    /** Nothing pending. */
    noPending(): ODataPending {
        return { entitySets: [], operations: [], fields: [] };
    },

    parseValueMeanings,
    formatValueMeanings,
    valueMeaningsProblem,
    fieldMatches,

    /** The fields the dialog lists for a search text and a filter. */
    filterFields<T extends Pick<ODataField, "name" | "label" | "selectable" | "filterable" | "writable" | "personal_data">>(
        fields: readonly T[], query: string | undefined | null, mode: ODataFieldFilter
    ): T[] {
        return fields.filter((field) => fieldMatches(field, query, mode));
    },

    navigationFollow,
    exampleWarning,
    hiddenKeys,
    entitySetIssues,
    boundOperations,
    removalBlockers,

    /** The most fields an entity set may hold. */
    MAX_FIELDS,

    /** The agents using a service, split by `allow_write`: only the first
     *  group can run a write the catalogue enables. */
    writers(usedBy: readonly ODataUsedBy[] | undefined | null): ODataWriters {
        const all = usedBy ?? [];
        return {
            allowed: all.filter((used) => used.allow_write === true).map((used) => used.agent),
            others: all.filter((used) => used.allow_write !== true).map((used) => used.agent)
        };
    },

    /**
     * What the server would refuse about the entity sets, one problem per
     * entity set at most: the rules of `EntitySetDef` and the unique names
     * of `ServiceDefinition` (agents/odata/models.py). The per-value rules
     * (text lengths, the size cap) stay the server's.
     */
    definitionProblems(definition: ODataDefinition | undefined | null): ODataEntityProblem[] {
        const entitySets = definition?.entity_sets ?? [];
        const names = entitySets.map((entitySet) => entitySet.name);
        const problems: ODataEntityProblem[] = [];
        entitySets.forEach((entitySet, index) => {
            const own = entitySetProblem(entitySet);
            if (own) {
                problems.push({ index, key: own.key, args: own.args });
            } else if (names.indexOf(entitySet.name) !== names.lastIndexOf(entitySet.name)) {
                problems.push({ index, key: "odataErrDuplicateEntitySet", args: [entitySet.name] });
            }
        });
        return problems;
    },

    /**
     * The rows a refused save names, as row position -> the server's text.
     *
     * `byLoc` is what `serverErrors` made of the 422; `names` are the entity
     * set names in the order they were sent. A `loc` inside
     * `definition.entity_sets.<n>` belongs to row n -- unless the message
     * names another entity set, then the name decides (the page may have
     * sent something else than it shows). A message at `definition` itself
     * ("duplicate entity set 'A'") goes to every row of that name. What
     * names no row here is left to the caller, which shows the whole text.
     */
    rowErrors(byLoc: Record<string, string>, names: readonly string[]): Record<number, string> {
        const rows: Record<number, string> = {};
        Object.keys(byLoc).forEach((loc) => {
            const placed = placement(loc, byLoc[loc], names);
            placed.rows.forEach((index) => {
                rows[index] = rows[index] ? `${rows[index]} ${placed.text}` : placed.text;
            });
        });
        return rows;
    },

    /**
     * The parts of a refused save as lines for above the form, each said
     * with the entity set it is about: "Item (A_Item): fields.4: ..." in
     * place of "definition.entity_sets.0.fields.4: ...". A position means
     * nothing to an admin, and the row it marks may be a screen away. A
     * part that names no row here keeps its location.
     */
    refusalLines(byLoc: Record<string, string>, rows: readonly { name: string; title: string }[]): string[] {
        const names = rows.map((row) => row.name);
        return Object.keys(byLoc).map((loc) => {
            const placed = placement(loc, byLoc[loc], names);
            const about = placed.rows.map((index) => entityLabel(rows[index])).join(", ");
            return about ? `${about}: ${placed.text}` : `${loc}: ${byLoc[loc]}`;
        });
    },

    /** "Item (A_Item)", or the name alone when that is the title. */
    entityLabel,

    /** A name for an entity set added by hand that no other one has. */
    newEntitySetName(names: readonly string[]): string {
        let name = "NewEntitySet";
        for (let n = 2; names.indexOf(name) !== -1; n++) {
            name = `NewEntitySet${n}`;
        }
        return name;
    },

    /** An entity set as "Add" starts it: nothing enabled, no fields. */
    emptyEntitySet(name: string): ODataEntitySet {
        return {
            name, title: "", path: "", entity_type: "", description: "",
            keys: [], operations: [], fields: [], navigations: [], examples: []
        };
    },

    /** A new service as the form starts with it: V2, enabled, technical
     * user, nothing in the definition. */
    emptyService(): ODataServiceInput {
        return {
            name: "", title: "", purpose: "", not_for: "", destination: "", user_context: false,
            odata_version: "v2", service_path: "", enabled: true,
            definition: { entity_sets: [], operations: [] }, metadata_fetched_at: null
        };
    },

    /**
     * The general fields of a service, checked the way the server checks
     * them. Returns field -> i18n key; an empty object means valid. The
     * definition's own rules (keys, selectable fields, ...) are the server's.
     */
    validate(input: ODataServiceInput): ODataErrors {
        const errors: ODataErrors = {};
        // Stripped first, as the server does for these two: what surrounds
        // the text is dropped there, so only what is inside it counts.
        const title = serverStrip(input.title);
        const purpose = serverStrip(input.purpose);
        const notFor = input.not_for ?? "";
        const path = input.service_path ?? "";
        const set = (field: ODataErrorField, key: string): void => {
            if (key) {
                errors[field] = key;
            }
        };

        set("name", nameProblem(input.name ?? ""));
        if (!title) {
            errors.title = "odataErrTitleRequired";
        } else if (title.length > MAX_TITLE) {
            errors.title = "odataErrTitleTooLong";
        } else if (!isOneLine(title)) {
            errors.title = "odataErrTitleOneLine";
        }
        if (!purpose) {
            errors.purpose = "odataErrPurposeRequired";
        } else if (purpose.length > MAX_PURPOSE) {
            errors.purpose = "odataErrPurposeTooLong";
        } else if (!isOneLine(purpose)) {
            errors.purpose = "odataErrPurposeOneLine";
        }
        if (notFor.length > MAX_NOT_FOR) {
            errors.not_for = "odataErrNotForTooLong";
        } else if (!isOneLine(notFor)) {
            errors.not_for = "odataErrNotForOneLine";
        }
        set("destination", destinationProblem(input.destination ?? ""));
        // Strict, like the server's StrictBool: "true" or 1 is not a flag.
        // This one decides whose identity reaches SAP. Left out, the server
        // takes its default (technical user, enabled), so that is no error.
        if (input.user_context !== undefined && typeof input.user_context !== "boolean") {
            errors.user_context = "odataErrBoolean";
        }
        if (!path) {
            errors.service_path = "odataErrPathRequired";
        } else if (!isConfinedPath(path)) {
            errors.service_path = "odataErrPathInvalid";
        }
        if (input.enabled !== undefined && typeof input.enabled !== "boolean") {
            errors.enabled = "odataErrBoolean";
        }
        // Required by the server: a payload without it is refused instead of
        // replacing the stored definition by an empty one.
        const definition: unknown = input.definition;
        if (definition === null || typeof definition !== "object" || Array.isArray(definition)) {
            errors.definition = "odataErrDefinitionRequired";
        }
        return errors;
    },

    /** What the duplicate dialog can check before asking the server: the
     *  copy's name, and its destination when one is given. */
    validateDuplicate(body: ODataDuplicateRequest): ODataErrors {
        const errors: ODataErrors = {};
        const name = nameProblem(body.name ?? "");
        if (name) {
            errors.name = name;
        }
        if (body.destination !== undefined) {
            const destination = destinationProblem(body.destination);
            if (destination) {
                errors.destination = destination;
            }
        }
        return errors;
    },

    /** The length the server holds against the 200 of a purpose: that of
     *  the stripped text. */
    purposeLength(purpose: string | undefined | null): number {
        return serverStrip(purpose).length;
    },

    /** The most characters a purpose may have. */
    MAX_PURPOSE,

    /** A text as the server stores a title or a purpose: without the white
     *  space around it (Unicode White_Space, which is not JS `trim()`). */
    serverStrip,

    /**
     * The payload fields of a service, as a deep copy and without anything
     * else: a stored service carries `id`, `counts`, `used_by` and more,
     * and the server refuses a payload with a key it does not know. The
     * definition always goes along -- a save sends the whole service.
     */
    payloadOf(service: ODataServiceInput): ODataServiceInput {
        const source = service as unknown as Record<string, unknown>;
        const payload: Record<string, unknown> = {};
        PAYLOAD_FIELDS.forEach((field) => {
            payload[field] = source[field];
        });
        return JSON.parse(JSON.stringify(payload)) as ODataServiceInput;
    },

    /**
     * What saving `current` over `stored` changes about who the agents
     * using the service act as in SAP: the identity (signed-in or technical
     * user) and the destination, which holds the technical user's
     * credential and names the system.
     */
    identityChange(stored: ODataServiceInput, current: ODataServiceInput): ODataIdentityChange {
        const before = stored.user_context === true;
        const after = current.user_context === true;
        return {
            runsAs: before === after ? null : (after ? "user" : "technical"),
            destination: stored.destination === current.destination
                ? null : { from: stored.destination, to: current.destination }
        };
    },

    /**
     * The fields a refused save names, from the 422 of the catalogue routes.
     *
     * Those routes answer `{"detail": "<loc>: <msg>; <loc>: <msg>"}`: one
     * string, not FastAPI's `detail[]` (which would echo the refused input),
     * so `AdminError.fieldErrors` is empty for them and this is what gives a
     * form its per-field messages. Keys are the server's `loc` (`title`,
     * `service_path`, `definition.entity_sets.0.fields.1`); a message may
     * itself contain "; ", so a part starts a new field only when it begins
     * with a `loc`. A refusal that names no field (a taken name, a service
     * still in use) gives an empty object: show `AdminError.detail` then.
     */
    serverErrors(detail: string | undefined | null): Record<string, string> {
        return this.serverRefusal(detail).byLoc;
    },

    /**
     * The same refusal with what `serverErrors` leaves out: `lead`, the text
     * before the first `<loc>: ` (all of it when no field is named), and
     * `more`, the "and n more" the server ends a long list with. A page that
     * rebuilds the text from `byLoc` appends both, so nothing the server
     * said is lost.
     */
    serverRefusal(detail: string | undefined | null): ODataRefusal {
        const byLoc: Record<string, string> = {};
        const lead: string[] = [];
        let more = "";
        let current = "";
        String(detail ?? "").split("; ").forEach((part) => {
            const match = SERVER_LOC_RE.exec(part);
            if (match) {
                current = match[1];
                const message = match[2];
                byLoc[current] = message.indexOf(VALUE_ERROR_PREFIX) === 0
                    ? message.substring(VALUE_ERROR_PREFIX.length) : message;
            } else if (current && /^and \d+ more$/.test(part)) {
                more = part;
            } else if (current) {
                byLoc[current] += `; ${part}`;
            } else if (part) {
                lead.push(part);
            }
        });
        return { lead: lead.join("; "), byLoc, more };
    },

    /**
     * `operations` with `op` switched on or off: a new array, without
     * duplicates, in the server's order.
     */
    toggleOperation(
        operations: readonly ODataEntityOp[], op: ODataEntityOp, enabled: boolean
    ): ODataEntityOp[] {
        return ENTITY_OPS.filter((candidate) => (
            candidate === op ? enabled : operations.indexOf(candidate) !== -1
        ));
    }
};

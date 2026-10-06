import type {
    ODataDefinition, ODataDuplicateRequest, ODataEntityOp, ODataEntitySet, ODataExampleQuery, ODataField,
    ODataMetadataPreview, ODataNavigation, ODataOperation, ODataPreviewEntitySet, ODataPreviewField,
    ODataPreviewOperation, ODataServiceInput, ODataUsedBy, ODataValueMeaning, ODataVersion
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
// `OperationDef.description` in agents/odata/models.py.
const MAX_OPERATION_DESCRIPTION = 600;
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

/** What one row of the operations table shows: flat, so that the table
 *  binds to plain values. Worked out from the definition (`operationRow`). */
export interface ODataOperationRow {
    /** The position of the operation in `definition.operations`. */
    index: number;
    name: string;
    /** The business title, or the name when there is none. */
    title: string;
    /** "ReleaseItem · POST": what SAP calls it and how it is sent. */
    technical: string;
    description: string;
    /** The title of the entity set it is bound to (its name when the
     *  definition does not hold it), or "" for an unbound operation. */
    boundTo: string;
    /** The parameter names, comma-separated; an optional one is in brackets. */
    parameters: string;
    enabled: boolean;
    /** Whether a call is a write (`operationIsWrite`): what the "Changes
     *  data" box shows. A POST is one whatever its stored flag says. */
    write: boolean;
    /** Sent with POST: "Changes data" cannot be unticked. */
    post: boolean;
    /** Why no agent can call it although it is enabled, as an i18n key; ""
     *  when it can be called, is not enabled, or was changed on the page
     *  (the reason is about the operation as it is stored). */
    uncallable: string;
    /** Why the last click was not taken (already in the user's language). */
    note: string;
    /** "Release item (ReleaseItem)": the accessible name of the row. */
    label: string;
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
    /** Another service path: the same calls then go to another SAP service. */
    servicePath: { from: string; to: string } | null;
    /** Another OData version: the calls are sent in another protocol. */
    version: { from: ODataVersion; to: ODataVersion } | null;
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
 * (`operation_is_write` in agents/odata/models.py): a read is only what is marked
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

/** The operations of a definition that every agent using the service can
 *  call, with or without `allow_write`, and whose calls are not recorded
 *  in the audit: enabled, and a read by `operationIsWrite`. */
function readOperations(definition: ODataDefinition | undefined | null): ODataOperation[] {
    return (definition?.operations ?? []).filter((o) => o.enabled === true && !operationIsWrite(o));
}

/**
 * The operations saving `current` over `stored` newly opens as READS:
 * enabled and a read in `current`, and not that in `stored` (matched by
 * name: a write there -- its flag set or missing --, not enabled, or
 * absent). A call of such an operation needs no `allow_write` any more and
 * is no longer recorded, so this is a widening like a new write, and is
 * said and asked about like one. An operation `stored` already has enabled
 * as a read is no entry, and a POST never is one: it stays a write whatever
 * its flag says.
 */
function newReadOperations(
    stored: ODataDefinition | undefined | null, current: ODataDefinition | undefined | null
): ODataNewOperation[] {
    const had = readOperations(stored).map((o) => o.name);
    return readOperations(current)
        .filter((o) => had.indexOf(o.name) === -1)
        .map((o) => ({ name: o.name, title: operationTitle(o) }));
}

/** The title and description of an operation as the server would refuse
 *  them (`OperationDef`): field -> i18n key; empty means valid. */
function operationTextProblems(
    title: string | undefined | null, description: string | undefined | null
): { title?: string; description?: string } {
    const problems: { title?: string; description?: string } = {};
    const name = title ?? "";
    if (name.length > MAX_TITLE) {
        problems.title = "odataErrTitleTooLong";
    } else if (!isOneLine(name)) {
        problems.title = "odataErrTitleOneLine";
    }
    if ((description ?? "").length > MAX_OPERATION_DESCRIPTION) {
        problems.description = "odataErrEntityDescriptionTooLong";
    }
    return problems;
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

/** The i18n key that says in plain words why an enabled operation cannot
 *  be called (the reason codes of `CALL_REFUSALS` in agents/odata/client.py
 *  and `INVALID_DEFINITION` in agents/odata/calls.py). */
const UNCALLABLE_TEXT: Record<string, string> = {
    calls_not_available: "odataUncallableKind",
    bound_set_missing: "odataUncallableBoundMissing",
    bound_set_without_key: "odataUncallableNoKey",
    key_not_declared: "odataUncallableKeyNotDeclared",
    bound_key_type: "odataUncallableKeyType",
    parameter_type: "odataUncallableParameterType",
    invalid_definition: "odataUncallableInvalid"
};

function uncallableKey(reason: string | undefined | null): string {
    if (!reason) {
        return "";
    }
    return Object.prototype.hasOwnProperty.call(UNCALLABLE_TEXT, reason) ? UNCALLABLE_TEXT[reason] : "odataUncallableOther";
}

/**
 * The table row of the operation at `index` of `definition`. `uncallable`:
 * operation name -> reason code, as the server said it about the STORED
 * service; it is shown while the row is enabled (a reason for an operation
 * that the admin has switched off again would describe nothing an agent
 * meets). `stored`: the definition that reason is about; an operation that
 * is not (or no longer) as it is there shows no reason, which would be
 * about another operation than the row shows.
 */
function operationRow(
    operation: ODataOperation, index: number, definition: ODataDefinition | undefined | null,
    uncallable: Record<string, string> = {}, stored?: ODataDefinition | null
): ODataOperationRow {
    const title = operationTitle(operation);
    const bound = operation.bound_to === null || operation.bound_to === undefined ? "" : operation.bound_to;
    const set = bound ? (definition?.entity_sets ?? []).filter((e) => e.name === bound)[0] : undefined;
    const saved = stored ? (stored.operations ?? []).filter((o) => o.name === operation.name)[0] : operation;
    const asSaved = !!saved && JSON.stringify(saved) === JSON.stringify(operation);
    const reason = asSaved && Object.prototype.hasOwnProperty.call(uncallable, `=${operation.name}`)
        ? uncallable[`=${operation.name}`] : "";
    return {
        index,
        name: operation.name,
        title,
        technical: `${operation.name} \u00b7 ${operation.http_method}`,
        description: (operation.description ?? "").trim(),
        boundTo: set ? titleOf(set) : bound,
        parameters: (operation.parameters ?? [])
            .map((p) => (p.required === false ? `[${p.name}]` : p.name)).join(", "),
        enabled: operation.enabled === true,
        write: operationIsWrite(operation),
        post: operation.http_method !== "GET",
        uncallable: operation.enabled === true ? uncallableKey(reason) : "",
        note: "",
        label: title === operation.name ? title : `${title} (${operation.name})`
    };
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


// --- import from $metadata --------------------------------------------------

/** What a row of the import dialog is about. */
export type ODataImportKind = "entity_set" | "field" | "removed_field" | "key_change" | "type_change"
    | "entity_type_change" | "operation" | "removed_entity_set" | "removed_operation" | "labels";

/** How a row compares with the service as the FORM has it. */
export type ODataImportStatus = "new" | "changed" | "in_service" | "removed";

/**
 * One thing the $metadata document offers, or one difference between the
 * document and the service of the form. `id` is what a selection names:
 * `set:<name>`, `field:<set>:<field>`, `rmfield:<set>:<field>`,
 * `key:<set>`, `type:<set>:<field>`, `etype:<set>`, `op:<name>`,
 * `rmset:<name>`, `rmop:<name>`, `labels`. EDM names hold no colon.
 *
 * Facts only, no sentence: the dialog words a row when it is shown.
 */
export interface ODataImportItem {
    id: string;
    kind: ODataImportKind;
    /** The id of the entity set row this row sits under, or "". */
    parent: string;
    name: string;
    /** SAP's label, or the admin's title of something that is removed. */
    label: string;
    status: ODataImportStatus;
    /** Ticking it changes something; a row that is only shown is not. */
    selectable: boolean;
    /** How many rows sit under this entity set row. */
    children: number;
    /** A field's type, or the type the document has now. */
    type: string;
    /** What the service holds, and what the document says (a type, a key
     *  as "A, B", an entity type). */
    was: string;
    now: string;
    /** An entity set: fields listed, fields of the document, navigations. */
    fields: number;
    fieldsTotal: number;
    navigations: number;
    /** Fields, keys or navigations of the entity set were cut by the server. */
    truncated: boolean;
    /** An operation: kind, method, parameters, the entity set it is bound to. */
    operationKind: string;
    method: string;
    parameters: number;
    boundTo: string;
    /** What SAP DECLARES (`creatable`, `updatable`, `deletable`,
     *  `filterable`): information, never taken over. */
    declared: string[];
    /** A new operation bound to an entity set the service does not have. */
    needs: string;
    /** A removal: the entity-set operations agents lose, how many fields
     *  they can read, filter or write today, whether an operation is
     *  enabled, whether a field is part of the key. */
    lostOperations: ODataEntityOp[];
    lostReadable: number;
    lostWritable: number;
    lostEnabled: boolean;
    lostKey: boolean;
    /** Why an entity set cannot be removed while these operations stay. */
    blockers: ODataRemovalBlocker[];
    /** Declared by the document, but left out by the parser: the reason code. */
    skippedReason: string;
    /** `labels`: how many empty labels the document can fill. */
    count: number;
}

export interface ODataImportPlan {
    items: ODataImportItem[];
    /** Top-level rows by status, and what the document offers in all. */
    counts: { entitySets: number; operations: number; isNew: number; changed: number; inService: number; removed: number };
    /** False: the document was not read to its end, so what was removed
     *  from it is not known (which is not the same as nothing). */
    removalsKnown: boolean;
}

/** Why Apply cannot take a selection as it is: an i18n key and its arguments. */
export interface ODataImportBlock {
    id: string;
    key: string;
    args: (string | number)[];
}

export interface ODataImportSummary {
    add: number;
    change: number;
    remove: number;
    total: number;
    blocked: ODataImportBlock[];
}

const MAX_OPERATIONS = 200;
// Control characters, the C1 block and the line separators: a label is one line.
const LABEL_BREAK_RE = new RegExp("[\\u0000-\\u001f\\u007f-\\u009f\\u2028\\u2029]+", "g");

/** A label of the document as the catalogue can store it: one line, at
 *  most 120 characters. */
function importLabel(text: string | undefined | null): string {
    return String(text ?? "").replace(LABEL_BREAK_RE, " ").replace(/\s+/g, " ").trim().slice(0, MAX_LABEL);
}

function importId(kind: string, first = "", second = ""): string {
    return [kind, first, second].filter(Boolean).join(":");
}

function blankImportItem(id: string, kind: ODataImportKind, name: string, status: ODataImportStatus): ODataImportItem {
    return {
        id, kind, parent: "", name, label: "", status, selectable: false, children: 0, type: "", was: "", now: "",
        fields: 0, fieldsTotal: 0, navigations: 0, truncated: false, operationKind: "", method: "", parameters: 0,
        boundTo: "", declared: [], needs: "", lostOperations: [], lostReadable: 0, lostWritable: 0,
        lostEnabled: false, lostKey: false, blockers: [], skippedReason: "", count: 0
    };
}

function namesOf(list: readonly { name: string }[] | undefined | null): string[] {
    return (list ?? []).map((entry) => entry.name);
}

/** The rows under an entity set the service already has: what the document
 *  says differently about it. */
function importDifferences(
    entitySet: ODataEntitySet, offered: ODataPreviewEntitySet, parent: string
): ODataImportItem[] {
    const rows: ODataImportItem[] = [];
    const child = (id: string, kind: ODataImportKind, name: string, status: ODataImportStatus): ODataImportItem => {
        const row = blankImportItem(id, kind, name, status);
        row.parent = parent;
        row.selectable = true;
        rows.push(row);
        return row;
    };
    const have = entitySet.fields ?? [];
    const haveNames = namesOf(have);
    const offer = offered.fields ?? [];
    const offerNames = namesOf(offer);
    const keysHave = namesOf(entitySet.keys);
    const keysOffer = namesOf(offered.keys);
    // A key the server cut cannot be taken over: it would not be the key.
    if ((offered.keys_total ?? keysOffer.length) <= keysOffer.length && keysHave.join("\n") !== keysOffer.join("\n")) {
        const row = child(importId("key", entitySet.name), "key_change", entitySet.name, "changed");
        row.was = keysHave.join(", ");
        row.now = keysOffer.join(", ");
    }
    if ((entitySet.entity_type ?? "") !== (offered.entity_type ?? "")) {
        const row = child(importId("etype", entitySet.name), "entity_type_change", entitySet.name, "changed");
        row.was = entitySet.entity_type ?? "";
        row.now = offered.entity_type ?? "";
    }
    offer.forEach((field) => {
        const at = haveNames.indexOf(field.name);
        if (at !== -1 && (have[at].type ?? "Edm.String") !== field.type) {
            const row = child(importId("type", entitySet.name, field.name), "type_change", field.name, "changed");
            row.label = importLabel(field.label);
            row.was = have[at].type ?? "Edm.String";
            row.now = field.type;
        }
    });
    offer.forEach((field) => {
        if (haveNames.indexOf(field.name) === -1) {
            const row = child(importId("field", entitySet.name, field.name), "field", field.name, "new");
            row.label = importLabel(field.label);
            row.type = field.type;
            row.declared = (["filterable", "creatable", "updatable"] as const).filter((key) => field.declared?.[key] === true);
        }
    });
    // Of a set whose fields were cut, a field that is not listed may still
    // be in the document: only what the server compared is "removed".
    const gone = offered.truncated
        ? (offered.status === "new" ? [] : (offered.removed_fields ?? []).filter((name) => offerNames.indexOf(name) === -1))
        : haveNames.filter((name) => offerNames.indexOf(name) === -1);
    gone.forEach((name) => {
        const field = have[haveNames.indexOf(name)];
        if (!field) {
            return;
        }
        const row = child(importId("rmfield", entitySet.name, name), "removed_field", name, "removed");
        row.label = field.label ?? "";
        row.type = field.type ?? "Edm.String";
        row.lostReadable = field.selectable === true ? 1 : 0;
        row.lostWritable = field.writable === true ? 1 : 0;
        row.lostKey = keysHave.indexOf(name) !== -1;
    });
    return rows;
}

/**
 * What the document `preview` offers, compared with `definition` -- the
 * service as the FORM has it, which may be ahead of the stored one the
 * server compared with. One row per entity set and operation of the
 * document, the differences of an entity set underneath it, then what the
 * service has and the document no longer declares.
 */
function importPlan(definition: ODataDefinition | undefined | null, preview: ODataMetadataPreview): ODataImportPlan {
    const items: ODataImportItem[] = [];
    const entitySets = definition?.entity_sets ?? [];
    const operations = definition?.operations ?? [];
    const setNames = namesOf(entitySets);
    const offeredSets = preview.entity_sets ?? [];
    const offeredOperations = preview.operations ?? [];
    const counts = {
        entitySets: offeredSets.length, operations: offeredOperations.length, isNew: 0, changed: 0, inService: 0, removed: 0
    };
    const count = (status: ODataImportStatus): void => {
        counts[status === "new" ? "isNew" : status === "in_service" ? "inService" : status] += 1;
    };
    let emptyLabels = 0;
    offeredSets.forEach((offered) => {
        const id = importId("set", offered.name);
        const row = blankImportItem(id, "entity_set", offered.name, "new");
        const stored = entitySets[setNames.indexOf(offered.name)];
        row.label = importLabel(offered.label);
        row.fields = (offered.fields ?? []).length;
        row.fieldsTotal = offered.fields_total ?? row.fields;
        row.navigations = (offered.navigations ?? []).length;
        row.truncated = offered.truncated === true;
        row.declared = (["creatable", "updatable", "deletable"] as const).filter((key) => offered.declared?.[key] === true);
        items.push(row);
        if (!stored) {
            row.selectable = true;
            count("new");
            return;
        }
        const children = importDifferences(stored, offered, id);
        row.children = children.length;
        row.status = children.length ? "changed" : "in_service";
        count(row.status);
        children.forEach((child) => items.push(child));
        const labels: Record<string, string> = {};
        (offered.fields ?? []).forEach((field) => { labels[field.name] = importLabel(field.label); });
        emptyLabels += (stored.fields ?? []).filter((field) => !(field.label ?? "") && !!labels[field.name]).length;
    });
    // Declared by the document but left out by the parser: not "removed".
    const leftOut: Record<string, string> = {};
    (preview.skipped ?? []).forEach((entry) => {
        if (entry.kind === "entity_set" && entry.entity_set && leftOut[entry.entity_set] === undefined) {
            leftOut[entry.entity_set] = entry.reason;
        }
    });
    (preview.skipped_stored_entity_sets ?? []).forEach((entry) => { leftOut[entry.name] = entry.reason; });
    const removalsKnown = preview.removed_complete !== false;
    const offeredSetNames = namesOf(offeredSets);
    const setsCut = (preview.totals?.entity_sets ?? offeredSets.length) > offeredSets.length;
    const operationsCut = (preview.totals?.operations ?? offeredOperations.length) > offeredOperations.length;
    entitySets.forEach((entitySet) => {
        if (offeredSetNames.indexOf(entitySet.name) !== -1) {
            return;
        }
        if (leftOut[entitySet.name] !== undefined) {
            const row = blankImportItem(importId("set", entitySet.name), "entity_set", entitySet.name, "in_service");
            row.label = entitySet.title ?? "";
            row.fields = row.fieldsTotal = (entitySet.fields ?? []).length;
            row.navigations = (entitySet.navigations ?? []).length;
            row.skippedReason = leftOut[entitySet.name];
            items.push(row);
            count("in_service");
            return;
        }
        // Past the cut of a long document nothing is known about a set
        // the server did not compare.
        if (!removalsKnown || (setsCut && (preview.removed_entity_sets ?? []).indexOf(entitySet.name) === -1)) {
            return;
        }
        const row = blankImportItem(importId("rmset", entitySet.name), "removed_entity_set", entitySet.name, "removed");
        row.label = entitySet.title ?? "";
        row.selectable = true;
        row.fields = row.fieldsTotal = (entitySet.fields ?? []).length;
        row.lostOperations = ENTITY_OPS.filter((op) => (entitySet.operations ?? []).indexOf(op) !== -1);
        row.lostReadable = (entitySet.fields ?? []).filter((field) => field.selectable === true).length;
        row.lostWritable = (entitySet.fields ?? []).filter((field) => field.writable === true).length;
        row.blockers = removalBlockers(definition, entitySet.name);
        items.push(row);
        count("removed");
    });
    const operationNames = namesOf(operations);
    offeredOperations.forEach((offered) => {
        const known = operationNames.indexOf(offered.name) !== -1;
        const row = blankImportItem(importId("op", offered.name), "operation", offered.name, known ? "in_service" : "new");
        row.label = importLabel(offered.label);
        row.operationKind = offered.kind;
        row.method = offered.http_method;
        row.parameters = (offered.parameters ?? []).length;
        row.boundTo = offered.bound_to ?? "";
        row.truncated = offered.truncated === true;
        row.selectable = !known;
        row.needs = !known && row.boundTo && setNames.indexOf(row.boundTo) === -1 ? row.boundTo : "";
        items.push(row);
        count(row.status);
    });
    const offeredOperationNames = namesOf(offeredOperations);
    operations.forEach((operation) => {
        if (offeredOperationNames.indexOf(operation.name) !== -1 || !removalsKnown
            || (operationsCut && (preview.removed_operations ?? []).indexOf(operation.name) === -1)) {
            return;
        }
        const row = blankImportItem(importId("rmop", operation.name), "removed_operation", operation.name, "removed");
        row.label = operation.title ?? "";
        row.selectable = true;
        row.operationKind = operation.kind;
        row.method = operation.http_method;
        row.parameters = (operation.parameters ?? []).length;
        row.boundTo = operation.bound_to ?? "";
        row.lostEnabled = operation.enabled === true;
        items.push(row);
        count("removed");
    });
    if (emptyLabels) {
        const row = blankImportItem("labels", "labels", "", "changed");
        row.selectable = true;
        row.count = emptyLabels;
        items.push(row);
    }
    return { items, counts, removalsKnown };
}

/** A selection as a lookup: `{kind: {first: {second | "": true}}}`. */
function importChoices(selection: readonly string[]): Record<string, Record<string, Record<string, boolean>>> {
    const choices: Record<string, Record<string, Record<string, boolean>>> = {};
    selection.forEach((id) => {
        const [kind, first = "", second = ""] = id.split(":");
        choices[kind] = choices[kind] ?? {};
        choices[kind][first] = choices[kind][first] ?? {};
        choices[kind][first][second] = true;
    });
    return choices;
}

/** A field as an import adds it: its name, type and label, and NOTHING an
 *  agent could use it for. What SAP declares about it is not looked at. */
function importedField(offered: ODataPreviewField): ODataField {
    return {
        name: offered.name, type: offered.type || "Edm.String", label: importLabel(offered.label),
        selectable: false, filterable: false, writable: false, hint: "", values: [], personal_data: false
    };
}

/** An entity set as an import adds it: no operation enabled, no field
 *  ticked, no description. */
function importedEntitySet(offered: ODataPreviewEntitySet): ODataEntitySet {
    return {
        name: offered.name, title: importLabel(offered.label), path: "",
        entity_type: offered.entity_type ?? "", description: "",
        keys: (offered.keys ?? []).map((key) => ({ name: key.name, type: key.type || "Edm.String" })),
        operations: [],
        fields: (offered.fields ?? []).slice(0, MAX_FIELDS).map(importedField),
        navigations: (offered.navigations ?? []).map((navigation) => ({
            name: navigation.name, target: navigation.target, collection: navigation.collection === true, description: ""
        })),
        examples: []
    };
}

/**
 * An operation as an import adds it: switched off. `changes_data` is the
 * document's suggestion only when the document KNOWS (`suggested.known`);
 * otherwise the operation counts as changing data, the safe side. What it
 * returns is kept when that is an entity set the service will hold.
 */
function importedOperation(offered: ODataPreviewOperation, setNames: readonly string[]): ODataOperation {
    const suggested = offered.suggested;
    const returned = suggested?.returns?.entity_set;
    return {
        name: offered.name, qualified_name: offered.qualified_name ?? "", title: importLabel(offered.label),
        kind: offered.kind, http_method: offered.http_method, bound_to: offered.bound_to ?? null,
        parameters: (offered.parameters ?? []).map((parameter) => ({
            name: parameter.name, type: parameter.type || "Edm.String", required: parameter.required !== false
        })),
        description: "", enabled: false,
        changes_data: !(suggested?.known === true && suggested.changes_data === false),
        returns: returned && setNames.indexOf(returned) !== -1
            ? { entity_set: returned, collection: suggested?.returns?.collection === true } : null
    };
}

/**
 * The definition after an import: a copy of `definition` with what
 * `selection` (ids of `importPlan` rows) names, and nothing else.
 *
 * - It widens nothing: whatever it adds arrives with every operation off,
 *   no field ticked and every operation disabled.
 * - It overwrites none of the admin's work: of an entity set, field or
 *   operation the service already has, only what a ticked row names
 *   changes (a type, the key, the entity type, an empty label).
 * - It removes only what a ticked `rm...` row names; an entity set an
 *   operation is still bound to or returns stays (`removalBlockers`), and a
 *   new operation bound to an entity set the result does not hold is not
 *   added (`importSummary` says both before Apply).
 */
function mergeImport(
    definition: ODataDefinition | undefined | null, preview: ODataMetadataPreview, selection: readonly string[]
): ODataDefinition {
    const merged = JSON.parse(JSON.stringify({
        entity_sets: definition?.entity_sets ?? [], operations: definition?.operations ?? []
    })) as ODataDefinition;
    const choices = importChoices(selection);
    const chosen = (kind: string, first = "", second = ""): boolean => choices[kind]?.[first]?.[second] === true;
    const fillLabels = chosen("labels");
    (preview.entity_sets ?? []).forEach((offered) => {
        const entitySet = merged.entity_sets.filter((candidate) => candidate.name === offered.name)[0];
        if (!entitySet) {
            if (chosen("set", offered.name) && merged.entity_sets.length < MAX_ENTITY_SETS) {
                merged.entity_sets.push(importedEntitySet(offered));
            }
            return;
        }
        const name = entitySet.name;
        const touched = fillLabels || !!choices.field?.[name] || !!choices.rmfield?.[name] || !!choices.type?.[name]
            || chosen("key", name) || chosen("etype", name);
        if (!touched) {
            return;
        }
        if (chosen("etype", name)) {
            entitySet.entity_type = offered.entity_type ?? "";
        }
        const offeredFields: Record<string, ODataPreviewField> = {};
        (offered.fields ?? []).forEach((field) => { offeredFields[field.name] = field; });
        entitySet.fields.forEach((field) => {
            const said = offeredFields[field.name];
            if (!said) {
                return;
            }
            if (chosen("type", name, field.name)) {
                field.type = said.type;
                entitySet.keys.forEach((key) => {
                    if (key.name === field.name) {
                        key.type = said.type;
                    }
                });
            }
            if (fillLabels && !(field.label ?? "")) {
                field.label = importLabel(said.label);
            }
        });
        const add = (field: ODataPreviewField): void => {
            if (entitySet.fields.length < MAX_FIELDS && !entitySet.fields.some((have) => have.name === field.name)) {
                entitySet.fields.push(importedField(field));
            }
        };
        (offered.fields ?? []).forEach((field) => {
            if (chosen("field", name, field.name)) {
                add(field);
            }
        });
        const remove = choices.rmfield?.[name];
        if (remove) {
            // Only a field the document does not list: a ticked removal
            // never takes a field that is still there.
            const gone = (field: { name: string }): boolean => remove[field.name] === true && !offeredFields[field.name];
            entitySet.fields = entitySet.fields.filter((field) => !gone(field));
            entitySet.keys = entitySet.keys.filter((key) => !gone(key));
        }
        if (chosen("key", name)) {
            // The key as the document has it; a key field the service does
            // not hold yet comes along, unticked: a key names a field.
            (offered.keys ?? []).forEach((key) => add(offeredFields[key.name] ?? {
                name: key.name, type: key.type, label: "", declared: { filterable: false, creatable: false, updatable: false }
            }));
            entitySet.keys = (offered.keys ?? []).map((key) => ({ name: key.name, type: key.type || "Edm.String" }));
        }
    });
    const dropOperations = choices.rmop ?? {};
    const offeredOperations = namesOf(preview.operations);
    merged.operations = merged.operations.filter((operation) => (
        !dropOperations[operation.name] || offeredOperations.indexOf(operation.name) !== -1
    ));
    const offeredSets = namesOf(preview.entity_sets);
    Object.keys(choices.rmset ?? {}).forEach((name) => {
        if (offeredSets.indexOf(name) === -1 && removalBlockers(merged, name).length === 0) {
            merged.entity_sets = merged.entity_sets.filter((entitySet) => entitySet.name !== name);
        }
    });
    const setNames = namesOf(merged.entity_sets);
    (preview.operations ?? []).forEach((offered) => {
        if (!chosen("op", offered.name) || merged.operations.length >= MAX_OPERATIONS
            || merged.operations.some((operation) => operation.name === offered.name)
            || (offered.bound_to && setNames.indexOf(offered.bound_to) === -1)) {
            return;
        }
        merged.operations.push(importedOperation(offered, setNames));
    });
    return merged;
}

/**
 * What Apply will do with `selection`: how many things it adds, changes
 * and removes, and what keeps it from being applied (`blocked`): a new
 * operation bound to an entity set that is neither in the service nor
 * ticked, an entity set to remove that an operation is still bound to or
 * returns, and more entity sets, fields or operations than a service holds.
 */
function importSummary(
    definition: ODataDefinition | undefined | null, preview: ODataMetadataPreview, selection: readonly string[]
): ODataImportSummary {
    const choices = importChoices(selection);
    const size = (kind: string): number => Object.keys(choices[kind] ?? {})
        .reduce((sum, first) => sum + Object.keys(choices[kind][first]).length, 0);
    const add = size("set") + size("field") + size("op");
    const change = size("key") + size("type") + size("etype") + size("labels");
    const remove = size("rmset") + size("rmfield") + size("rmop");
    const blocked: ODataImportBlock[] = [];
    const entitySets = definition?.entity_sets ?? [];
    const setNames = namesOf(entitySets);
    const after = setNames.filter((name) => !choices.rmset?.[name]).concat(Object.keys(choices.set ?? {}));
    (preview.operations ?? []).forEach((offered) => {
        if (choices.op?.[offered.name] && offered.bound_to && after.indexOf(offered.bound_to) === -1) {
            blocked.push({ id: importId("op", offered.name), key: "odataImportNeedsSet", args: [offered.name, offered.bound_to] });
        }
    });
    const kept: ODataDefinition = {
        entity_sets: entitySets,
        operations: (definition?.operations ?? []).filter((operation) => !choices.rmop?.[operation.name])
    };
    Object.keys(choices.rmset ?? {}).forEach((name) => {
        const entitySet = entitySets[setNames.indexOf(name)];
        removalBlockers(kept, name).forEach((blocker) => {
            blocked.push({
                id: importId("rmset", name), key: blocker.key,
                args: [entitySet ? titleOf(entitySet) : name, name, blocker.operations.join(", ")]
            });
        });
    });
    if (after.length > MAX_ENTITY_SETS) {
        blocked.push({ id: "", key: "odataImportTooManySets", args: [after.length, MAX_ENTITY_SETS] });
    }
    const operationsAfter = (kept.operations ?? []).length + size("op");
    if (operationsAfter > MAX_OPERATIONS) {
        blocked.push({ id: "", key: "odataImportTooManyOperations", args: [operationsAfter, MAX_OPERATIONS] });
    }
    Object.keys(choices.field ?? {}).forEach((name) => {
        const entitySet = entitySets[setNames.indexOf(name)];
        const total = (entitySet?.fields ?? []).length + Object.keys(choices.field[name]).length
            - Object.keys(choices.rmfield?.[name] ?? {}).length;
        if (total > MAX_FIELDS) {
            blocked.push({ id: importId("set", name), key: "odataImportTooManyFields", args: [name, total, MAX_FIELDS] });
        }
    });
    return { add, change, remove, total: add + change + remove, blocked };
}

/** Which rows of a plan the dialog lists for a search text, a status filter
 *  and the entity sets that are opened up. A row under an entity set is
 *  listed when that set is expanded and the row itself matches; an entity
 *  set also when one of its rows does. */
function importRows(
    items: readonly ODataImportItem[], query: string, filter: ODataImportStatus | "all",
    expanded: Record<string, boolean>
): ODataImportItem[] {
    const needle = (query ?? "").trim().toLowerCase();
    const matches = (item: ODataImportItem): boolean => (
        (filter === "all" || item.status === filter)
        && (!needle || item.name.toLowerCase().indexOf(needle) !== -1 || item.label.toLowerCase().indexOf(needle) !== -1)
    );
    const withMatch: Record<string, boolean> = {};
    items.forEach((item) => {
        if (item.parent && matches(item)) {
            withMatch[item.parent] = true;
        }
    });
    return items.filter((item) => (
        item.parent ? expanded[item.parent] === true && matches(item) : matches(item) || withMatch[item.id] === true
    ));
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
     * that is a write (`operationIsWrite`): the rule the server runs calls
     * by, and the one behind the `has_write` flag of its answers
     * (`operation_is_write` in agents/odata/models.py).
     */
    hasWrite(definition: ODataDefinition | undefined | null): boolean {
        return (definition?.entity_sets ?? []).some((e) => writeOpsOf(e).length > 0)
            || writeOperations(definition).length > 0;
    },

    operationIsWrite,

    writeOperations,

    operationRow,

    /** One table row per operation, in the definition's order. */
    operationRows(
        definition: ODataDefinition | undefined | null, uncallable: Record<string, string> = {},
        stored?: ODataDefinition | null
    ): ODataOperationRow[] {
        return (definition?.operations ?? []).map((operation, index) => (
            operationRow(operation, index, definition, uncallable, stored)
        ));
    },

    operationTitle,
    operationTextProblems,

    /** The most characters the description of an operation may have. */
    MAX_OPERATION_DESCRIPTION,

    /** `uncallable_operations` of a service answer as a lookup for
     *  `operationRow`: operation name (behind "=", so that no name collides
     *  with a property of every object) -> reason code. */
    uncallableByName(
        listed: readonly { name: string; reason: string }[] | undefined | null
    ): Record<string, string> {
        const byName: Record<string, string> = {};
        (Array.isArray(listed) ? listed : []).forEach((entry) => {
            if (entry && typeof entry.name === "string" && typeof entry.reason === "string") {
                byName[`=${entry.name}`] = entry.reason;
            }
        });
        return byName;
    },

    uncallableKey,

    /** The agents of `usedBy` that the job scheduler can start, by name:
     *  runs started there have no signed-in user. An agent that is switched
     *  off is not started, and `expose_api` without a slug is no endpoint. */
    jobAgents(usedBy: readonly ODataUsedBy[] | undefined | null): string[] {
        return (usedBy ?? [])
            .filter((used) => used.expose_api === true && used.enabled !== false && !!(used.api_slug ?? "").trim())
            .map((used) => used.agent);
    },

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

    /**
     * The operations saving `current` over `stored` newly lets EVERY agent
     * that uses the service call, without `allow_write` and without a
     * record in the audit (`newReadOperations`). Beside `pendingWrites`,
     * not in it: who it reaches and what it means are other sentences.
     */
    pendingReads(
        stored: ODataDefinition | undefined | null, current: ODataDefinition | undefined | null
    ): ODataNewOperation[] {
        return newReadOperations(stored, current);
    },

    /** The operations of `list` that `other` does not name. */
    operationsMinus(list: readonly ODataNewOperation[], other: readonly ODataNewOperation[]): ODataNewOperation[] {
        const names = other.map((operation) => operation.name);
        return list.filter((operation) => names.indexOf(operation.name) === -1);
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

    importPlan,
    mergeImport,
    importSummary,
    importRows,
    importLabel,

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
     * credential and names the system -- and where their calls go: the
     * service path and the OData version. A changed path or version sends
     * the reads and writes of every agent that uses the service to another
     * SAP service, so it is asked about like a changed destination.
     */
    identityChange(stored: ODataServiceInput, current: ODataServiceInput): ODataIdentityChange {
        const before = stored.user_context === true;
        const after = current.user_context === true;
        return {
            runsAs: before === after ? null : (after ? "user" : "technical"),
            destination: stored.destination === current.destination
                ? null : { from: stored.destination, to: current.destination },
            servicePath: stored.service_path === current.service_path
                ? null : { from: stored.service_path, to: current.service_path },
            version: stored.odata_version === current.odata_version
                ? null : { from: stored.odata_version, to: current.odata_version }
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

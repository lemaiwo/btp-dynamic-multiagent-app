import type {
    ODataDefinition, ODataDuplicateRequest, ODataEntityOp, ODataEntitySet, ODataOperation, ODataServiceInput,
    ODataUsedBy
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

/** Everything a save would newly let agents with `allow_write` do. */
export interface ODataPending {
    entitySets: ODataNewWrite[];
    operations: ODataNewOperation[];
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
     * there). `switchedOn`: the save also switches the stored, disabled
     * service on, which opens every write it has.
     */
    pendingWrites(
        stored: ODataDefinition | undefined | null, current: ODataDefinition | undefined | null,
        switchedOn = false
    ): ODataPending {
        return {
            entitySets: newEntityWrites(stored, current, switchedOn),
            operations: newWriteOperations(stored, current, switchedOn)
        };
    },

    /** How many write operations `pending` holds: each ticked operation of
     *  each entity set, and each function import or action. */
    pendingCount(pending: ODataPending): number {
        return pending.entitySets.reduce((sum, write) => sum + write.operations.length, 0) + pending.operations.length;
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
        return { entitySets, operations: pending.operations.filter((operation) => names.indexOf(operation.name) === -1) };
    },

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

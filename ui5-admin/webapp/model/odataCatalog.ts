import type {
    ODataDefinition, ODataEntityOp, ODataEntitySet, ODataServiceInput
} from "../service/types";

/**
 * Pure logic for the OData catalogue pages: what a service's definition adds
 * up to, the empty form, and the client-side mirror of the server's rules.
 *
 * No `sap.m` in here, so all of it is unit-testable. The server
 * (`agents/odata/models.py`) stays authoritative: whatever `validate` misses
 * is still refused there and surfaced through `AdminError.fieldErrors`.
 */

/** Entity-set operations in the server's order (`ENTITY_OPS`). */
export const ENTITY_OPS: readonly ODataEntityOp[] = ["list", "get", "create", "update", "delete"];

/** The entity-set operations an agent gets only with `allow_write` (`WRITE_OPS`). */
export const WRITE_OPS: readonly ODataEntityOp[] = ["create", "update", "delete"];

// Mirrors of SERVICE_NAME_RE and DESTINATION_NAME_RE in agents/odata/models.py.
const SERVICE_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$/;
const DESTINATION_NAME_RE = /^[A-Za-z0-9_.-]{1,200}$/;

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

/** The form fields `validate` can report on. */
export type ODataErrorField = "name" | "title" | "purpose" | "not_for" | "destination" | "service_path";

/** Field -> i18n key of what is wrong with it. Empty means valid. */
export type ODataErrors = Partial<Record<ODataErrorField, string>>;

function hasControlCharacter(value: string): boolean {
    for (let i = 0; i < value.length; i++) {
        const code = value.charCodeAt(i);
        if (code < 0x20 || code === 0x7f) {
            return true;
        }
    }
    return false;
}

/**
 * Whether `path` is a service path the server accepts: it starts with one
 * `/`, has no `..`, `//`, `?`, `#`, backslash or control character, does not
 * end in `/` and is at most 512 characters. Starting with `/` and having no
 * `//` is what rules out a scheme or a host: those always come from the
 * destination, never from here.
 */
function isConfinedPath(path: string): boolean {
    return path.length > 1
        && path.length <= MAX_SERVICE_PATH
        && path.charAt(0) === "/"
        && path.charAt(path.length - 1) !== "/"
        && path.indexOf("..") === -1
        && path.indexOf("//") === -1
        && !/[?#\\]/.test(path)
        && !hasControlCharacter(path);
}

function writeOpsOf(entitySet: ODataEntitySet): ODataEntityOp[] {
    const enabled = entitySet.operations ?? [];
    return WRITE_OPS.filter((op) => enabled.indexOf(op) !== -1);
}

function titleOf(entitySet: ODataEntitySet): string {
    return (entitySet.title ?? "").trim() || entitySet.name;
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
     * that changes data. Same rule as `has_write` in the server's answer.
     */
    hasWrite(definition: ODataDefinition | undefined | null): boolean {
        return (definition?.entity_sets ?? []).some((e) => writeOpsOf(e).length > 0)
            || (definition?.operations ?? []).some((o) => o.enabled && o.changes_data);
    },

    /**
     * What `allow_write` opens, one entry per entity set with a write
     * operation -- "Item text (create, update)" -- followed by the enabled
     * operations that change data, by title.
     */
    writeSummary(definition: ODataDefinition | undefined | null): string[] {
        const entitySets = (definition?.entity_sets ?? [])
            .filter((e) => writeOpsOf(e).length > 0)
            .map((e) => `${titleOf(e)} (${writeOpsOf(e).join(", ")})`);
        const operations = (definition?.operations ?? [])
            .filter((o) => o.enabled && o.changes_data)
            .map((o) => (o.title ?? "").trim() || o.name);
        return entitySets.concat(operations);
    },

    /** How many of an entity set's fields an agent can read, of how many. */
    fieldCount(entitySet: ODataEntitySet): ODataFieldCount {
        const fields = entitySet.fields ?? [];
        return { selected: fields.filter((f) => f.selectable).length, total: fields.length };
    },

    /** The business title of an entity set, or its technical name. */
    titleOf,

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
        const name = input.name ?? "";
        const title = (input.title ?? "").trim();
        const purpose = (input.purpose ?? "").trim();
        const destination = input.destination ?? "";
        const path = input.service_path ?? "";

        if (!name) {
            errors.name = "odataErrNameRequired";
        } else if (!SERVICE_NAME_RE.test(name)) {
            errors.name = "odataErrNameInvalid";
        }
        if (!title) {
            errors.title = "odataErrTitleRequired";
        } else if (title.length > MAX_TITLE) {
            errors.title = "odataErrTitleTooLong";
        }
        if (!purpose) {
            errors.purpose = "odataErrPurposeRequired";
        } else if (purpose.length > MAX_PURPOSE) {
            errors.purpose = "odataErrPurposeTooLong";
        }
        if ((input.not_for ?? "").length > MAX_NOT_FOR) {
            errors.not_for = "odataErrNotForTooLong";
        }
        if (!destination) {
            errors.destination = "odataErrDestinationRequired";
        } else if (!DESTINATION_NAME_RE.test(destination)) {
            errors.destination = "odataErrDestinationInvalid";
        }
        if (!path) {
            errors.service_path = "odataErrPathRequired";
        } else if (!isConfinedPath(path)) {
            errors.service_path = "odataErrPathInvalid";
        }
        return errors;
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

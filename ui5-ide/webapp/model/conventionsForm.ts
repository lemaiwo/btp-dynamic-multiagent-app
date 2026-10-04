import type { Conventions, ConventionsClearable, ConventionsCreate } from "../service/types";

/**
 * The conventions dialog's form logic (Task U12), free of controls.
 *
 * - The target rule is the server's `schemas.TARGET_PATTERN`: a letter or
 *   digit, then up to 63 of `[A-Za-z0-9_.-]`.
 * - A Save sends only what changed against the baseline: the row as the form
 *   last loaded it (`resync` moves the baseline to a fresh row and keeps the
 *   user's edits), never a row another write replaced meanwhile. Values go out
 *   trimmed. A field emptied in the form (by its clear button or by deleting
 *   the text; blank counts as empty) is an explicit `clear`, never an empty
 *   string value, so the server never sees a field both set and cleared; a
 *   stored blank value can be cleared too. `clean_core_level` cannot be cleared.
 * - Neither a Save nor a create ever carries `non_production`: the flag goes
 *   out only through its own confirmed request.
 * - Values are plain text throughout: nothing here (or in the dialog, which
 *   binds them as values) interprets `{...}`.
 */

/** EventBus channel/event the pages listen to after a conventions write (targets or flags changed). */
export const CONVENTIONS_CHANNEL = "ide";
export const CONVENTIONS_CHANGED = "conventionsChanged";

/**
 * What a conventions write tells the pages: the one target the server just
 * answered for, and its flag. A page merges it into what it read itself
 * (its list may be newer than the dialog's).
 */
export interface ConventionsChangedData {
    target: string;
    nonProduction: boolean;
}

export const TARGET_MAX = 64;
const TARGET_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

export const CLEARABLE: readonly ConventionsClearable[] = ["label", "destination", "namespace", "package", "atc_variant", "free_text"];
export const FIELDS = [...CLEARABLE, "clean_core_level"] as const;
export type Field = typeof FIELDS[number];
export type Form = Record<Field, string>;

export function isValidTarget(name: string): boolean {
    return TARGET_PATTERN.test(name);
}

/** The stored row as form strings (null and missing are ""). */
export function formOf(row: Conventions | undefined): Form {
    const form = {} as Form;
    for (const key of FIELDS) {
        const value = row?.[key];
        form[key] = typeof value === "string" ? value : "";
    }
    return form;
}

const isEmpty = (value: string | undefined): boolean => !value || !value.trim();

/** `PUT /conventions/{target}`: the fields changed against `base`, trimmed, and the fields to clear. */
export function updateBody(base: Form, form: Form): { body: Partial<Record<Field, string>>; clear: ConventionsClearable[] } {
    const body: Partial<Record<Field, string>> = {};
    const clear: ConventionsClearable[] = [];
    for (const key of FIELDS) {
        const raw = form[key] ?? "";
        const before = base[key] ?? "";
        if (raw === before) {
            // Untouched (a stored blank value included).
            continue;
        }
        const value = raw.trim();
        if (!value) {
            if ((CLEARABLE as readonly string[]).includes(key) && before !== "") {
                clear.push(key as ConventionsClearable);
            }
            continue;
        }
        if (value !== before) {
            body[key] = value;
        }
    }
    return { body, clear };
}

/**
 * After another write answered with `fresh` (the flag, a re-read): untouched
 * fields take the fresh values, edited ones keep the user's text, and the
 * fresh row becomes the baseline, so a later Save sends only the user's edits.
 */
export function resync(base: Form, form: Form, fresh: Conventions): { base: Form; form: Form } {
    const next = formOf(fresh);
    const merged = { ...next };
    for (const key of FIELDS) {
        if ((form[key] ?? "") !== (base[key] ?? "")) {
            merged[key] = form[key];
        }
    }
    return { base: next, form: merged };
}

/** `POST /conventions`: the trimmed target and the filled fields, trimmed; never the flag. */
export function createBody(target: string, form: Form): ConventionsCreate {
    const body: ConventionsCreate = { target: target.trim() };
    for (const key of FIELDS) {
        if (!isEmpty(form[key])) {
            body[key] = form[key].trim();
        }
    }
    return body;
}

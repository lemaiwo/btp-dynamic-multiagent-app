import type { InvokeLimits, JsonSchema } from "../service/types";

/**
 * The agent form's "Invoke API" panel: the result schema and the attachment
 * limits as the form edits them (text), and back into what the agent PUT /
 * POST carries. Mirrors `agents/invoke_config.py` (`clean_limits`) and adds
 * no rule of its own beyond "valid JSON, one object": the schema subset is
 * the server's to check, and its 422 names the field.
 *
 * Refusals are i18n keys (with arguments), never a text that quotes what
 * was typed: a schema or a limit is not echoed back.
 */

/** The three media types the server can recognise by their magic bytes;
 *  also its default when `media_types` is absent. */
export const INVOKE_MEDIA_TYPES = ["image/png", "image/jpeg", "application/pdf"];

/** The server's defaults (`DEFAULT_*`), shown as placeholders. */
export const DEFAULT_MAX_FILE_MB = 10;
export const DEFAULT_MAX_FILES = 5;
export const DEFAULT_MAX_PDF_PAGES = 20;

/** The server's ceilings (`CEILING_*`): a value above is refused there. */
export const CEILING_MAX_FILE_MB = 25;
export const CEILING_MAX_FILES = 10;
export const CEILING_MAX_PDF_PAGES = 100;

const MB = 1024 * 1024;

/** The panel's state, all text: a blank field means "the server's default". */
export interface InvokeForm {
    schemaText: string;
    mediaTypes: string[];
    maxFileMb: string;
    maxFiles: string;
    maxPdfPages: string;
    /** The stored `max_file_bytes` and the text it was shown as: while the
     *  field still holds that text, Save sends the stored bytes unchanged
     *  (the shown MB value is rounded). Absent for a new or blank limit. */
    loadedFileBytes?: number;
    loadedFileMbText?: string;
}

/** Decimals of the shown MB value: 6 shows even 1 byte (0.000001). */
const MB_DECIMALS = 6;

/** Bytes as MB text with at most MB_DECIMALS decimals, never in exponent
 *  notation (10485760 is "10", 1 is "0.000001"). */
export function formatMb(bytes: number): string {
    const text = (bytes / MB).toFixed(MB_DECIMALS).replace(/\.?0+$/, "");
    return text === "0" || text === "" ? (1 / 10 ** MB_DECIMALS).toFixed(MB_DECIMALS) : text;
}

/** The field a refusal belongs to, so the controller can mark its control. */
export type InvokeField = "result_schema" | "media_types" | "max_file_mb" | "max_files" | "max_pdf_pages";

export interface InvokeRefusal {
    key: string;
    args?: string[];
}

export interface InvokeParsed {
    result_schema: JsonSchema | null;
    invoke_limits: InvokeLimits | null;
    /** Empty when the form can be sent. */
    errors: Partial<Record<InvokeField, InvokeRefusal>>;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
    return !!value && typeof value === "object" && !Array.isArray(value);
}

/** The panel's fields for what the server answered (null = blank). */
export function toForm(schema: JsonSchema | null | undefined, limits: InvokeLimits | null | undefined): InvokeForm {
    const l = isPlainObject(limits) ? limits : {};
    const bytes = typeof l.max_file_bytes === "number" ? l.max_file_bytes : undefined;
    const mbText = bytes !== undefined ? formatMb(bytes) : "";
    return {
        schemaText: isPlainObject(schema) ? JSON.stringify(schema, null, 2) : "",
        mediaTypes: Array.isArray(l.media_types) ? l.media_types.filter((t) => typeof t === "string") : [],
        // Rounded for display; an untouched field sends `loadedFileBytes`.
        maxFileMb: mbText,
        maxFiles: typeof l.max_files === "number" ? String(l.max_files) : "",
        maxPdfPages: typeof l.max_pdf_pages === "number" ? String(l.max_pdf_pages) : "",
        ...(bytes !== undefined ? { loadedFileBytes: bytes, loadedFileMbText: mbText } : {})
    };
}

/** The media type options: the three the server knows, plus any stored type
 *  outside them (so a selection is never dropped unseen). */
export function mediaTypeOptions(selected: string[]): string[] {
    return INVOKE_MEDIA_TYPES.concat(selected.filter((t) => INVOKE_MEDIA_TYPES.indexOf(t) === -1));
}

function count(text: string, ceiling: number): number | null {
    if (!/^\d+$/.test(text)) {
        return null;
    }
    const n = Number(text);
    return n >= 1 && n <= ceiling ? n : null;
}

/**
 * What the form sends. A blank schema is `null`; every blank limit is left
 * out, and no limit at all is `invoke_limits: null` (the server's defaults).
 */
export function fromForm(form: InvokeForm): InvokeParsed {
    const errors: InvokeParsed["errors"] = {};

    let schema: JsonSchema | null = null;
    const schemaText = (form.schemaText ?? "").trim();
    if (schemaText) {
        let parsed: unknown;
        try {
            parsed = JSON.parse(schemaText);
        } catch {
            errors.result_schema = { key: "invokeSchemaInvalidJson" };
        }
        if (!errors.result_schema) {
            if (isPlainObject(parsed)) {
                schema = parsed;
            } else {
                errors.result_schema = { key: "invokeSchemaNotObject" };
            }
        }
    }

    const limits: InvokeLimits = {};
    const types = (form.mediaTypes ?? []).filter((t) => typeof t === "string" && t !== "");
    if (types.length > 0) {
        // In the order of the options, once each, as the server stores them.
        limits.media_types = mediaTypeOptions(types).filter((t) => types.indexOf(t) !== -1);
    }

    const mb = (form.maxFileMb ?? "").trim();
    if (mb && typeof form.loadedFileBytes === "number" && mb === form.loadedFileMbText) {
        limits.max_file_bytes = form.loadedFileBytes;
    } else if (mb) {
        const value = /^\d+(\.\d+)?$/.test(mb) ? Number(mb) : NaN;
        const bytes = Math.round(value * MB);
        if (!(value > 0) || value > CEILING_MAX_FILE_MB || bytes < 1) {
            errors.max_file_mb = { key: "invokeMaxFileMbInvalid", args: [String(CEILING_MAX_FILE_MB)] };
        } else {
            limits.max_file_bytes = bytes;
        }
    }

    const numbers: [InvokeField, keyof InvokeLimits, string, number, string][] = [
        ["max_files", "max_files", form.maxFiles, CEILING_MAX_FILES, "invokeMaxFilesInvalid"],
        ["max_pdf_pages", "max_pdf_pages", form.maxPdfPages, CEILING_MAX_PDF_PAGES, "invokeMaxPdfPagesInvalid"]
    ];
    numbers.forEach(([field, key, raw, ceiling, refusal]) => {
        const text = (raw ?? "").trim();
        if (!text) {
            return;
        }
        const n = count(text, ceiling);
        if (n === null) {
            errors[field] = { key: refusal, args: [String(ceiling)] };
        } else {
            (limits as Record<string, number>)[key] = n;
        }
    });

    return {
        result_schema: schema,
        invoke_limits: Object.keys(limits).length > 0 ? limits : null,
        errors
    };
}

export default { toForm, fromForm, mediaTypeOptions };

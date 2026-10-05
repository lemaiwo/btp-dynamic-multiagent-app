/**
 * A JSON-schema subset validator for the contract test (FakeBackend against
 * the backend's response models in `test/contract/ide-api.schema.json`).
 *
 * Covers what the exported pydantic schemas use: `type` (also a list),
 * `required`, `properties`, `additionalProperties` (false or a schema),
 * `enum`, `const`, `items`, `anyOf`, `oneOf`, `$ref` into `#/$defs/`,
 * `minLength`/`maxLength`, `maxItems`, `maxProperties`, `minimum`,
 * `pattern`. Annotations (`title`, `description`, `default`, `examples`,
 * `discriminator`, `$defs`) are ignored. Any other keyword (`allOf`, `not`,
 * `format`, `minItems`, a tuple `items`, ...) throws "unsupported keyword X
 * at <schema path>" instead of passing silently: a schema the validator
 * cannot judge is a test failure, never a green check. `assertSupported`
 * walks a whole schema document up front. Errors are path-qualified
 * (`$.files[0].path`).
 */

export type Schema = Record<string, unknown>;

export interface SchemaDoc {
    $defs: Record<string, Schema>;
}

function typeOf(value: unknown): string {
    if (value === null) {
        return "null";
    }
    if (Array.isArray(value)) {
        return "array";
    }
    return typeof value;
}

function matchesType(value: unknown, type: string): boolean {
    switch (type) {
        case "null": return value === null;
        case "array": return Array.isArray(value);
        case "object": return typeof value === "object" && value !== null && !Array.isArray(value);
        case "integer": return typeof value === "number" && Number.isInteger(value);
        case "number": return typeof value === "number" && Number.isFinite(value);
        case "string": return typeof value === "string";
        case "boolean": return typeof value === "boolean";
        default: return false;
    }
}

function equal(a: unknown, b: unknown): boolean {
    return JSON.stringify(a) === JSON.stringify(b);
}

/** The keywords `check` implements. */
const KNOWN: ReadonlySet<string> = new Set([
    "$ref", "type", "enum", "const", "anyOf", "oneOf", "minLength", "maxLength", "pattern", "minimum",
    "maxItems", "items", "properties", "required", "additionalProperties", "maxProperties"
]);

/** Keywords that do not constrain a value; never descended into (`discriminator` only steers `oneOf`). */
const ANNOTATIONS: ReadonlySet<string> = new Set(["title", "description", "default", "examples", "discriminator", "$defs"]);

/** Throws for a keyword that is neither implemented nor an annotation, or an implemented one in a shape `check` does not handle. */
function guard(schema: Schema, at: string): void {
    for (const keyword of Object.keys(schema)) {
        if (!KNOWN.has(keyword) && !ANNOTATIONS.has(keyword)) {
            throw new Error(`unsupported keyword ${keyword} at ${at}`);
        }
    }
    if (Array.isArray(schema.items) || ("items" in schema && (typeof schema.items !== "object" || schema.items === null))) {
        throw new Error(`unsupported keyword items (tuple form) at ${at}`);
    }
    if ("additionalProperties" in schema && typeof schema.additionalProperties !== "boolean"
        && (typeof schema.additionalProperties !== "object" || schema.additionalProperties === null)) {
        throw new Error(`unsupported keyword additionalProperties (not a boolean or schema) at ${at}`);
    }
    if ("$ref" in schema && !/^#\/\$defs\/.+$/.test(String(schema.$ref))) {
        throw new Error(`unsupported keyword $ref (not into #/$defs/) at ${at}`);
    }
}

/**
 * Walks every schema of `doc.$defs` (and every subschema reachable without
 * a `$ref`) and throws on the first keyword `check` would not judge.
 */
export function assertSupported(doc: SchemaDoc): void {
    const walk = (schema: Schema, at: string): void => {
        guard(schema, at);
        for (const [name, sub] of Object.entries((schema.properties ?? {}) as Record<string, Schema>)) {
            walk(sub, `${at}/properties/${name}`);
        }
        if (schema.items && typeof schema.items === "object") {
            walk(schema.items as Schema, `${at}/items`);
        }
        if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
            walk(schema.additionalProperties as Schema, `${at}/additionalProperties`);
        }
        for (const key of ["anyOf", "oneOf"]) {
            ((schema[key] ?? []) as Schema[]).forEach((sub, i) => walk(sub, `${at}/${key}/${i}`));
        }
    };
    for (const [name, schema] of Object.entries(doc.$defs)) {
        walk(schema, `#/$defs/${name}`);
    }
}

function resolve(doc: SchemaDoc, ref: string): Schema {
    const m = /^#\/\$defs\/(.+)$/.exec(ref);
    const found = m ? doc.$defs[m[1]] : undefined;
    if (!found) {
        throw new Error(`Unresolvable $ref ${ref}`);
    }
    return found;
}

function check(doc: SchemaDoc, schema: Schema, value: unknown, path: string, errors: string[]): void {
    guard(schema, path);
    if (typeof schema.$ref === "string") {
        check(doc, resolve(doc, schema.$ref), value, path, errors);
    }
    if (schema.type !== undefined) {
        const types = Array.isArray(schema.type) ? schema.type as string[] : [schema.type as string];
        if (!types.some((t) => matchesType(value, t))) {
            errors.push(`${path}: expected ${types.join("|")}, got ${typeOf(value)}`);
            return;
        }
    }
    if (Array.isArray(schema.enum) && !schema.enum.some((e) => equal(e, value))) {
        errors.push(`${path}: ${JSON.stringify(value)} is not one of ${JSON.stringify(schema.enum)}`);
    }
    if ("const" in schema && !equal(schema.const, value)) {
        errors.push(`${path}: expected ${JSON.stringify(schema.const)}`);
    }
    if (Array.isArray(schema.anyOf)) {
        const ok = (schema.anyOf as Schema[]).some((s) => validateAgainst(doc, s, value, path).length === 0);
        if (!ok) {
            const why = (schema.anyOf as Schema[]).map((s) => validateAgainst(doc, s, value, path).join("; ")).join(" | ");
            errors.push(`${path}: matches no anyOf branch (${why})`);
        }
    }
    if (Array.isArray(schema.oneOf)) {
        const hits = (schema.oneOf as Schema[]).filter((s) => validateAgainst(doc, s, value, path).length === 0).length;
        if (hits !== 1) {
            errors.push(`${path}: matches ${hits} oneOf branches, expected exactly 1`);
        }
    }
    if (typeof value === "string") {
        const length = Array.from(value).length;
        if (typeof schema.minLength === "number" && length < schema.minLength) {
            errors.push(`${path}: shorter than ${schema.minLength}`);
        }
        if (typeof schema.maxLength === "number" && length > schema.maxLength) {
            errors.push(`${path}: longer than ${schema.maxLength}`);
        }
        if (typeof schema.pattern === "string" && !new RegExp(schema.pattern, "u").test(value)) {
            errors.push(`${path}: does not match ${schema.pattern}`);
        }
    }
    if (typeof value === "number" && typeof schema.minimum === "number" && value < schema.minimum) {
        errors.push(`${path}: below ${schema.minimum}`);
    }
    if (Array.isArray(value)) {
        if (typeof schema.maxItems === "number" && value.length > schema.maxItems) {
            errors.push(`${path}: more than ${schema.maxItems} items`);
        }
        if (schema.items && typeof schema.items === "object") {
            value.forEach((item, i) => check(doc, schema.items as Schema, item, `${path}[${i}]`, errors));
        }
    }
    if (matchesType(value, "object")) {
        const obj = value as Record<string, unknown>;
        const props = (schema.properties ?? {}) as Record<string, Schema>;
        if (typeof schema.maxProperties === "number" && Object.keys(obj).length > schema.maxProperties) {
            errors.push(`${path}: more than ${schema.maxProperties} properties`);
        }
        for (const name of (schema.required ?? []) as string[]) {
            if (!Object.prototype.hasOwnProperty.call(obj, name)) {
                errors.push(`${path}.${name}: required, missing`);
            }
        }
        for (const [name, v] of Object.entries(obj)) {
            if (Object.prototype.hasOwnProperty.call(props, name)) {
                check(doc, props[name], v, `${path}.${name}`, errors);
            } else if (schema.additionalProperties === false) {
                errors.push(`${path}.${name}: not allowed`);
            } else if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
                check(doc, schema.additionalProperties as Schema, v, `${path}.${name}`, errors);
            }
        }
    }
}

function validateAgainst(doc: SchemaDoc, schema: Schema, value: unknown, path: string): string[] {
    const errors: string[] = [];
    check(doc, schema, value, path, errors);
    return errors;
}

/**
 * Validates `value` against `#/$defs/<model>` (or an array of it with
 * `model[]`); answers the errors, empty when it conforms.
 */
export function validate(doc: SchemaDoc, model: string, value: unknown): string[] {
    const list = model.endsWith("[]");
    const name = list ? model.slice(0, -2) : model;
    const ref: Schema = { $ref: `#/$defs/${name}` };
    return validateAgainst(doc, list ? { type: "array", items: ref } : ref, value, "$");
}

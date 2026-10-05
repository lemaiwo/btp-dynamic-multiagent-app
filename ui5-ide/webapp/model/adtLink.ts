/**
 * "Open in ADT" links (contract §1.7, assumption A3): an `adt://` URI that
 * Eclipse ADT opens in the project named like the session target.
 *
 *   adt://<target>/sap/bc/adt/<path>/source/main[#start=<line>,0]
 *   adt://<target>/sap/bc/adt/oo/classes/<name>/includes/<include>[#start=<line>,0]
 *
 * The second form is a class include (test classes, local definitions and
 * implementations, macros): the line belongs to that include, so the link
 * opens it, not the class's main source. An include ADT has no URI for
 * gives no link.
 *
 * Every part is validated before it is placed: the target against the
 * server's target pattern, the type against a closed map, the name against
 * the ABAP object name shape (optional `/NAMESPACE/` prefix). Anything that
 * does not validate gives `null` (no link, the button is hidden); the only
 * scheme ever emitted is `adt:`.
 */

/** The server's `TARGET_PATTERN` (`agents/ide/schemas.py`). */
const TARGET_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
/** `[/NAMESPACE/]NAME`, letters, digits and `_`. */
const NAME_RE = /^(?:\/[A-Za-z0-9_]{1,20}\/)?[A-Za-z0-9_]{1,40}$/;

const ADT_PATHS: Record<string, string> = {
    CLAS: "oo/classes",
    INTF: "oo/interfaces",
    PROG: "programs/programs",
    INCL: "programs/includes",
    DDLS: "ddic/ddl/sources",
    DCLS: "acm/dcl/sources",
    DDLX: "ddic/ddlx/sources",
    BDEF: "bo/behaviordefinitions",
    SRVD: "ddic/srvd/sources"
};

/** abapGit's class include file part -> the ADT include name. */
const CLASS_INCLUDES: Record<string, string> = {
    testclasses: "testclasses",
    locals_def: "definitions",
    locals_imp: "implementations",
    macros: "macros"
};

/**
 * The class include of an abapGit path (`src/CLAS/<name>.clas.<include>.abap`),
 * as its file part (`testclasses`, `locals_def`, ...); `null` for a class's
 * main source and for any other path.
 */
export function classIncludeOf(path: string): string | null {
    const m = /^src\/CLAS\/[^/]+?\.clas\.([a-z_]+)\.abap$/.exec(typeof path === "string" ? path : "");
    return m ? m[1] : null;
}

/**
 * The ADT URI of an object's main source, or of a class include when
 * `include` is given; `null` when any part does not validate (FUNC: no
 * link) or the include is not one ADT can open.
 */
export function adtUri(
    target: string, type: string, name: string, line?: number | null, include?: string | null
): string | null {
    if (typeof target !== "string" || typeof type !== "string" || typeof name !== "string") {
        return null;
    }
    if (!TARGET_RE.test(target) || !NAME_RE.test(name)) {
        return null;
    }
    const base = Object.prototype.hasOwnProperty.call(ADT_PATHS, type) ? ADT_PATHS[type] : null;
    if (!base) {
        return null;
    }
    let fragment = "";
    if (line !== undefined && line !== null) {
        if (typeof line !== "number" || !Number.isSafeInteger(line) || line < 1) {
            return null;
        }
        fragment = `#start=${line},0`;
    }
    let tail = "source/main";
    if (include !== undefined && include !== null) {
        const adtInclude = type === "CLAS" && typeof include === "string"
            && Object.prototype.hasOwnProperty.call(CLASS_INCLUDES, include) ? CLASS_INCLUDES[include] : null;
        if (!adtInclude) {
            return null;
        }
        tail = `includes/${adtInclude}`;
    }
    const encodedName = name.toLowerCase().split("/").map(encodeURIComponent).join("%2f");
    return `adt://${encodeURIComponent(target)}/sap/bc/adt/${base}/${encodedName}/${tail}${fragment}`;
}

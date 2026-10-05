import type { ODataEntitySet, ODataServiceInput } from "com/agent/admin/service/types";

/**
 * One table of refused input for the general fields of a catalogue service,
 * used twice: `odataCatalog.validate` must name `clientKey` for the field,
 * and the fake backend must answer the 422 the real one answers
 * (`server`, the text `validate_odata_service` in agents/odata/models.py
 * builds: "<loc>: <msg>"). The two can then not drift apart unnoticed.
 */
export interface RuleCase {
    rule: string;
    field: "name" | "title" | "purpose" | "not_for" | "destination" | "service_path"
        | "user_context" | "enabled" | "definition";
    /** What the field is set to. `undefined` leaves the key out of the JSON
     *  body, which is how a missing field reaches the server. */
    value: unknown;
    clientKey: string;
    server: string;
}

export const VALID_INPUT: ODataServiceInput = {
    name: "purchase-requisitions", title: "Purchase requisitions",
    purpose: "Read requisitions and their items to judge an approval", not_for: "",
    destination: "S4_ODATA_USER", user_context: true, odata_version: "v2",
    service_path: "/sap/opu/odata/sap/SRV", enabled: true,
    definition: { entity_sets: [], operations: [] }, metadata_fetched_at: null
};

const NAME_PATTERN = "name: String should match pattern '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$'";
const DESTINATION_PATTERN = "destination: String should match pattern '^[A-Za-z0-9_.-]{1,200}$'";

function path(rule: string, value: string, message: string): RuleCase {
    return {
        rule: `service path: ${rule}`, field: "service_path", value,
        clientKey: value ? "odataErrPathInvalid" : "odataErrPathRequired",
        server: `service_path: Value error, ${message}`
    };
}

const CONTROL = "service_path must not contain control characters";
const WHITESPACE = "service_path must not contain whitespace";
const NOT_A_URL = "service_path is a path, not a URL; the host comes from the destination";
const NO_QUERY = "service_path must not contain '?' or '#'; query parameters belong in the destination";
const NO_EMPTY_SEGMENT = "service_path must not contain '//'";
const NO_DOT_SEGMENT = "service_path must not contain '.' or '..' segments";

/** One case per rule of `confine_service_path` (agents/odata/urls.py). */
export const PATH_CASES: RuleCase[] = [
    path("empty", "", "service_path is required"),
    path("513 characters", "/" + "a".repeat(512), "service_path must be at most 512 characters"),
    path("a tab", "/sap/\topu", CONTROL),
    path("DEL", "/sap/op\u007fu", CONTROL),
    path("a C1 control character", "/sap/\u0085opu", CONTROL),
    path("a space inside", "/sap/ opu", WHITESPACE),
    path("a leading space (not trimmed)", " /sap/opu", WHITESPACE),
    path("a trailing space (not trimmed)", "/sap/opu ", WHITESPACE),
    path("a no-break space", "/sap/\u00a0opu", WHITESPACE),
    path("a backslash", "/sap\\opu", "service_path must not contain a backslash"),
    path("a URL", "https://s4.internal:44300/sap", NOT_A_URL),
    path("a URL further down", "/sap/http://s4.internal", NOT_A_URL),
    path("no leading slash", "sap/opu", "service_path must start with '/'"),
    path("a query", "/sap/opu?sap-client=100", NO_QUERY),
    path("a fragment", "/sap/opu#x", NO_QUERY),
    path("a percent sign", "/sap/%2e%2e/opu", "service_path must not contain '%'"),
    path("a trailing slash", "/sap/opu/", "service_path must not end with '/'"),
    path("only a slash", "/", "service_path must not end with '/'"),
    path("a host", "//s4.internal/sap", NO_EMPTY_SEGMENT),
    path("an empty segment", "/sap//opu", NO_EMPTY_SEGMENT),
    path("a '..' segment", "/sap/../etc", NO_DOT_SEGMENT),
    path("'..' inside a segment", "/sap/a..b", NO_DOT_SEGMENT),
    path("a '.' segment", "/sap/./opu", NO_DOT_SEGMENT),
    path("a last '.' segment", "/sap/opu/.", NO_DOT_SEGMENT)
];

export const VALID_PATHS: string[] = [
    "/sap/opu/odata/sap/API_SRV",
    "/sap/opu/odata/sap/API_SRV;v=0002",
    "/sap/opu/odata4/sap/api/srvd_a2x/sap/pr/0001",
    "/a",
    "/a.b/c-d_e",
    "/a:b",
    "/" + "a".repeat(511)
];

export const GENERAL_CASES: RuleCase[] = [
    { rule: "name: empty", field: "name", value: "", clientKey: "odataErrNameRequired", server: NAME_PATTERN },
    { rule: "name: not a slug", field: "name", value: "Not A Slug", clientKey: "odataErrNameInvalid", server: NAME_PATTERN },
    { rule: "name: 65 characters", field: "name", value: "a".repeat(65), clientKey: "odataErrNameInvalid", server: NAME_PATTERN },
    {
        rule: "title: blank", field: "title", value: "   ", clientKey: "odataErrTitleRequired",
        server: "title: String should have at least 1 character"
    },
    {
        rule: "title: 121 characters", field: "title", value: "x".repeat(121), clientKey: "odataErrTitleTooLong",
        server: "title: String should have at most 120 characters"
    },
    {
        rule: "purpose: blank", field: "purpose", value: " \n", clientKey: "odataErrPurposeRequired",
        server: "purpose: String should have at least 1 character"
    },
    {
        rule: "purpose: 201 characters", field: "purpose", value: "x".repeat(201), clientKey: "odataErrPurposeTooLong",
        server: "purpose: String should have at most 200 characters"
    },
    {
        rule: "not for: 201 characters", field: "not_for", value: "x".repeat(201), clientKey: "odataErrNotForTooLong",
        server: "not_for: String should have at most 200 characters"
    },
    {
        rule: "destination: empty", field: "destination", value: "", clientKey: "odataErrDestinationRequired",
        server: DESTINATION_PATTERN
    },
    {
        rule: "destination: a space", field: "destination", value: "has space",
        clientKey: "odataErrDestinationInvalid", server: DESTINATION_PATTERN
    }
];

function oneLine(field: "title" | "purpose" | "not_for", what: string, value: string, clientKey: string): RuleCase {
    return {
        rule: `${field}: ${what}`, field, value, clientKey,
        server: `${field}: Value error, ${field} must be one line of text without control characters`
    };
}

/**
 * `_one_line` in agents/odata/models.py: title, purpose and not_for are one
 * line each -- no control character (Unicode category Cc: tab, CR, LF, the
 * C1 range) and no line or paragraph separator (Zl, Zp). Title and purpose
 * are stripped first, so only a character INSIDE the text is refused there.
 */
export const ONE_LINE_CASES: RuleCase[] = [
    oneLine("title", "a tab", "Purchase\trequisitions", "odataErrTitleOneLine"),
    oneLine("title", "a C1 control character", "Purchase\u0085requisitions", "odataErrTitleOneLine"),
    oneLine("purpose", "a line feed inside", "Read requisitions\nand their items", "odataErrPurposeOneLine"),
    oneLine("purpose", "a carriage return inside", "Read requisitions\r\nand their items", "odataErrPurposeOneLine"),
    oneLine("purpose", "a line separator", "Read requisitions\u2028and their items", "odataErrPurposeOneLine"),
    oneLine("not_for", "a paragraph separator", "Purchase orders\u2029Contracts", "odataErrNotForOneLine"),
    oneLine("not_for", "a trailing line feed (not stripped)", "Purchase orders\n", "odataErrNotForOneLine"),
    oneLine("not_for", "a NUL", "Purchase\u0000orders", "odataErrNotForOneLine"),
    // U+001C..U+001F are no white space to the server: not stripped, refused.
    oneLine("title", "a trailing unit separator (not stripped)", "Purchase requisitions\u001f", "odataErrTitleOneLine"),
    oneLine("purpose", "a leading file separator (not stripped)", "\u001cRead requisitions", "odataErrPurposeOneLine")
];

/** Texts the one-line rule accepts: a no-break space is no control
 *  character, and what surrounds a title or purpose is stripped. */
export const ONE_LINE_ACCEPTED: Partial<ODataServiceInput>[] = [
    { title: "Purchase\u00a0requisitions" },
    { purpose: "  Read requisitions \n" },
    { title: "\tPurchase requisitions\r\n" },
    // The server strips Unicode White_Space, which is not JS trim(): U+0085
    // (NEL) goes, and U+FEFF stays and counts as text.
    { title: "Purchase requisitions\u0085" },
    { purpose: "\u2028Read requisitions\u3000" },
    { title: "\ufeff" }
];

function strictBoolean(field: "user_context" | "enabled", value: unknown): RuleCase {
    return {
        rule: `${field}: ${JSON.stringify(value)} is not a boolean`, field, value,
        clientKey: "odataErrBoolean", server: `${field}: Input should be a valid boolean`
    };
}

/** `StrictBool`: only a JSON boolean, never something that coerces to one. */
export const BOOLEAN_CASES: RuleCase[] = [
    strictBoolean("user_context", "true"), strictBoolean("user_context", "false"),
    strictBoolean("user_context", 1), strictBoolean("user_context", 0),
    strictBoolean("enabled", "true"), strictBoolean("enabled", "false"),
    strictBoolean("enabled", 1), strictBoolean("enabled", 0)
];

/** `definition` has no default: left out, the payload is refused instead of
 *  replacing the stored definition by an empty one. */
export const DEFINITION_CASES: RuleCase[] = [{
    rule: "definition: missing", field: "definition", value: undefined,
    clientKey: "odataErrDefinitionRequired", server: "definition: Field required"
}];

export const RULE_CASES: RuleCase[] = GENERAL_CASES.concat(PATH_CASES, ONE_LINE_CASES, BOOLEAN_CASES, DEFINITION_CASES);

export function withField(testCase: RuleCase): ODataServiceInput {
    return { ...VALID_INPUT, [testCase.field]: testCase.value } as ODataServiceInput;
}

// --- one entity set ----------------------------------------------------------

/**
 * One table of refused entity sets, used twice like `RULE_CASES`:
 * `odataCatalog.entitySetIssues` must name `clientKey` at `loc` first, and
 * the fake backend must answer the 422 the real one answers (`server`, as
 * `validate_odata_service` in agents/odata/models.py words it -- the texts
 * were taken from the real validator). What the entity set dialog checks
 * before Apply and what the fake refuses can then not drift apart.
 */
export interface EntityRuleCase {
    rule: string;
    /** Makes the valid entity set break the rule. */
    change: (entitySet: ODataEntitySet) => void;
    /** Where inside the entity set, as the server's `loc` ("" = the whole). */
    loc: string;
    clientKey: string;
    /** What follows "definition.entity_sets.0[.<loc>]: ". */
    server: string;
}

/** An entity set the server accepts: List, Get and Update, a key that is
 *  readable, one writable field, a navigation and an example query. */
export function validEntitySet(): ODataEntitySet {
    return {
        name: "A_Item", title: "Item", path: "", entity_type: "ItemType", description: "One item.",
        keys: [{ name: "Id", type: "Edm.String" }], operations: ["list", "get", "update"],
        fields: [
            {
                name: "Id", type: "Edm.String", label: "Id", selectable: true, filterable: true, writable: false,
                hint: "", values: [], personal_data: false
            },
            {
                name: "Status", type: "Edm.String", label: "Status", selectable: true, filterable: false,
                writable: true, hint: "", values: [{ value: "B", meaning: "open" }], personal_data: false
            }
        ],
        navigations: [{ name: "to_Text", target: "A_Text", collection: true, description: "" }],
        examples: [{ description: "Open items", filter: "Status eq 'B'", select: ["Id"], orderby: "", top: 5 }]
    };
}

const EDM = "String should match pattern '^[A-Za-z_][A-Za-z0-9_.]{0,127}$'";
const atMost = (n: number): string => `String should have at most ${n} characters`;
const AT_LEAST_ONE = "String should have at least 1 character";
const oneLineOf = (field: string): string => `Value error, ${field} must be one line of text without control characters`;

function entityRule(
    rule: string, loc: string, clientKey: string, server: string, change: (entitySet: ODataEntitySet) => void
): EntityRuleCase {
    return { rule, loc, clientKey, server, change };
}

export const ENTITY_CASES: EntityRuleCase[] = [
    entityRule("name: not an EDM name", "name", "odataErrEntityName", EDM, (e) => { e.name = "1x"; }),
    entityRule("title: 121 characters", "title", "odataErrTitleTooLong", atMost(120), (e) => { e.title = "x".repeat(121); }),
    entityRule("title: a tab", "title", "odataErrTitleOneLine", oneLineOf("title"), (e) => { e.title = "a\tb"; }),
    entityRule("description: 601 characters", "description", "odataErrEntityDescriptionTooLong", atMost(600), (e) => {
        e.description = "x".repeat(601);
    }),
    entityRule("key: not an EDM name", "keys.0.name", "odataErrKeyName", EDM, (e) => { e.keys[0].name = "a-b"; }),
    entityRule("fields: 501", "fields", "odataErrTooManyFields", "List should have at most 500 items after validation, not 501", (e) => {
        for (let i = 0; i < 499; i++) {
            e.fields.push({ ...e.fields[1], name: `F${i}`, values: [] });
        }
    }),
    entityRule("field: not an EDM name", "fields.1.name", "odataErrFieldName", EDM, (e) => { e.fields[1].name = "a b"; }),
    entityRule("field: no type", "fields.1.type", "odataErrFieldType", AT_LEAST_ONE, (e) => { e.fields[1].type = ""; }),
    entityRule("field: a type of 201 characters", "fields.1.type", "odataErrFieldType", atMost(200), (e) => {
        e.fields[1].type = "x".repeat(201);
    }),
    entityRule("label: 121 characters", "fields.1.label", "odataErrFieldLabelTooLong", atMost(120), (e) => {
        e.fields[1].label = "x".repeat(121);
    }),
    entityRule("label: a line break", "fields.1.label", "odataErrFieldLabelOneLine", oneLineOf("label"), (e) => {
        e.fields[1].label = "a\nb";
    }),
    entityRule("hint: 301 characters", "fields.1.hint", "odataErrFieldHintTooLong", atMost(300), (e) => {
        e.fields[1].hint = "x".repeat(301);
    }),
    entityRule("value meaning: no value", "fields.1.values.0.value", "odataErrFieldValues", AT_LEAST_ONE, (e) => {
        e.fields[1].values[0].value = "";
    }),
    entityRule("value meaning: a value of 65 characters", "fields.1.values.0.value", "odataErrFieldValues", atMost(64), (e) => {
        e.fields[1].values[0].value = "x".repeat(65);
    }),
    entityRule("value meaning: no meaning", "fields.1.values.0.meaning", "odataErrFieldValues", AT_LEAST_ONE, (e) => {
        e.fields[1].values[0].meaning = "";
    }),
    entityRule("value meaning: a meaning of 201 characters", "fields.1.values.0.meaning", "odataErrFieldValues", atMost(200), (e) => {
        e.fields[1].values[0].meaning = "x".repeat(201);
    }),
    entityRule("value meaning: a tab in the meaning", "fields.1.values.0.meaning", "odataErrFieldValues", oneLineOf("meaning"), (e) => {
        e.fields[1].values[0].meaning = "a\tb";
    }),
    entityRule(
        "field: filterable but not readable", "fields.1", "odataErrFilterNotSelectable",
        "Value error, field 'Status' is filterable but not selectable; a filterable field must also be selectable",
        (e) => { e.fields[1].selectable = false; e.fields[1].filterable = true; }
    ),
    entityRule("a field with a wrong value is not also checked as a whole", "fields.1.label", "odataErrFieldLabelTooLong", atMost(120), (e) => {
        e.fields[1].label = "x".repeat(121); e.fields[1].selectable = false; e.fields[1].filterable = true;
    }),
    entityRule("navigation: not an EDM name", "navigations.0.name", "odataErrNavigationName", EDM, (e) => {
        e.navigations[0].name = "a b";
    }),
    entityRule("navigation: no target", "navigations.0.target", "odataErrNavigationName", EDM, (e) => {
        e.navigations[0].target = "";
    }),
    entityRule("navigation: a description of 301 characters", "navigations.0.description", "odataErrNavigationDescription", atMost(300), (e) => {
        e.navigations[0].description = "x".repeat(301);
    }),
    entityRule("example: no description", "examples.0.description", "odataErrExampleDescriptionRequired", AT_LEAST_ONE, (e) => {
        e.examples[0].description = "";
    }),
    entityRule("example: a description of 201 characters", "examples.0.description", "odataErrExampleDescriptionTooLong", atMost(200), (e) => {
        e.examples[0].description = "x".repeat(201);
    }),
    entityRule("example: a filter of 1001 characters", "examples.0.filter", "odataErrExampleFilterTooLong", atMost(1000), (e) => {
        e.examples[0].filter = "x".repeat(1001);
    }),
    entityRule("example: a select entry of 129 characters", "examples.0.select.0", "odataErrExampleSelectTooLong", atMost(128), (e) => {
        e.examples[0].select = ["x".repeat(129)];
    }),
    entityRule("example: an orderby of 301 characters", "examples.0.orderby", "odataErrExampleOrderbyTooLong", atMost(300), (e) => {
        e.examples[0].orderby = "x".repeat(301);
    }),
    entityRule("example: top 0", "examples.0.top", "odataErrExampleTop", "Input should be greater than or equal to 1", (e) => {
        e.examples[0].top = 0;
    }),
    entityRule(
        "example: top 1.5", "examples.0.top", "odataErrExampleTop",
        "Input should be a valid integer, got a number with a fractional part", (e) => { e.examples[0].top = 1.5; }
    ),
    entityRule("a field listed twice", "", "odataErrDuplicateField", "Value error, duplicate field 'Status' in entity set 'A_Item'", (e) => {
        e.fields.push({ ...e.fields[1] });
    }),
    entityRule(
        "a navigation listed twice", "", "odataErrDuplicateNavigation",
        "Value error, duplicate navigation 'to_Text' in entity set 'A_Item'", (e) => { e.navigations.push({ ...e.navigations[0] }); }
    ),
    entityRule("a key listed twice", "", "odataErrDuplicateKey", "Value error, duplicate key 'Id' in entity set 'A_Item'", (e) => {
        e.keys.push({ ...e.keys[0] });
    }),
    entityRule(
        "a key that is no field", "", "odataErrKeyNotField",
        "Value error, key 'Other' of entity set 'A_Item' is not one of its fields", (e) => { e.keys[0].name = "Other"; }
    ),
    entityRule("Get without a key", "", "odataNeedsKey", "Value error, entity set 'A_Item' has 'get' but no key", (e) => {
        e.keys = [];
    }),
    entityRule(
        "List without a readable field", "", "odataNeedsSelectable",
        "Value error, entity set 'A_Item' has 'list' but no selectable field", (e) => {
            e.fields.forEach((f) => { f.selectable = false; f.filterable = false; });
        }
    ),
    entityRule(
        "Update without a writable field", "", "odataNeedsWritable",
        "Value error, entity set 'A_Item' has 'update' but no writable field", (e) => {
            e.fields.forEach((f) => { f.writable = false; });
        }
    ),
    entityRule(
        "Create without a writable field", "", "odataNeedsWritable",
        "Value error, entity set 'A_Item' has 'create' but no writable field", (e) => {
            e.operations = ["create"];
            e.fields.forEach((f) => { f.writable = false; });
        }
    )
];

/** Entity sets the server accepts although they look close to a rule. */
export const ENTITY_ACCEPTED: { rule: string; change: (entitySet: ODataEntitySet) => void }[] = [
    { rule: "a hint may have a line break", change: (e) => { e.fields[1].hint = "a\nb"; } },
    {
        rule: "Delete needs no writable field",
        change: (e) => { e.operations = ["delete"]; e.fields.forEach((f) => { f.writable = false; }); }
    },
    {
        rule: "a writable field need not be readable",
        change: (e) => { e.fields[1].selectable = false; e.fields[1].filterable = false; }
    },
    { rule: "no example, no navigation", change: (e) => { e.examples = []; e.navigations = []; } }
];

export function changed(change: (entitySet: ODataEntitySet) => void): ODataEntitySet {
    const entitySet = validEntitySet();
    change(entitySet);
    return entitySet;
}

export function withEntitySet(entitySet: ODataEntitySet): ODataServiceInput {
    return { ...VALID_INPUT, definition: { entity_sets: [entitySet], operations: [] } };
}

/** The 422 detail the server answers for `testCase` on the first entity set. */
export function entityRefusal(testCase: EntityRuleCase): string {
    return `definition.entity_sets.0${testCase.loc ? `.${testCase.loc}` : ""}: ${testCase.server}`;
}

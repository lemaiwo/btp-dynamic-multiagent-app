import type { ODataServiceInput } from "com/agent/admin/service/types";

/**
 * One table of refused input for the general fields of a catalogue service,
 * used twice: `odataCatalog.validate` must name `clientKey` for the field,
 * and the fake backend must answer the 422 the real one answers
 * (`server`, the text `validate_odata_service` in agents/odata/models.py
 * builds: "<loc>: <msg>"). The two can then not drift apart unnoticed.
 */
export interface RuleCase {
    rule: string;
    field: "name" | "title" | "purpose" | "not_for" | "destination" | "service_path";
    value: string;
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
    path("a no-break space", "/sap/ opu", WHITESPACE),
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

export const RULE_CASES: RuleCase[] = GENERAL_CASES.concat(PATH_CASES);

export function withField(testCase: RuleCase): ODataServiceInput {
    return { ...VALID_INPUT, [testCase.field]: testCase.value };
}

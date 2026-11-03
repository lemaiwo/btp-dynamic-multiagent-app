import { emptyForm, type InaField, type InaQueryForm } from "./inaQueries";
import type { InaDefinition } from "../service/inaTypes";

/**
 * The state of the analytical query detail page (`qry` model of
 * `InaQueryDetail.controller.ts`) and the constants its view needs.
 */

/** The kind of the strip under the toolbar (a `MessageStrip` type). */
export type ResultType = "Success" | "Information" | "Warning" | "Error";

/** The fields of the form that a refusal can name, in its order, with their ids. */
export const FORM_FIELDS: readonly { field: InaField; id: string }[] = [
    { field: "title", id: "inaDetailTitle" },
    { field: "purpose", id: "inaDetailPurpose" },
    { field: "not_for", id: "inaDetailNotFor" },
    { field: "destination", id: "inaDetailDestination" },
    { field: "query", id: "inaDetailQuery" },
    { field: "package", id: "inaDetailPackage" }
];

/** The words of a variable's type (`InaVariableType`). */
export const VARIABLE_TYPES: Record<string, string> = {
    single: "inaVariableSingle", interval: "inaVariableInterval", multiple: "inaVariableMultiple"
};

/** The model of the page (`qry`). */
export interface DetailState {
    loaded: boolean;
    /** A stored query is shown (false while loading and after a failed load). */
    exists: boolean;
    /** A save, describe, test or delete is on its way. */
    busy: boolean;
    /** A question is open: nothing is saved or sent until it is answered. */
    asking: boolean;
    title: string;
    /** What the form holds. */
    data: InaQueryForm;
    /** The query as it was loaded: the tags, the dirty check, the identity question. */
    stored: InaQueryForm;
    /** What a save sends back as `expected_updated_at`. */
    updated_at: string | null;
    usedBy: string[];
    definition: InaDefinition;
    describedAt: string | null;
    describedText: string;
    errors: Partial<Record<InaField, string>>;
    saveError: string;
    loadFailed: boolean;
    loadError: string;
    changedElsewhere: boolean;
    deletedElsewhere: boolean;
    /** The strip under the toolbar: what Test answered, or what Describe or Test has to say. */
    test: { type: ResultType; text: string } | null;
}

/** The page before anything is loaded: no form, nothing to save. */
export function blankDetail(): DetailState {
    return {
        loaded: false, exists: false, busy: false, asking: false, title: "",
        data: emptyForm(), stored: emptyForm(), updated_at: null, usedBy: [],
        definition: { dimensions: [], measures: [], variables: [] }, describedAt: null, describedText: "",
        errors: {}, saveError: "", loadFailed: false, loadError: "", changedElsewhere: false,
        deletedElsewhere: false, test: null
    };
}

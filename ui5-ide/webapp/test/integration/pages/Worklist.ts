import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Common from "./Common";
import { backend } from "./Common";

/** Worklist-page steps (Task U6). The page is the `sessions` route. */

export const OPTS = { viewName: "Worklist" };

/** The cell texts of a row, by the column's id suffix. */
export interface RowView {
    id: string;
    title: string;
    objects: string;
    type: string;
    system: string;
    stage: string;
    status: string;
    statusState: string;
    reason: string;
    changed: string;
}

/** Reads a row through its binding context (the row model is the view's `wl` model). */
export function rowOf(item: ColumnListItem): RowView {
    const ctx = item.getBindingContext("wl");
    const get = (p: string): string => String(ctx?.getProperty(p) ?? "");
    return {
        id: get("id"), title: get("title"), objects: get("objects"), type: get("typeText"), system: get("target"),
        stage: get("stageText"), status: get("statusText"), statusState: get("statusState"),
        reason: get("reason"), changed: get("changedText")
    };
}

export function rows(table: Table): RowView[] {
    return (table.getItems() as ColumnListItem[]).map(rowOf);
}

/**
 * Four more sessions beside the seeded "Explain the order class", one per
 * reason the server words: a pending trace approval, an addressed comment,
 * proposals not yet approved, a document version not yet approved.
 */
export function onePerReason(fake: typeof backend): void {
    fake.allowDiagnose();
    const diagnose = fake.addSession("Why is the order list slow?", [], [], "diagnose");
    fake.addApproval(diagnose.id);
    fake.dataOf(diagnose.id)!.findings.push(
        { id: "f-1", kind: "trace", ref_id: "TRC-7", title: "Slow order list", program: null, include: null,
            line: null, occurred_at: null, created_at: "2026-10-03T09:00:00Z" },
        { id: "f-2", kind: "dump", ref_id: "DUMP-1", title: "CX_SY_ZERODIVIDE", program: null, include: null,
            line: null, occurred_at: null, created_at: "2026-10-03T08:00:00Z" }
    );

    const commented = fake.addSession("Unit tests for tax determination");
    commented.stage = "design";
    fake.addArtifact(commented.id, "design");
    fake.dataOf(commented.id)!.comments.push({
        id: "c-answered", anchor: "document", path: null, revision: null, line_start: null, line_end: null,
        kind: "design", version: 1, paragraph: 0, body: "Use the released API.", state: "addressed",
        answer: "Switched to the released API.", created_at: "2026-10-03T10:00:00", updated_at: "2026-10-03T10:00:00"
    });

    const propose = fake.addSession("Round amounts by currency decimals");
    propose.stage = "propose";
    fake.addRevision(propose.id, "src/CLAS/zcl_price_calc.clas.abap", "CLASS zcl_price_calc DEFINITION.\nENDCLASS.");

    const plan = fake.addSession("New field on the order header");
    plan.stage = "plan";
    fake.addArtifact(plan.id, "plan");
}

export function iSeeRows(Then: Common, count: number, message: string): void {
    Then.waitFor({
        id: "worklistTable",
        ...OPTS,
        check: function (control: UI5Element) {
            return (control as Table).getItems().length === count;
        },
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The worklist does not show ${count} rows`
    });
}

/** Presses the radio button of the row titled `title` (the table is SingleSelectLeft). */
export function iSelectRow(When: Common, title: string): void {
    When.waitFor({
        id: "worklistTable",
        ...OPTS,
        check: function (control: UI5Element) {
            return rows(control as Table).some((r) => r.title === title);
        },
        success: function (this: Opa5, control: UI5Element) {
            const item = ((control as Table).getItems() as ColumnListItem[]).find((i) => rowOf(i).title === title)!;
            this.waitFor({
                controlType: "sap.m.RadioButton",
                ...OPTS,
                matchers: function (radio: UI5Element) {
                    let parent = radio.getParent();
                    while (parent && parent !== item) {
                        parent = parent.getParent();
                    }
                    return parent === item;
                },
                actions: new Press(),
                errorMessage: `No selector on the row "${title}"`
            });
        },
        errorMessage: `No row "${title}"`
    });
}

export function iSearch(When: Common, text: string): void {
    When.waitFor({
        id: "worklistSearch",
        ...OPTS,
        actions: new EnterText({ text, clearTextFirst: true }),
        errorMessage: "No search field"
    });
}

export function theHashIs(Then: Common, pattern: RegExp, message: string): void {
    Then.waitFor({
        check: function () {
            return pattern.test(HashChanger.getInstance().getHash());
        },
        success: function () {
            Opa5.assert.ok(true, `${message} (${HashChanger.getInstance().getHash()})`);
        },
        errorMessage: `The hash does not match ${pattern}: ${HashChanger.getInstance().getHash()}`
    });
}

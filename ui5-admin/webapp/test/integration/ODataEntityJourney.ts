import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type Button from "sap/m/Button";
import type Input from "sap/m/Input";
import type Link from "sap/m/Link";
import type MessageStrip from "sap/m/MessageStrip";
import type SearchField from "sap/m/SearchField";
import type Control from "sap/ui/core/Control";
import type UI5Element from "sap/ui/core/Element";
import type { ODataEntitySet, ODataField } from "com/agent/admin/service/types";
import Common, { backend } from "./pages/Common";
import { iPressInDialog } from "./pages/Dialogs";
import { TABLE, VIEW as LIST_VIEW, messageOf } from "./pages/ODataList";
import {
    ENTITY_TABLE, PAGE, VIEW, accessibleName, announced, entityHeader, entityItem, entityRow, entityTitles, hasFocus,
    opBox, pressSegment, stripOf, viewOf, withId, type Op
} from "./pages/ODataDetail";
import {
    DIALOG, box, confirmButtons, exampleInput, exampleItems, exampleRemove, exampleRows, fieldItem, fieldItems, fieldNames,
    fieldRow, header, hintInput, labelInput, meaningsInput, navigationDescription, navigationRows, part, personalToggle,
    tab, type ExamplePart, type Grant
} from "./pages/ODataEntity";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("OData entity set dialog journey");

const JOBS = "purchase-requisitions-jobs";
const V4 = "purchase-requisitions-v4";
const PUT = `PUT odata/services/${JOBS}`;
const ITEM = "Requisition item";
const ITEM_NAME = "A_PurchaseRequisitionItem";
const HEADER = "Requisition header";
const ACCOUNT = "Account assignment";
const ITEM_TEXT = "Item text";
const DELIVERY = "Delivery address";
const AUDITED = "Every write call is audited.";
const KEYS_HINT = "Agents always see the names of the key fields: they need them to fetch a record. "
    + "A key's value comes back only when the key field has Read ticked.";
const FILTER_UNTICKED = "Filter was unticked as well: a field must be readable to be filtered.";
const FILTER_NEEDS_READ = "A field must be readable before it can be filtered.";
const PERSONAL_QUESTION = "\"CreatedByUser\" is marked as personal data. Let agents read it?";
const DISCARD = "Discard the changes to this entity set?";

function pending(writes: string): string {
    return `Not saved yet: ${writes}. After Save, agents whose server entry has "Allow writes" can run this in SAP. `
        + AUDITED;
}

function stored(name: string) {
    return backend.odataServices.filter((service) => service.name === name)[0];
}

/** The entity set at `index` as the form holds it: what Save would send. */
function formSet(page: UI5Element, index: number): ODataEntitySet {
    const model = viewOf(page).getModel("svc") as unknown as { getProperty(path: string): ODataEntitySet };
    return model.getProperty(`/data/definition/entity_sets/${index}`);
}

function formField(page: UI5Element, index: number, name: string): ODataField {
    return formSet(page, index).fields.filter((field) => field.name === name)[0];
}

function iStart(Given: Common, Then: Common, name: string): void {
    Given.iStartTheApp(`odata-services/${name}`);
    Then.waitFor({
        id: "odataName", viewName: VIEW,
        check: function (input: UI5Element) { return (input as Input).getValue() === name; },
        success: function () { Opa5.assert.ok(true, `${name} is loaded`); },
        errorMessage: `The form does not show ${name}`
    });
}

/** Opens the service `name` after `prepare` changed what the backend holds. */
function iStartPrepared(Given: Common, When: Common, name: string, prepare: () => void): void {
    Given.iStartTheApp("odata-services");
    When.waitFor({
        id: TABLE, viewName: LIST_VIEW,
        success: function () {
            prepare();
            HashChanger.getInstance().setHash(`odata-services/${name}`);
        },
        errorMessage: "The list did not load"
    });
}

/** Presses the row titled `title` of the entity sets table. */
function iOpen(When: Common, title: string): void {
    When.waitFor({
        id: ENTITY_TABLE, viewName: VIEW,
        check: function (table: UI5Element) { return !!entityItem(table, title); },
        success: function (table: UI5Element) { new Press().executeOn(entityItem(table, title)); },
        errorMessage: `No row "${title}" in the entity sets table`
    });
}

/** Runs `assert` on the entity set dialog once `check` holds for it. */
function inDialog(
    Then: Common, what: string, check: (dialog: UI5Element) => boolean, assert: (dialog: UI5Element) => void
): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: withId(DIALOG),
        check: function (dialogs: UI5Element[]) { return check(dialogs[0]); },
        success: function (dialogs: UI5Element[]) { assert(dialogs[0]); },
        errorMessage: `Not seen in the entity set dialog: ${what}`
    });
}

/** Does something in the dialog as a user does. */
function iDo(When: Common, what: string, act: (dialog: UI5Element) => void): void {
    inDialog(When, what, function () { return true; }, act);
}

function iPressPart(When: Common, id: string): void {
    iDo(When, `press ${id}`, function (dialog: UI5Element) { new Press().executeOn(part(dialog, id)); });
}

function iType(When: Common, what: string, pick: (dialog: UI5Element) => Control, text: string): void {
    iDo(When, what, function (dialog: UI5Element) {
        new EnterText({ text, clearTextFirst: true }).executeOn(pick(dialog));
    });
}

function iTypeIn(When: Common, id: string, text: string): void {
    iType(When, `type into ${id}`, function (dialog: UI5Element) { return part(dialog, id); }, text);
}

function iSearch(When: Common, text: string): void {
    iDo(When, `search "${text}"`, function (dialog: UI5Element) {
        const search = part<SearchField>(dialog, "entityFieldSearch");
        search.setValue(text);
        search.fireLiveChange({ newValue: text });
    });
}

function iTickField(When: Common, name: string, grant: Grant): void {
    inDialog(When, `the field ${name}`, function (dialog: UI5Element) {
        return !!fieldItem(dialog, name);
    }, function (dialog: UI5Element) {
        new Press().executeOn(box(fieldItem(dialog, name), grant));
    });
}

function iSelectTab(When: Common, key: string): void {
    iDo(When, `the tab ${key}`, function (dialog: UI5Element) { new Press().executeOn(tab(dialog, key) as unknown as Control); });
}

/** Waits for a question (a message box) that asks `text`. */
function iSeeAQuestion(Then: Common, text: string, what: string): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check: function (dialogs: UI5Element[]) { return dialogs.some((dialog) => messageOf(dialog) === text); },
        success: function () { Opa5.assert.ok(true, `${what}: ${text}`); },
        errorMessage: `No question "${text}": ${what}`
    });
}

/** Runs `assert` on the page once no dialog is open and `check` holds. */
function onPage(Then: Common, what: string, check: (page: UI5Element) => boolean, assert: (page: UI5Element) => void): void {
    Then.waitFor({
        id: PAGE, viewName: VIEW,
        check: function (page: UI5Element) {
            return document.querySelectorAll(".sapMDialogOpen").length === 0 && check(page);
        },
        success: assert,
        errorMessage: `Not seen on the page: ${what}`
    });
}

function iTickOp(When: Common, title: string, op: Op): void {
    When.waitFor({
        id: ENTITY_TABLE, viewName: VIEW,
        check: function (table: UI5Element) { return !!entityItem(table, title); },
        success: function (table: UI5Element) { new Press().executeOn(opBox(entityItem(table, title), op)); },
        errorMessage: `No row "${title}"`
    });
}

function always(): boolean {
    return true;
}

opaTest("a row press opens the dialog with title, technical name, description, keys and three tabs with counts", function (Given: Common, When: Common, Then: Common) {
    iStart(Given, Then, JOBS);
    iOpen(When, ITEM);
    inDialog(Then, "the item", always, function (dialog: UI5Element) {
        const shown = header(dialog);
        Opa5.assert.strictEqual(shown.title, ITEM, "the dialog is titled with the business name");
        Opa5.assert.strictEqual(shown.business, ITEM, "which can be changed");
        Opa5.assert.strictEqual(shown.name, ITEM_NAME, "the technical name");
        Opa5.assert.strictEqual(shown.nameEditable, false, "is SAP's and stays");
        Opa5.assert.strictEqual(
            shown.description, "Central entity for approval decisions: release status, quantity, value, plant, supplier."
        );
        Opa5.assert.deepEqual(
            shown.keys, ["PurchaseRequisition (Edm.String)", "PurchaseRequisitionItem (Edm.String)"], "the keys, for reading"
        );
        Opa5.assert.strictEqual(shown.keysHint, KEYS_HINT, "what agents see of a key is said next to them");
        Opa5.assert.strictEqual(shown.keyWarning, "", "both keys are readable");
        Opa5.assert.deepEqual(shown.tabs, ["Fields (24 of 89)", "Navigations (4)", "Example queries (2)"], "three tabs with counts");
        Opa5.assert.strictEqual(shown.showing, "Showing 89 of 89 fields");
        Opa5.assert.strictEqual(fieldItems(dialog).length, 50, "fifty rows are rendered");
        Opa5.assert.deepEqual(fieldRow(dialog, "PurReqnReleaseStatus"), {
            name: "PurReqnReleaseStatus", type: "Edm.String", key: false, label: "Release status",
            read: true, filter: true, write: false, hint: "",
            meanings: "B = awaiting release; 05 = released; 08 = rejected", enumHint: "",
            personal: false, tag: "", tagState: "", note: "", confirm: ""
        }, "a field: label, the three permissions, value meanings");
        Opa5.assert.strictEqual(fieldRow(dialog, "PurchaseRequisition").key, true, "a key field is marked as one");
        Opa5.assert.strictEqual(fieldRow(dialog, "PurchaseRequisition").hint, "Key, 10 digits with leading zeros");
        Opa5.assert.strictEqual(fieldRow(dialog, "RequestedQuantity").write, true, "a writable field");

        const row = fieldItem(dialog, "PurReqnReleaseStatus");
        Opa5.assert.strictEqual(accessibleName(box(row, "read")), "Read PurReqnReleaseStatus");
        Opa5.assert.strictEqual(accessibleName(box(row, "filter")), "Filter PurReqnReleaseStatus");
        Opa5.assert.strictEqual(
            accessibleName(box(row, "write")), "Write PurReqnReleaseStatus write permission",
            "every checkbox is named by what it grants and the field's technical name"
        );
        Opa5.assert.strictEqual(
            box(fieldItem(dialog, "RequestedQuantity"), "write").getValueState(), "Warning", "a ticked Write stands out"
        );
        Opa5.assert.strictEqual(box(row, "write").getValueState(), "None", "an unticked one does not");
        Opa5.assert.ok(hasFocus(part(dialog, "entityTitle")), "the focus starts on the business name, not on a checkbox");
        Opa5.assert.strictEqual(backend.requests.filter((r) => /^(PUT|POST)/.test(r)).length, 0, "nothing is sent");
    });

    // Looking is not editing: Cancel asks nothing.
    iPressPart(When, "entityCancelButton");
    onPage(Then, "the page again", always, function (page: UI5Element) {
        Opa5.assert.strictEqual(entityRow(page, ITEM).fields, "24 of 89", "as it was");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false);
    });
    Then.iStopTheApp();
});

opaTest("unticking Read also unticks Filter and says so; Filter on an unread field is refused; the count is live", function (Given: Common, When: Common, Then: Common) {
    iStart(Given, Then, JOBS);
    iOpen(When, ITEM);
    iTickField(When, "Plant", "read");
    inDialog(Then, "Plant unread", function (dialog: UI5Element) {
        return !fieldRow(dialog, "Plant").read;
    }, function (dialog: UI5Element) {
        const row = fieldRow(dialog, "Plant");
        Opa5.assert.strictEqual(row.filter, false, "Filter went with Read");
        Opa5.assert.strictEqual(row.note, FILTER_UNTICKED, "and the row says so");
        Opa5.assert.strictEqual(announced(), FILTER_UNTICKED, "to a screen reader too");
        Opa5.assert.strictEqual(header(dialog).tabs[0], "Fields (23 of 89)", "the count follows");
    });

    iTickField(When, "Plant", "filter");
    inDialog(Then, "the refused Filter", function (dialog: UI5Element) {
        return fieldRow(dialog, "Plant").note === FILTER_NEEDS_READ;
    }, function (dialog: UI5Element) {
        const row = fieldRow(dialog, "Plant");
        Opa5.assert.strictEqual(row.filter, false, "Filter is not ticked");
        Opa5.assert.strictEqual(row.read, false, "and Read is not ticked for the admin");
    });

    iTickField(When, "Plant", "read");
    inDialog(Then, "Plant read again", function (dialog: UI5Element) {
        return fieldRow(dialog, "Plant").read;
    }, function (dialog: UI5Element) {
        const row = fieldRow(dialog, "Plant");
        Opa5.assert.strictEqual(row.filter, false, "ticking Read does not tick Filter");
        Opa5.assert.strictEqual(row.note, "", "the note is gone");
        Opa5.assert.strictEqual(header(dialog).tabs[0], "Fields (24 of 89)");
    });
    iTickField(When, "Plant", "filter");
    inDialog(Then, "Plant filterable", function (dialog: UI5Element) {
        return fieldRow(dialog, "Plant").filter;
    }, function () {
        Opa5.assert.ok(true, "a readable field can be filtered");
    });
    Then.iStopTheApp();
});

opaTest("Write is independent of Read, and a field newly writable is said next to Save and asked about by Save", function (Given: Common, When: Common, Then: Common) {
    const WRITES = `the field Field030 writable on "${ITEM}" (${ITEM_NAME})`;
    let agent = "";

    iStart(Given, Then, JOBS);
    iOpen(When, ITEM);
    iSearch(When, "Field030");
    iTickField(When, "Field030", "write");
    inDialog(Then, "the ticked Write", function (dialog: UI5Element) {
        return fieldRow(dialog, "Field030").write;
    }, function (dialog: UI5Element) {
        agent = stored(JOBS).used_by[0].agent;
        const row = fieldRow(dialog, "Field030");
        Opa5.assert.strictEqual(row.read, false, "Read is not ticked with it");
        Opa5.assert.strictEqual(row.filter, false, "nor Filter");
        Opa5.assert.strictEqual(box(fieldItem(dialog, "Field030"), "write").getValueState(), "Warning", "it stands out");
        Opa5.assert.strictEqual(header(dialog).tabs[0], "Fields (24 of 89)", "the count is of readable fields");
    });
    iPressPart(When, "entityApplyButton");

    onPage(Then, "the pending field", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").text, pending(WRITES), "next to Save");
        Opa5.assert.strictEqual(formField(page, 0, "Field030").writable, true, "the form holds it");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "Apply sends nothing");
    });

    When.waitFor({ id: "odataSaveButton", viewName: VIEW, actions: new Press(), errorMessage: "No Save" });
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(
                messageOf(dialogs[0]),
                `The agent ${agent} uses this service.\n\nSaving enables these write operations in SAP: ${WRITES}.\n\n`
                + `The agent ${agent} has "Allow writes" and will be able to run them.\n\n${AUDITED}`,
                "Save asks, naming the field, the entity set and the agent that can then write it"
            );
            Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
        },
        errorMessage: "Save did not ask about the writable field"
    });
    iPressInDialog(When, "Save");
    onPage(Then, "the saved field", function (page: UI5Element) {
        return backend.countRequests(PUT) === 1 && !stripOf(page, "odataPendingWrites").visible;
    }, function () {
        const field = stored(JOBS).definition.entity_sets[0].fields.filter((f) => f.name === "Field030")[0];
        Opa5.assert.deepEqual(
            [field.writable, field.selectable, field.filterable], [true, false, false], "stored as writable, and only that"
        );
    });
    Then.iStopTheApp();
});

opaTest("personal data is marked by hand; ticking Read on it asks in place, and only the confirmation ticks it", function (Given: Common, When: Common, Then: Common) {
    iStart(Given, Then, JOBS);
    iOpen(When, ITEM);
    iSearch(When, "CreatedByUser");
    inDialog(Then, "the personal-data field", function (dialog: UI5Element) {
        return fieldNames(dialog).length === 1;
    }, function (dialog: UI5Element) {
        const row = fieldRow(dialog, "CreatedByUser");
        Opa5.assert.strictEqual(row.personal, true, "it is marked");
        Opa5.assert.strictEqual(row.tag, "Personal data, not readable", "and the tag says that agents do not get it");
        Opa5.assert.strictEqual(row.tagState, "None", "which is no warning");
        const marker = personalToggle(fieldItem(dialog, "CreatedByUser")).getDomRef()!;
        const named = (marker.getAttribute("aria-labelledby") ?? "").split(" ")
            .map((id) => document.getElementById(id)?.textContent ?? "").join(" ");
        Opa5.assert.ok(
            named.indexOf("Personal data marker of CreatedByUser") === 0, `the marker is named with the field: ${named}`
        );
    });

    iTickField(When, "CreatedByUser", "read");
    inDialog(Then, "the question in place", function (dialog: UI5Element) {
        return !!fieldRow(dialog, "CreatedByUser").confirm;
    }, function (dialog: UI5Element) {
        const row = fieldRow(dialog, "CreatedByUser");
        Opa5.assert.strictEqual(row.confirm, PERSONAL_QUESTION, "the row asks");
        Opa5.assert.strictEqual(row.read, false, "nothing is ticked yet");
        Opa5.assert.strictEqual(header(dialog).tabs[0], "Fields (24 of 89)");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 1, "no dialog on top of the dialog");
        Opa5.assert.deepEqual(
            confirmButtons(fieldItem(dialog, "CreatedByUser")).map((button) => button.getText()),
            ["Let agents read it", "Keep unread"]
        );
    });
    iDo(When, "keep it unread", function (dialog: UI5Element) {
        new Press().executeOn(confirmButtons(fieldItem(dialog, "CreatedByUser"))[1]);
    });
    inDialog(Then, "kept unread", function (dialog: UI5Element) {
        return !fieldRow(dialog, "CreatedByUser").confirm;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(fieldRow(dialog, "CreatedByUser").read, false, "still unread");
    });

    iTickField(When, "CreatedByUser", "read");
    iDo(When, "confirm", function (dialog: UI5Element) {
        new Press().executeOn(confirmButtons(fieldItem(dialog, "CreatedByUser"))[0]);
    });
    inDialog(Then, "read after the confirmation", function (dialog: UI5Element) {
        return fieldRow(dialog, "CreatedByUser").read;
    }, function (dialog: UI5Element) {
        const row = fieldRow(dialog, "CreatedByUser");
        Opa5.assert.strictEqual(row.tag, "Personal data, readable by agents", "the tag now warns, in words");
        Opa5.assert.strictEqual(row.tagState, "Warning");
        Opa5.assert.strictEqual(row.confirm, "", "the question is gone");
        Opa5.assert.strictEqual(header(dialog).tabs[0], "Fields (25 of 89)");
    });

    // Marking a field that agents already read warns at once.
    iSearch(When, "Plant");
    iDo(When, "mark Plant", function (dialog: UI5Element) {
        new Press().executeOn(personalToggle(fieldItem(dialog, "Plant")));
    });
    inDialog(Then, "Plant marked", function (dialog: UI5Element) {
        return fieldRow(dialog, "Plant").personal;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(fieldRow(dialog, "Plant").tag, "Personal data, readable by agents");
        Opa5.assert.strictEqual(fieldRow(dialog, "Plant").read, true, "marking changes no permission");
    });

    iPressPart(When, "entityApplyButton");
    onPage(Then, "the applied markers", always, function (page: UI5Element) {
        Opa5.assert.strictEqual(formField(page, 0, "CreatedByUser").selectable, true, "the confirmed Read is in the form");
        Opa5.assert.strictEqual(formField(page, 0, "Plant").personal_data, true, "and so is the marker");
        Opa5.assert.strictEqual(entityRow(page, ITEM).fields, "25 of 89", "the table counts it");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before Save");
    });
    Then.iStopTheApp();
});

opaTest("Tick Read for the fields shown says how many, leaves personal data and Filter and Write alone, and can be undone", function (Given: Common, When: Common, Then: Common) {
    iStart(Given, Then, JOBS);
    iOpen(When, ITEM);
    inDialog(Then, "all fields", always, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            part<Button>(dialog, "entityReadAll").getText(), "Tick Read for 64 fields shown",
            "89 fields, 24 readable, one marked as personal data"
        );
    });
    iDo(When, "only personal data", function (dialog: UI5Element) { pressSegment(part(dialog, "entityFieldFilter"), "personal"); });
    inDialog(Then, "the personal-data filter", function (dialog: UI5Element) {
        return fieldNames(dialog).length === 1;
    }, function (dialog: UI5Element) {
        Opa5.assert.deepEqual(fieldNames(dialog), ["CreatedByUser"]);
        Opa5.assert.strictEqual(header(dialog).showing, "Showing 1 of 89 fields");
        Opa5.assert.strictEqual(part<Button>(dialog, "entityReadAll").getEnabled(), false, "nothing to tick: never personal data");
        Opa5.assert.strictEqual(part<Button>(dialog, "entityReadAll").getText(), "No field shown to tick Read for");
    });
    iDo(When, "all again", function (dialog: UI5Element) { pressSegment(part(dialog, "entityFieldFilter"), "all"); });
    iSearch(When, "Field08");
    inDialog(Then, "nine fields", function (dialog: UI5Element) {
        return fieldNames(dialog).length === 9;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(part<Button>(dialog, "entityReadAll").getText(), "Tick Read for 9 fields shown");
    });
    iPressPart(When, "entityReadAll");
    inDialog(Then, "nine more readable", function (dialog: UI5Element) {
        return header(dialog).tabs[0] === "Fields (33 of 89)";
    }, function (dialog: UI5Element) {
        const row = fieldRow(dialog, "Field084");
        Opa5.assert.deepEqual([row.read, row.filter, row.write], [true, false, false], "Read only");
        const done = part<MessageStrip>(dialog, "entityReadAllDone");
        Opa5.assert.strictEqual(done.getVisible(), true, "what was done is said");
        Opa5.assert.strictEqual(done.getText(), "Read ticked for 9 fields. Fields marked as personal data were left out.");
        Opa5.assert.strictEqual(part<Button>(dialog, "entityReadAll").getEnabled(), false, "nothing left to tick here");
    });
    iDo(When, "undo", function (dialog: UI5Element) { new Press().executeOn(part<Link>(dialog, "entityReadAllUndo")); });
    inDialog(Then, "undone", function (dialog: UI5Element) {
        return header(dialog).tabs[0] === "Fields (24 of 89)";
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(fieldRow(dialog, "Field084").read, false, "unticked again");
        Opa5.assert.strictEqual(part<MessageStrip>(dialog, "entityReadAllDone").getVisible(), false);
    });
    Then.iStopTheApp();
});

opaTest("value meanings typed as text are stored as pairs; a part that cannot be stored blocks Apply and is named", function (Given: Common, When: Common, Then: Common) {
    iStart(Given, Then, JOBS);
    iOpen(When, ITEM);
    iType(When, "meanings of Plant", function (dialog: UI5Element) { return meaningsInput(fieldItem(dialog, "Plant")); }, "1010 = Hamburg; 1020");
    iType(When, "hint of Plant", function (dialog: UI5Element) { return hintInput(fieldItem(dialog, "Plant")); }, "The delivering plant");
    iType(When, "label of Plant", function (dialog: UI5Element) { return labelInput(fieldItem(dialog, "Plant")); }, "Delivering plant");
    iPressPart(When, "entityApplyButton");
    inDialog(Then, "the refused Apply", function (dialog: UI5Element) {
        return !!header(dialog).issues;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            header(dialog).issues,
            "Not applied. Plant: write each value meaning as \"value = meaning\", separated by \";\".",
            "the dialog stays and names the field"
        );
        Opa5.assert.strictEqual(meaningsInput(fieldItem(dialog, "Plant")).getValueState(), "Error", "and marks it");
    });
    iType(When, "meanings of Plant", function (dialog: UI5Element) { return meaningsInput(fieldItem(dialog, "Plant")); }, "1010 = Hamburg; 1020 = Berlin");
    iPressPart(When, "entityApplyButton");
    onPage(Then, "the applied texts", always, function (page: UI5Element) {
        const plant = formField(page, 0, "Plant");
        Opa5.assert.deepEqual(
            plant.values, [{ value: "1010", meaning: "Hamburg" }, { value: "1020", meaning: "Berlin" }], "value/meaning pairs"
        );
        Opa5.assert.strictEqual(plant.hint, "The delivering plant");
        Opa5.assert.strictEqual(plant.label, "Delivering plant");
        Opa5.assert.deepEqual(
            formField(page, 0, "PurReqnReleaseStatus").values,
            [{ value: "B", meaning: "awaiting release" }, { value: "05", meaning: "released" }, { value: "08", meaning: "rejected" }],
            "the meanings nobody touched are as they were"
        );
    });
    Then.iStopTheApp();
});

opaTest("on a V4 service a field whose type is no Edm type says that its values must be listed to filter on it", function (Given: Common, When: Common, Then: Common) {
    const ENUM = "Not an Edm type: agents can filter on this field only by the values listed under value meanings.";
    iStartPrepared(Given, When, V4, function () {
        stored(V4).definition.entity_sets[0].fields[1].type = "com.sap.gateway.ReleaseStatus";
    });
    iOpen(When, "Requisition");
    inDialog(Then, "the V4 entity set", always, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(fieldRow(dialog, "Field002").type, "com.sap.gateway.ReleaseStatus");
        Opa5.assert.strictEqual(fieldRow(dialog, "Field002").enumHint, ENUM, "the enum field says it");
        Opa5.assert.strictEqual(fieldRow(dialog, "PurchaseRequisition").enumHint, "", "an Edm field does not");
    });
    Then.iStopTheApp();
});

opaTest("Apply writes into the form and sends nothing; Cancel and Escape ask before discarding and leave the form untouched", function (Given: Common, When: Common, Then: Common) {
    iStart(Given, Then, JOBS);
    iOpen(When, ITEM);
    iTypeIn(When, "entityTitle", "Item renamed");
    iPressPart(When, "entityCancelButton");
    iSeeAQuestion(Then, DISCARD, "Cancel with changes asks");
    iPressInDialog(When, "Keep editing");
    inDialog(Then, "still open", function () {
        return document.querySelectorAll(".sapMDialogOpen").length === 1;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(header(dialog).business, "Item renamed", "the input is kept");
    });

    iDo(When, "Escape", function (dialog: UI5Element) {
        (dialog as Control).getDomRef()!.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true
        }));
    });
    iSeeAQuestion(Then, DISCARD, "Escape is Cancel");
    iPressInDialog(When, "Discard");
    onPage(Then, "the untouched form", always, function (page: UI5Element) {
        Opa5.assert.strictEqual(formSet(page, 0).title, ITEM, "nothing was written into the form");
        Opa5.assert.deepEqual(entityTitles(page)[0], ITEM);
    });

    iOpen(When, ITEM);
    inDialog(Then, "opened again", always, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(header(dialog).business, ITEM, "the discarded text is gone");
    });
    iTypeIn(When, "entityTitle", "Item renamed");
    iDo(When, "the description", function (dialog: UI5Element) {
        new EnterText({ text: "Start here.", clearTextFirst: true }).executeOn(part(dialog, "entityDescription"));
    });
    iPressPart(When, "entityApplyButton");
    onPage(Then, "the applied entity set", function (page: UI5Element) {
        return entityTitles(page)[0] === "Item renamed";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(entityRow(page, "Item renamed").description, "Start here.", "the row shows it");
        Opa5.assert.strictEqual(entityRow(page, "Item renamed").technical, ITEM_NAME);
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before Save");
        Opa5.assert.strictEqual(stored(JOBS).definition.entity_sets[0].title, ITEM, "nothing is stored");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "no write is pending");
    });

    When.waitFor({ id: "odataSaveButton", viewName: VIEW, actions: new Press(), errorMessage: "No Save" });
    onPage(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function () {
        Opa5.assert.strictEqual(stored(JOBS).definition.entity_sets[0].title, "Item renamed", "the page's Save stores it");
        Opa5.assert.strictEqual(stored(JOBS).definition.entity_sets[0].description, "Start here.");
        Opa5.assert.strictEqual(typeof backend.bodies[PUT]?.expected_updated_at, "string", "on the version the form was loaded from");
    });
    Then.iStopTheApp();
});

opaTest("after an entity set is removed and another renamed, a tick lands on the entity set of its row", function (Given: Common, When: Common, Then: Common) {
    iStart(Given, Then, JOBS);

    // An entity set an operation is bound to stays: the server would refuse the save.
    iOpen(When, ITEM);
    iPressPart(When, "entityRemoveButton");
    iSeeAQuestion(
        Then,
        `The entity set "${ITEM}" (${ITEM_NAME}) cannot be removed: these operations are bound to it: Release item.`,
        "the refused removal"
    );
    iPressInDialog(When, "Close");
    iPressPart(When, "entityCancelButton");

    iOpen(When, HEADER);
    iPressPart(When, "entityRemoveButton");
    iSeeAQuestion(
        Then,
        `Remove the entity set "${HEADER}" (A_PurchaseRequisitionHeader) from this service? `
        + "Agents lose everything it offers once the service is saved.",
        "removing asks"
    );
    iPressInDialog(When, "Remove");
    onPage(Then, "four entity sets", function (page: UI5Element) {
        return entityTitles(page).length === 4;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityTitles(page), [ITEM, ACCOUNT, ITEM_TEXT, DELIVERY], "the header is gone");
        Opa5.assert.strictEqual(entityHeader(page).title, "Entity sets (4)");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent");
    });

    iOpen(When, ITEM);
    iTypeIn(When, "entityTitle", "Item renamed");
    iPressPart(When, "entityApplyButton");
    onPage(Then, "the renamed row", function (page: UI5Element) {
        return entityTitles(page)[0] === "Item renamed";
    }, function () {
        Opa5.assert.ok(true, "renamed");
    });

    // The rows moved up by one: the tick must follow the row, not its old position.
    iTickOp(When, ACCOUNT, "get");
    onPage(Then, "Get on the account assignment", function (page: UI5Element) {
        return entityRow(page, ACCOUNT).ticked.length === 2;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(formSet(page, 1).name, "A_PurReqnAcctAssgmt", "the entity set at the row's position");
        Opa5.assert.deepEqual(formSet(page, 1).operations, ["list", "get"], "got the tick");
        Opa5.assert.deepEqual(formSet(page, 0).operations, ["list", "get", "update"], "the one before it did not");
        Opa5.assert.deepEqual(formSet(page, 2).operations, ["list", "get", "create", "update"], "nor the one after it");
        Opa5.assert.strictEqual(entityRow(page, ACCOUNT).note, "", "and the click was taken, not refused as stale");
    });
    iTickOp(When, "Item renamed", "delete");
    onPage(Then, "the pending write under the new title", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stripOf(page, "odataPendingWrites").text, pending(`Delete on "Item renamed" (${ITEM_NAME})`),
            "the strip names the renamed entity set"
        );
        Opa5.assert.deepEqual(formSet(page, 0).operations, ["list", "get", "update", "delete"]);
        Opa5.assert.strictEqual(formSet(page, 0).title, "Item renamed");
    });
    Then.iStopTheApp();
});

opaTest("a key field without Read while Get is on is warned about next to the keys", function (Given: Common, When: Common, Then: Common) {
    const WARNING = "Get is enabled, but agents cannot read the key field PurchaseRequisitionItem: "
        + "they can fetch a record and will not see that key in the result.";
    iStart(Given, Then, JOBS);
    iOpen(When, ITEM);
    iTickField(When, "PurchaseRequisitionItem", "read");
    inDialog(Then, "the key warning", function (dialog: UI5Element) {
        return !!header(dialog).keyWarning;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(header(dialog).keyWarning, WARNING);
        Opa5.assert.strictEqual(part<MessageStrip>(dialog, "entityKeyWarning").getType(), "Warning", "a warning, with an icon");
        Opa5.assert.strictEqual(part<MessageStrip>(dialog, "entityKeyWarning").getShowIcon(), true);
    });
    iTickField(When, "PurchaseRequisitionItem", "read");
    inDialog(Then, "no key warning", function (dialog: UI5Element) {
        return !header(dialog).keyWarning;
    }, function () {
        Opa5.assert.ok(true, "readable again: no warning");
    });

    // It is a warning: Apply goes through, and the page's Save stores it.
    iTickField(When, "PurchaseRequisitionItem", "read");
    iPressPart(When, "entityApplyButton");
    onPage(Then, "applied with the warning", always, function (page: UI5Element) {
        Opa5.assert.strictEqual(formField(page, 0, "PurchaseRequisitionItem").selectable, false);
        Opa5.assert.strictEqual(formField(page, 0, "PurchaseRequisitionItem").filterable, false, "Filter went with it");
    });
    Then.iStopTheApp();
});

opaTest("the navigations tab says per navigation whether an agent could follow it today", function (Given: Common, When: Common, Then: Common) {
    iStartPrepared(Given, When, JOBS, function () {
        stored(JOBS).definition.entity_sets[0].navigations.push({
            name: "to_Supplier", target: "A_Supplier", collection: false, description: ""
        });
    });
    iOpen(When, ITEM);
    iSelectTab(When, "navigations");
    inDialog(Then, "the navigations", function (dialog: UI5Element) {
        return navigationRows(dialog).length === 5;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(header(dialog).tabs[1], "Navigations (5)");
        Opa5.assert.deepEqual(navigationRows(dialog), [
            {
                name: "to_PurchaseReqnItemText", target: "A_PurchaseReqnItemText", kind: "Collection", description: "",
                follow: "Yes", state: "Success"
            },
            {
                name: "to_PurchaseReqnAcctAssgmt", target: "A_PurReqnAcctAssgmt", kind: "Collection", description: "",
                follow: "Yes", state: "Success"
            },
            {
                name: "to_PurchaseReqnDeliveryAddress", target: "A_PurReqAddDelivery", kind: "Single", description: "",
                follow: "No: Get is off on Delivery address (A_PurReqAddDelivery).", state: "Information"
            },
            {
                name: "to_PurchaseRequisition", target: "A_PurchaseRequisitionHeader", kind: "Single",
                description: "The header of this item.", follow: "Yes", state: "Success"
            },
            {
                name: "to_Supplier", target: "A_Supplier", kind: "Single", description: "",
                follow: "No: the target A_Supplier is not in this service.", state: "Information"
            }
        ], "name, target, collection or single, description, and whether it can be followed");
    });
    iType(When, "a description", function (dialog: UI5Element) { return navigationDescription(dialog, 0); }, "The requester's text.");
    iPressPart(When, "entityApplyButton");
    onPage(Then, "the applied description", always, function (page: UI5Element) {
        Opa5.assert.strictEqual(formSet(page, 0).navigations[0].description, "The requester's text.");
        Opa5.assert.strictEqual(formSet(page, 0).navigations.length, 5, "the navigations are kept");
    });

    // An entity set without Get: none of its navigations can be followed.
    iOpen(When, ACCOUNT);
    iSelectTab(When, "navigations");
    inDialog(Then, "no navigations", always, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(header(dialog).tabs[1], "Navigations (0)");
        Opa5.assert.deepEqual(navigationRows(dialog), []);
    });
    Then.iStopTheApp();
});

opaTest("an example that mentions an unreadable field is warned about and still applied; examples can be added and removed", function (Given: Common, When: Common, Then: Common) {
    const example = function (index: number, which: ExamplePart) {
        return function (dialog: UI5Element) { return exampleInput(exampleItems(dialog)[index], which); };
    };
    iStart(Given, Then, JOBS);
    iOpen(When, ITEM);
    iSelectTab(When, "examples");
    inDialog(Then, "the examples", function (dialog: UI5Element) {
        return exampleRows(dialog).length === 2;
    }, function (dialog: UI5Element) {
        Opa5.assert.deepEqual(exampleRows(dialog), [
            {
                description: "Items awaiting release in one plant", filter: "PurReqnReleaseStatus eq 'B' and Plant eq '1010'",
                select: "PurchaseRequisition, PurchaseRequisitionItem, ItemNetAmount", orderby: "", top: "20", warning: ""
            },
            {
                description: "All items of one requisition", filter: "PurchaseRequisition eq '0010000123'",
                select: "", orderby: "PurchaseRequisitionItem", top: "", warning: ""
            }
        ], "description, select, filter, orderby and top of each");
    });

    iType(When, "a filter on an unreadable field", example(0, "filter"), "CreatedByUser eq 'JDOE'");
    inDialog(Then, "the warning", function (dialog: UI5Element) {
        return !!exampleRows(dialog)[0].warning;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            exampleRows(dialog)[0].warning,
            "Agents will not see this example: it mentions CreatedByUser, which they may not read.",
            "which example is dropped at run time, and why"
        );
    });
    iType(When, "a select of unreadable fields", example(1, "select"), "PurchaseRequisition, Field050");
    inDialog(Then, "the trimmed select", function (dialog: UI5Element) {
        return !!exampleRows(dialog)[1].warning;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            exampleRows(dialog)[1].warning, "Agents will see this example without Field050 in $select: not readable."
        );
    });

    iPressPart(When, "entityAddExample");
    inDialog(Then, "a third example", function (dialog: UI5Element) {
        return exampleRows(dialog).length === 3;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(header(dialog).tabs[2], "Example queries (3)");
        Opa5.assert.deepEqual(
            exampleRows(dialog)[2], { description: "", select: "", filter: "", orderby: "", top: "", warning: "" }, "empty"
        );
    });
    iPressPart(When, "entityApplyButton");
    inDialog(Then, "the refused Apply", function (dialog: UI5Element) {
        return !!header(dialog).issues;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(header(dialog).issues, "Not applied. Example 3: say what it answers.", "the server would refuse it");
    });
    iType(When, "its description", example(2, "description"), "Items of one plant");
    iType(When, "its select", example(2, "select"), "Plant,ItemNetAmount");
    iType(When, "its top", example(2, "top"), "10");
    iDo(When, "remove the second", function (dialog: UI5Element) {
        new Press().executeOn(exampleRemove(exampleItems(dialog)[1]));
    });
    inDialog(Then, "two examples", function (dialog: UI5Element) {
        return exampleRows(dialog).length === 2;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(header(dialog).tabs[2], "Example queries (2)");
        Opa5.assert.strictEqual(exampleRows(dialog)[1].description, "Items of one plant", "the third moved up");
    });
    iPressPart(When, "entityApplyButton");
    onPage(Then, "the applied examples", always, function (page: UI5Element) {
        Opa5.assert.deepEqual(formSet(page, 0).examples, [
            {
                description: "Items awaiting release in one plant", filter: "CreatedByUser eq 'JDOE'",
                select: ["PurchaseRequisition", "PurchaseRequisitionItem", "ItemNetAmount"], orderby: "", top: 20
            },
            { description: "Items of one plant", filter: "", select: ["Plant", "ItemNetAmount"], orderby: "", top: 10 }
        ], "the warned example is applied as written: a warning, not a block");
    });
    Then.iStopTheApp();
});

opaTest("an entity set with 500 fields renders fifty rows, is searched by name and label, filtered, and counts live", function (Given: Common, When: Common, Then: Common) {
    iStartPrepared(Given, When, JOBS, function () {
        const fields: ODataField[] = [];
        for (let i = 0; i < 500; i++) {
            fields.push({
                name: i === 0 ? "PurchaseRequisition" : i === 1 ? "PurchaseRequisitionItem" : `Field${String(i + 1).padStart(3, "0")}`,
                type: "Edm.String", label: `Label ${i + 1}`, selectable: false, filterable: false, writable: false,
                hint: "", values: [], personal_data: i === 499
            });
        }
        stored(JOBS).definition.entity_sets[4].fields = fields;
    });
    iOpen(When, DELIVERY);
    inDialog(Then, "500 fields", always, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(fieldItems(dialog).length, 50, "fifty rows are rendered");
        Opa5.assert.strictEqual(header(dialog).showing, "Showing 500 of 500 fields");
        Opa5.assert.strictEqual(header(dialog).tabs[0], "Fields (0 of 500)");
    });
    iSearch(When, "field49");
    inDialog(Then, "the search by name", function (dialog: UI5Element) {
        return fieldNames(dialog).length === 10;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(fieldNames(dialog)[0], "Field490", "found beyond the rendered rows");
        Opa5.assert.strictEqual(header(dialog).showing, "Showing 10 of 500 fields");
    });
    iTickField(When, "Field499", "read");
    inDialog(Then, "one readable", function (dialog: UI5Element) {
        return header(dialog).tabs[0] === "Fields (1 of 500)";
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(fieldNames(dialog).length, 10, "the list holds still under the pointer");
    });
    iSearch(When, "Label 7");
    inDialog(Then, "the search by label", function (dialog: UI5Element) {
        return fieldNames(dialog).length === 11;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(fieldRow(dialog, "Field007").label, "Label 7");
        Opa5.assert.strictEqual(header(dialog).showing, "Showing 11 of 500 fields");
    });
    iSearch(When, "");
    iDo(When, "ticked only", function (dialog: UI5Element) { pressSegment(part(dialog, "entityFieldFilter"), "ticked"); });
    inDialog(Then, "the ticked fields", function (dialog: UI5Element) {
        return fieldNames(dialog).length === 1;
    }, function (dialog: UI5Element) {
        Opa5.assert.deepEqual(fieldNames(dialog), ["Field499"]);
        Opa5.assert.strictEqual(header(dialog).showing, "Showing 1 of 500 fields");
    });
    iDo(When, "unticked only", function (dialog: UI5Element) { pressSegment(part(dialog, "entityFieldFilter"), "unticked"); });
    inDialog(Then, "the unticked fields", function (dialog: UI5Element) {
        return header(dialog).showing === "Showing 499 of 500 fields";
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(fieldItems(dialog).length, 50);
    });
    iPressPart(When, "entityApplyButton");
    onPage(Then, "the applied tick", always, function (page: UI5Element) {
        Opa5.assert.strictEqual(entityRow(page, DELIVERY).fields, "1 of 500");
        Opa5.assert.strictEqual(formSet(page, 4).fields.length, 500, "all fields are kept, shown or not");
        Opa5.assert.strictEqual(formField(page, 4, "Field499").selectable, true);
        Opa5.assert.strictEqual(formSet(page, 4).fields.filter((field) => field.selectable).length, 1, "and only that one is readable");
    });
    Then.iStopTheApp();
});

opaTest("an entity set added by hand gets its technical name in the dialog, checked like the server checks it", function (Given: Common, When: Common, Then: Common) {
    iStart(Given, Then, JOBS);
    When.waitFor({ id: "odataAddEntitySetButton", viewName: VIEW, actions: new Press(), errorMessage: "No Add" });
    inDialog(Then, "the new entity set", always, function (dialog: UI5Element) {
        const shown = header(dialog);
        Opa5.assert.strictEqual(shown.name, "NewEntitySet");
        Opa5.assert.strictEqual(shown.nameEditable, true, "its name can be changed until it is saved");
        Opa5.assert.deepEqual(shown.keys, ["This entity set has no key."]);
        Opa5.assert.deepEqual(shown.tabs, ["Fields (0 of 0)", "Navigations (0)", "Example queries (0)"]);
    });
    iTypeIn(When, "entityName", "A_PurReqAddDelivery");
    iPressPart(When, "entityApplyButton");
    inDialog(Then, "the taken name", function (dialog: UI5Element) {
        return !!header(dialog).issues;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            header(dialog).issues, "Not applied. Another entity set has the name A_PurReqAddDelivery as well."
        );
        Opa5.assert.strictEqual(part<Input>(dialog, "entityName").getValueState(), "Error");
    });
    iTypeIn(When, "entityName", "1 bad");
    iPressPart(When, "entityApplyButton");
    inDialog(Then, "the invalid name", function (dialog: UI5Element) {
        return header(dialog).issues.indexOf("must start with a letter") !== -1;
    }, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            header(dialog).issues,
            "Not applied. The technical name must start with a letter or underscore and hold only letters, digits, underscores and dots."
        );
    });
    iTypeIn(When, "entityName", "A_Custom");
    iTypeIn(When, "entityTitle", "Custom");
    iPressPart(When, "entityApplyButton");
    onPage(Then, "the named entity set", function (page: UI5Element) {
        return entityTitles(page).length === 6 && entityTitles(page)[5] === "Custom";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(entityRow(page, "Custom").technical, "A_Custom");
        Opa5.assert.deepEqual(entityRow(page, "Custom").ticked, [], "nothing is enabled");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0);
    });
    Then.iStopTheApp();
});

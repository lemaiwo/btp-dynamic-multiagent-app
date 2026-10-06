import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type Input from "sap/m/Input";
import type Text from "sap/m/Text";
import type Button from "sap/m/Button";
import type CheckBox from "sap/m/CheckBox";
import type List from "sap/m/List";
import type Panel from "sap/m/Panel";
import type HBox from "sap/m/HBox";
import type VBox from "sap/m/VBox";
import type MessageStrip from "sap/m/MessageStrip";
import type SearchField from "sap/m/SearchField";
import type ObjectStatus from "sap/m/ObjectStatus";
import type CustomListItem from "sap/m/CustomListItem";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type JSONModel from "sap/ui/model/json/JSONModel";
import Common, { backend } from "./pages/Common";
import FakeBackend from "./FakeBackend";
import { iPressInDialog } from "./pages/Dialogs";
import { DIALOG as ENTITY_DIALOG, box as fieldBox, fieldItem, part as entityPart } from "./pages/ODataEntity";
import { TABLE, VIEW as LIST_VIEW, messageOf } from "./pages/ODataList";
import {
    VIEW, entityItem, entityRow, entityTitles, opBox, operationBox, operationItem, operationRow, operationsHeader, stripOf, toasts,
    viewOf, withId
} from "./pages/ODataDetail";
import type { ODataEntitySet, ODataField, ODataServiceInput } from "../../service/types";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("OData import from $metadata journey");

const JOBS = "purchase-requisitions-jobs";
const UNUSED = "purchase-requisitions-v4";
const PATH = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV";
const READ = "POST odata/metadata";
const ITEM = "A_PurchaseRequisitionItem";

function stored(name: string) {
    return backend.odataServices.filter((service) => service.name === name)[0];
}

function byId<T extends Control>(page: UI5Element, id: string): T {
    return viewOf(page).byId(id) as T;
}

/** The form of the page, as the model holds it. */
function formData(page: UI5Element): ODataServiceInput {
    return (viewOf(page).getModel("svc") as JSONModel).getProperty("/data") as ODataServiceInput;
}

function formSet(page: UI5Element, name: string): ODataEntitySet {
    return formData(page).definition.entity_sets.filter((entitySet) => entitySet.name === name)[0];
}

/** The fields of an entity set an agent could use in any way. */
function ticked(entitySet: ODataEntitySet): string[] {
    return entitySet.fields.filter((f: ODataField) => f.selectable || f.filterable || f.writable).map((f) => f.name);
}

function writes(): number {
    return backend.requests.filter((request) => /^(PUT|POST odata\/services)/.test(request)).length;
}

interface ImportRow {
    name: string;
    hint: string;
    note: string;
    status: string;
    canTick: boolean;
    ticked: boolean;
    child: boolean;
}

function rowParts(item: CustomListItem): { box: CheckBox; toggle: Button; row: ImportRow } {
    const line = item.getContent()[0] as HBox;
    const [lead, body, status] = line.getItems() as [HBox, VBox, ObjectStatus];
    const [toggle, box] = lead.getItems() as [Button, CheckBox];
    const [name, hint, note] = body.getItems() as [Text, Text, ObjectStatus];
    return {
        box, toggle,
        row: {
            name: name.getText(false), hint: hint.getText(false), note: note.getVisible() ? note.getText() : "",
            status: status.getText(), canTick: box.getVisible(), ticked: box.getSelected(),
            child: line.data("level") === "child"
        }
    };
}

function listItems(page: UI5Element): CustomListItem[] {
    return byId<List>(page, "importList").getItems() as CustomListItem[];
}

function importRows(page: UI5Element): ImportRow[] {
    return listItems(page).map((item) => rowParts(item).row);
}

function importRow(page: UI5Element, name: string): ImportRow {
    return importRows(page).filter((row) => row.name === name)[0];
}

function strip(page: UI5Element, id: string): string {
    const control = byId<MessageStrip>(page, id);
    return control.getVisible() ? control.getText() : "";
}

function texts(page: UI5Element, id: string): string[] {
    return (byId<VBox>(page, id).getItems() as unknown as { getText(): string }[]).map((item) => item.getText());
}

function apply(page: UI5Element): { text: string; enabled: boolean } {
    const button = byId<Button>(page, "importApplyButton");
    return { text: button.getText(), enabled: button.getEnabled() };
}

/** Markup that a name, label or message of the document would have become. */
function markup(page: UI5Element): number {
    return byId<Control>(page, "odataImportDialog").getDomRef()?.querySelectorAll("b, script, img, i").length ?? -1;
}

/** Runs `prepare` on the fake once the list is shown, then opens `name`
 *  and waits for its form. `shown` gets a control of the page. */
function iOpen(Given: Common, When: Common, name: string, shown: (page: UI5Element) => void, prepare?: () => void): void {
    Given.iStartTheApp("odata-services");
    When.waitFor({
        id: TABLE,
        viewName: LIST_VIEW,
        success: function () {
            if (prepare) {
                prepare();
            }
            HashChanger.getInstance().setHash(`odata-services/${name}`);
        },
        errorMessage: "The list did not load"
    });
    When.waitFor({
        id: "odataName",
        viewName: VIEW,
        check: function (input: UI5Element) { return (input as Input).getValue() === (name === "new" ? "" : name); },
        success: shown,
        errorMessage: `The form does not show ${name}`
    });
}

/** Presses "Import from $metadata": in the toolbar, or in its overflow menu. */
function iOpenTheImport(When: Common): void {
    When.waitFor({
        id: "odataDetailToolbar",
        viewName: VIEW,
        success: function (toolbar: UI5Element) {
            const button = viewOf(toolbar).byId("odataImportMetadataButton") as Control;
            if (button.getDomRef()) {
                new Press().executeOn(button);
                return;
            }
            new Press().executeOn((toolbar as unknown as { _getOverflowButton(): Control })._getOverflowButton());
            When.waitFor({
                controlType: "sap.m.Button", searchOpenDialogs: true, matchers: withId("odataImportMetadataButton"),
                actions: new Press(), errorMessage: "No import button in the overflow menu"
            });
        },
        errorMessage: "No toolbar"
    });
}

/** Presses "Import from $metadata" for a dialog that is a stand-in: nothing opens. */
function iOpenTheImportStub(When: Common): void {
    When.waitFor({
        id: "odataDetailToolbar", viewName: VIEW,
        success: function (toolbar: UI5Element) {
            const button = viewOf(toolbar).byId("odataImportMetadataButton") as Control;
            if (button.getDomRef()) {
                new Press().executeOn(button);
                return;
            }
            new Press().executeOn((toolbar as unknown as { _getOverflowButton(): Control })._getOverflowButton());
            When.waitFor({
                controlType: "sap.m.Button", searchOpenDialogs: true, matchers: withId("odataImportMetadataButton"),
                actions: new Press(), errorMessage: "No import button in the overflow menu"
            });
        },
        errorMessage: "No toolbar"
    });
}

function iPressInImport(When: Common, id: string): void {
    When.waitFor({
        controlType: "sap.m.Button", searchOpenDialogs: true, matchers: withId(id),
        actions: new Press(), errorMessage: `No button ${id} in the import dialog`
    });
}

/** With a dialog open: runs `assert` once `check` holds. */
function iSeeInImport(Then: Common, what: string, check: () => boolean, assert: () => void): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check,
        success: assert,
        errorMessage: `Not seen in the import dialog: ${what}`
    });
}

/** Does something with the dialog open, as a queued step. */
function iDo(When: Common, action: () => void): void {
    When.waitFor({ controlType: "sap.m.Dialog", searchOpenDialogs: true, success: action, errorMessage: "No dialog is open" });
}

function iTick(When: Common, page: () => UI5Element, name: string): void {
    When.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check: function () { return !!importRow(page(), name); },
        success: function () {
            const item = listItems(page()).filter((candidate) => rowParts(candidate).row.name === name)[0];
            new Press().executeOn(rowParts(item).box);
        },
        errorMessage: `No row ${name} in the import dialog`
    });
}

/** Waits until no dialog is open, then runs `assert` on the page. */
function iSeeThePage(Then: Common, what: string, assert: () => void, check: () => boolean = () => true): void {
    Then.waitFor({
        id: "odataName",
        viewName: VIEW,
        check: function () { return document.querySelectorAll(".sapMDialogOpen").length === 0 && check(); },
        success: assert,
        errorMessage: `Not seen on the page: ${what}`
    });
}

function iReadTheMetadata(When: Common, Then: Common, page: () => UI5Element): void {
    iOpenTheImport(When);
    iPressInImport(When, "importReadButton");
    iSeeInImport(Then, "the document", function () { return strip(page(), "importSummary") !== ""; }, function () {
        Opa5.assert.ok(true, "the document is listed");
    });
}

opaTest("Read metadata posts destination, path, version and identity of the form and lists the document against the service", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    const page = () => shown;

    iOpen(Given, When, JOBS, function (element) { shown = element; });
    iOpenTheImport(When);
    iSeeInImport(Then, "the dialog before a read", function () { return !!byId<Control>(shown, "odataImportDialog")?.getDomRef(); }, function () {
        Opa5.assert.deepEqual([
            byId<Text>(shown, "importDestination").getText(false), byId<Text>(shown, "importVersion").getText(false),
            byId<Text>(shown, "importPath").getText(false), byId<Text>(shown, "importIdentity").getText(false)
        ], ["S4_ODATA_TECH", "V2", PATH, "Reads the metadata as the technical user of the destination."],
        "where the read goes is the form's, shown and not edited");
        Opa5.assert.strictEqual(backend.countRequests(READ), 0, "nothing is read before the button is pressed");
        Opa5.assert.deepEqual(apply(shown), { text: "Nothing selected", enabled: false });
    });
    iPressInImport(When, "importReadButton");
    iSeeInImport(Then, "the document", function () { return strip(shown, "importSummary") !== ""; }, function () {
        Opa5.assert.deepEqual(backend.bodies[READ], {
            destination: "S4_ODATA_TECH", service_path: PATH, odata_version: "v2", user_context: false, service: JOBS
        }, "the request names the stored service, so the server compares with it");
        Opa5.assert.strictEqual(
            strip(shown, "importSummary"),
            "The metadata declares entity sets: 5, operations: 1. New: 0. Changed: 1. Already in this service: 5. No longer in the metadata: 0."
        );
        Opa5.assert.deepEqual(importRows(shown).map((row) => [row.name, row.status, row.canTick, row.child]), [
            [ITEM, "2 differences", false, false],
            ["PurReqnOrigin", "new", true, true],
            ["LastChangeDateTime", "new", true, true],
            ["A_PurchaseRequisitionHeader", "in service", false, false],
            ["A_PurReqnAcctAssgmt", "in service", false, false],
            ["A_PurchaseReqnItemText", "in service", false, false],
            ["A_PurReqAddDelivery", "in service", false, false],
            ["ReleaseItem", "in service", false, false]
        ], "existing entity sets are tagged 'in service'; the changed one lists its new fields underneath");
        Opa5.assert.strictEqual(importRow(shown, ITEM).hint, "Purchase requisition item · 91 fields · 4 navigations · SAP declares: update");
        Opa5.assert.strictEqual(
            importRow(shown, "PurReqnOrigin").hint, "Origin of requisition · Edm.String · SAP declares: filter, update",
            "what SAP declares is said, as information"
        );
        Opa5.assert.strictEqual(importRow(shown, "ReleaseItem").hint,
            "Function import (V2) · POST · 3 parameters · bound to A_PurchaseRequisitionItem");
        Opa5.assert.deepEqual(importRows(shown).filter((row) => row.ticked), [], "nothing is ticked for the admin");
        Opa5.assert.deepEqual(apply(shown), { text: "Nothing selected", enabled: false });
        Opa5.assert.strictEqual(byId<Panel>(shown, "importSkipped").getHeaderText(), "Left out of the document, because it could not be read: 1");
        Opa5.assert.deepEqual(texts(shown, "importSkippedList"), ["Field at position 38 of A_PurReqnAcctAssgmt: invalid_type"]);
        Opa5.assert.strictEqual(markup(shown), 0);
    });
    iPressInDialog(When, "Cancel");
    iSeeThePage(Then, "the page after Cancel", function () {
        Opa5.assert.strictEqual(writes(), 0);
    });
    void page;
    Then.iStopTheApp();
});

opaTest("ticking one new field enables 'Add 1 item(s)'; Apply adds it unticked, sets when the metadata was read, and nothing is stored until Save", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    let shown: UI5Element;
    let before = "";
    const page = () => shown;

    iOpen(Given, When, JOBS, function (element) {
        shown = element;
        before = JSON.stringify(formData(shown).definition);
    });
    iReadTheMetadata(When, Then, page);
    iTick(When, page, "PurReqnOrigin");
    iSeeInImport(Then, "the count on Apply", function () { return apply(shown).enabled; }, function () {
        Opa5.assert.deepEqual(apply(shown), { text: "Add 1 item(s)", enabled: true });
        Opa5.assert.strictEqual(JSON.stringify(formData(shown).definition), before, "a tick changes nothing on the page");
    });
    iPressInImport(When, "importApplyButton");
    iSeeThePage(Then, "the applied import", function () {
        const expected = JSON.parse(before) as ODataServiceInput["definition"];
        expected.entity_sets[0].fields.push({
            name: "PurReqnOrigin", type: "Edm.String", label: "Origin of requisition", selectable: false,
            filterable: false, writable: false, hint: "", values: [], personal_data: false
        });
        Opa5.assert.deepEqual(
            formData(shown).definition, expected,
            "the field arrives unticked although SAP declares it filterable and updatable; all the admin's work is as it was"
        );
        Opa5.assert.strictEqual(formData(shown).metadata_fetched_at, "2026-10-05T09:00:00+00:00", "when the document was read is in the form");
        Opa5.assert.ok(/^Metadata read /.test(byId<Text>(shown, "odataMetadataInfo").getText(false)), "and said above the entity sets");
        Opa5.assert.ok(/ of 90$/.test(entityRow(shown, "Requisition item").fields), "one more field in the row");
        Opa5.assert.deepEqual(ticked(formSet(shown, ITEM)), ticked(stored(JOBS).definition.entity_sets[0]), "none more readable or writable");
        Opa5.assert.strictEqual(stripOf(shown, "odataPendingWrites").visible, false, "an import opens nothing: no pending strip");
        Opa5.assert.strictEqual(writes(), 0, "nothing is sent by Apply");
        Opa5.assert.strictEqual(stored(JOBS).definition.entity_sets[0].fields.length, 89, "and nothing is stored before Save");
        Opa5.assert.ok(toasts().indexOf("Imported. Review the entity sets and save.") !== -1);
    }, function () { return formSet(shown, ITEM).fields.length === 90; });
    When.waitFor({ id: "odataSaveButton", viewName: VIEW, actions: new Press(), errorMessage: "No Save" });
    iSeeThePage(Then, "the save", function () {
        const sent = backend.bodies[PUT] as unknown as ODataServiceInput;
        Opa5.assert.strictEqual(sent.metadata_fetched_at, "2026-10-05T09:00:00+00:00", "Save stores when the metadata was read");
        Opa5.assert.strictEqual(sent.definition.entity_sets[0].fields.length, 90, "and the field");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "Save asked nothing: nothing was opened");
    }, function () { return backend.countRequests(PUT) === 1; });
    Then.iStopTheApp();
});

opaTest("a first import into a new service: whatever SAP declares, entity sets arrive with nothing enabled and operations switched off", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    const page = () => shown;

    iOpen(Given, When, "new", function (element) { shown = element; }, function () {
        const offer = backend.metadataPreview;
        offer.entity_sets.forEach((entitySet) => {
            entitySet.declared = { creatable: true, updatable: true, deletable: true };
            entitySet.fields.forEach((f) => { f.declared = { filterable: true, creatable: true, updatable: true }; });
        });
        offer.operations.push(FakeBackend.previewOperation({
            name: "GetStrategy", kind: "function_import", http_method: "GET", label: "<b>x</b> {y}",
            suggested: { changes_data: false, known: false, returns: { entity_set: ITEM, collection: false, type: "" } }
        }));
    });
    iOpenTheImport(When);
    iPressInImport(When, "importReadButton");
    iSeeInImport(Then, "why nothing is read", function () { return strip(shown, "importError") !== ""; }, function () {
        Opa5.assert.strictEqual(strip(shown, "importError"),
            "The metadata cannot be read yet: enter a valid destination and service path on the page first.");
        Opa5.assert.strictEqual(backend.countRequests(READ), 0, "no request without a destination and a path");
    });
    iPressInDialog(When, "Cancel");
    When.waitFor({ id: "odataDestination", viewName: VIEW, actions: new EnterText({ text: "S4_ODATA_USER" }), errorMessage: "No destination field" });
    When.waitFor({ id: "odataServicePath", viewName: VIEW, actions: new EnterText({ text: PATH }), errorMessage: "No path field" });
    iReadTheMetadata(When, Then, page);
    iSeeInImport(Then, "everything as new", function () { return importRows(shown).length === 7; }, function () {
        Opa5.assert.deepEqual(backend.bodies[READ], {
            destination: "S4_ODATA_USER", service_path: PATH, odata_version: "v2", user_context: false
        }, "a service that is not stored names none to compare with");
        Opa5.assert.deepEqual(importRows(shown).map((row) => [row.status, row.canTick, row.ticked]),
            new Array(7).fill(["new", true, false]), "everything is new, and nothing is ticked");
        Opa5.assert.strictEqual(importRow(shown, "GetStrategy").hint.indexOf("<b>x</b> {y} · "), 0, "a label is shown as the text it is");
        Opa5.assert.strictEqual(markup(shown), 0, "and makes no markup");
        Opa5.assert.ok(
            (byId<Control>(shown, "importList").getDomRef()?.textContent ?? "").indexOf("<b>x</b> {y} · Function import (V2)") !== -1,
            "what is on screen is the label itself, character for character"
        );
    });
    iTick(When, page, "ReleaseItem");
    iSeeInImport(Then, "what blocks Apply", function () { return texts(shown, "importBlocked").length === 1; }, function () {
        Opa5.assert.deepEqual(texts(shown, "importBlocked"), [
            `The operation ReleaseItem is bound to the entity set ${ITEM}, which is not in this service. Tick ${ITEM} too, or untick ReleaseItem.`
        ]);
        Opa5.assert.strictEqual(apply(shown).enabled, false, "an operation without its entity set is not applied");
    });
    iTick(When, page, ITEM);
    iTick(When, page, "GetStrategy");
    iSeeInImport(Then, "three to add", function () { return apply(shown).enabled; }, function () {
        Opa5.assert.deepEqual(apply(shown), { text: "Add 3 item(s)", enabled: true });
    });
    iPressInImport(When, "importApplyButton");
    iSeeThePage(Then, "the imported service", function () {
        const definition = formData(shown).definition;
        Opa5.assert.deepEqual(definition.entity_sets.map((e) => [e.name, e.operations, ticked(e), e.fields.length]),
            [[ITEM, [], [], 91]], "no operation enabled and no field ticked, although SAP declares create, update, delete and filter");
        Opa5.assert.deepEqual(definition.operations.map((o) => [o.name, o.enabled, o.changes_data, o.returns]), [
            ["ReleaseItem", false, true, null],
            ["GetStrategy", false, true, { entity_set: ITEM, collection: false }]
        ], "operations are switched off; a suggestion the document does not know is not taken: it counts as changing data");
        Opa5.assert.deepEqual(entityTitles(shown), ["Purchase requisition item"]);
        const row = entityRow(shown, "Purchase requisition item");
        Opa5.assert.deepEqual([row.ticked, row.fields], [[], "0 of 91"]);
        Opa5.assert.deepEqual(
            [operationRow(shown, "ReleaseItem").enabled, operationRow(shown, "<b>x</b> {y}").enabled], [false, false]
        );
        Opa5.assert.strictEqual(stripOf(shown, "odataPendingWrites").visible, false, "nothing pending: the import opened nothing");
        Opa5.assert.strictEqual(byId<Control>(shown, "odataOperationsTable").getDomRef()!.querySelectorAll("b").length, 0);
        Opa5.assert.strictEqual(writes(), 0, "nothing is stored");
    }, function () { return formData(shown).definition.entity_sets.length === 1; });
    Then.iStopTheApp();
});

opaTest("Cancel leaves the form exactly as it was, also with ticks; a page that is left closes the import", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    let before = "";
    const page = () => shown;

    iOpen(Given, When, JOBS, function (element) {
        shown = element;
        before = JSON.stringify(formData(shown));
    });
    iReadTheMetadata(When, Then, page);
    iTick(When, page, "PurReqnOrigin");
    iTick(When, page, "LastChangeDateTime");
    iSeeInImport(Then, "two ticks", function () { return apply(shown).text === "Add 2 item(s)"; }, function () {
        Opa5.assert.ok(true, "two fields are ticked");
    });
    iPressInImport(When, "importCancelButton");
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check: function (dialogs: UI5Element[]) { return dialogs.length === 2; },
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(messageOf(dialogs[1]), "Close the import without applying the 2 ticked item(s)?", "Cancel asks first");
        },
        errorMessage: "No question about the ticks"
    });
    iPressInDialog(When, "Discard");
    iSeeThePage(Then, "the page after Cancel", function () {
        Opa5.assert.strictEqual(JSON.stringify(formData(shown)), before, "the form is exactly as it was: definition and metadata date");
        Opa5.assert.strictEqual(
            JSON.stringify((viewOf(shown).getModel("svc") as JSONModel).getProperty("/original")), before, "so there is nothing to save"
        );
        Opa5.assert.strictEqual(writes(), 0);
    });

    // The address changes under the open dialog: it goes, nothing is applied.
    iReadTheMetadata(When, Then, page);
    iTick(When, page, "PurReqnOrigin");
    iDo(When, function () { HashChanger.getInstance().setHash(`odata-services/${UNUSED}`); });
    iSeeThePage(Then, "the other service", function () {
        Opa5.assert.strictEqual(
            JSON.stringify(formData(shown).definition), JSON.stringify(stored(UNUSED).definition),
            "the other service is shown as stored, without anything of the import"
        );
    }, function () { return byId<Input>(shown, "odataName").getValue() === UNUSED; });
    Then.iStopTheApp();
});

opaTest("a re-import lists what the document no longer has and removes it only when ticked; a changed key is taken only when ticked", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    let before = "";
    const page = () => shown;

    iOpen(Given, When, JOBS, function (element) {
        shown = element;
        before = JSON.stringify(formData(shown).definition);
    }, function () {
        const offer = backend.metadataPreview;
        // The document lost an entity set, a field and the operation; the
        // header's key grew by a field the service does not hold.
        offer.entity_sets = offer.entity_sets.filter((entitySet) => entitySet.name !== "A_PurReqAddDelivery");
        offer.entity_sets[0].fields = offer.entity_sets[0].fields.filter((f) => f.name !== "PurchaseRequisitionItemText");
        offer.entity_sets[1].keys = offer.entity_sets[1].keys.concat([{ name: "Client", type: "Edm.String" }]);
        offer.entity_sets[1].keys_total = 2;
        offer.operations = [];
        offer.totals = { entity_sets: 4, operations: 0, skipped: 0 };
        offer.skipped = [];
        const definition = stored(JOBS).definition;
        definition.operations[0].bound_to = "A_PurReqAddDelivery";
        definition.entity_sets.filter((e) => e.name === "A_PurReqAddDelivery")[0].title = "<b>x</b> {y}";
        definition.entity_sets[0].fields.push({
            name: "PurchaseRequisitionItemText", type: "Edm.String", label: "Short text", selectable: true,
            filterable: false, writable: true, hint: "", values: [], personal_data: false
        });
    });
    iReadTheMetadata(When, Then, page);
    iSeeInImport(Then, "what is gone", function () { return !!importRow(shown, "A_PurReqAddDelivery"); }, function () {
        Opa5.assert.strictEqual(
            strip(shown, "importSummary"),
            "The metadata declares entity sets: 4, operations: 0. New: 0. Changed: 2. Already in this service: 2. No longer in the metadata: 2."
        );
        Opa5.assert.deepEqual(importRow(shown, "PurchaseRequisitionItemText"), {
            name: "PurchaseRequisitionItemText", hint: "Short text · Edm.String", status: "no longer in the metadata",
            note: "Tick to remove the field from this service, with its label, hint and value meanings. "
                + "Agents can read it today. Agents can write it today.",
            canTick: true, ticked: false, child: true
        }, "a removed field says what goes with it, and is not ticked");
        Opa5.assert.deepEqual(importRow(shown, "Key"), {
            name: "Key", status: "changed", note: "", canTick: true, ticked: false, child: true,
            hint: "This service has PurchaseRequisition; the metadata says PurchaseRequisition, Client. "
                + "Tick to take the key from the metadata."
        }, "a changed key is a row the admin must tick");
        Opa5.assert.ok(renderedText(shown, "importList").indexOf("<b>x</b> {y}") !== -1, "a removed row shows the stored title as the characters it is");
        Opa5.assert.strictEqual(markup(shown), 0);
        Opa5.assert.strictEqual(importRow(shown, "A_PurReqAddDelivery").note,
            "Tick to remove it from this service, with its title, description, fields and examples. "
            + "The entity set \"<b>x</b> {y}\" (A_PurReqAddDelivery) cannot be removed while these operations are bound to it: "
            + "Release item. Tick them for removal too, or untick the entity set.");
        Opa5.assert.strictEqual(importRow(shown, "ReleaseItem").note,
            "Tick to remove the operation from this service, with its title and description. It is enabled: agents can call it today.");
        Opa5.assert.strictEqual(byId<Button>(shown, "importSelectRemoved").getText(), "Tick all 3 to remove");
    });
    iTick(When, page, "A_PurReqAddDelivery");
    iSeeInImport(Then, "the blocked removal", function () { return texts(shown, "importBlocked").length === 1; }, function () {
        Opa5.assert.deepEqual(apply(shown), { text: "Apply: add 0, change 0, remove 1", enabled: false },
            "an entity set an operation is bound to is not removed");
    });
    iPressInImport(When, "importSelectRemoved");
    iSeeInImport(Then, "all removals ticked", function () { return apply(shown).enabled; }, function () {
        Opa5.assert.deepEqual(apply(shown), { text: "Apply: add 0, change 0, remove 3", enabled: true });
        Opa5.assert.deepEqual(texts(shown, "importBlocked"), [], "with the operation ticked too, nothing blocks");
        Opa5.assert.strictEqual(importRow(shown, "Key").ticked, false, "the key change is not ticked along");
        Opa5.assert.strictEqual(JSON.stringify(formData(shown).definition), before, "and nothing has left the form yet");
    });
    iPressInImport(When, "importApplyButton");
    iSeeThePage(Then, "the form without what was removed", function () {
        const expected = JSON.parse(before) as ODataServiceInput["definition"];
        expected.entity_sets[0].fields.pop();
        expected.entity_sets.pop();
        expected.operations = [];
        Opa5.assert.deepEqual(formData(shown).definition, expected,
            "exactly the three ticked things are gone; the key of the header and everything else is as it was");
        Opa5.assert.deepEqual(operationsHeader(shown).titles, []);
        Opa5.assert.strictEqual(entityTitles(shown).indexOf("Delivery address"), -1);
        Opa5.assert.strictEqual(writes(), 0, "nothing is stored before Save");
        Opa5.assert.strictEqual(stored(JOBS).definition.entity_sets.length, 5);
    }, function () { return formData(shown).definition.entity_sets.length === 4; });
    Then.iStopTheApp();
});

opaTest("a refusal of the read is said inside the dialog by its code, in plain words and as text", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    const CASES: [string, number, string, string][] = [
        ["user_token_required", 424, "This fetch runs in SAP as the signed-in user",
            "Metadata not read: this service runs as the signed-in user, and no user sign-in reached the server with this "
            + "request. Sign in again and retry."],
        ["timeout", 504, "the $metadata preview did not finish within 25 seconds",
            "Metadata not read: reading the document took too long and was stopped. Try again; a very large service may not be readable."],
        ["busy", 429, "other previews or test calls are running; try again in a moment",
            "Metadata not read: other metadata reads or test calls are running. Try again in a moment."],
        ["sap_error", 502, "HTTP 403 from the OData service: <b>x</b> {y}",
            "Metadata not read. SAP answered: HTTP 403 from the OData service: <b>x</b> {y}"],
        // The status of another code: the code decides, not the status.
        // What an on-premise destination that cannot be used answers: the server's own text, as text.
        ["destination_error", 504, "the destination <b>x</b> {y} has no CloudConnectorLocationId",
            "Metadata not read: the destination <b>x</b> {y} has no CloudConnectorLocationId"],
        // A service that runs as the signed-in user on a destination that does not sign in as the user: refused, in the server's fixed words.
        ["destination_error", 502,
            "destination 'S4_<b>x</b>' does not sign in as the user: a service that runs as the signed-in user needs a "
            + "user-propagating destination (OAuth2JWTBearer, OAuth2UserTokenExchange, OAuth2SAMLBearerAssertion) or, on-premise, PrincipalPropagation",
            "Metadata not read: destination 'S4_<b>x</b>' does not sign in as the user: a service that runs as the signed-in user needs a "
            + "user-propagating destination (OAuth2JWTBearer, OAuth2UserTokenExchange, OAuth2SAMLBearerAssertion) or, on-premise, PrincipalPropagation"],
        ["proxy_refused", 502,
            "HTTP 407 from the connectivity proxy: the connectivity proxy refused the request before it reached SAP",
            "Metadata not read: the connectivity proxy refused the request before it reached SAP. Check this app's connectivity "
            + "service binding and the CloudConnectorLocationId of the destination; for a service that runs as the signed-in user "
            + "also the principal propagation mode and the trust of the Cloud Connector."]
    ];

    iOpen(Given, When, JOBS, function (element) { shown = element; });
    iOpenTheImport(When);
    CASES.forEach(function ([code, status, detail, expected], index) {
        iDo(When, function () {
            backend.failNext = { path: "odata/metadata", status, body: { detail }, headers: { "X-OData-Error": code } };
        });
        iPressInImport(When, "importReadButton");
        iSeeInImport(Then, code, function () {
            return backend.countRequests(READ) === index + 1 && strip(shown, "importError") === expected;
        }, function () {
            Opa5.assert.strictEqual(strip(shown, "importError"), expected, `${code} is said in plain words`);
            Opa5.assert.strictEqual(strip(shown, "importSummary"), "", "and nothing is listed");
            Opa5.assert.strictEqual(markup(shown), 0, "as text");
            Opa5.assert.ok(
                (byId<Control>(shown, "importError").getDomRef()?.textContent ?? "").indexOf(expected) !== -1,
                `what is on screen is the text itself, character for character: ${code}`
            );
            Opa5.assert.strictEqual(apply(shown).enabled, false);
        });
    });
    iPressInImport(When, "importReadButton");
    iSeeInImport(Then, "a read that works", function () { return strip(shown, "importSummary") !== ""; }, function () {
        Opa5.assert.strictEqual(strip(shown, "importError"), "", "the refusal goes with the next read");
    });
    iPressInDialog(When, "Cancel");
    iSeeThePage(Then, "the page", function () { Opa5.assert.strictEqual(writes(), 0); });
    Then.iStopTheApp();
});

opaTest("a cut document, an incomplete one and what could not be compared are said; nothing is offered for removal then", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    const page = () => shown;

    iOpen(Given, When, JOBS, function (element) { shown = element; }, function () {
        const offer = backend.metadataPreview;
        offer.truncated = true;
        offer.totals = { entity_sets: 300, operations: 250, skipped: 1 };
        offer.removed_complete = false;
        offer.entity_sets = offer.entity_sets.slice(0, 3);
        offer.entity_sets[0].truncated = true;
        offer.entity_sets[0].fields_total = 600;
        offer.entity_sets[1].label = "<b>x</b> {y}";
        offer.warnings = [
            { code: "metadata_incomplete", message: "parts of the $metadata document were too long to read" },
            { code: "technical_credential", message: "server words" },
            { code: "something_new", message: "<i>a warning this page does not know</i>" },
            { code: "another_new", message: "<b>x</b> {y}" }
        ];
    });
    iReadTheMetadata(When, Then, page);
    iSeeInImport(Then, "what is said about the document", function () { return strip(shown, "importTruncated") !== ""; }, function () {
        Opa5.assert.strictEqual(strip(shown, "importTruncated"),
            "The document is larger than what can be listed: it declares 300 entity sets and 250 operations; the first 3 and 1 "
            + "are listed. Some entity sets may be listed with part of their fields.");
        Opa5.assert.strictEqual(strip(shown, "importRemovalsUnknown"),
            "The document was not read to its end, so it is not known whether something was removed from it. Nothing is offered for removal.");
        Opa5.assert.deepEqual(texts(shown, "importWarnings"), [
            "Parts of the $metadata document could not be read and were left out, so this preview may be incomplete.",
            // No longer a warning of the preview (the server refuses such a read): not in this page's words.
            "server words",
            "<i>a warning this page does not know</i>",
            "<b>x</b> {y}"
        ], "a known warning in this page's words, an unknown one in the server's, as text");
        Opa5.assert.strictEqual(importRow(shown, ITEM).note,
            "Only 91 of its 600 fields are listed. A key field may be missing: check the key after the import.");
        Opa5.assert.strictEqual(importRow(shown, "A_PurchaseRequisitionHeader").hint.indexOf("<b>x</b> {y} · "), 0);
        Opa5.assert.deepEqual(importRows(shown).filter((row) => row.status === "no longer in the metadata"), [],
            "the two entity sets the document does not list are not called removed");
        Opa5.assert.strictEqual(byId<Button>(shown, "importSelectRemoved").getVisible(), false);
        Opa5.assert.strictEqual(markup(shown), 0, "no name, label or message became markup");
        const warningsOnScreen = renderedText(shown, "importWarnings");
        Opa5.assert.ok(warningsOnScreen.indexOf("<i>a warning this page does not know</i>") !== -1 && warningsOnScreen.indexOf("<b>x</b> {y}") !== -1,
            `[${warningsOnScreen}] the warnings are on screen as the characters the server sent: no markup, no binding syntax taken`);
        const strips = (byId<Control>(shown, "odataImportDialog") as unknown as {
            findAggregatedObjects(deep: boolean, check: (c: Control) => boolean): MessageStrip[];
        }).findAggregatedObjects(true, (control) => control.isA("sap.m.MessageStrip"));
        Opa5.assert.ok(strips.length >= 5, `the dialog has its strips (${strips.length})`);
        Opa5.assert.deepEqual(strips.filter((strip) => strip.getEnableFormattedText()).map((strip) => strip.getId()), [],
            "no message strip of the dialog formats its text");
    });
    iPressInDialog(When, "Cancel");
    iSeeThePage(Then, "the page", function () { Opa5.assert.strictEqual(writes(), 0); });
    Then.iStopTheApp();
});

opaTest("200 entity sets, one of them with 500 fields: listed fifty at a time, found by search and by status", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    const page = () => shown;

    iOpen(Given, When, JOBS, function (element) { shown = element; }, function () {
        const offer = backend.metadataPreview;
        const item = offer.entity_sets[0];
        const fields = item.fields.slice();
        for (let i = fields.length; i < 500; i++) {
            fields.push({ name: `Extra${i}`, type: "Edm.String", label: `Extra field ${i}`, declared: { filterable: true, creatable: true, updatable: true } });
        }
        item.fields = fields;
        item.fields_total = 500;
        const sets = [item];
        for (let i = 1; i < 200; i++) {
            sets.push({ ...offer.entity_sets[1], name: `Set${i}`, label: `Entity set ${i}` });
        }
        offer.entity_sets = sets;
        offer.totals = { entity_sets: 200, operations: 1, skipped: 1 };
    });
    iReadTheMetadata(When, Then, page);
    iSeeInImport(Then, "the first rows", function () { return importRows(shown).length > 0; }, function () {
        Opa5.assert.strictEqual(importRows(shown).length, 50, "fifty rows are rendered, whatever the document holds");
        Opa5.assert.strictEqual(importRow(shown, ITEM).status, "411 differences");
        Opa5.assert.strictEqual(importRows(shown).filter((row) => row.child).length, 0, "411 fields are not listed until their entity set is opened");
    });
    When.waitFor({
        controlType: "sap.m.SearchField", searchOpenDialogs: true, matchers: withId("importSearch"),
        actions: new EnterText({ text: "entity set 19" }), errorMessage: "No search field"
    });
    iSeeInImport(Then, "the search result", function () { return importRows(shown).length === 11; }, function () {
        Opa5.assert.deepEqual(importRows(shown).map((row) => row.name),
            ["Set19", "Set190", "Set191", "Set192", "Set193", "Set194", "Set195", "Set196", "Set197", "Set198", "Set199"]);
    });
    iTick(When, page, "Set199");
    When.waitFor({
        controlType: "sap.m.SearchField", searchOpenDialogs: true, matchers: withId("importSearch"),
        actions: new EnterText({ text: "" }), errorMessage: "No search field"
    });
    iDo(When, function () {
        const select = byId<Control>(shown, "importFilter") as unknown as { setSelectedKey(key: string): void; fireChange(p: object): void };
        select.setSelectedKey("changed");
        select.fireChange({});
    });
    iSeeInImport(Then, "the changed ones", function () { return importRows(shown).length === 1; }, function () {
        Opa5.assert.deepEqual(importRows(shown).map((row) => row.name), [ITEM]);
        Opa5.assert.deepEqual(apply(shown), { text: "Add 1 item(s)", enabled: true }, "a tick outside the filter still counts");
    });
    iPressInImport(When, "importApplyButton");
    iSeeThePage(Then, "the added entity set", function () {
        const added = formSet(shown, "Set199");
        Opa5.assert.deepEqual([added.title, added.operations, ticked(added)], ["Entity set 199", [], []]);
        Opa5.assert.strictEqual(writes(), 0);
    }, function () { return formData(shown).definition.entity_sets.length === 6; });
    Then.iStopTheApp();
});

opaTest("Save, Create in the duplicate dialog and Read metadata take the destination the field shows, also without a focus-out", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${UNUSED}`;
    const COPY = `POST odata/services/${UNUSED}/duplicate`;
    let shown: UI5Element;
    type Handlers = { onSave(): Promise<void>; onDuplicateConfirm(): Promise<void> };
    const controller = () => viewOf(shown).getController() as unknown as Handlers;
    /** What a touch keyboard leaves behind: the text is in the field, and
     *  the field never fired `change`. */
    const show = (id: string, value: string) => (byId<Input>(shown, id) as unknown as { setDOMValue(v: string): void }).setDOMValue(value);

    iOpen(Given, When, UNUSED, function (element) { shown = element; });
    When.waitFor({
        id: "odataDestination", viewName: VIEW,
        success: function () {
            show("odataDestination", "S4_ODATA_TECH");
            Opa5.assert.strictEqual(formData(shown).destination, "S4_ODATA_USER", "the form does not know what the field shows");
        },
        errorMessage: "No destination field"
    });
    iOpenTheImport(When);
    iPressInImport(When, "importReadButton");
    iSeeInImport(Then, "the read", function () { return backend.countRequests(READ) === 1; }, function () {
        Opa5.assert.strictEqual(backend.bodies[READ]?.destination, "S4_ODATA_TECH", "Read metadata reads through the destination the field shows");
    });
    iPressInDialog(When, "Cancel");
    iSeeThePage(Then, "the page", function () {
        show("odataDestination", "S4_ODATA_USER2");
        void controller().onSave();
    });
    iSeeThePage(Then, "the save", function () {
        Opa5.assert.strictEqual(backend.bodies[PUT]?.destination, "S4_ODATA_USER2", "Save stores the destination the field shows");
    }, function () { return backend.countRequests(PUT) === 1; });
    When.waitFor({ id: "odataDuplicateButton", viewName: VIEW, actions: new Press(), errorMessage: "No Duplicate" });
    When.waitFor({
        controlType: "sap.m.Input", searchOpenDialogs: true, matchers: withId("odataDuplicateName"),
        actions: new EnterText({ text: "copy-of-v4" }), errorMessage: "No name field in the duplicate dialog"
    });
    iDo(When, function () {
        show("odataDuplicateDestination", "S4_ODATA_TECH");
        void controller().onDuplicateConfirm();
    });
    Then.waitFor({
        check: function () { return backend.countRequests(COPY) === 1; },
        success: function () {
            Opa5.assert.strictEqual(backend.bodies[COPY]?.destination, "S4_ODATA_TECH", "Create copies to the destination the field shows");
        },
        errorMessage: "The duplicate was not sent"
    });
    Then.iStopTheApp();
});

/** The dialog's own text of a region, as it is on screen. */
function renderedText(page: UI5Element, id: string): string {
    return byId<Control>(page, id).getDomRef()?.textContent ?? "";
}

/** Waits `ms` after `since()` before `assert` runs: what a held answer does once released. */
function iSeeLater(Then: Common, since: () => number, ms: number, assert: () => void, inDialog = false): void {
    Then.waitFor({
        ...(inDialog ? { controlType: "sap.m.Dialog", searchOpenDialogs: true } : { id: "odataName", viewName: VIEW }),
        check: function () { return Date.now() - since() > ms; },
        success: assert,
        errorMessage: "The wait did not end"
    });
}

opaTest("an answer that comes after Cancel and reopening is not shown in the new dialog", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    let released = 0;
    const page = () => shown;
    void page;

    iOpen(Given, When, JOBS, function (element) { shown = element; }, function () {
        backend.metadataHeld = true;
        backend.metadataPreview.warnings = [{ code: "something_new", message: "from the first read" }];
    });
    iOpenTheImport(When);
    iPressInImport(When, "importReadButton");
    iSeeInImport(Then, "the read is on its way", function () { return backend.countRequests(READ) === 1; }, function () {
        Opa5.assert.strictEqual(byId<Button>(shown, "importReadButton").getText(), "Reading the metadata...");
        Opa5.assert.strictEqual(byId<Button>(shown, "importReadButton").getEnabled(), false, "a second read cannot be started meanwhile");
    });
    iPressInImport(When, "importCancelButton");
    iSeeThePage(Then, "the page after Cancel", function () { Opa5.assert.ok(true, "closed with nothing ticked: no question"); });
    iOpenTheImport(When);
    iSeeInImport(Then, "the reopened dialog", function () { return byId<Button>(shown, "importReadButton").getText() === "Read metadata"; }, function () {
        Opa5.assert.ok(true, "it is not reading");
    });
    iDo(When, function () { released = Date.now(); backend.releaseMetadata(); });
    iSeeLater(Then, () => released, 200, function () {
        Opa5.assert.strictEqual(strip(shown, "importSummary"), "", "the answer to the first read is not listed");
        Opa5.assert.deepEqual(texts(shown, "importWarnings"), [], "nor its warning");
        Opa5.assert.deepEqual(importRows(shown), [], "nor its rows");
        Opa5.assert.deepEqual(apply(shown), { text: "Nothing selected", enabled: false });
        Opa5.assert.strictEqual(byId<Button>(shown, "importReadButton").getEnabled(), true, "and the dialog is not left reading");
        backend.metadataHeld = false;
    }, true);
    iPressInImport(When, "importReadButton");
    iSeeInImport(Then, "a read of the reopened dialog", function () { return strip(shown, "importSummary") !== ""; }, function () {
        Opa5.assert.deepEqual(texts(shown, "importWarnings"), ["from the first read"], "the dialog works for a read of its own");
    });
    iPressInDialog(When, "Cancel");
    iSeeThePage(Then, "the page", function () { Opa5.assert.strictEqual(writes(), 0); });
    Then.iStopTheApp();
});

opaTest("an answer that comes after the address changed to another service is not shown there", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    let released = 0;

    iOpen(Given, When, JOBS, function (element) { shown = element; }, function () {
        backend.metadataHeld = true;
        backend.metadataPreview.warnings = [{ code: "something_new", message: "from the first service" }];
    });
    iOpenTheImport(When);
    iPressInImport(When, "importReadButton");
    iSeeInImport(Then, "the read is on its way", function () { return backend.countRequests(READ) === 1; }, function () {
        Opa5.assert.ok(true, "asked");
    });
    iDo(When, function () { HashChanger.getInstance().setHash(`odata-services/${UNUSED}`); });
    iSeeThePage(Then, "the other service", function () {
        Opa5.assert.strictEqual(
            JSON.stringify(formData(shown).definition), JSON.stringify(stored(UNUSED).definition), "shown as stored"
        );
    }, function () { return byId<Input>(shown, "odataName").getValue() === UNUSED; });
    // The import of the other service is opened before the first answer comes.
    iOpenTheImport(When);
    iSeeInImport(Then, "the import of the other service", function () { return byId<Button>(shown, "importReadButton").getText() === "Read metadata"; }, function () {
        Opa5.assert.ok(true, "open, not reading");
    });
    iDo(When, function () { released = Date.now(); backend.releaseMetadata(); });
    iSeeLater(Then, () => released, 200, function () {
        Opa5.assert.strictEqual(strip(shown, "importSummary"), "", "the answer for the first service is not listed here");
        Opa5.assert.deepEqual(texts(shown, "importWarnings"), []);
        Opa5.assert.deepEqual(importRows(shown), []);
        Opa5.assert.strictEqual(byId<Button>(shown, "importReadButton").getEnabled(), true);
        backend.metadataHeld = false;
    }, true);
    iPressInDialog(When, "Cancel");
    Then.iStopTheApp();
});

opaTest("an Apply that arrives after another service was loaded into the page is not written into that service", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    let answer: (result: { definition: ODataServiceInput["definition"]; metadata_fetched_at: string } | undefined) => void = () => undefined;
    let waited = 0;

    iOpen(Given, When, JOBS, function (element) { shown = element; });
    // The dialog is a stand-in the test holds: its Apply comes when the test says, which is after the page
    // has loaded the other service (a real one closes with an animation, and the answer can be that late).
    When.waitFor({
        id: "odataDetailToolbar", viewName: VIEW,
        success: function () {
            const controller = viewOf(shown).getController() as unknown as { importDialog: unknown };
            controller.importDialog = {
                open: () => new Promise((resolve) => { answer = resolve as typeof answer; }),
                dismiss: () => undefined
            };
        },
        errorMessage: "No toolbar"
    });
    iOpenTheImportStub(When);
    When.waitFor({
        id: "odataName", viewName: VIEW,
        success: function () { HashChanger.getInstance().setHash(`odata-services/${UNUSED}`); },
        errorMessage: "No page"
    });
    iSeeThePage(Then, "the other service", function () { Opa5.assert.ok(true, "shown"); },
        function () { return byId<Input>(shown, "odataName").getValue() === UNUSED && (viewOf(shown).getModel("svc") as JSONModel).getProperty("/loaded") === true; });
    When.waitFor({
        id: "odataName", viewName: VIEW,
        success: function () {
            const merged = JSON.parse(JSON.stringify(stored(JOBS).definition)) as ODataServiceInput["definition"];
            merged.entity_sets[0].title = "Written by the import of the first service";
            answer({ definition: merged, metadata_fetched_at: "2026-10-05T09:00:00+00:00" });
            waited = Date.now();
        },
        errorMessage: "No page"
    });
    iSeeLater(Then, () => waited, 300, function () {
        Opa5.assert.strictEqual(
            JSON.stringify(formData(shown).definition), JSON.stringify(stored(UNUSED).definition),
            "the other service is as stored: nothing of the import of the first one is in it"
        );
        Opa5.assert.strictEqual(formData(shown).metadata_fetched_at, stored(UNUSED).metadata_fetched_at);
        Opa5.assert.strictEqual(toasts().indexOf("Imported. Review the entity sets and save."), -1, "and it does not say it applied anything");
        Opa5.assert.strictEqual(writes(), 0);
    });
    Then.iStopTheApp();
});

opaTest("after a first import, an imported operation switched on and a field made writable on an imported entity set reach the pending strip and the Save question", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    const page = () => shown;
    const FIELD = "PurReqnOrigin";

    iOpen(Given, When, "new", function (element) { shown = element; });
    When.waitFor({ id: "odataTitle", viewName: VIEW, actions: new EnterText({ text: "Imported service" }), errorMessage: "No title field" });
    When.waitFor({ id: "odataName", viewName: VIEW, actions: new EnterText({ text: "imported-service" }), errorMessage: "No name field" });
    When.waitFor({ id: "odataPurpose", viewName: VIEW, actions: new EnterText({ text: "Read requisitions" }), errorMessage: "No purpose field" });
    When.waitFor({ id: "odataDestination", viewName: VIEW, actions: new EnterText({ text: "S4_ODATA_USER" }), errorMessage: "No destination field" });
    When.waitFor({ id: "odataServicePath", viewName: VIEW, actions: new EnterText({ text: PATH }), errorMessage: "No path field" });
    iReadTheMetadata(When, Then, page);
    iTick(When, page, ITEM);
    iTick(When, page, "ReleaseItem");
    iPressInImport(When, "importApplyButton");
    iSeeThePage(Then, "the imported service, nothing pending", function () {
        Opa5.assert.strictEqual(stripOf(shown, "odataPendingWrites").visible, false, "the import opened nothing");
    }, function () { return formData(shown).definition.entity_sets.length === 1; });
    // The admin switches on the imported operation, makes a field of the imported entity set writable and ticks Create.
    When.waitFor({
        id: "odataOperationsTable", viewName: VIEW,
        check: function (table: UI5Element) { return !!operationItem(table, "ReleaseItem"); },
        success: function (table: UI5Element) { new Press().executeOn(operationBox(operationItem(table, "ReleaseItem"), "enabled")); },
        errorMessage: "No operations table"
    });
    When.waitFor({
        id: "odataEntityTable", viewName: VIEW,
        success: function (table: UI5Element) { new Press().executeOn(entityItem(table, "Purchase requisition item")); },
        errorMessage: "No entity sets table"
    });
    When.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true, matchers: withId(ENTITY_DIALOG),
        success: function (dialogs: UI5Element[]) {
            const search = entityPart<SearchField>(dialogs[0], "entityFieldSearch");
            search.setValue(FIELD);
            search.fireLiveChange({ newValue: FIELD });
        },
        errorMessage: "No entity set dialog"
    });
    When.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true, matchers: withId(ENTITY_DIALOG),
        check: function (dialogs: UI5Element[]) { return !!fieldItem(dialogs[0], FIELD); },
        success: function (dialogs: UI5Element[]) { new Press().executeOn(fieldBox(fieldItem(dialogs[0], FIELD), "write")); },
        errorMessage: "No field to tick"
    });
    When.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true, matchers: withId(ENTITY_DIALOG),
        success: function (dialogs: UI5Element[]) { new Press().executeOn(entityPart(dialogs[0], "entityApplyButton")); },
        errorMessage: "No Apply in the entity set dialog"
    });
    When.waitFor({
        id: "odataEntityTable", viewName: VIEW,
        check: function (table: UI5Element) { return !!entityItem(table, "Purchase requisition item"); },
        success: function (table: UI5Element) { new Press().executeOn(opBox(entityItem(table, "Purchase requisition item"), "create")); },
        errorMessage: "No entity sets table"
    });
    let strip1 = "";
    iSeeThePage(Then, "the pending strip", function () {
        strip1 = stripOf(shown, "odataPendingWrites").text;
        Opa5.assert.ok(strip1.indexOf("ReleaseItem") !== -1, `the imported operation is named: ${strip1}`);
        Opa5.assert.ok(strip1.indexOf(FIELD) !== -1, `and the imported field made writable: ${strip1}`);
        Opa5.assert.ok(/create/i.test(strip1), `and Create on the imported entity set: ${strip1}`);
        Opa5.assert.strictEqual(writes(), 0, "nothing is sent yet");
    }, function () { return stripOf(shown, "odataPendingWrites").visible; });
    When.waitFor({ id: "odataSaveButton", viewName: VIEW, actions: new Press(), errorMessage: "No Save" });
    Then.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        check: function (dialogs: UI5Element[]) { return dialogs.some((dialog) => messageOf(dialog).indexOf("Saving enables these writes in SAP") !== -1); },
        success: function (dialogs: UI5Element[]) {
            const asked = dialogs.map((dialog) => messageOf(dialog)).filter((text) => text.indexOf("Saving enables") !== -1)[0];
            Opa5.assert.ok(asked.indexOf("ReleaseItem") !== -1 && asked.indexOf(FIELD) !== -1, `Save asks, naming both: ${asked}`);
            Opa5.assert.strictEqual(writes(), 0, "nothing is sent before the answer");
        },
        errorMessage: "Save did not ask"
    });
    iPressInDialog(When, "Cancel");
    Then.iStopTheApp();
});

opaTest("a service that runs as the signed-in user is read as the signed-in user", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    const page = () => shown;

    iOpen(Given, When, "purchase-requisitions", function (element) { shown = element; });
    iReadTheMetadata(When, Then, page);
    iSeeInImport(Then, "the request", function () { return backend.countRequests(READ) === 1; }, function () {
        Opa5.assert.strictEqual(backend.bodies[READ]?.user_context, true, "user_context is true in the request");
        Opa5.assert.strictEqual(backend.bodies[READ]?.destination, "S4_ODATA_USER");
    });
    iPressInDialog(When, "Cancel");
    Then.iStopTheApp();
});

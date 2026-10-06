import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type Input from "sap/m/Input";
import type Text from "sap/m/Text";
import type Panel from "sap/m/Panel";
import type MessageStrip from "sap/m/MessageStrip";
import type ObjectStatus from "sap/m/ObjectStatus";
import type SegmentedButton from "sap/m/SegmentedButton";
import type SideNavigation from "sap/tnt/SideNavigation";
import type NavigationList from "sap/tnt/NavigationList";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import Common, { backend } from "./pages/Common";
import { iPressInDialog, iSeeADialog } from "./pages/Dialogs";
import { TABLE, VIEW as LIST_VIEW, buttonsOf, itemOf, messageOf } from "./pages/ODataList";
import {
    ENTITY_TABLE, PAGE, VIEW, accessibleName, actionsOf, announced, counterState, entityHeader, entityItem,
    entityItems, entityRow, entityTitles, formOf, hasFocus, inView, opBox, pageTitle, pressMore, pressSegment, rowTop,
    scrollerOf, stateOf, stripOf, tableHeaderScrolledAway, tagsOf, toasts, viewOf, withId,
    OPERATIONS_TABLE, operationBox, operationItem, operationRemove, operationRow, operationsHeader, testStripOf,
    usedByRows, changesDataOf, operationDialogOf,
    type FormTexts, type Op
} from "./pages/ODataDetail";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("OData service detail journey");

const JOBS = "purchase-requisitions-jobs";
const USER = "purchase-requisitions";
const UNUSED = "purchase-requisitions-v4";
const PATH = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV";

const USER_HINT = "Calls run under each user's own SAP authorizations. Runs without a signed-in user are refused.";
const TECHNICAL_HINT = "Works in chat, jobs and workflows. Every user of the agent sees what this SAP user may see.";
/** The page's own navigation button. */
const BACK = `${PAGE}-navButton`;
const DISCARD_QUESTION = "You have unsaved changes to this service. Discard them?";

function hash(): string {
    return HashChanger.getInstance().getHash();
}

function stored(name: string) {
    return backend.odataServices.filter((service) => service.name === name)[0];
}

/** Waits until the form shows the service `name`, then hands a control of
 *  the view to `assert`. */
function iSeeTheService(Then: Common, name: string, what: string, assert?: (page: UI5Element) => void): void {
    Then.waitFor({
        id: "odataName",
        viewName: VIEW,
        check: function (input: UI5Element) { return (input as Input).getValue() === name; },
        success: function (input: UI5Element) {
            Opa5.assert.strictEqual((input as Input).getValue(), name, what);
            if (assert) {
                assert(input);
            }
        },
        errorMessage: `The form does not show ${name || "a new service"}: ${what}`
    });
}

function iEnter(When: Common, id: string, text: string): void {
    When.waitFor({ id, viewName: VIEW, actions: new EnterText({ text }), errorMessage: `No field ${id}` });
}

function iPress(When: Common, id: string): void {
    When.waitFor({ id, viewName: VIEW, actions: new Press(), errorMessage: `No button ${id}` });
}

function iChoose(When: Common, id: string, key: string): void {
    When.waitFor({
        id,
        viewName: VIEW,
        actions: function (segmented: UI5Element | null) { pressSegment(segmented as UI5Element, key); },
        errorMessage: `No segmented button ${id}`
    });
}

/** Runs `assert` on the page once `check` holds for it. */
function iSee(Then: Common, what: string, check: (page: UI5Element) => boolean, assert: (page: UI5Element) => void): void {
    Then.waitFor({
        id: PAGE,
        viewName: VIEW,
        check,
        success: assert,
        errorMessage: `Not seen: ${what}`
    });
}

function iSeeTheHash(Then: Common, expected: string, what: string, assert?: () => void): void {
    Then.waitFor({
        check: function () { return hash() === expected; },
        success: function () {
            Opa5.assert.strictEqual(hash(), expected, what);
            if (assert) {
                assert();
            }
        },
        errorMessage: `The hash is not ${expected}: ${what}`
    });
}

function iSeeNoDialog(Then: Common, what: string, assert?: () => void): void {
    Then.waitFor({
        id: PAGE,
        viewName: VIEW,
        success: function () {
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, what);
            if (assert) {
                assert();
            }
        }
    });
}

/** Clicks an item of the side navigation, as a user does. */
function iPressTheNavItem(When: Common, key: string): void {
    When.waitFor({
        id: "sideNavigation",
        viewName: "App",
        success: function (element: UI5Element) {
            const items = ((element as SideNavigation).getItem() as NavigationList).getItems();
            const item = items.filter((candidate) => candidate.getKey() === key)[0];
            new Press().executeOn(item as unknown as Control);
        },
        errorMessage: `No item ${key} in the side navigation`
    });
}

/** A field of the duplicate dialog. */
function iEnterInDialog(When: Common, id: string, text: string): void {
    When.waitFor({
        controlType: "sap.m.Input",
        searchOpenDialogs: true,
        matchers: withId(id),
        actions: new EnterText({ text }),
        errorMessage: `No field ${id} in the open dialog`
    });
}

opaTest("the header shows version, identity and Write tags and the General form is filled", function (Given: Common, When: Common, Then: Common) {
    const expected: FormTexts = {
        title: "Purchase requisitions (jobs)", name: JOBS, nameEditable: false,
        purpose: "Nightly checks and release of requisitions", counter: "42 / 200",
        notFor: "Purchase orders, contracts or supplier master data: use the matching service",
        destination: "S4_ODATA_TECH", runsAs: "technical", runsAsHint: TECHNICAL_HINT,
        version: "v2", servicePath: PATH, enabled: true
    };

    Given.iStartTheApp(`odata-services/${JOBS}`);

    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(pageTitle(page), "Purchase requisitions (jobs)", "the page is titled with the business name");
        Opa5.assert.deepEqual(tagsOf(page), ["V2", "Technical user", "Write"], "the header tags");
        Opa5.assert.deepEqual(formOf(page), expected, "every General field shows the stored value");
        Opa5.assert.deepEqual(
            actionsOf(page), { save: true, duplicate: true, remove: true }, "Save, Duplicate and Delete are offered"
        );
        Opa5.assert.strictEqual(
            (viewOf(page).byId("odataDetailLoadFailed") as MessageStrip).getVisible(), false, "no load error"
        );
    });
    Then.waitFor({
        id: "sideNavigation",
        viewName: "App",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual(
                (element as SideNavigation).getSelectedKey(), "odataServices",
                "the OData services item stays selected on the detail route"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("a disabled V4 service of the signed-in user shows those tags and that hint", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${UNUSED}`);

    iSeeTheService(Then, UNUSED, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.deepEqual(tagsOf(page), ["V4", "Signed-in user", "Disabled"], "V4, signed-in user, disabled, no Write");
        const form = formOf(page);
        Opa5.assert.strictEqual(form.version, "v4", "V4 is selected");
        Opa5.assert.strictEqual(form.runsAs, "user", "Signed-in user is selected");
        Opa5.assert.strictEqual(form.runsAsHint, USER_HINT, "with its explanation");
        Opa5.assert.strictEqual(form.enabled, false, "the switch is off");
    });

    Then.iStopTheApp();
});

opaTest("a row of the list and New service both land on a working detail page", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    When.waitFor({
        id: TABLE,
        viewName: LIST_VIEW,
        matchers: function (table: UI5Element) { return itemOf(table, USER); },
        actions: new Press()
    });
    iSeeTheService(Then, USER, "the row opened its service", function (page: UI5Element) {
        Opa5.assert.strictEqual(pageTitle(page), "Purchase requisitions", "with its title");
        Opa5.assert.strictEqual(hash(), `odata-services/${USER}`, "on its own route");
    });
    Then.waitFor({
        id: "sideNavigation",
        viewName: "App",
        success: function (element: UI5Element) {
            const navigation = element as SideNavigation;
            Opa5.assert.strictEqual(navigation.getSelectedKey(), "odataServices", "the item is still selected");
            const selected = (navigation.getItem() as NavigationList).getSelectedItem();
            Opa5.assert.strictEqual(
                (selected as unknown as { getKey(): string } | null)?.getKey(), "odataServices",
                "and is the one the list highlights"
            );
        }
    });

    iPress(When, BACK);
    When.waitFor({ id: "addODataServiceButton", viewName: LIST_VIEW, actions: new Press() });
    iSeeTheService(Then, "", "New service opened an empty form", function (page: UI5Element) {
        Opa5.assert.strictEqual(pageTitle(page), "New service", "titled New service");
        Opa5.assert.deepEqual(tagsOf(page), [], "a service that does not exist yet has no tags");
        Opa5.assert.deepEqual(formOf(page), {
            title: "", name: "", nameEditable: true, purpose: "", counter: "0 / 200", notFor: "",
            destination: "", runsAs: "technical", runsAsHint: TECHNICAL_HINT, version: "v2",
            servicePath: "", enabled: true
        }, "the form starts empty: V2, technical user, enabled");
        Opa5.assert.deepEqual(
            actionsOf(page), { save: true, duplicate: false, remove: false },
            "nothing to duplicate or delete yet"
        );
    });

    Then.iStopTheApp();
});

opaTest("purpose is required, counted and one line; a refused form sends nothing", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");

    iEnter(When, "odataPurpose", "");
    iSee(Then, "the counter at 0", function (page: UI5Element) {
        return formOf(page).counter === "0 / 200";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).counter, "0 / 200", "the counter follows the text");
    });
    iPress(When, "odataSaveButton");
    iSee(Then, "the purpose marked", function (page: UI5Element) {
        return stateOf(page, "odataPurpose").state === "Error";
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(stateOf(page, "odataPurpose"), {
            state: "Error", text: "Describe in one line when an agent should use this service."
        }, "an empty purpose is marked with what to do");
        Opa5.assert.strictEqual(stateOf(page, "odataTitle").state, "None", "the other fields are not");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing was sent");
    });

    iEnter(When, "odataPurpose", "Release requisitions at night");
    iSee(Then, "the counter at 29", function (page: UI5Element) {
        return formOf(page).counter === "29 / 200";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).counter, "29 / 200", "the counter counts what was typed");
        Opa5.assert.strictEqual(counterState(page), "None", "within the limit it is not marked");
        Opa5.assert.strictEqual(stateOf(page, "odataPurpose").state, "None", "editing the field clears its error");
    });

    iEnter(When, "odataPurpose", "x".repeat(201));
    iPress(When, "odataSaveButton");
    iSee(Then, "a purpose that is too long", function (page: UI5Element) {
        return stateOf(page, "odataPurpose").state === "Error";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).counter, "201 / 200", "the counter shows the excess");
        Opa5.assert.strictEqual(counterState(page), "Error", "and is marked as an error");
        Opa5.assert.strictEqual(
            stateOf(page, "odataPurpose").text, "Use at most 200 characters.", "201 characters are refused"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing was sent");
    });

    // A line break inside the purpose: set on the control, as a paste does.
    When.waitFor({
        id: "odataPurpose",
        viewName: VIEW,
        success: function (area: UI5Element) {
            const control = area as unknown as { setValue(v: string): void; fireLiveChange(p: object): void };
            control.setValue("Release requisitions\nat night");
            control.fireLiveChange({ value: "Release requisitions\nat night" });
        }
    });
    iPress(When, "odataSaveButton");
    iSee(Then, "a purpose of two lines", function (page: UI5Element) {
        return stateOf(page, "odataPurpose").state === "Error";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stateOf(page, "odataPurpose").text, "Use one line of text, without tabs or line breaks.",
            "two lines are refused"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing was sent");
        Opa5.assert.strictEqual(
            stored(JOBS).purpose, "Nightly checks and release of requisitions", "the stored purpose is untouched"
        );
    });

    Then.iStopTheApp();
});

opaTest("saving sends the whole service with its definition and shows the toast", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");

    iEnter(When, "odataPurpose", "Changed");
    iEnter(When, "odataTitle", "Requisitions at night");
    iPress(When, "odataEnabledSwitch");
    iPress(When, "odataSaveButton");

    iSee(Then, "the saved title", function (page: UI5Element) {
        return pageTitle(page) === "Requisitions at night";
    }, function (page: UI5Element) {
        const service = stored(JOBS);
        Opa5.assert.strictEqual(backend.countRequests(`PUT odata/services/${JOBS}`), 1, "one PUT");
        Opa5.assert.strictEqual(service.purpose, "Changed", "the purpose is stored");
        Opa5.assert.strictEqual(service.title, "Requisitions at night", "and the title");
        Opa5.assert.strictEqual(service.enabled, false, "and the switch");
        Opa5.assert.strictEqual(
            service.definition.entity_sets.length, 5,
            "the definition went along unchanged, although only General fields were edited"
        );
        Opa5.assert.strictEqual(service.definition.operations.length, 1, "with its operation");
        Opa5.assert.strictEqual(service.destination, "S4_ODATA_TECH", "what was not edited is as before");
        Opa5.assert.deepEqual(
            tagsOf(page), ["V2", "Technical user", "Write", "Disabled"], "the header follows what was saved"
        );
        Opa5.assert.ok(toasts().indexOf("Service saved") !== -1, "the toast says Service saved");
        Opa5.assert.strictEqual(hash(), `odata-services/${JOBS}`, "the page stays on the service");
    });

    // Saved means clean: leaving does not ask.
    iPress(When, BACK);
    iSeeTheHash(Then, "odata-services", "back on the list without a question");

    Then.iStopTheApp();
});

opaTest("a new service posts and navigates to its own route; the name is editable only until then", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services/new");
    iSeeTheService(Then, "", "an empty form", function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).nameEditable, true, "the name can be typed");
    });

    iPress(When, "odataSaveButton");
    iSee(Then, "the required fields marked", function (page: UI5Element) {
        return stateOf(page, "odataName").state === "Error";
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(
            ["odataTitle", "odataName", "odataPurpose", "odataNotFor", "odataDestination", "odataServicePath"]
                .map((id) => stateOf(page, id).text),
            [
                "Enter a business name.", "Enter a name.",
                "Describe in one line when an agent should use this service.", "",
                "Enter the name of the destination.", "Enter the service path."
            ],
            "each required field says what is missing"
        );
        Opa5.assert.strictEqual(backend.countRequests("POST odata/services"), 0, "nothing was sent");
    });

    iEnter(When, "odataTitle", "Suppliers");
    iEnter(When, "odataName", "Not A Slug");
    iEnter(When, "odataPurpose", "Look up suppliers");
    iEnter(When, "odataDestination", "S4_ODATA_USER");
    iEnter(When, "odataServicePath", "/sap/opu/odata/sap/API_BUSINESS_PARTNER?x=1");
    iChoose(When, "odataRunsAs", "user");
    iChoose(When, "odataVersion", "v4");
    iPress(When, "odataSaveButton");
    iSee(Then, "name and path refused", function (page: UI5Element) {
        return stateOf(page, "odataServicePath").state === "Error";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stateOf(page, "odataName").text,
            "Use lower-case letters, digits and hyphens (at most 64 characters), starting and ending with a letter or digit.",
            "the name rule"
        );
        Opa5.assert.strictEqual(
            stateOf(page, "odataServicePath").text,
            "Enter a path that starts with \"/\" and has no host, query, space, \"%\" or closing \"/\".",
            "the path rule"
        );
        Opa5.assert.strictEqual(stateOf(page, "odataTitle").state, "None", "what is fine is no longer marked");
        Opa5.assert.strictEqual(backend.countRequests("POST odata/services"), 0, "still nothing sent");
    });

    iEnter(When, "odataName", "suppliers");
    iEnter(When, "odataServicePath", "/sap/opu/odata/sap/API_BUSINESS_PARTNER");
    iPress(When, "odataSaveButton");

    iSeeTheHash(Then, "odata-services/suppliers", "the page moved to the new service's own route");
    iSeeTheService(Then, "suppliers", "and shows it", function (page: UI5Element) {
        const service = stored("suppliers");
        Opa5.assert.strictEqual(backend.countRequests("POST odata/services"), 1, "one POST");
        Opa5.assert.strictEqual(backend.odataServices.length, 5, "the catalogue has a fifth service");
        Opa5.assert.deepEqual(
            [service.title, service.purpose, service.destination, service.user_context, service.odata_version,
                service.service_path, service.enabled],
            ["Suppliers", "Look up suppliers", "S4_ODATA_USER", true, "v4",
                "/sap/opu/odata/sap/API_BUSINESS_PARTNER", true],
            "with what the form held"
        );
        Opa5.assert.deepEqual(service.definition, { entity_sets: [], operations: [] }, "and an empty definition");
        Opa5.assert.strictEqual(formOf(page).nameEditable, false, "the name is display-only from now on");
        Opa5.assert.strictEqual(pageTitle(page), "Suppliers", "the title is the business name");
        Opa5.assert.deepEqual(tagsOf(page), ["V4", "Signed-in user"], "the tags appear");
        Opa5.assert.deepEqual(
            actionsOf(page), { save: true, duplicate: true, remove: true }, "it can now be duplicated and deleted"
        );
    });

    Then.iStopTheApp();
});

opaTest("a name that is taken is said on the name field", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services/new");
    iSeeTheService(Then, "", "an empty form");

    iEnter(When, "odataTitle", "Partners again");
    iEnter(When, "odataName", "business-partners");
    iEnter(When, "odataPurpose", "Look up suppliers");
    iEnter(When, "odataDestination", "S4_ODATA_USER");
    iEnter(When, "odataServicePath", "/sap/opu/odata/sap/API_BUSINESS_PARTNER");
    iPress(When, "odataSaveButton");

    iSee(Then, "the name refused", function (page: UI5Element) {
        return stateOf(page, "odataName").state === "Error";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stateOf(page, "odataName").text, "Service name 'business-partners' already exists",
            "the server's text is on the field it is about"
        );
        Opa5.assert.strictEqual(hash(), "odata-services/new", "the form stays open");
        Opa5.assert.strictEqual(backend.odataServices.length, 4, "nothing was created");
        Opa5.assert.strictEqual(formOf(page).title, "Partners again", "and keeps what was typed");
    });

    Then.iStopTheApp();
});

opaTest("a 422 from the server lands on the matching field, the rest is shown as text", function (Given: Common, When: Common, Then: Common) {
    const DETAIL = "purpose: Value error, purpose must be one line of text without control characters; "
        + "service_path: Value error, service_path must start with '/'; "
        + "definition.entity_sets.0: Value error, entity set '<b>A_Item</b>' has 'list' but no selectable field";

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        // Set once the page has loaded, so that it is the save that fails.
        backend.failNext = { path: `odata/services/${JOBS}`, method: "PUT", status: 422, body: { detail: DETAIL } };
    });

    iEnter(When, "odataTitle", "Edited");
    iPress(When, "odataSaveButton");

    iSee(Then, "the refusal on the form", function (page: UI5Element) {
        return stateOf(page, "odataServicePath").state === "Error";
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(stateOf(page, "odataServicePath"), {
            state: "Error", text: "service_path must start with '/'"
        }, "the path's message is on the path field");
        Opa5.assert.deepEqual(stateOf(page, "odataPurpose"), {
            state: "Error", text: "purpose must be one line of text without control characters"
        }, "the purpose's message is on the purpose field");
        Opa5.assert.strictEqual(stateOf(page, "odataTitle").state, "None", "a field the server did not name is not marked");

        const strip = viewOf(page).byId("odataSaveError") as MessageStrip;
        Opa5.assert.strictEqual(strip.getVisible(), true, "what maps to no field is shown above the form");
        Opa5.assert.strictEqual(
            strip.getText(), `The service was not saved. The server answered: ${DETAIL}`,
            "as the server's own text"
        );
        Opa5.assert.strictEqual(strip.getEnableFormattedText(), false, "as text, never as HTML");
        Opa5.assert.strictEqual(
            strip.getDomRef()?.querySelector("b"), null, "markup in the answer is not rendered"
        );
        Opa5.assert.strictEqual(formOf(page).title, "Edited", "the form keeps the edit");
        Opa5.assert.strictEqual(stored(JOBS).title, "Purchase requisitions (jobs)", "nothing was stored");
    });

    // Editing takes away what the refused save said above the form.
    iEnter(When, "odataNotFor", "Contracts");
    iSee(Then, "the strip gone", function (page: UI5Element) {
        return !stripOf(page, "odataSaveError").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataSaveError").visible, false, "an edit clears the strip");
        Opa5.assert.strictEqual(
            stateOf(page, "odataServicePath").state, "Error", "a field keeps its own error until it is edited"
        );
    });

    // The next save goes through and clears the refusal.
    iPress(When, "odataSaveButton");
    iSee(Then, "the saved title", function (page: UI5Element) {
        return pageTitle(page) === "Edited";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            (viewOf(page).byId("odataSaveError") as MessageStrip).getVisible(), false, "the strip is gone"
        );
        Opa5.assert.strictEqual(stateOf(page, "odataServicePath").state, "None", "and the field errors");
        Opa5.assert.strictEqual(stored(JOBS).title, "Edited", "the service is saved");
    });

    Then.iStopTheApp();
});

opaTest("switching Runs as changes the explanation", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${UNUSED}`);
    iSeeTheService(Then, UNUSED, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).runsAsHint, USER_HINT, "signed-in user: own authorizations");
    });

    iChoose(When, "odataRunsAs", "technical");
    iSee(Then, "the technical hint", function (page: UI5Element) {
        return formOf(page).runsAsHint === TECHNICAL_HINT;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).runsAsHint, TECHNICAL_HINT, "technical user: everyone sees what it may see");
        Opa5.assert.deepEqual(tagsOf(page), ["V4", "Signed-in user", "Disabled"], "the header still shows what is saved");
    });

    iChoose(When, "odataRunsAs", "user");
    iSee(Then, "the user hint", function (page: UI5Element) {
        return formOf(page).runsAsHint === USER_HINT;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).runsAsHint, USER_HINT, "and back");
    });

    // Nobody uses this service: an identity change needs no confirmation.
    iChoose(When, "odataRunsAs", "technical");
    iPress(When, "odataSaveButton");
    iSee(Then, "the saved identity", function (page: UI5Element) {
        return tagsOf(page).indexOf("Technical user") !== -1;
    }, function () {
        Opa5.assert.strictEqual(stored(UNUSED).user_context, false, "saved without a question");
    });

    Then.iStopTheApp();
});

opaTest("changing who the agents act as asks first and names the agents", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${USER}`;
    let agent = "";

    Given.iStartTheApp(`odata-services/${USER}`);
    iSeeTheService(Then, USER, "the service is loaded", function () {
        agent = stored(USER).used_by[0].agent;
    });

    iChoose(When, "odataRunsAs", "technical");
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `The agent ${agent} uses this service.\n\n`
            + "Runs as changes from \"Signed-in user\" to \"Technical user\". The service then works in chat, "
            + "jobs and workflows, and every user of the agent sees what that SAP user may see.",
            "the question names the agent and what the change means"
        );
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Save", "Cancel"], "Save or Cancel");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
    }, "the identity confirmation");

    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled", function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "Cancel sends nothing");
        Opa5.assert.strictEqual(stored(USER).user_context, true, "the service still runs as the signed-in user");
    });
    iSee(Then, "the form after Cancel", function () { return true; }, function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).runsAs, "technical", "the form keeps the choice");
    });

    // The destination as well: both changes are named.
    iEnter(When, "odataDestination", "S4_ODATA_TECH");
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `The agent ${agent} uses this service.\n\n`
            + "Runs as changes from \"Signed-in user\" to \"Technical user\". The service then works in chat, "
            + "jobs and workflows, and every user of the agent sees what that SAP user may see.\n\n"
            + "The destination changes from S4_ODATA_USER to S4_ODATA_TECH. It decides which SAP system is "
            + "called and with which credentials.",
            "both changes are named"
        );
    }, "the identity confirmation with the destination");
    iPressInDialog(When, "Save");

    iSee(Then, "the saved identity", function (page: UI5Element) {
        return tagsOf(page).indexOf("Technical user") !== -1;
    }, function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 1, "one PUT after Save");
        Opa5.assert.strictEqual(stored(USER).user_context, false, "the service runs as the technical user");
        Opa5.assert.strictEqual(stored(USER).destination, "S4_ODATA_TECH", "through the other destination");
    });

    Then.iStopTheApp();
});

opaTest("switching a used service to the signed-in user explains what that refuses; other edits ask nothing", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    let agent = "";

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        agent = stored(JOBS).used_by[0].agent;
    });

    // A used service, but nothing about identity changes: no question.
    iEnter(When, "odataNotFor", "Contracts");
    iPress(When, "odataSaveButton");
    iSee(Then, "the first save", function () {
        return backend.countRequests(PUT) === 1;
    }, function () {
        Opa5.assert.strictEqual(stored(JOBS).not_for, "Contracts", "saved without a question");
    });

    iChoose(When, "odataRunsAs", "user");
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `The agent ${agent} uses this service.\n\n`
            + "Runs as changes from \"Technical user\" to \"Signed-in user\". Calls then run under each user's "
            + "own SAP authorizations and are refused in runs without a signed-in user.",
            "the question says what the signed-in user means for runs"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 1, "nothing more was sent yet");
    }, "the identity confirmation");
    iPressInDialog(When, "Cancel");

    Then.iStopTheApp();
});

opaTest("Duplicate asks for name, destination and identity and opens the copy", function (Given: Common, When: Common, Then: Common) {
    const DUPLICATE = `POST odata/services/${USER}/duplicate`;

    Given.iStartTheApp(`odata-services/${USER}`);
    iSeeTheService(Then, USER, "the service is loaded");

    iPress(When, "odataDuplicateButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        const field = (id: string) => (dialog as unknown as {
            findAggregatedObjects(deep: boolean, filter: (c: UI5Element) => boolean): UI5Element[];
        }).findAggregatedObjects(true, withId(id))[0];
        Opa5.assert.strictEqual((dialog as unknown as { getTitle(): string }).getTitle(), "Duplicate service", "the dialog");
        Opa5.assert.strictEqual((field("odataDuplicateName") as Input).getValue(), "", "the copy needs a name of its own");
        Opa5.assert.strictEqual(
            (field("odataDuplicateDestination") as Input).getValue(), "S4_ODATA_USER", "the destination starts as the source's"
        );
        Opa5.assert.strictEqual(
            (field("odataDuplicateRunsAs") as SegmentedButton).getSelectedKey(), "user", "and so does the identity"
        );
        Opa5.assert.strictEqual(
            (field("odataDuplicateHint") as Text).getText(false),
            "The same API under another identity is a second service. Give the copy its own name and destination.",
            "with the reason for a copy"
        );
        Opa5.assert.strictEqual(
            (field("odataDuplicateUnsaved") as MessageStrip).getVisible(), false, "no warning about unsaved changes"
        );
    }, "the duplicate dialog");

    // Refused by the form: nothing is sent.
    iPressInDialog(When, "Duplicate");
    Then.waitFor({
        controlType: "sap.m.Input",
        searchOpenDialogs: true,
        matchers: withId("odataDuplicateName"),
        success: function (inputs: UI5Element[]) {
            Opa5.assert.strictEqual((inputs[0] as Input).getValueStateText(), "Enter a name.", "a copy without a name is refused");
            Opa5.assert.strictEqual(backend.countRequests(DUPLICATE), 0, "nothing was sent");
        }
    });

    // Refused by the server: the name is taken. The dialog stays.
    iEnterInDialog(When, "odataDuplicateName", JOBS);
    iPressInDialog(When, "Duplicate");
    Then.waitFor({
        controlType: "sap.m.Input",
        searchOpenDialogs: true,
        matchers: withId("odataDuplicateName"),
        check: function (inputs: UI5Element[]) { return (inputs[0] as Input).getValueState() === "Error"; },
        success: function (inputs: UI5Element[]) {
            Opa5.assert.strictEqual(
                (inputs[0] as Input).getValueStateText(), `Service name '${JOBS}' already exists`,
                "a taken name is said on the name field, in the server's words"
            );
            Opa5.assert.strictEqual(backend.odataServices.length, 4, "no copy was made");
        }
    });

    iEnterInDialog(When, "odataDuplicateName", "purchase-requisitions-nightly");
    iEnterInDialog(When, "odataDuplicateDestination", "S4_ODATA_TECH");
    When.waitFor({
        controlType: "sap.m.SegmentedButton",
        searchOpenDialogs: true,
        matchers: withId("odataDuplicateRunsAs"),
        actions: function (segmented: UI5Element | null) { pressSegment(segmented as UI5Element, "technical"); }
    });
    iPressInDialog(When, "Duplicate");

    iSeeTheHash(Then, "odata-services/purchase-requisitions-nightly", "the page moved to the copy");
    iSeeTheService(Then, "purchase-requisitions-nightly", "and shows it", function (page: UI5Element) {
        const copy = stored("purchase-requisitions-nightly");
        Opa5.assert.strictEqual(backend.odataServices.length, 5, "the catalogue has a fifth service");
        Opa5.assert.deepEqual(
            [copy.destination, copy.user_context, copy.title], ["S4_ODATA_TECH", false, "Purchase requisitions (copy)"],
            "under the other identity, titled as a copy"
        );
        Opa5.assert.deepEqual(copy.definition, stored(USER).definition, "with the same definition");
        Opa5.assert.deepEqual(copy.used_by, [], "used by nobody yet");
        Opa5.assert.deepEqual(tagsOf(page), ["V2", "Technical user"], "the copy's tags");
        Opa5.assert.strictEqual(formOf(page).destination, "S4_ODATA_TECH", "the copy's destination");
        Opa5.assert.strictEqual(stored(USER).destination, "S4_ODATA_USER", "the source is untouched");
    });

    Then.iStopTheApp();
});

opaTest("Delete asks, names the service and returns to the list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${UNUSED}`);
    iSeeTheService(Then, UNUSED, "the service is loaded");

    iPress(When, "odataDeleteButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            "Delete the OData service \"Purchase requisitions (V4)\" (purchase-requisitions-v4)? "
            + "This cannot be undone.",
            "the same question as on the list: business title and technical name"
        );
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Delete", "Cancel"], "Delete or Cancel");
    }, "the delete confirmation");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled", function () {
        Opa5.assert.strictEqual(backend.countRequests(`DELETE odata/services/${UNUSED}`), 0, "Cancel sends nothing");
        Opa5.assert.strictEqual(hash(), `odata-services/${UNUSED}`, "and stays on the page");
    });

    // With an unsaved edit: the delete is the answer to it, no second question.
    iEnter(When, "odataTitle", "Edited and then deleted");
    iPress(When, "odataDeleteButton");
    iPressInDialog(When, "Delete");

    iSeeTheHash(Then, "odata-services", "back on the list", function () {
        Opa5.assert.strictEqual(backend.countRequests(`DELETE odata/services/${UNUSED}`), 1, "one DELETE");
        Opa5.assert.strictEqual(backend.odataServices.length, 3, "the service is gone");
    });
    Then.waitFor({
        id: TABLE,
        viewName: LIST_VIEW,
        check: function (table: UI5Element) { return !itemOf(table, UNUSED); },
        success: function () { Opa5.assert.ok(true, "and the list no longer shows it"); },
        errorMessage: "The list still shows the deleted service"
    });

    Then.iStopTheApp();
});

opaTest("a service that agents use is not deleted: the server's refusal is shown as it came", function (Given: Common, When: Common, Then: Common) {
    let refusal = "";

    Given.iStartTheApp(`odata-services/${USER}`);
    iSeeTheService(Then, USER, "the service is loaded", function () {
        refusal = `Service '${USER}' is used by agent(s) '${stored(USER).used_by[0].agent}'`;
    });

    iPress(When, "odataDeleteButton");
    iPressInDialog(When, "Delete");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), refusal, "the 409 text of the server, naming the agents");
        Opa5.assert.strictEqual(hash(), `odata-services/${USER}`, "the page stays");
        Opa5.assert.strictEqual(backend.odataServices.length, 4, "nothing was deleted");
    }, "the refusal");
    iPressInDialog(When, "Close");
    iSeeTheService(Then, USER, "the service is still shown", function () {
        Opa5.assert.strictEqual(backend.countRequests(`GET odata/services/${USER}`), 2, "and was read again after the refusal");
    });

    Then.iStopTheApp();
});

opaTest("leaving with unsaved changes asks before discarding them", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");

    iEnter(When, "odataTitle", "Half an edit");

    // The page's own way back.
    iPress(When, BACK);
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), DISCARD_QUESTION, "the back button asks");
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Discard", "Cancel"], "Discard or Cancel");
        Opa5.assert.strictEqual(hash(), `odata-services/${JOBS}`, "nothing happened yet");
    }, "the discard question");
    iPressInDialog(When, "Cancel");
    iSee(Then, "the edit kept", function () { return true; }, function (page: UI5Element) {
        Opa5.assert.strictEqual(hash(), `odata-services/${JOBS}`, "Cancel stays on the page");
        Opa5.assert.strictEqual(formOf(page).title, "Half an edit", "with the edit");
    });

    // The side navigation.
    iPressTheNavItem(When, "skills");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), DISCARD_QUESTION, "the side navigation asks as well");
        Opa5.assert.strictEqual(hash(), `odata-services/${JOBS}`, "and has not navigated");
    }, "the discard question from the side navigation");
    iPressInDialog(When, "Cancel");
    Then.waitFor({
        id: "sideNavigation",
        viewName: "App",
        check: function (element: UI5Element) { return (element as SideNavigation).getSelectedKey() === "odataServices"; },
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual(
                (element as SideNavigation).getSelectedKey(), "odataServices",
                "after Cancel the highlighted item is again the page that is shown"
            );
            Opa5.assert.strictEqual(hash(), `odata-services/${JOBS}`, "which is still the service");
        },
        errorMessage: "The side navigation did not return to OData services"
    });

    // Discard leaves, and nothing was saved.
    iPress(When, BACK);
    iPressInDialog(When, "Discard");
    iSeeTheHash(Then, "odata-services", "Discard goes back to the list", function () {
        Opa5.assert.strictEqual(backend.countRequests(`PUT odata/services/${JOBS}`), 0, "nothing was saved");
        Opa5.assert.strictEqual(stored(JOBS).title, "Purchase requisitions (jobs)", "the service is as it was");
    });

    // Coming back shows the stored service, not the discarded edit.
    When.waitFor({
        id: TABLE,
        viewName: LIST_VIEW,
        matchers: function (table: UI5Element) { return itemOf(table, JOBS); },
        actions: new Press()
    });
    iSeeTheService(Then, JOBS, "the service again", function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).title, "Purchase requisitions (jobs)", "without the discarded edit");
    });
    iPressTheNavItem(When, "skills");
    iSeeTheHash(Then, "skills", "a clean form lets the side navigation through without a question");

    Then.iStopTheApp();
});

const CHANGED_ELSEWHERE = "This service was changed elsewhere. Reload to see the current version; "
    + "your changes are kept until you reload.";
const DELETED_ELSEWHERE = "This service was deleted elsewhere. Your input is kept: you can save it as a new "
    + "service, or go back to the list.";
const TO_TECHNICAL = "Runs as changes from \"Signed-in user\" to \"Technical user\". The service then works in chat, "
    + "jobs and workflows, and every user of the agent sees what that SAP user may see.";

function usedBy(agent: string) {
    return { agent_id: 77, agent, enabled: true, expose_api: false, api_slug: "", allow_write: false };
}

opaTest("a service that was changed elsewhere is not saved over; the form keeps its input until Reload", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        // Another tab saves the service: its definition and purpose change.
        const service = stored(JOBS);
        service.purpose = "Saved in another tab";
        service.definition.entity_sets.pop();
        service.updated_at = "2026-10-05T10:15:00+00:00";
    });

    iEnter(When, "odataTitle", "My edit");
    iPress(When, "odataSaveButton");
    iSee(Then, "the changed-elsewhere strip", function (page: UI5Element) {
        return stripOf(page, "odataChangedElsewhere").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataChangedElsewhere").text, CHANGED_ELSEWHERE, "the page says so");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "no PUT was sent");
        Opa5.assert.strictEqual(stored(JOBS).purpose, "Saved in another tab", "the other save stands");
        Opa5.assert.strictEqual(stored(JOBS).definition.entity_sets.length, 4, "with its definition");
        Opa5.assert.strictEqual(formOf(page).title, "My edit", "the form keeps the input");
        Opa5.assert.strictEqual(
            formOf(page).purpose, "Nightly checks and release of requisitions", "and still shows what it loaded"
        );
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no dialog on top");
    });

    // Saving again changes nothing: still refused, still nothing sent.
    iPress(When, "odataSaveButton");
    iSee(Then, "the second attempt", function () {
        return backend.countRequests(`GET odata/services/${JOBS}`) === 3;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "a second Save sends nothing either");
        Opa5.assert.strictEqual(stripOf(page, "odataChangedElsewhere").visible, true, "the strip stays");
    });

    iPress(When, "odataReload");
    iSee(Then, "the current version", function (page: UI5Element) {
        return formOf(page).purpose === "Saved in another tab";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).title, "Purchase requisitions (jobs)", "Reload drops the input");
        Opa5.assert.strictEqual(stripOf(page, "odataChangedElsewhere").visible, false, "and the strip");
    });

    // From the current version the save goes through, with the definition
    // the other tab saved.
    iEnter(When, "odataTitle", "My edit");
    iPress(When, "odataSaveButton");
    iSee(Then, "the save", function (page: UI5Element) {
        return pageTitle(page) === "My edit";
    }, function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 1, "one PUT");
        Opa5.assert.strictEqual(
            backend.bodies[PUT]?.expected_updated_at, "2026-10-05T10:15:00+00:00",
            "the PUT says which version it was made from"
        );
        Opa5.assert.strictEqual(stored(JOBS).definition.entity_sets.length, 4, "the other tab's definition survived");
        Opa5.assert.strictEqual(stored(JOBS).purpose, "Saved in another tab", "and its purpose");
    });

    Then.iStopTheApp();
});

opaTest("an agent attached after the page loaded is named before its identity changes", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${UNUSED}`;

    Given.iStartTheApp(`odata-services/${UNUSED}`);
    iSeeTheService(Then, UNUSED, "the service is loaded, used by nobody", function () {
        // Attaching a service changes the agent, not the service: its
        // updated_at stays, only `used_by` tells.
        stored(UNUSED).used_by.push(usedBy("late-agent"));
    });

    iChoose(When, "odataRunsAs", "technical");
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog), `The agent late-agent uses this service.\n\n${TO_TECHNICAL}`,
            "the question names the agent that was attached after the page loaded"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing was sent before the answer");
    }, "the identity confirmation");
    iPressInDialog(When, "Save");
    iSee(Then, "the saved identity", function (page: UI5Element) {
        return tagsOf(page).indexOf("Technical user") !== -1;
    }, function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 1, "one PUT after Save");
        Opa5.assert.strictEqual(
            backend.bodies[PUT]?.expected_updated_at, "2026-10-05T08:00:00+00:00",
            "the PUT carries the updated_at the form was loaded with"
        );
        Opa5.assert.strictEqual(stored(UNUSED).user_context, false, "saved");
    });

    Then.iStopTheApp();
});

opaTest("a destination-only change on a service of several agents names them all", function (Given: Common, When: Common, Then: Common) {
    const NAME = "business-partners";
    let agents: string[] = [];

    Given.iStartTheApp(`odata-services/${NAME}`);
    iSeeTheService(Then, NAME, "the service is loaded", function () {
        agents = stored(NAME).used_by.map((used) => used.agent);
    });

    iEnter(When, "odataDestination", "S4_ODATA_TECH");
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(agents.length, 2, "two agents use it");
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `These agents use this service: ${agents.join(", ")}.\n\n`
            + "The destination changes from S4_ODATA_USER to S4_ODATA_TECH. It decides which SAP system is "
            + "called and with which credentials.",
            "all agents are named, and only the destination is said to change"
        );
    }, "the identity confirmation");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled", function () {
        Opa5.assert.strictEqual(backend.countRequests(`PUT odata/services/${NAME}`), 0, "Cancel sends nothing");
    });

    Then.iStopTheApp();
});

opaTest("after a refused save the next save asks about the identity again", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${USER}`;

    Given.iStartTheApp(`odata-services/${USER}`);
    iSeeTheService(Then, USER, "the service is loaded", function () {
        backend.failNext = {
            path: `odata/services/${USER}`, method: "PUT", status: 422,
            body: { detail: "definition: Value error, the definition is larger than 2000000 bytes" }
        };
    });

    iChoose(When, "odataRunsAs", "technical");
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "asked first");
    }, "the first confirmation");
    iPressInDialog(When, "Save");
    iSee(Then, "the refusal", function (page: UI5Element) {
        return stripOf(page, "odataSaveError").visible;
    }, function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 1, "the save was sent and refused");
        Opa5.assert.strictEqual(stored(USER).user_context, true, "nothing changed");
    });

    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.ok(messageOf(dialog).indexOf(TO_TECHNICAL) !== -1, "the question is asked again");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 1, "and nothing more was sent before the answer");
    }, "the second confirmation");
    iPressInDialog(When, "Save");
    iSee(Then, "the saved identity", function (page: UI5Element) {
        return tagsOf(page).indexOf("Technical user") !== -1;
    }, function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 2, "the second save went through");
        Opa5.assert.strictEqual(stored(USER).user_context, false, "and is stored");
    });

    Then.iStopTheApp();
});

opaTest("a service that was deleted elsewhere keeps the input and can be saved as a new one", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${UNUSED}`);
    iSeeTheService(Then, UNUSED, "the service is loaded", function () {
        backend.odataServices = backend.odataServices.filter((service) => service.name !== UNUSED);
    });

    iEnter(When, "odataTitle", "Kept input");
    iPress(When, "odataSaveButton");
    iSee(Then, "the deleted-elsewhere strip", function (page: UI5Element) {
        return stripOf(page, "odataDeletedElsewhere").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataDeletedElsewhere").text, DELETED_ELSEWHERE, "the page says so");
        Opa5.assert.strictEqual(backend.countRequests(`PUT odata/services/${UNUSED}`), 0, "no PUT was sent");
        Opa5.assert.strictEqual(formOf(page).title, "Kept input", "the form keeps the input");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no error dialog on top");
    });

    iPress(When, "odataSaveAsNew");
    iSee(Then, "the form of a new service", function (page: UI5Element) {
        return formOf(page).nameEditable;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(pageTitle(page), "New service", "the page is a new service now");
        Opa5.assert.strictEqual(formOf(page).name, UNUSED, "with the same name, which can be changed");
        Opa5.assert.deepEqual(tagsOf(page), [], "and no tags of a stored service");
        Opa5.assert.deepEqual(actionsOf(page), { save: true, duplicate: false, remove: false }, "only Save is offered");
        Opa5.assert.strictEqual(stripOf(page, "odataDeletedElsewhere").visible, false, "the strip is gone");
    });
    iPress(When, "odataSaveButton");
    iSee(Then, "the service created again", function (page: UI5Element) {
        return pageTitle(page) === "Kept input";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(backend.countRequests("POST odata/services"), 1, "one POST");
        Opa5.assert.strictEqual(stored(UNUSED).title, "Kept input", "the service exists again with the input");
        Opa5.assert.strictEqual(stored(UNUSED).definition.entity_sets.length, 2, "and the definition the page held");
        Opa5.assert.strictEqual(formOf(page).nameEditable, false, "the name is display-only again");
    });

    Then.iStopTheApp();
});

opaTest("a 404 or a 409 answered to the save itself is said the same way, with the server's text", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    const STALE = `Service '${JOBS}' was changed since it was loaded; reload it and save again`;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        // Changed in the moment between the check and the save.
        backend.failNext = { path: `odata/services/${JOBS}`, method: "PUT", status: 409, body: { detail: STALE } };
    });

    iEnter(When, "odataTitle", "Raced");
    iPress(When, "odataSaveButton");
    iSee(Then, "the 409", function (page: UI5Element) {
        return stripOf(page, "odataChangedElsewhere").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 1, "the PUT was sent and refused");
        Opa5.assert.strictEqual(stripOf(page, "odataChangedElsewhere").text, CHANGED_ELSEWHERE, "changed elsewhere");
        Opa5.assert.strictEqual(
            stripOf(page, "odataSaveError").text, `The service was not saved. The server answered: ${STALE}`,
            "with the server's own text"
        );
        Opa5.assert.strictEqual(formOf(page).title, "Raced", "the form keeps the input");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no dialog on top");
        backend.failNext = {
            path: `odata/services/${JOBS}`, method: "PUT", status: 404, body: { detail: "Service not found" }
        };
    });

    iPress(When, "odataReload");
    iSeeTheService(Then, JOBS, "reloaded");
    iEnter(When, "odataTitle", "Raced again");
    iPress(When, "odataSaveButton");
    iSee(Then, "the 404", function (page: UI5Element) {
        return stripOf(page, "odataDeletedElsewhere").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataDeletedElsewhere").text, DELETED_ELSEWHERE, "deleted elsewhere");
        Opa5.assert.strictEqual(formOf(page).title, "Raced again", "the form keeps the input");
    });

    Then.iStopTheApp();
});

opaTest("the browser's back button or an edited address asks too, and brings the form back", function (Given: Common, When: Common, Then: Common) {
    const HERE = `odata-services/${JOBS}`;
    const go = function (target: string): void {
        When.waitFor({
            id: PAGE,
            viewName: VIEW,
            success: function () { HashChanger.getInstance().setHash(target); }
        });
    };

    Given.iStartTheApp(HERE);
    iSeeTheService(Then, JOBS, "the service is loaded");
    iEnter(When, "odataTitle", "Half an edit");

    // Another page of the app.
    go("skills");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), DISCARD_QUESTION, "leaving by the address asks");
    }, "the discard question");
    iPressInDialog(When, "Cancel");
    iSeeTheHash(Then, HERE, "Cancel is back on the service");
    iSeeTheService(Then, JOBS, "which is shown again", function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).title, "Half an edit", "with the edit");
        Opa5.assert.strictEqual(backend.countRequests(`GET odata/services/${JOBS}`), 1, "and was not read again");
    });

    // Another service: the same route, another name.
    go(`odata-services/${USER}`);
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), DISCARD_QUESTION, "another service asks as well");
    }, "the discard question for another service");
    iPressInDialog(When, "Cancel");
    iSeeTheService(Then, JOBS, "still this service", function (page: UI5Element) {
        Opa5.assert.strictEqual(formOf(page).title, "Half an edit", "with the edit");
    });

    // An address that is no page at all.
    go("no/such/page");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), DISCARD_QUESTION, "an unknown address asks as well");
    }, "the discard question for an unknown address");
    iPressInDialog(When, "Discard");
    iSeeTheHash(Then, "no/such/page", "Discard goes where the address said", function () {
        Opa5.assert.strictEqual(backend.countRequests(`PUT odata/services/${JOBS}`), 0, "nothing was saved");
    });

    // Behind the not-found page no form is left to ask about.
    iPressTheNavItem(When, "skills");
    iSeeTheHash(Then, "skills", "the side navigation goes on without a question", function () {
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no dialog");
    });

    Then.iStopTheApp();
});

opaTest("a clean form left for an unknown address leaves no form and no guard behind", function (Given: Common, When: Common, Then: Common) {
    let view: ReturnType<typeof viewOf> | undefined;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        view = viewOf(page);
        HashChanger.getInstance().setHash("no/such/page");
    });
    iSeeTheHash(Then, "no/such/page", "the not-found page, without a question", function () {
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no dialog");
        // No route matched, so only the router's "bypassed" tells the page.
        const model = view?.getModel("svc") as unknown as { getProperty(path: string): unknown };
        Opa5.assert.strictEqual(model.getProperty("/loaded"), false, "the page behind it holds no form any more");
        Opa5.assert.strictEqual(model.getProperty("/data/name"), "", "and no service");
    });
    iPressTheNavItem(When, "skills");
    iSeeTheHash(Then, "skills", "and the side navigation goes on without a question");

    Then.iStopTheApp();
});

opaTest("a service that cannot be loaded shows that, not an empty form to save", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services/no-such-service");

    iSee(Then, "the load error", function (page: UI5Element) {
        return (viewOf(page).byId("odataDetailLoadFailed") as MessageStrip).getVisible();
    }, function (page: UI5Element) {
        const view = viewOf(page);
        Opa5.assert.strictEqual(
            (view.byId("odataDetailLoadFailed") as MessageStrip).getText(),
            "The OData service \"no-such-service\" could not be loaded: Service not found",
            "the page says which service failed and why"
        );
        Opa5.assert.strictEqual((view.byId("odataGeneralPanel") as Panel).getVisible(), false, "there is no form");
        Opa5.assert.deepEqual(
            actionsOf(page), { save: false, duplicate: false, remove: false }, "and nothing to save, copy or delete"
        );
        Opa5.assert.deepEqual(tagsOf(page), [], "nor tags of a service that is not there");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no dialog on top of it");
    });

    // The service appears (someone created it): Try again loads it.
    Then.waitFor({
        id: PAGE,
        viewName: VIEW,
        success: function () {
            const copy = JSON.parse(JSON.stringify(stored(UNUSED))) as ReturnType<typeof stored>;
            backend.odataServices.push({ ...copy, id: 99, name: "no-such-service", title: "Now it exists" });
        }
    });
    iPress(When, "odataDetailRetry");
    iSeeTheService(Then, "no-such-service", "Try again loaded it", function (page: UI5Element) {
        Opa5.assert.strictEqual(pageTitle(page), "Now it exists", "with its title");
        Opa5.assert.strictEqual(
            (viewOf(page).byId("odataDetailLoadFailed") as MessageStrip).getVisible(), false, "the error is gone"
        );
        Opa5.assert.strictEqual((viewOf(page).byId("odataGeneralPanel") as Panel).getVisible(), true, "the form is back");
    });

    Then.iStopTheApp();
});

opaTest("a server error while loading is shown the same way and leaves nothing to save", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(
        `odata-services/${JOBS}`, { path: `odata/services/${JOBS}`, status: 500, body: { detail: "database is down" } }
    );

    iSee(Then, "the load error", function (page: UI5Element) {
        return (viewOf(page).byId("odataDetailLoadFailed") as MessageStrip).getVisible();
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            (viewOf(page).byId("odataDetailLoadFailed") as MessageStrip).getText(),
            `The OData service "${JOBS}" could not be loaded: database is down`,
            "the reason is the server's"
        );
        Opa5.assert.strictEqual(actionsOf(page).save, false, "Save is not offered");
        Opa5.assert.strictEqual(backend.countRequests(`PUT odata/services/${JOBS}`), 0, "and nothing was sent");
    });

    Then.iStopTheApp();
});

// --- the entity sets table (U4) ----------------------------------------------

const ITEM = "Requisition item";
const HEADER = "Requisition header";
const ACCOUNT = "Account assignment";
const ITEM_TEXT = "Item text";
const DELIVERY = "Delivery address";
const NEEDS_SELECTABLE = "Tick at least one readable field before enabling List or Get.";
const NEEDS_WRITABLE = "Tick at least one writable field before enabling Create or Update.";
const NEEDS_KEY = "This entity set has no key; Get, Update and Delete are not available.";
const AUDITED = "Every write call is audited.";
const NO_AGENTS = "No agent uses this service yet. An agent attached later with \"Allow writes\" can run them.";
const PROBLEM_ROW = "Showing only the entity set with a problem.";
const RELEASE = "the operation \"Release item\" (ReleaseItem)";
// The fields an agent can send once Update (or Create) is on for their
// entity set: named with it when that becomes the case.
const ITEM_FIELDS = "the fields RequestedQuantity, DeliveryDate writable on \"Requisition item\" (A_PurchaseRequisitionItem)";
const TEXT_FIELD = "the field NoteDescription writable on \"Item text\" (A_PurchaseReqnItemText)";
const LIST_REFRESHED = "The list of entity sets was refreshed; this click was not applied. Check the boxes and tick again.";
const CANNOT_VERIFY = "Not saved: this service was loaded without its version, so the page cannot tell whether it was "
    + "changed elsewhere. Reload the page and try again.";

function pending(writes: string): string {
    return `Not saved yet: ${writes}. After Save, agents whose server entry has "Allow writes" can run this in SAP. `
        + AUDITED;
}

/** The operations the form holds for the entity set at `index`: what Save
 *  would send, not what the checkboxes show. */
function formOps(page: UI5Element, index: number): string[] {
    const model = viewOf(page).getModel("svc") as unknown as { getProperty(path: string): string[] };
    return model.getProperty(`/data/definition/entity_sets/${index}/operations`);
}

/** What the last PUT sent as the operations of the entity set at `index`. */
function sentOps(name: string, index: number): string[] {
    const definition = backend.bodies[`PUT odata/services/${name}`]?.definition as {
        entity_sets: { name: string; operations: string[] }[];
    };
    return definition.entity_sets[index].operations;
}

/** Clicks the checkbox of `op` in the row titled `title`. */
function iTick(When: Common, title: string, op: Op): void {
    When.waitFor({
        id: ENTITY_TABLE,
        viewName: VIEW,
        check: function (table: UI5Element) { return !!entityItem(table, title); },
        success: function (table: UI5Element) { new Press().executeOn(opBox(entityItem(table, title), op)); },
        errorMessage: `No row "${title}" in the entity sets table`
    });
}

/**
 * Opens the service `name` after `prepare` changed what the backend holds:
 * the app starts on the list, so the service is read once, as prepared.
 */
function iOpenPrepared(Given: Common, When: Common, name: string, prepare: () => void): void {
    Given.iStartTheApp("odata-services");
    When.waitFor({
        id: TABLE,
        viewName: LIST_VIEW,
        success: function () {
            prepare();
            HashChanger.getInstance().setHash(`odata-services/${name}`);
        },
        errorMessage: "The list did not load"
    });
}

opaTest("the table lists the entity sets with their five operation checkboxes and the field count", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.deepEqual(entityTitles(page), [ITEM, HEADER, ACCOUNT, ITEM_TEXT, DELIVERY], "one row per entity set");
        const header = entityHeader(page);
        Opa5.assert.strictEqual(header.title, "Entity sets (5)", "the section counts them");
        Opa5.assert.ok(/^Metadata read .*2026/.test(header.metadata), `when the metadata was read: ${header.metadata}`);
        Opa5.assert.strictEqual(header.canAdd, true, "Add is offered");

        Opa5.assert.deepEqual(entityRow(page, ITEM), {
            title: ITEM, technical: "A_PurchaseRequisitionItem",
            description: "Central entity for approval decisions: release status, quantity, value, plant, supplier.",
            noDescription: "", ticked: ["list", "get", "update"], fields: "24 of 89", hint: "", note: "", error: ""
        }, "the item: List, Get and Update, 24 of 89 fields");
        Opa5.assert.deepEqual(entityRow(page, ITEM_TEXT).ticked, ["list", "get", "create", "update"], "the item text");
        Opa5.assert.deepEqual(entityRow(page, ACCOUNT).ticked, ["list"], "the account assignment");
        Opa5.assert.deepEqual(entityRow(page, DELIVERY), {
            title: DELIVERY, technical: "A_PurReqAddDelivery", description: "", noDescription: "None",
            ticked: [], fields: "0 of 31", hint: "", note: "", error: ""
        }, "nothing is on for an entity set nobody enabled; its missing description is no warning yet");

        // Writes are told apart from reads, and not by colour alone.
        const item = entityItem(page, ITEM);
        Opa5.assert.strictEqual(opBox(item, "update").getValueState(), "Warning", "a ticked write stands out");
        Opa5.assert.strictEqual(opBox(item, "delete").getValueState(), "None", "an unticked one does not");
        Opa5.assert.strictEqual(opBox(item, "list").getValueState(), "None", "nor does a read");
        Opa5.assert.strictEqual(
            accessibleName(opBox(item, "update")), "Update Requisition item A_PurchaseRequisitionItem write operation",
            "a write checkbox is named by operation, entity set (title and technical name) and as a write"
        );
        Opa5.assert.strictEqual(
            accessibleName(opBox(item, "delete")), "Delete Requisition item A_PurchaseRequisitionItem write operation",
            "ticked or not"
        );
        Opa5.assert.strictEqual(
            accessibleName(opBox(entityItem(page, DELIVERY), "list")), "List Delivery address A_PurReqAddDelivery",
            "a read by operation and entity set"
        );
        Opa5.assert.strictEqual(
            opBox(item, "update").getTooltip_AsString(), "Update Requisition item: write operation, changes data in SAP",
            "the tooltip of a write says that it is one"
        );
        Opa5.assert.strictEqual(opBox(item, "get").getTooltip_AsString(), "Get Requisition item", "a read's does not");
        const columns = Array.from(viewOf(page).byId(ENTITY_TABLE)!.getDomRef()!.querySelectorAll("th.odataWriteCol"));
        Opa5.assert.deepEqual(
            columns.map((th) => (th.textContent ?? "").replace(/\s+/g, "")),
            ["CreateWrite", "UpdateWrite", "DeleteWrite"], "the three write columns are marked in words"
        );
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "nothing is pending");
    });

    // Looking is not editing.
    iPress(When, BACK);
    iSeeTheHash(Then, "odata-services", "back on the list without a question");

    Then.iStopTheApp();
});

opaTest("a new service has no entity sets and says so", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services/new");
    iSeeTheService(Then, "", "the empty form", function (page: UI5Element) {
        Opa5.assert.deepEqual(entityTitles(page), [], "no rows");
        Opa5.assert.strictEqual(entityHeader(page).title, "Entity sets (0)");
        Opa5.assert.strictEqual(entityHeader(page).metadata, "Metadata never read");
        Opa5.assert.strictEqual(
            (viewOf(page).byId(ENTITY_TABLE) as unknown as { getNoDataText(): string }).getNoDataText(),
            "No entity sets yet."
        );
    });
    Then.iStopTheApp();
});

opaTest("ticking a write says what it will allow; Save asks with the agents' names, and only Save sends it", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    const WRITES = "Delete on \"Requisition item\" (A_PurchaseRequisitionItem)";
    let agent = "";
    let shown: UI5Element;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        agent = stored(JOBS).used_by[0].agent;
        shown = page;
    });

    iTick(When, ITEM, "delete");
    iSee(Then, "the pending write", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stripOf(page, "odataPendingWrites").text, pending(WRITES), "the section says what the tick will allow"
        );
        Opa5.assert.deepEqual(entityRow(page, ITEM).ticked, ["list", "get", "update", "delete"], "the box is ticked");
        Opa5.assert.strictEqual(opBox(entityItem(page, ITEM), "delete").getValueState(), "Warning", "and stands out");
        Opa5.assert.deepEqual(formOps(page, 0), ["list", "get", "update", "delete"], "the form holds it");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "ticking asks nothing");
        Opa5.assert.strictEqual(backend.requests.filter((r) => /^(PUT|POST)/.test(r)).length, 0, "and sends nothing");
        Opa5.assert.deepEqual(stored(JOBS).definition.entity_sets[0].operations, ["list", "get", "update"], "nothing is stored");
    });

    // It is an unsaved change like any other.
    iPress(When, BACK);
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), DISCARD_QUESTION, "leaving asks");
    }, "the unsaved-changes question");
    iPressInDialog(When, "Cancel");

    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `The agent ${agent} uses this service.\n\n`
            + `Saving enables these writes in SAP: ${WRITES}.\n\n`
            + `The agent ${agent} has "Allow writes" and will be able to run them.\n\n${AUDITED}`,
            "the question names the entity set, the operation and the agent that can then run it"
        );
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Save", "Cancel"], "Save or Cancel");
        Opa5.assert.strictEqual(
            (dialog as unknown as { getTitle(): string }).getTitle(), "Enable write operations?", "under its own title"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
        Opa5.assert.strictEqual(actionsOf(shown).save, false, "Save cannot be pressed again while the question is open");
    }, "the write confirmation");

    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled", function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "Cancel sends nothing");
        Opa5.assert.deepEqual(stored(JOBS).definition.entity_sets[0].operations, ["list", "get", "update"], "nothing is stored");
    });
    iSee(Then, "the form after Cancel", function () { return true; }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityRow(page, ITEM).ticked, ["list", "get", "update", "delete"], "the tick stays");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, true, "and is still announced");
        Opa5.assert.strictEqual(actionsOf(page).save, true, "Save is back");
    });

    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function () {
        Opa5.assert.ok(true, "the next Save asks again");
    }, "the write confirmation, again");
    iPressInDialog(When, "Save");

    iSee(Then, "the saved write", function (page: UI5Element) {
        return backend.countRequests(PUT) === 1 && !stripOf(page, "odataPendingWrites").visible;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(sentOps(JOBS, 0), ["list", "get", "update", "delete"], "the PUT carries the operations of the entity set");
        Opa5.assert.strictEqual(
            (backend.bodies[PUT]?.definition as { entity_sets: unknown[] }).entity_sets.length, 5, "within the whole definition"
        );
        Opa5.assert.strictEqual(typeof backend.bodies[PUT]?.expected_updated_at, "string", "on the version the form was loaded from");
        Opa5.assert.deepEqual(
            stored(JOBS).definition.entity_sets[0].operations, ["list", "get", "update", "delete"], "it is stored"
        );
        Opa5.assert.deepEqual(entityRow(page, ITEM).ticked, ["list", "get", "update", "delete"], "and shown as saved");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "nothing is pending any more");
    });

    Then.iStopTheApp();
});

opaTest("unticking needs no confirmation, and what Save sends is what the boxes show", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");

    iTick(When, ITEM, "update");
    iTick(When, ITEM, "get");
    iTick(When, ITEM_TEXT, "create");
    iSee(Then, "the unticked boxes", function (page: UI5Element) {
        return entityRow(page, ITEM).ticked.length === 1;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityRow(page, ITEM).ticked, ["list"], "Update and Get are off");
        Opa5.assert.strictEqual(opBox(entityItem(page, ITEM), "update").getValueState(), "None", "an unticked write is a plain box");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "switching a write off announces nothing");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "and asks nothing");
        Opa5.assert.strictEqual(
            entityRow(page, ITEM).hint, "Get is off: agents cannot follow the navigations of this entity set.",
            "an entity set with navigations and no Get says what that means"
        );
        Opa5.assert.strictEqual(entityRow(page, ACCOUNT).hint, "", "one without navigations does not");
    });

    iPress(When, "odataSaveButton");
    iSee(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "Save asks nothing either, although an agent uses the service");
        Opa5.assert.deepEqual(sentOps(JOBS, 0), ["list"], "the item");
        Opa5.assert.deepEqual(sentOps(JOBS, 3), ["list", "get", "update"], "the item text");
        Opa5.assert.deepEqual(sentOps(JOBS, 1), ["list", "get"], "what was not touched is sent as it was");
        Opa5.assert.deepEqual(stored(JOBS).definition.entity_sets[0].operations, ["list"], "and stored");
        Opa5.assert.deepEqual(tagsOf(page), ["V2", "Technical user", "Write"], "the service still writes (item text, the operation)");
    });

    // Ticking the same write again is new again: it was saved as off.
    iTick(When, ITEM, "update");
    iSee(Then, "the pending write", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stripOf(page, "odataPendingWrites").text,
            pending(`Update on "Requisition item" (A_PurchaseRequisitionItem); ${ITEM_FIELDS}`),
            "against what is stored now: the operation, and the fields it lets agents send again"
        );
    });
    // And off again: the form is as saved, nothing pending, nothing unsaved.
    iTick(When, ITEM, "update");
    iPress(When, BACK);
    iSeeTheHash(Then, "odata-services", "ticked and unticked is no change");

    Then.iStopTheApp();
});

opaTest("an agent without Allow writes is named as such, and identity and writes are one question", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${USER}`;
    let agent = "";

    Given.iStartTheApp(`odata-services/${USER}`);
    iSeeTheService(Then, USER, "the service is loaded", function () {
        agent = stored(USER).used_by[0].agent;
        // A second agent whose server entry allows writes.
        stored(USER).used_by.push({ ...stored(USER).used_by[0], agent_id: 999, agent: "writer-agent", allow_write: true });
    });

    iTick(When, HEADER, "delete");
    iTick(When, ITEM, "delete");
    iChoose(When, "odataRunsAs", "technical");
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `These agents use this service: ${agent}, writer-agent.\n\n`
            + "Runs as changes from \"Signed-in user\" to \"Technical user\". The service then works in chat, "
            + "jobs and workflows, and every user of the agent sees what that SAP user may see.\n\n"
            + "Saving enables these writes in SAP: Delete on \"Requisition item\" (A_PurchaseRequisitionItem); "
            + "Delete on \"Requisition header\" (A_PurchaseRequisitionHeader).\n\n"
            + "The agent writer-agent has \"Allow writes\" and will be able to run them.\n\n"
            + `The agent ${agent} uses this service without "Allow writes". Nothing changes for it until that is `
            + `switched on in its server entry.\n\n${AUDITED}`,
            "one question: who they act as, what is enabled, who can run it and who cannot -- with the agent attached after the page loaded"
        );
        Opa5.assert.strictEqual(
            (dialog as unknown as { getTitle(): string }).getTitle(), "Confirm these changes", "under a title for both"
        );
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 1, "one dialog, not two");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
    }, "the combined confirmation");
    iPressInDialog(When, "Save");

    iSee(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function () {
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no second question");
        Opa5.assert.strictEqual(stored(USER).user_context, false, "the identity is stored");
        Opa5.assert.deepEqual(stored(USER).definition.entity_sets[1].operations, ["list", "get", "delete"], "and the writes");
    });

    Then.iStopTheApp();
});

opaTest("a service no agent uses asks before its new writes are saved, and says that nobody can run them yet", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${UNUSED}`;

    Given.iStartTheApp(`odata-services/${UNUSED}`);
    iSeeTheService(Then, UNUSED, "the service is loaded");
    iTick(When, "Requisition", "delete");
    iSee(Then, "the pending write", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stripOf(page, "odataPendingWrites").text, pending("Delete on \"Requisition\" (PurchaseReqn)")
        );
    });
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `Saving enables these writes in SAP: Delete on "Requisition" (PurchaseReqn).\n\n${NO_AGENTS}\n\n${AUDITED}`,
            "the question says what is enabled and that no agent uses the service yet"
        );
        Opa5.assert.strictEqual(
            (dialog as unknown as { getTitle(): string }).getTitle(), "Enable write operations?", "under the write title"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
    }, "the write confirmation without agents");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled", function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "Cancel sends nothing");
        Opa5.assert.deepEqual(stored(UNUSED).definition.entity_sets[0].operations, ["list", "get"], "nothing is stored");
    });
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function () { Opa5.assert.ok(true, "asked again"); }, "the write confirmation, again");
    iPressInDialog(When, "Save");
    iSee(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(stored(UNUSED).definition.entity_sets[0].operations, ["list", "get", "delete"]);
        Opa5.assert.ok(tagsOf(page).indexOf("Write") !== -1, "the header says Write once it is saved");
    });

    Then.iStopTheApp();
});

opaTest("a tick the entity set cannot carry is refused with the reason, and a missing description becomes a warning once something is on", function (Given: Common, When: Common, Then: Common) {
    iOpenPrepared(Given, When, JOBS, function () {
        // The account assignment loses its key.
        stored(JOBS).definition.entity_sets[2].keys = [];
    });
    iSeeTheService(Then, JOBS, "the service is loaded");

    iTick(When, DELIVERY, "list");
    iSee(Then, "the refusal", function (page: UI5Element) {
        return entityRow(page, DELIVERY).note !== "";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(entityRow(page, DELIVERY).note, NEEDS_SELECTABLE, "List without a readable field: the row says why");
        Opa5.assert.deepEqual(entityRow(page, DELIVERY).ticked, [], "the box is not ticked");
        Opa5.assert.deepEqual(formOps(page, 4), [], "and the form is unchanged");
    });
    iTick(When, DELIVERY, "create");
    iSee(Then, "the other refusal", function (page: UI5Element) {
        return entityRow(page, DELIVERY).note === NEEDS_WRITABLE;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityRow(page, DELIVERY).ticked, [], "Create without a writable field is refused as well");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "a refused write is not pending");
    });
    iTick(When, ACCOUNT, "get");
    iSee(Then, "the key refusal", function (page: UI5Element) {
        return entityRow(page, ACCOUNT).note === NEEDS_KEY;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityRow(page, ACCOUNT).ticked, ["list"], "Get without a key is refused; List stays");
        Opa5.assert.strictEqual(entityRow(page, DELIVERY).note, NEEDS_WRITABLE, "each row keeps its own reason");
    });

    // Refused ticks changed nothing: leaving does not ask.
    iPress(When, BACK);
    iSeeTheHash(Then, "odata-services", "nothing is unsaved");

    // Delete needs a key and nothing else, so it can be ticked on the
    // delivery address -- and then its missing description matters.
    When.waitFor({
        id: TABLE,
        viewName: LIST_VIEW,
        success: function () { HashChanger.getInstance().setHash(`odata-services/${JOBS}`); }
    });
    iSeeTheService(Then, JOBS, "the service is loaded again", function (page: UI5Element) {
        Opa5.assert.strictEqual(entityRow(page, DELIVERY).noDescription, "None", "no warning while nothing is on");
    });
    iTick(When, DELIVERY, "delete");
    iSee(Then, "the warning tag", function (page: UI5Element) {
        return entityRow(page, DELIVERY).noDescription === "Warning";
    }, function (page: UI5Element) {
        const row = entityRow(page, DELIVERY);
        Opa5.assert.deepEqual(row.ticked, ["delete"], "Delete is ticked");
        Opa5.assert.strictEqual(row.note, "", "no refusal");
        const tag = (entityItem(page, DELIVERY).getCells()[1] as unknown as { getItems(): ObjectStatus[] }).getItems()[1];
        Opa5.assert.strictEqual(tag.getText(), "No description yet", "the tag of an entity set without a description");
        Opa5.assert.strictEqual(tag.getState(), "Warning", "is a warning once an operation is on");
        Opa5.assert.strictEqual(entityRow(page, ITEM).noDescription, "", "a described entity set has no tag");
    });

    Then.iStopTheApp();
});

opaTest("an entity set the server would refuse is marked on its row and nothing is sent", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    const GET = `GET odata/services/${JOBS}`;

    iOpenPrepared(Given, When, JOBS, function () {
        // Stored with List on and no readable field (an import, an older version).
        stored(JOBS).definition.entity_sets[2].fields.forEach((field) => {
            field.selectable = false;
            field.filterable = false;
        });
    });
    iSeeTheService(Then, JOBS, "the service is loaded");

    iEnter(When, "odataPurpose", "");
    // The row in question is not on screen: the search hides it.
    iEnter(When, "odataEntitySearch", "header");
    iPress(When, "odataSaveButton");
    iSee(Then, "the marked row", function (page: UI5Element) {
        return !!entityItem(page, ACCOUNT) && entityRow(page, ACCOUNT).error !== "";
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityTitles(page), [ACCOUNT], "the search hid the marked row: the table shows the marked row alone");
        Opa5.assert.deepEqual(
            stripOf(page, "odataProblemsOnly"), { visible: true, text: PROBLEM_ROW }, "and says that it does"
        );
        Opa5.assert.strictEqual(entityRow(page, ACCOUNT).error, NEEDS_SELECTABLE, "the row says what is wrong, in the user's words");
        Opa5.assert.strictEqual(
            stripOf(page, "odataSaveError").text, "Not saved. Check the marked entity sets: Account assignment (A_PurReqnAcctAssgmt).",
            "and the page names it above the form"
        );
        Opa5.assert.strictEqual(stateOf(page, "odataPurpose").state, "Error", "together with what is wrong in General");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent");
        Opa5.assert.strictEqual(backend.countRequests(GET), 1, "not even the read before a save");
    });

    // Switching the operation off repairs it.
    iEnter(When, "odataPurpose", "Nightly checks");
    iTick(When, ACCOUNT, "list");
    iSee(Then, "the repaired row", function (page: UI5Element) {
        return entityRow(page, ACCOUNT).error === "";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataSaveError").visible, false, "the edit clears what was said above the form");
    });
    iPress(When, "odataSaveButton");
    iSee(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function () {
        Opa5.assert.deepEqual(stored(JOBS).definition.entity_sets[2].operations, [], "saved without the operation");
    });

    Then.iStopTheApp();
});

opaTest("a refused save marks the rows the server names, with its words as text", function (Given: Common, When: Common, Then: Common) {
    const DETAIL = "definition.entity_sets.1: Value error, entity set 'A_PurchaseRequisitionHeader' has 'list' but no selectable field; "
        + "definition.entity_sets.4.fields.2: Value error, field '<b>Field003</b>' is filterable but not selectable; "
        + "a filterable field must also be selectable; "
        + "definition.entity_sets.0.<unknown field>: Extra inputs are not permitted; "
        + "definition.entity_sets.3: Value error, entity set 'A_SomethingElse' has 'get' but no key";

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        backend.failNext = { path: `odata/services/${JOBS}`, method: "PUT", status: 422, body: { detail: DETAIL } };
    });

    iEnter(When, "odataTitle", "Edited");
    iPress(When, "odataSaveButton");
    iSee(Then, "the marked rows", function (page: UI5Element) {
        return entityRow(page, HEADER).error !== "";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            entityRow(page, HEADER).error, "entity set 'A_PurchaseRequisitionHeader' has 'list' but no selectable field",
            "the message is on the row its position names"
        );
        Opa5.assert.strictEqual(
            entityRow(page, DELIVERY).error,
            "fields.2: field '<b>Field003</b>' is filterable but not selectable; a filterable field must also be selectable",
            "a problem inside an entity set says where in it"
        );
        Opa5.assert.strictEqual(
            entityRow(page, ITEM).error, "<unknown field>: Extra inputs are not permitted", "an unknown key is the row's as well"
        );
        Opa5.assert.strictEqual(
            entityRow(page, ITEM_TEXT).error, "", "a message that names another entity set than the row at its position marks nothing"
        );
        Opa5.assert.strictEqual(entityRow(page, ACCOUNT).error, "", "nor is a row marked that was not named");
        Opa5.assert.strictEqual(
            entityItem(page, DELIVERY).getDomRef()?.querySelector("b"), null, "markup in the answer is not rendered"
        );
        Opa5.assert.strictEqual(
            stripOf(page, "odataSaveError").text,
            "The service was not saved. The server answered: "
            + "Requisition header (A_PurchaseRequisitionHeader): entity set 'A_PurchaseRequisitionHeader' has 'list' "
            + "but no selectable field; "
            + "Delivery address (A_PurReqAddDelivery): fields.2: field '<b>Field003</b>' is filterable but not "
            + "selectable; a filterable field must also be selectable; "
            + "Requisition item (A_PurchaseRequisitionItem): <unknown field>: Extra inputs are not permitted; "
            + "definition.entity_sets.3: entity set 'A_SomethingElse' has 'get' but no key",
            "the whole answer is still above the form, each part with the entity set it is about"
        );
        Opa5.assert.strictEqual(formOf(page).title, "Edited", "the form keeps the edit");
    });

    // Editing a row takes its message away; the others stay until the next save.
    iTick(When, HEADER, "list");
    iSee(Then, "the edited row", function (page: UI5Element) {
        return entityRow(page, HEADER).error === "";
    }, function (page: UI5Element) {
        Opa5.assert.notStrictEqual(entityRow(page, DELIVERY).error, "", "another row keeps its message");
    });
    iTick(When, HEADER, "list");
    iPress(When, "odataSaveButton");
    iSee(Then, "the save", function (page: UI5Element) {
        return pageTitle(page) === "Edited";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(entityRow(page, DELIVERY).error, "", "a save that goes through clears them all");
        Opa5.assert.strictEqual(entityRow(page, ITEM).error, "");
    });

    Then.iStopTheApp();
});

opaTest("the search finds an entity set by title, technical name or description, and a tick lands on the right one", function (Given: Common, When: Common, Then: Common) {
    function iSearch(text: string): void {
        iEnter(When, "odataEntitySearch", text);
    }
    function iSeeRows(expected: string[], what: string, assert?: (page: UI5Element) => void): void {
        iSee(Then, what, function (page: UI5Element) {
            return entityTitles(page).join("|") === expected.join("|");
        }, function (page: UI5Element) {
            Opa5.assert.deepEqual(entityTitles(page), expected, what);
            if (assert) {
                assert(page);
            }
        });
    }

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");

    iSearch("header");
    iSeeRows([HEADER], "by title", function (page: UI5Element) {
        Opa5.assert.strictEqual(entityHeader(page).title, "Entity sets (5)", "the section still counts all of them");
    });
    iSearch("acctassgmt");
    iSeeRows([ACCOUNT], "by technical name, whatever the case");
    iSearch("justification");
    iSeeRows([ITEM_TEXT], "by description");

    // The row on screen is the fourth entity set, not the first.
    iTick(When, ITEM_TEXT, "delete");
    iSee(Then, "the tick", function (page: UI5Element) {
        return entityRow(page, ITEM_TEXT).ticked.indexOf("delete") !== -1;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(formOps(page, 3), ["list", "get", "create", "update", "delete"], "the item text got the tick");
        Opa5.assert.deepEqual(formOps(page, 0), ["list", "get", "update"], "the first entity set did not");
    });

    iSearch("no such thing");
    iSeeRows([], "nothing matches", function (page: UI5Element) {
        Opa5.assert.strictEqual(
            (viewOf(page).byId(ENTITY_TABLE) as unknown as { getNoDataText(): string }).getNoDataText(),
            "No entity set matches the search.", "and the table says so"
        );
    });
    iSearch("");
    iSeeRows([ITEM, HEADER, ACCOUNT, ITEM_TEXT, DELIVERY], "an empty search shows all", function (page: UI5Element) {
        Opa5.assert.deepEqual(entityRow(page, ITEM_TEXT).ticked, ["list", "get", "create", "update", "delete"], "with the tick");
    });

    Then.iStopTheApp();
});

opaTest("a service with 200 entity sets renders twenty rows, is searched whole, and takes no more", function (Given: Common, When: Common, Then: Common) {
    iOpenPrepared(Given, When, UNUSED, function () {
        const sets = stored(UNUSED).definition.entity_sets;
        const template = JSON.stringify(sets[0]);
        for (let n = sets.length; n < 200; n++) {
            const copy = JSON.parse(template) as typeof sets[0];
            copy.name = `Generated${n}`;
            copy.title = `Generated set ${n}`;
            copy.operations = [];
            sets.push(copy);
        }
    });
    iSeeTheService(Then, UNUSED, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(entityHeader(page).title, "Entity sets (200)", "all are counted");
        Opa5.assert.strictEqual(entityItems(page).length, 20, "twenty rows are rendered");
        Opa5.assert.strictEqual(entityHeader(page).canAdd, false, "Add is off at the server's limit");
    });

    iEnter(When, "odataEntitySearch", "Generated set 187");
    iSee(Then, "the found row", function (page: UI5Element) {
        return entityTitles(page).join() === "Generated set 187";
    }, function () {
        Opa5.assert.ok(true, "the search reaches a row that was not rendered");
    });
    iTick(When, "Generated set 187", "list");
    iSee(Then, "the tick", function (page: UI5Element) {
        return entityRow(page, "Generated set 187").ticked.length === 1;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(formOps(page, 187), ["list"], "the tick is on entity set 187");
        Opa5.assert.deepEqual(formOps(page, 0), ["list", "get"], "and nowhere else");
    });

    Then.iStopTheApp();
});

/** Leaves the entity set dialog by its Cancel button. */
function iApplyTheEntityDialog(When: Common): void {
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: withId("entityApplyButton"),
        actions: new Press(),
        errorMessage: "No entity set dialog to apply"
    });
}

opaTest("Add appends an entity set with nothing enabled, and it is saved with the rest", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");
    iEnter(When, "odataEntitySearch", "header");
    iPress(When, "odataAddEntitySetButton");
    // Add opens the dialog of the new entity set; applied as it is, it
    // stays (cancelled, it would be gone again).
    iApplyTheEntityDialog(When);
    iSee(Then, "the new row", function (page: UI5Element) {
        return entityTitles(page).length === 6;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(
            entityTitles(page), [ITEM, HEADER, ACCOUNT, ITEM_TEXT, DELIVERY, "NewEntitySet"],
            "the new entity set is the last row, and the search no longer hides it"
        );
        Opa5.assert.deepEqual(entityRow(page, "NewEntitySet"), {
            title: "NewEntitySet", technical: "NewEntitySet", description: "", noDescription: "None",
            ticked: [], fields: "0 of 0", hint: "", note: "", error: ""
        }, "nothing is enabled by default");
        Opa5.assert.strictEqual(entityHeader(page).title, "Entity sets (6)");
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent yet");
    });
    iPress(When, "odataAddEntitySetButton");
    // Add opens the dialog of the new entity set; applied as it is, it
    // stays (cancelled, it would be gone again).
    iApplyTheEntityDialog(When);
    iSee(Then, "a second new row", function (page: UI5Element) {
        return entityTitles(page).length === 7;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(entityTitles(page)[6], "NewEntitySet2", "a second one gets a name of its own");
    });

    iPress(When, "odataSaveButton");
    iSee(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function () {
        const sets = stored(JOBS).definition.entity_sets;
        Opa5.assert.strictEqual(sets.length, 7, "both are stored");
        Opa5.assert.deepEqual(
            sets[5], {
                name: "NewEntitySet", title: "", path: "", entity_type: "", description: "",
                keys: [], operations: [], fields: [], navigations: [], examples: []
            } as unknown as typeof sets[0], "with nothing enabled"
        );
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "and without a question: no write was enabled");
    });

    Then.iStopTheApp();
});

// --- leftovers of the detail page's review -------------------------------------

opaTest("when the read before the save fails, nothing is sent and the page says so", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    const path = `odata/services/${JOBS}`;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        backend.failNext = { path, method: "GET", status: 500, body: { detail: "database is down" } };
    });

    iEnter(When, "odataTitle", "Edited");
    iPress(When, "odataSaveButton");
    iSee(Then, "the failed read", function (page: UI5Element) {
        return stripOf(page, "odataSaveError").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stripOf(page, "odataSaveError").text,
            "Not saved: the current version of the service could not be read (database is down). Nothing was sent.",
            "a server error on the read is said on the page, with the server's reason"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "no PUT was sent");
        Opa5.assert.strictEqual(formOf(page).title, "Edited", "the form keeps the input");
        Opa5.assert.strictEqual(actionsOf(page).save, true, "and can be saved again");
        Opa5.assert.strictEqual(stripOf(page, "odataChangedElsewhere").visible, false, "it is not taken for a change elsewhere");
        backend.failNext = { path, method: "GET", status: 0, body: null, network: true };
    });

    // No answer at all.
    iPress(When, "odataSaveButton");
    iSee(Then, "the unanswered read", function (page: UI5Element) {
        return /Failed to fetch/.test(stripOf(page, "odataSaveError").text);
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stripOf(page, "odataSaveError").text,
            "Not saved: the current version of the service could not be read (Failed to fetch). Nothing was sent.",
            "a network error is said the same way"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "still no PUT");
        Opa5.assert.strictEqual(actionsOf(page).save, true, "the page is not left busy");
    });

    // Once the backend answers again, the same input is saved.
    iPress(When, "odataSaveButton");
    iSee(Then, "the save", function (page: UI5Element) {
        return pageTitle(page) === "Edited";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 1, "one PUT");
        Opa5.assert.strictEqual(stripOf(page, "odataSaveError").visible, false, "the message is gone");
    });

    Then.iStopTheApp();
});

opaTest("a service that was loaded without its version is not saved blindly", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;

    iOpenPrepared(Given, When, JOBS, function () {
        // The real backend always sends updated_at; an answer without it
        // must not switch the stale-write check off.
        (stored(JOBS) as unknown as { updated_at: null }).updated_at = null;
    });
    iSeeTheService(Then, JOBS, "the service is loaded");

    iEnter(When, "odataTitle", "Edited");
    iPress(When, "odataSaveButton");
    iSee(Then, "the refusal", function (page: UI5Element) {
        return stripOf(page, "odataSaveError").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stripOf(page, "odataSaveError").text,
            "Not saved: this service was loaded without its version, so the page cannot tell whether it was changed "
            + "elsewhere. Reload the page and try again.",
            "the page says why it does not save"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "no PUT without a version to save on");
        Opa5.assert.strictEqual(formOf(page).title, "Edited", "the input stays");
    });

    Then.iStopTheApp();
});

opaTest("an answer without a version to the read before the save stops the save as well", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        (stored(JOBS) as unknown as { updated_at: null }).updated_at = null;
    });
    iEnter(When, "odataTitle", "Edited");
    iPress(When, "odataSaveButton");
    iSee(Then, "the strip", function (page: UI5Element) {
        return stripOf(page, "odataSaveError").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataSaveError").text, CANNOT_VERIFY, "the page says it cannot verify");
        Opa5.assert.strictEqual(
            stripOf(page, "odataChangedElsewhere").visible, false, "which is not the same as \"changed elsewhere\""
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "what cannot be compared is not saved over");
    });

    Then.iStopTheApp();
});

opaTest("two saves in a row both go through: the second is made on the version the first one answered", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    let first = "";

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");

    iEnter(When, "odataTitle", "First");
    iPress(When, "odataSaveButton");
    iSee(Then, "the first save", function (page: UI5Element) {
        return pageTitle(page) === "First";
    }, function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 1, "one PUT");
        first = stored(JOBS).updated_at as string;
        Opa5.assert.notStrictEqual(backend.bodies[PUT]?.expected_updated_at, first, "the save moved the version");
    });

    iTick(When, ITEM, "get");
    iEnter(When, "odataTitle", "Second");
    iPress(When, "odataSaveButton");
    iSee(Then, "the second save", function (page: UI5Element) {
        return pageTitle(page) === "Second";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 2, "a second PUT");
        Opa5.assert.strictEqual(
            backend.bodies[PUT]?.expected_updated_at, first, "made on the version the first save answered"
        );
        Opa5.assert.strictEqual(stripOf(page, "odataChangedElsewhere").visible, false, "no false \"changed elsewhere\"");
        Opa5.assert.strictEqual(stripOf(page, "odataSaveError").visible, false, "and no refusal");
        Opa5.assert.deepEqual(stored(JOBS).definition.entity_sets[0].operations, ["list", "update"], "with the second edit");
    });

    Then.iStopTheApp();
});

// --- review round 1 ----------------------------------------------------------------

/** Puts 198 generated entity sets behind the two of the unused service. */
function twoHundred(): void {
    const sets = stored(UNUSED).definition.entity_sets;
    const template = JSON.stringify(sets[0]);
    for (let n = sets.length; n < 200; n++) {
        const copy = JSON.parse(template) as typeof sets[0];
        copy.name = `Generated${n}`;
        copy.title = `Generated set ${n}`;
        copy.operations = [];
        sets.push(copy);
    }
}

opaTest("a write ticked far down a long table, after More, is said next to Save without moving the table, and each change is announced", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${UNUSED}`;
    const ROW = "Generated set 25";
    const WRITES = "Delete on \"Generated set 25\" (Generated25)";
    let top = 0;

    iOpenPrepared(Given, When, UNUSED, twoHundred);
    iSeeTheService(Then, UNUSED, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(entityTitles(page).length, 20, "twenty rows to begin with");
        pressMore(page);
    });
    iSee(Then, "forty rows", function (page: UI5Element) {
        return entityTitles(page).length === 40;
    }, function (page: UI5Element) {
        // A row that only "More" brought, in the middle of the window.
        entityItem(page, ROW).getDomRef()?.scrollIntoView({ block: "center" });
        Opa5.assert.strictEqual(tableHeaderScrolledAway(page), true, "the page is scrolled: the table's header row is out of view");
        Opa5.assert.ok(scrollerOf(page).scrollTop > 500, `far down (${scrollerOf(page).scrollTop} px)`);
        top = rowTop(page, ROW);
    });

    iTick(When, ROW, "delete");
    iSee(Then, "the pending write in view", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible && inView(page, "odataPendingWrites") && announced() !== "";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").text, pending(WRITES), "what the tick will allow");
        Opa5.assert.strictEqual(inView(page, "odataPendingWrites"), true, "the text about it is on screen although the table's top is not");
        Opa5.assert.strictEqual(inView(page, "odataSaveButton"), true, "next to Save");
        Opa5.assert.strictEqual(tableHeaderScrolledAway(page), true, "the page is still scrolled");
        Opa5.assert.ok(
            Math.abs(rowTop(page, ROW) - top) <= 1,
            `the ticked row did not move when the strip appeared (${top} -> ${rowTop(page, ROW)})`
        );
        Opa5.assert.strictEqual(
            announced(), `${WRITES} will be enabled by Save. Writes pending: 1.`,
            "a screen reader is told what changed, and how many are pending"
        );
    });

    // The last pending write is unticked: the strip goes, the row stays put.
    iTick(When, ROW, "delete");
    iSee(Then, "nothing pending", function (page: UI5Element) {
        return !stripOf(page, "odataPendingWrites").visible && announced().indexOf("no longer") !== -1;
    }, function (page: UI5Element) {
        Opa5.assert.ok(
            Math.abs(rowTop(page, ROW) - top) <= 1,
            `the row did not move when the strip went away (${top} -> ${rowTop(page, ROW)})`
        );
        Opa5.assert.strictEqual(
            announced(), `${WRITES} is no longer pending. No writes are pending.`,
            "that nothing is pending any more is announced too"
        );
    });

    iTick(When, ROW, "delete");
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `Saving enables these writes in SAP: ${WRITES}.\n\n${NO_AGENTS}\n\n${AUDITED}`,
            "Save asks although no agent uses the service"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
    }, "the write confirmation");
    iPressInDialog(When, "Save");
    iSee(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function () {
        Opa5.assert.deepEqual(stored(UNUSED).definition.entity_sets[25].operations, ["delete"], "entity set 25 got the write");
        Opa5.assert.strictEqual(
            stored(UNUSED).definition.entity_sets.filter((set) => set.operations.indexOf("delete") !== -1).length, 1,
            "and no other"
        );
    });

    Then.iStopTheApp();
});

opaTest("a tick on a row that no longer is its entity set changes nothing, shows what is really there and says so", function (Given: Common, When: Common, Then: Common) {
    // What a later task could do: the entity sets change order while the
    // table still shows the old rows.
    const reorder = function (page: UI5Element): void {
        const model = viewOf(page).getModel("svc") as unknown as { getProperty(path: string): unknown[] };
        const sets = model.getProperty("/data/definition/entity_sets");
        sets.unshift(sets.splice(1, 1)[0]);
    };
    const unchanged = function (page: UI5Element, item: number, header: number): void {
        Opa5.assert.deepEqual(formOps(page, header), ["list", "get"], "the header did not lose or gain an operation");
        Opa5.assert.deepEqual(formOps(page, item), ["list", "get", "update"], "nor did the item: the click was not taken");
        Opa5.assert.deepEqual(entityRow(page, ITEM).ticked, ["list", "get", "update"], "the item's boxes show what will be saved");
        Opa5.assert.deepEqual(entityRow(page, HEADER).ticked, ["list", "get"], "and so do the header's");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "no write is pending");
        Opa5.assert.strictEqual(entityRow(page, ITEM).note, LIST_REFRESHED, "the row says that the click was not applied");
        Opa5.assert.strictEqual(announced(), LIST_REFRESHED, "and a screen reader is told");
    };

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", reorder);

    // The row says "Requisition item", position 0 -- where the header is
    // now, which has a key and would take Delete.
    iTick(When, ITEM, "delete");
    iSee(Then, "the rows worked out again", function (page: UI5Element) {
        return entityTitles(page)[0] === HEADER && entityRow(page, ITEM).note !== "";
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityTitles(page).slice(0, 2), [HEADER, ITEM], "the table shows the entity sets as they are");
        unchanged(page, 1, 0);
        // Stale once more, the other way round.
        reorder(page);
    });

    // An untick on the stale row: position 1 is the header now, which would lose Get.
    iTick(When, ITEM, "get");
    iSee(Then, "the rows worked out again, a second time", function (page: UI5Element) {
        return entityTitles(page)[0] === ITEM && entityRow(page, ITEM).note !== "";
    }, function (page: UI5Element) {
        unchanged(page, 0, 1);
    });

    Then.iStopTheApp();
});

opaTest("what Save asks about is worked out against the service as it is stored now", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    let agent = "";

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        agent = stored(JOBS).used_by[0].agent;
        // Stored without Update meanwhile, and (which the real backend
        // does not do) under the same version: the form still has it on.
        stored(JOBS).definition.entity_sets[0].operations = ["list", "get"];
    });
    iEnter(When, "odataTitle", "Edited");
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `The agent ${agent} uses this service.\n\n`
            + "Saving enables these writes in SAP: Update on \"Requisition item\" (A_PurchaseRequisitionItem); "
            + `${ITEM_FIELDS}.\n\n`
            + `The agent ${agent} has "Allow writes" and will be able to run them.\n\n${AUDITED}`,
            "a write the stored service does not have is one this save enables, with the fields it can then send"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
    }, "the write confirmation");
    iPressInDialog(When, "Cancel");

    Then.iStopTheApp();
});

opaTest("switching a service with writes back on asks about all of them; switching it off asks nothing", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    const ALL = "Update on \"Requisition item\" (A_PurchaseRequisitionItem); "
        + `Create, Update on "Item text" (A_PurchaseReqnItemText); ${RELEASE}`;
    let agent = "";

    iOpenPrepared(Given, When, JOBS, function () {
        stored(JOBS).enabled = false;
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        agent = stored(JOBS).used_by[0].agent;
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "a disabled service has nothing pending");
    });

    iPress(When, "odataEnabledSwitch");
    iSee(Then, "the pending writes", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").text, pending(ALL), "switching on makes every write of it pending");
    });
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `The agent ${agent} uses this service.\n\n`
            + "The service is switched on again: all its write operations become available.\n\n"
            + `Saving enables these writes in SAP: ${ALL}.\n\n`
            + `The agent ${agent} has "Allow writes" and will be able to run them.\n\n${AUDITED}`,
            "the question lists every write the service has"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
    }, "the write confirmation");
    iPressInDialog(When, "Save");
    iSee(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stored(JOBS).enabled, true, "the service is on");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "nothing is pending");
    });

    // Off again: fewer rights, no question.
    iPress(When, "odataEnabledSwitch");
    iPress(When, "odataSaveButton");
    iSee(Then, "the second save", function () {
        return backend.countRequests(PUT) === 2;
    }, function () {
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "switching off asks nothing");
        Opa5.assert.strictEqual(stored(JOBS).enabled, false, "the service is off");
    });

    Then.iStopTheApp();
});

opaTest("a tick under an active search is what the PUT carries, and the search stays after the save", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");
    iEnter(When, "odataEntitySearch", "justification");
    iSee(Then, "the one row", function (page: UI5Element) {
        return entityTitles(page).join() === ITEM_TEXT;
    }, function () { Opa5.assert.ok(true, "only the item text is shown"); });
    iTick(When, ITEM_TEXT, "delete");
    iTick(When, ITEM_TEXT, "get");
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.ok(
            messageOf(dialog).indexOf("Delete on \"Item text\" (A_PurchaseReqnItemText)") !== -1, "the question names the row that was ticked"
        );
    }, "the write confirmation");
    iPressInDialog(When, "Save");
    iSee(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(sentOps(JOBS, 3), ["list", "create", "update", "delete"], "the PUT body carries the ticks on the fourth entity set");
        Opa5.assert.deepEqual(sentOps(JOBS, 0), ["list", "get", "update"], "the first one, which the search hid, is sent as it was");
        Opa5.assert.deepEqual(sentOps(JOBS, 4), [], "and so is the last");
        Opa5.assert.deepEqual(entityTitles(page), [ITEM_TEXT], "the search still filters after the save");
        Opa5.assert.strictEqual(
            (viewOf(page).byId("odataEntitySearch") as unknown as { getValue(): string }).getValue(), "justification",
            "and still says what it filters by"
        );
    });

    Then.iStopTheApp();
});

const REFUSED_FIELD = "definition.entity_sets.4.fields.2: Value error, field 'Field003' is filterable but not selectable; "
    + "a filterable field must also be selectable; and 2 more";
const REFUSED_FIELD_SHOWN = "The service was not saved. The server answered: Delivery address (A_PurReqAddDelivery): "
    + "fields.2: field 'Field003' is filterable but not selectable; a filterable field must also be selectable; and 2 more";

opaTest("a refused save shows the row it names even when the search hid it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        backend.failNext = { path: `odata/services/${JOBS}`, method: "PUT", status: 422, body: { detail: REFUSED_FIELD } };
    });
    iEnter(When, "odataTitle", "Edited");
    iEnter(When, "odataEntitySearch", "header");
    iPress(When, "odataSaveButton");
    iSee(Then, "the marked row", function (page: UI5Element) {
        return !!entityItem(page, DELIVERY) && entityRow(page, DELIVERY).error !== "";
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityTitles(page), [DELIVERY], "the table shows the marked row alone");
        Opa5.assert.deepEqual(stripOf(page, "odataProblemsOnly"), { visible: true, text: PROBLEM_ROW }, "and says so");
        Opa5.assert.strictEqual(
            (viewOf(page).byId("odataEntitySearch") as unknown as { getValue(): string }).getValue(), "",
            "the search that hid it is emptied"
        );
        Opa5.assert.strictEqual(
            stripOf(page, "odataSaveError").text, REFUSED_FIELD_SHOWN,
            "above the form the message names the entity set, not only its position, and keeps the server's 'and n more'"
        );
    });

    // The way back.
    iPress(When, "odataShowAll");
    iSee(Then, "all rows again", function (page: UI5Element) {
        return entityTitles(page).length === 5;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataProblemsOnly").visible, false, "the note about the filter is gone");
        Opa5.assert.strictEqual(entityRow(page, DELIVERY).error !== "", true, "the row is still marked");
    });

    Then.iStopTheApp();
});

opaTest("Discard drops the ticks: nothing is sent, and the service is shown as stored", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");
    iTick(When, ITEM, "delete");
    iTick(When, ITEM, "get");
    iSee(Then, "the pending write", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible;
    }, function () { Opa5.assert.ok(true, "a write is pending"); });

    iPress(When, BACK);
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), DISCARD_QUESTION, "leaving asks");
    }, "the unsaved-changes question");
    iPressInDialog(When, "Discard");
    iSeeTheHash(Then, "odata-services", "on the list", function () {
        Opa5.assert.strictEqual(backend.requests.filter((r) => /^(PUT|POST)/.test(r)).length, 0, "nothing was sent");
    });

    When.waitFor({
        id: TABLE,
        viewName: LIST_VIEW,
        success: function () { HashChanger.getInstance().setHash(`odata-services/${JOBS}`); }
    });
    iSeeTheService(Then, JOBS, "opened again", function (page: UI5Element) {
        Opa5.assert.deepEqual(entityRow(page, ITEM).ticked, ["list", "get", "update"], "the rows show the stored operations");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "and no write is pending");
        Opa5.assert.strictEqual(entityRow(page, ITEM).hint, "", "nor is the hint of the unticked Get left over");
    });

    Then.iStopTheApp();
});

opaTest("a service saved as new after it was deleted elsewhere asks about all its writes", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        backend.odataServices = backend.odataServices.filter((service) => service.name !== JOBS);
    });
    iEnter(When, "odataTitle", "Kept input");
    iPress(When, "odataSaveButton");
    iPress(When, "odataSaveAsNew");
    iSee(Then, "the pending writes", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible;
    }, function () { Opa5.assert.ok(true, "for a service that does not exist, every write is new"); });
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            "Saving enables these writes in SAP: Update on \"Requisition item\" (A_PurchaseRequisitionItem); "
            + `Create, Update on "Item text" (A_PurchaseReqnItemText); ${RELEASE}; ${ITEM_FIELDS}; ${TEXT_FIELD}.`
            + `\n\n${NO_AGENTS}\n\n${AUDITED}`,
            "a new service is asked about too: its operations and its writable fields"
        );
        Opa5.assert.strictEqual(backend.countRequests("POST odata/services"), 0, "nothing is sent before the answer");
    }, "the write confirmation of a new service");
    iPressInDialog(When, "Save");
    iSee(Then, "the service created", function () {
        return backend.countRequests("POST odata/services") === 1;
    }, function () { Opa5.assert.strictEqual(stored(JOBS).title, "Kept input"); });

    Then.iStopTheApp();
});

// --- review round 2 ----------------------------------------------------------------

opaTest("switching on a service whose only writes are operations says so, asks naming the operation, and sends only after the answer", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    let agent = "";

    iOpenPrepared(Given, When, JOBS, function () {
        const service = stored(JOBS);
        service.enabled = false;
        service.definition.entity_sets.forEach((set) => {
            set.operations = set.operations.filter((op) => op === "list" || op === "get");
        });
        // Stored as "only reads", but sent with POST: the server runs it as a write.
        service.definition.operations[0].changes_data = false;
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        agent = stored(JOBS).used_by[0].agent;
        Opa5.assert.deepEqual(
            tagsOf(page), ["V2", "Technical user", "Write", "Disabled"], "the Write tag follows the same rule as the question"
        );
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "a disabled service has nothing pending");
    });

    iPress(When, "odataEnabledSwitch");
    iSee(Then, "the pending operation", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible && announced() !== "";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").text, pending(RELEASE), "switching on opens the operation");
        Opa5.assert.strictEqual(
            announced(), `${RELEASE} will be enabled by Save. Writes pending: 1.`, "which is announced"
        );
    });
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `The agent ${agent} uses this service.\n\n`
            + "The service is switched on again: all its write operations become available.\n\n"
            + `Saving enables these writes in SAP: ${RELEASE}.\n\n`
            + `The agent ${agent} has "Allow writes" and will be able to run them.\n\n${AUDITED}`,
            "the question names the operation"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
    }, "the write confirmation");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled", function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "Cancel sends nothing");
    });
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function () { Opa5.assert.ok(true, "asked again"); }, "the write confirmation, again");
    iPressInDialog(When, "Save");
    iSee(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stored(JOBS).enabled, true, "the service is on");
        Opa5.assert.strictEqual(stored(JOBS).definition.operations[0].enabled, true, "with its operation, sent back as stored");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "nothing is pending");
    });

    Then.iStopTheApp();
});

opaTest("Duplicate lists the writes the copy gets and says so on its button; a source without writes shows neither", function (Given: Common, When: Common, Then: Common) {
    const DUPLICATE = `POST odata/services/${JOBS}/duplicate`;
    const COPIED = "Update on \"Requisition item\" (A_PurchaseRequisitionItem); "
        + `Create, Update on "Item text" (A_PurchaseReqnItemText); ${RELEASE}`;
    const says = (writes: string) => `The copy gets the write operations of this service: ${writes}. `
        + "No agent uses the copy yet; an agent attached later with \"Allow writes\" can run them.";
    const child = (dialog: UI5Element, id: string) => (dialog as unknown as {
        findAggregatedObjects(deep: boolean, filter: (c: UI5Element) => boolean): UI5Element[];
    }).findAggregatedObjects(true, withId(id))[0];

    Given.iStartTheApp(`odata-services/${USER}`);
    iSeeTheService(Then, USER, "a service without writes");
    iPress(When, "odataDuplicateButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual((child(dialog, "odataDuplicateWrites") as MessageStrip).getVisible(), false, "nothing about writes");
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Duplicate", "Cancel"], "and a plain Duplicate button");
    }, "the duplicate dialog of a read-only service");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "closed", function () {
        HashChanger.getInstance().setHash(`odata-services/${JOBS}`);
    });

    iSeeTheService(Then, JOBS, "a service with writes");
    iPress(When, "odataDuplicateButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        const strip = child(dialog, "odataDuplicateWrites") as MessageStrip;
        Opa5.assert.strictEqual(strip.getVisible(), true, "the dialog says what the copy gets");
        Opa5.assert.strictEqual(
            strip.getText(), says(COPIED), "every write of the source: entity sets and operations, and who can run them"
        );
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Duplicate with write operations", "Cancel"], "the button says it too");
        Opa5.assert.strictEqual(hasFocus(child(dialog, "odataDuplicateCancel")), true, "the focus starts on Cancel");
        // Saved elsewhere while the dialog is open: the copy would get more.
        stored(JOBS).definition.entity_sets[0].operations = ["list", "get", "update", "delete"];
    }, "the duplicate dialog of a service with writes");

    iEnterInDialog(When, "odataDuplicateName", "purchase-requisitions-nightly");
    iPressInDialog(When, "Duplicate with write operations");
    Then.waitFor({
        controlType: "sap.m.MessageStrip",
        searchOpenDialogs: true,
        matchers: withId("odataDuplicateError"),
        check: function (strips: UI5Element[]) { return (strips[0] as MessageStrip).getVisible(); },
        success: function (strips: UI5Element[]) {
            Opa5.assert.strictEqual(
                (strips[0] as MessageStrip).getText(),
                "The service was changed elsewhere since this dialog opened. The write operations listed here are what "
                + "the copy gets now. Check them and press the button again.",
                "a list that is no longer true is not copied by"
            );
            Opa5.assert.strictEqual(backend.countRequests(DUPLICATE), 0, "nothing was sent");
        },
        errorMessage: "The dialog did not say that the service changed"
    });
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            (child(dialog, "odataDuplicateWrites") as MessageStrip).getText(),
            says(COPIED.replace("Update on \"Requisition item\"", "Update, Delete on \"Requisition item\"")),
            "the list is the current one"
        );
    }, "the duplicate dialog, with the current writes");
    iPressInDialog(When, "Duplicate with write operations");

    iSeeTheHash(Then, "odata-services/purchase-requisitions-nightly", "the page moved to the copy", function () {
        Opa5.assert.strictEqual(backend.countRequests(DUPLICATE), 1, "one copy");
        Opa5.assert.deepEqual(stored("purchase-requisitions-nightly").definition, stored(JOBS).definition, "with what the dialog listed");
    });

    Then.iStopTheApp();
});

opaTest("a strip for many pending writes names three and counts the rest, and so does the announcement", function (Given: Common, When: Common, Then: Common) {
    const three = [2, 3, 4].map((n) => `Delete on "Generated set ${n}" (Generated${n})`).join("; ");
    const CAPPED = `${three}; and 9 more (Save lists them all)`;

    iOpenPrepared(Given, When, UNUSED, function () {
        twoHundred();
        stored(UNUSED).definition.entity_sets.slice(2, 14).forEach((set) => { set.operations = ["delete"]; });
    });
    iSeeTheService(Then, UNUSED, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "a disabled service has nothing pending");
    });
    iPress(When, "odataEnabledSwitch");
    iSee(Then, "the pending writes", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible && announced() !== "";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").text, pending(CAPPED), "three entity sets and a count");
        Opa5.assert.strictEqual(
            announced(), `${CAPPED} will be enabled by Save. Writes pending: 12.`, "announced as briefly"
        );
    });
    iSee(Then, "the room kept for the strip", function (page: UI5Element) {
        return parseFloat(scrollerOf(page).style.scrollPaddingTop) > 0;
    }, function (page: UI5Element) {
        const strip = viewOf(page).byId("odataPendingWrites")!.getDomRef()!.getBoundingClientRect();
        Opa5.assert.ok(
            parseFloat(scrollerOf(page).style.scrollPaddingTop) >= strip.height,
            "a row that gets the keyboard focus is scrolled to below the strip, not under it"
        );
    });
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        const all = Array.from({ length: 12 }, (_, n) => `Delete on "Generated set ${n + 2}" (Generated${n + 2})`).join("; ");
        Opa5.assert.strictEqual(
            messageOf(dialog),
            "The service is switched on again: all its write operations become available.\n\n"
            + `Saving enables these writes in SAP: ${all}.\n\n${NO_AGENTS}\n\n${AUDITED}`,
            "the question lists all twelve"
        );
    }, "the write confirmation");
    iPressInDialog(When, "Cancel");

    Then.iStopTheApp();
});

opaTest("a marked row far down a long table is shown on its own, in view and announced, with a way back", function (Given: Common, When: Common, Then: Common) {
    const ROW = "Generated set 150";
    const ABOVE = "Not saved. Check the marked entity sets: Generated set 150 (Generated150).";

    iOpenPrepared(Given, When, UNUSED, function () {
        twoHundred();
        // Stored with Delete and without a key (an import, an older version).
        const set = stored(UNUSED).definition.entity_sets[150];
        set.keys = [];
        set.operations = ["delete"];
    });
    iSeeTheService(Then, UNUSED, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(entityTitles(page).indexOf(ROW), -1, "row 150 is not among the twenty rendered rows");
        // The admin is somewhere down the table when pressing Save.
        entityItems(page)[19].getDomRef()?.scrollIntoView();
    });
    iPress(When, "odataSaveButton");
    iSee(Then, "the marked row", function (page: UI5Element) {
        return entityTitles(page).join() === ROW && announced() !== "";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(entityRow(page, ROW).error, NEEDS_KEY, "the row says what is wrong");
        Opa5.assert.deepEqual(stripOf(page, "odataProblemsOnly"), { visible: true, text: PROBLEM_ROW }, "the table says what it shows");
        Opa5.assert.strictEqual(stripOf(page, "odataSaveError").text, ABOVE, "the page names the entity set above the form");
        const row = entityItem(page, ROW).getDomRef()!.getBoundingClientRect();
        // Its top: in a narrow window the row is a tall popin.
        Opa5.assert.ok(
            row.top >= 0 && row.top < window.innerHeight - 40,
            `the marked row is on screen (top ${row.top} of ${window.innerHeight})`
        );
        Opa5.assert.strictEqual(hasFocus(entityItem(page, ROW)), true, "and has the focus");
        Opa5.assert.strictEqual(announced(), `${ABOVE} ${PROBLEM_ROW}`, "a screen reader is told both");
        Opa5.assert.strictEqual(backend.requests.filter((r) => /^(PUT|POST)/.test(r)).length, 0, "nothing was sent");
    });

    iPress(When, "odataShowAll");
    iSee(Then, "all rows again", function (page: UI5Element) {
        return entityTitles(page).length === 20;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataProblemsOnly").visible, false, "the note about the filter is gone");
    });
    // The search finds it, still marked.
    iEnter(When, "odataEntitySearch", ROW);
    iSee(Then, "the row by search", function (page: UI5Element) {
        return entityTitles(page).join() === ROW;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(entityRow(page, ROW).error, NEEDS_KEY, "the mark stays until the row is repaired");
    });

    Then.iStopTheApp();
});

opaTest("a refused save leaves a search alone that shows the marked row", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        backend.failNext = { path: `odata/services/${JOBS}`, method: "PUT", status: 422, body: { detail: REFUSED_FIELD } };
    });
    iEnter(When, "odataTitle", "Edited");
    iEnter(When, "odataEntitySearch", "A_PurReqAddDelivery");
    iPress(When, "odataSaveButton");
    iSee(Then, "the marked row", function (page: UI5Element) {
        return !!entityItem(page, DELIVERY) && entityRow(page, DELIVERY).error !== "" && announced() !== "";
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityTitles(page), [DELIVERY], "the row the admin searched for");
        Opa5.assert.strictEqual(
            (viewOf(page).byId("odataEntitySearch") as unknown as { getValue(): string }).getValue(), "A_PurReqAddDelivery",
            "the search is kept: it hides no marked row"
        );
        Opa5.assert.strictEqual(stripOf(page, "odataProblemsOnly").visible, false, "and the table is not filtered otherwise");
        Opa5.assert.strictEqual(stripOf(page, "odataSaveError").text, REFUSED_FIELD_SHOWN, "the server's text, with its tail");
        Opa5.assert.strictEqual(announced(), REFUSED_FIELD_SHOWN, "announced");
    });

    Then.iStopTheApp();
});

// --- leftovers of the table's review ------------------------------------------

opaTest("in the view of marked rows a second refused save shows and counts the rows that are marked now", function (Given: Common, When: Common, Then: Common) {
    iOpenPrepared(Given, When, UNUSED, function () {
        twoHundred();
        [150, 160].forEach((n) => {
            const set = stored(UNUSED).definition.entity_sets[n];
            set.keys = [];
            set.operations = ["delete"];
        });
    });
    iSeeTheService(Then, UNUSED, "the service is loaded");
    iPress(When, "odataSaveButton");
    iSee(Then, "two marked rows", function (page: UI5Element) {
        return entityTitles(page).length === 2;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityTitles(page), ["Generated set 150", "Generated set 160"], "the two marked rows alone");
        Opa5.assert.strictEqual(
            stripOf(page, "odataProblemsOnly").text, "Showing only the 2 entity sets with a problem.", "and how many"
        );
    });

    // One is repaired; the next save is refused for the other alone.
    iTick(When, "Generated set 150", "delete");
    iPress(When, "odataSaveButton");
    iSee(Then, "one marked row", function (page: UI5Element) {
        return entityTitles(page).length === 1;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(entityTitles(page), ["Generated set 160"], "the row that is still marked");
        Opa5.assert.deepEqual(
            stripOf(page, "odataProblemsOnly"), { visible: true, text: PROBLEM_ROW }, "the sentence counts again"
        );
        Opa5.assert.strictEqual(backend.requests.filter((r) => /^(PUT|POST)/.test(r)).length, 0, "nothing was sent");
    });

    Then.iStopTheApp();
});

opaTest("many writes that are no longer pending are announced without pointing at a Save question", function (Given: Common, When: Common, Then: Common) {
    const three = [2, 3, 4].map((n) => `Delete on "Generated set ${n}" (Generated${n})`).join("; ");

    iOpenPrepared(Given, When, UNUSED, function () {
        twoHundred();
        stored(UNUSED).definition.entity_sets.slice(2, 14).forEach((set) => { set.operations = ["delete"]; });
    });
    iSeeTheService(Then, UNUSED, "the service is loaded");
    iPress(When, "odataEnabledSwitch");
    iSee(Then, "the pending writes", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").visible;
    }, function () {
        Opa5.assert.ok(true, "twelve writes are pending");
    });
    iPress(When, "odataEnabledSwitch");
    iSee(Then, "nothing pending", function (page: UI5Element) {
        return !stripOf(page, "odataPendingWrites").visible && announced().indexOf("no longer pending") !== -1;
    }, function () {
        Opa5.assert.strictEqual(
            announced(), `${three}; and 9 more is no longer pending. No writes are pending.`,
            "three and a count, and no \"(Save lists them all)\": Save will not ask about them"
        );
    });

    Then.iStopTheApp();
});

opaTest("unticking one of two pending writes says which one went and how many remain", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");
    iTick(When, ITEM, "delete");
    iTick(When, ITEM_TEXT, "delete");
    iSee(Then, "two pending writes", function (page: UI5Element) {
        return entityRow(page, ITEM_TEXT).ticked.indexOf("delete") !== -1;
    }, function () {
        Opa5.assert.ok(true, "two writes are pending");
    });
    iTick(When, ITEM, "delete");
    iSee(Then, "one pending write", function (page: UI5Element) {
        return entityRow(page, ITEM).ticked.indexOf("delete") === -1 && announced().indexOf("no longer pending") !== -1;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            announced(),
            "Delete on \"Requisition item\" (A_PurchaseRequisitionItem) is no longer pending. Writes pending: 1.",
            "removed while another remains"
        );
        Opa5.assert.strictEqual(
            stripOf(page, "odataPendingWrites").text, pending("Delete on \"Item text\" (A_PurchaseReqnItemText)"),
            "the strip keeps the other"
        );
    });

    Then.iStopTheApp();
});

opaTest("Duplicate says so when the service has no write operations any more, and points at no list", function (Given: Common, When: Common, Then: Common) {
    const DUPLICATE = `POST odata/services/${JOBS}/duplicate`;
    const child = (dialog: UI5Element, id: string) => (dialog as unknown as {
        findAggregatedObjects(deep: boolean, filter: (c: UI5Element) => boolean): UI5Element[];
    }).findAggregatedObjects(true, withId(id))[0];

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "a service with writes");
    iPress(When, "odataDuplicateButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual((child(dialog, "odataDuplicateWrites") as MessageStrip).getVisible(), true, "writes are listed");
        // Saved elsewhere while the dialog is open: no write is left.
        const definition = stored(JOBS).definition;
        definition.entity_sets.forEach((set) => {
            set.operations = set.operations.filter((op) => op === "list" || op === "get");
        });
        definition.operations[0].enabled = false;
    }, "the duplicate dialog of a service with writes");
    iEnterInDialog(When, "odataDuplicateName", "purchase-requisitions-nightly");
    iPressInDialog(When, "Duplicate with write operations");
    Then.waitFor({
        controlType: "sap.m.MessageStrip",
        searchOpenDialogs: true,
        matchers: withId("odataDuplicateError"),
        check: function (strips: UI5Element[]) { return (strips[0] as MessageStrip).getVisible(); },
        success: function (strips: UI5Element[]) {
            Opa5.assert.strictEqual(
                (strips[0] as MessageStrip).getText(),
                "The service was changed elsewhere since this dialog opened: it has no write operations any more. "
                + "Press the button again to copy it as it is now.",
                "what changed, without a list that is not there"
            );
            Opa5.assert.strictEqual(backend.countRequests(DUPLICATE), 0, "nothing was sent");
        },
        errorMessage: "The dialog did not say that the service changed"
    });
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual((child(dialog, "odataDuplicateWrites") as MessageStrip).getVisible(), false, "no list of writes");
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Duplicate", "Cancel"], "and a plain Duplicate button");
    }, "the duplicate dialog, without writes");
    iPressInDialog(When, "Duplicate");
    iSeeTheHash(Then, "odata-services/purchase-requisitions-nightly", "the page moved to the copy", function () {
        Opa5.assert.strictEqual(backend.countRequests(DUPLICATE), 1, "one copy");
    });

    Then.iStopTheApp();
});

// --- operations, used by and the test call (U6) -----------------------------------

const RELEASE_ROW = "Release item";
const STRATEGY = "Release strategy";
const CHANGES_OFF = "Only untick this when the operation is known not to change data. "
    + "A call is then not audited and needs no write permission.";
const POST_IS_WRITE = "An operation that is sent with POST always counts as changing data; this cannot be unticked.";
const TEST = `POST odata/services/${JOBS}/test`;
const TEST_OK = "Test call: 200 from S4_ODATA_TECH in 412 ms. Read 1 row of A_PurchaseRequisitionItem as the technical user.";

/** The operation at `index` as the form holds it: what Save would send. */
function formOperation(page: UI5Element, index: number): Record<string, unknown> {
    const model = viewOf(page).getModel("svc") as unknown as { getProperty(path: string): Record<string, unknown> };
    return model.getProperty(`/data/definition/operations/${index}`);
}

/**
 * Presses "Test call" as a user does: in the toolbar, or -- in a window too
 * narrow for all actions -- in the toolbar's overflow menu, opened first.
 */
/** The sentence of the strip next to Save about operations that are newly
 *  marked as only reading (the agents follow it). */
function pendingRead(operations: string): string {
    return `Not saved yet, marked as only reading: ${operations}. After Save, every agent that uses this service `
        + "can call this, also without \"Allow writes\", and the calls are no longer recorded in the audit.";
}

/** What the Save question says about them. */
function readQuestion(operations: string): string {
    return `Saving marks these operations as only reading: ${operations}. Every agent that uses this service can then `
        + "call them, also without \"Allow writes\", and their calls are no longer recorded in the audit.";
}

function iPressTestCall(When: Common): void {
    When.waitFor({
        id: "odataDetailToolbar",
        viewName: VIEW,
        success: function (toolbar: UI5Element) {
            const button = viewOf(toolbar).byId("odataTestCallButton") as Control;
            if (button.getDomRef()) {
                new Press().executeOn(button);
                return;
            }
            new Press().executeOn(
                (toolbar as unknown as { _getOverflowButton(): Control })._getOverflowButton()
            );
            When.waitFor({
                controlType: "sap.m.Button",
                searchOpenDialogs: true,
                matchers: withId("odataTestCallButton"),
                actions: new Press(),
                errorMessage: "No Test call in the overflow menu"
            });
        },
        errorMessage: "No toolbar"
    });
}

/** Clicks the "Changes data" or the "Enabled" box of the operation titled `title`. */
function iTickOperation(When: Common, title: string, which: "changes" | "enabled"): void {
    When.waitFor({
        id: OPERATIONS_TABLE,
        viewName: VIEW,
        check: function (table: UI5Element) { return !!operationItem(table, title); },
        success: function (table: UI5Element) { new Press().executeOn(operationBox(operationItem(table, title), which)); },
        errorMessage: `No row "${title}" in the operations table`
    });
}

opaTest("the operations table shows method, binding, parameters and both switches", function (Given: Common, When: Common, Then: Common) {
    iOpenPrepared(Given, When, JOBS, function () {
        // What a remote $metadata document can hold: markup in a name's place.
        stored(JOBS).definition.operations.push({
            name: "Probe", qualified_name: "", title: "<b>Probe</b>", kind: "function_import", http_method: "GET",
            bound_to: null, parameters: [{ name: "Depth", type: "Edm.Int32", required: false }],
            description: "<img src=x onerror=alert(1)>", enabled: false, changes_data: false
        });
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.deepEqual(operationsHeader(page), {
            title: "Operations (2)", noData: "No operations. Import them from $metadata.",
            titles: [RELEASE_ROW, "<b>Probe</b>"]
        }, "the section counts and lists the operations");
        Opa5.assert.deepEqual(operationRow(page, RELEASE_ROW), {
            title: RELEASE_ROW, technical: "ReleaseItem · POST",
            description: "Releases one requisition item with a release code.",
            boundTo: "Requisition item",
            parameters: "PurchaseRequisition, PurchaseRequisitionItem, ReleaseCode",
            changesData: true, enabled: true, enabledState: "Warning", uncallable: "", note: ""
        }, "method, the title of the bound entity set, the three parameters and both switches; an enabled write stands out");
        Opa5.assert.deepEqual(operationRow(page, "<b>Probe</b>"), {
            title: "<b>Probe</b>", technical: "Probe · GET", description: "<img src=x onerror=alert(1)>",
            boundTo: "Unbound", parameters: "[Depth]",
            changesData: false, enabled: false, enabledState: "None", uncallable: "", note: ""
        }, "an unbound GET operation that only reads, off; an optional parameter in brackets");
        const table = viewOf(page).byId(OPERATIONS_TABLE)!.getDomRef()!;
        Opa5.assert.strictEqual(table.querySelectorAll("b, img").length, 0, "markup in a title or description is shown as text");
        Opa5.assert.strictEqual(
            accessibleName(operationBox(operationItem(page, RELEASE_ROW), "enabled")),
            "Enabled Release item (ReleaseItem)", "the Enabled box is named with its operation"
        );
        Opa5.assert.strictEqual(
            accessibleName(operationBox(operationItem(page, RELEASE_ROW), "changes")),
            "Changes data Release item (ReleaseItem)", "and so is Changes data"
        );
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "nothing is pending on a stored service");
    });
    Then.iStopTheApp();
});

opaTest("enabling an operation that changes data is said next to Save, asked about by Save, and only Save sends it", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    const RETURNS = { entity_set: "A_PurchaseRequisitionItem", collection: false };
    let agent = "";
    let before = "";

    iOpenPrepared(Given, When, JOBS, function () {
        const operation = stored(JOBS).definition.operations[0];
        operation.enabled = false;
        // What the page does not edit must come back from a save as it was.
        operation.returns = RETURNS;
        before = JSON.stringify(operation);
        agent = stored(JOBS).used_by[0].agent;
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(operationRow(page, RELEASE_ROW).enabled, false, "the operation is off");
        Opa5.assert.strictEqual(operationRow(page, RELEASE_ROW).enabledState, "None");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "nothing pending");
    });

    iTickOperation(When, RELEASE_ROW, "enabled");
    iSee(Then, "the pending operation", function (page: UI5Element) {
        return operationRow(page, RELEASE_ROW).enabled;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stripOf(page, "odataPendingWrites").text, pending(RELEASE), "the strip next to Save names the operation"
        );
        Opa5.assert.ok(announced().indexOf(RELEASE) !== -1, "and it is announced");
        Opa5.assert.strictEqual(operationRow(page, RELEASE_ROW).enabledState, "Warning", "the box stands out");
        Opa5.assert.strictEqual(formOperation(page, 0).enabled, true, "the form holds it");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "ticking asks nothing");
        Opa5.assert.strictEqual(backend.requests.filter((r) => /^(PUT|POST)/.test(r)).length, 0, "and sends nothing");
        Opa5.assert.strictEqual(stored(JOBS).definition.operations[0].enabled, false, "nothing is stored");
    });

    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `The agent ${agent} uses this service.\n\n`
            + `Saving enables these writes in SAP: ${RELEASE}.\n\n`
            + `The agent ${agent} has "Allow writes" and will be able to run them.\n\n${AUDITED}`,
            "the Save question names the operation and the agent that can then call it"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
    }, "the write confirmation");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled", function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "Cancel sends nothing");
        Opa5.assert.strictEqual(stored(JOBS).definition.operations[0].enabled, false, "nothing is stored");
    });

    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function () { Opa5.assert.ok(true, "the next Save asks again"); }, "the write confirmation, again");
    iPressInDialog(When, "Save");
    iSee(Then, "the saved operation", function (page: UI5Element) {
        return backend.countRequests(PUT) === 1 && !stripOf(page, "odataPendingWrites").visible;
    }, function (page: UI5Element) {
        const body = backend.bodies[PUT] as Record<string, unknown>;
        const sent = (body.definition as { operations: Record<string, unknown>[] }).operations;
        Opa5.assert.deepEqual(
            sent, [{ ...JSON.parse(before) as Record<string, unknown>, enabled: true }],
            "the PUT carries the operation as it was stored, `returns` included, with only `enabled` changed"
        );
        Opa5.assert.strictEqual("uncallable_operations" in body, false, "the read-only list is not sent back");
        Opa5.assert.deepEqual(stored(JOBS).definition.operations[0].returns, RETURNS, "and `returns` is still stored");
        Opa5.assert.strictEqual(operationRow(page, RELEASE_ROW).enabled, true, "the saved service shows it enabled");
        Opa5.assert.deepEqual(tagsOf(page), ["V2", "Technical user", "Write"], "with the Write tag");
    });
    Then.iStopTheApp();
});

opaTest("unticking Changes data asks with the audit warning; a POST operation stays a write", function (Given: Common, When: Common, Then: Common) {
    const STRATEGY_WRITE = "the operation \"Release strategy\" (GetReleaseStrategy)";

    iOpenPrepared(Given, When, UNUSED, function () {
        // A GET function that still counts as a write, as an import leaves it.
        stored(UNUSED).definition.operations[2].changes_data = true;
    });
    let shown: UI5Element;
    iSeeTheService(Then, UNUSED, "the service is loaded", function (page: UI5Element) {
        shown = page;
        Opa5.assert.strictEqual(operationRow(page, STRATEGY).changesData, true, "Changes data is ticked");
        Opa5.assert.strictEqual(operationRow(page, STRATEGY).technical, "GetReleaseStrategy · GET");
    });

    iTickOperation(When, STRATEGY, "changes");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), CHANGES_OFF, "the question carries the audit warning");
        Opa5.assert.strictEqual(
            (dialog as unknown as { getTitle(): string }).getTitle(), "Mark \"Release strategy\" as not changing data?"
        );
        Opa5.assert.deepEqual(buttonsOf(dialog), ["It only reads", "Cancel"], "the answer or Cancel");
        Opa5.assert.strictEqual(operationRow(shown, STRATEGY).changesData, true, "the box stays ticked until the answer");
        Opa5.assert.strictEqual(formOperation(shown, 2).changes_data, true, "and so does the form");
    }, "the question about Changes data");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled");
    iSee(Then, "after Cancel", function () { return true; }, function (page: UI5Element) {
        Opa5.assert.strictEqual(operationRow(page, STRATEGY).changesData, true, "Cancel leaves it ticked");
        Opa5.assert.strictEqual(formOperation(page, 2).changes_data, true, "and the form as it was");
    });

    iTickOperation(When, STRATEGY, "changes");
    iPressInDialog(When, "It only reads");
    iSee(Then, "the unticked box", function (page: UI5Element) {
        return !operationRow(page, STRATEGY).changesData;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(formOperation(page, 2).changes_data, false, "the form holds the answer");
        Opa5.assert.strictEqual(backend.requests.filter((r) => /^(PUT|POST)/.test(r)).length, 0, "nothing is sent");
    });

    // Switched off and marked as only reading, nothing was said yet: no
    // agent can call it. Enabled, it is a call that needs no "Allow writes"
    // and is not recorded -- said next to Save, like a pending write.
    iSee(Then, "the unticked box, still off", function () { return true; }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "off: nothing is pending");
    });
    iTickOperation(When, STRATEGY, "enabled");
    iSee(Then, "the enabled read", function (page: UI5Element) {
        return operationRow(page, STRATEGY).enabled;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(stripOf(page, "odataPendingWrites"), {
            visible: true, text: `${pendingRead(STRATEGY_WRITE)} No agent uses this service yet.`
        }, "enabled later, the read is said next to Save, by name");
        Opa5.assert.strictEqual(
            announced(),
            `${STRATEGY_WRITE} will be marked as only reading by Save: callable without "Allow writes" and no longer recorded.`,
            "and announced"
        );
        Opa5.assert.strictEqual(operationRow(page, STRATEGY).enabledState, "None", "it is no write: the box does not stand out");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "enabling asks nothing; Save does");
    });
    iTickOperation(When, STRATEGY, "changes");
    iSee(Then, "the write again", function (page: UI5Element) {
        return operationRow(page, STRATEGY).changesData;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "ticking it asks nothing");
        Opa5.assert.strictEqual(
            stripOf(page, "odataPendingWrites").text, pending(STRATEGY_WRITE),
            "the enabled operation is a pending write now, and no pending read any more"
        );
        Opa5.assert.strictEqual(
            announced(),
            `${STRATEGY_WRITE} will be enabled by Save. Writes pending: 1. ${STRATEGY_WRITE} is no longer pending as only reading.`,
            "both changes in one announcement"
        );
    });

    // Sent with POST it is run as a write whatever the flag says: the box
    // is shown, not offered, and says why.
    iSee(Then, "the box of a POST operation", function () { return true; }, function (page: UI5Element) {
        Opa5.assert.deepEqual(
            changesDataOf(operationItem(page, "Release")), { editable: false, tooltip: POST_IS_WRITE },
            "the Changes data box of a POST operation cannot be operated, and its tooltip says why"
        );
        Opa5.assert.strictEqual(operationRow(page, "Release").changesData, true, "it is shown ticked");
        Opa5.assert.deepEqual(
            changesDataOf(operationItem(page, STRATEGY)),
            { editable: true, tooltip: "Changes data Release strategy" }, "the box of a GET operation is offered"
        );
        // Whatever gets a click through to the handler: it is refused there too.
        const box = operationBox(operationItem(page, "Release"), "changes");
        box.setSelected(false);
        (box as unknown as { fireSelect(parameters: object): void }).fireSelect({ selected: false });
    });
    iSee(Then, "the refused untick", function (page: UI5Element) {
        return operationRow(page, "Release").note !== "";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(operationRow(page, "Release").note, POST_IS_WRITE, "the row says why");
        Opa5.assert.strictEqual(operationRow(page, "Release").changesData, true, "the box is ticked again");
        Opa5.assert.strictEqual(formOperation(page, 0).changes_data, true, "the form is unchanged");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "and nothing is asked");
    });
    Then.iStopTheApp();
});

opaTest("an enabled operation that no agent can call says why on its row", function (Given: Common, When: Common, Then: Common) {
    const WHY = "No agent can call this: a key field of the entity set it is bound to is not one of its parameters, "
        + "so a call cannot name the entity.";

    iOpenPrepared(Given, When, JOBS, function () {
        // V2 sends the key as parameters: one key field is not declared.
        stored(JOBS).definition.operations[0].parameters.splice(1, 1);
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(operationRow(page, RELEASE_ROW).uncallable, WHY, "the reason, in plain words");
        Opa5.assert.deepEqual(stripOf(page, "odataUncallableStrip"), {
            visible: true, text: "1 enabled operation cannot be called by any agent as saved. Its row says why."
        }, "and the section counts such rows, of the service as saved");
    });
    // Switched off, no agent meets it: nothing to explain.
    iTickOperation(When, RELEASE_ROW, "enabled");
    iSee(Then, "the operation off", function (page: UI5Element) {
        return !operationRow(page, RELEASE_ROW).enabled;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(operationRow(page, RELEASE_ROW).uncallable, "", "no reason on a disabled operation");
        Opa5.assert.strictEqual(stripOf(page, "odataUncallableStrip").visible, false, "and no count");
    });
    Then.iStopTheApp();
});

opaTest("an operation is removed after a question, and Save sends the definition without it", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    const iPressRemove = function (): void {
        When.waitFor({
            id: OPERATIONS_TABLE, viewName: VIEW,
            success: function (table: UI5Element) { new Press().executeOn(operationRemove(operationItem(table, RELEASE_ROW))); },
            errorMessage: "No operations table"
        });
    };

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");
    iPressRemove();
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            "Remove the operation \"Release item\" (ReleaseItem) from this service? Agents can no longer call it once the "
            + "service is saved. An import from $metadata brings the operation back, switched off, without its title "
            + "and description.",
            "the question names the operation and the way back the page has: the import"
        );
    }, "the remove question");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled");
    iSee(Then, "after Cancel", function () { return true; }, function (page: UI5Element) {
        Opa5.assert.strictEqual(operationsHeader(page).title, "Operations (1)", "Cancel keeps it");
    });
    iPressRemove();
    iPressInDialog(When, "Remove");
    iSee(Then, "the empty table", function (page: UI5Element) {
        return operationsHeader(page).title === "Operations (0)";
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(operationsHeader(page).titles, [], "the row is gone");
        Opa5.assert.strictEqual(stored(JOBS).definition.operations.length, 1, "nothing is stored before Save");
    });
    iPress(When, "odataSaveButton");
    iSee(Then, "the save", function () { return backend.countRequests(PUT) === 1; }, function () {
        Opa5.assert.deepEqual(
            (backend.bodies[PUT]?.definition as { operations: unknown[] }).operations, [], "the PUT carries no operation"
        );
        Opa5.assert.strictEqual(
            (backend.bodies[PUT]?.definition as { entity_sets: unknown[] }).entity_sets.length, 5, "and all entity sets"
        );
    });
    Then.iStopTheApp();
});

opaTest("Used by lists the agent, whether it may write, and its run endpoint", function (Given: Common, When: Common, Then: Common) {
    let agent = "";
    iOpenPrepared(Given, When, JOBS, function () {
        const first = stored(JOBS).used_by[0];
        agent = first.agent;
        first.expose_api = true;
        first.api_slug = "pr-release-job";
        stored(JOBS).used_by.push({ ...first, agent_id: 998, agent: "<i>reader</i>", allow_write: false, expose_api: false, api_slug: "", enabled: false });
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.deepEqual(usedByRows(page), [
            [agent, "", "Yes/Warning", "pr-release-job"],
            ["<i>reader</i>", "Agent disabled", "No/None", "–"]
        ], "agent, whether its entry allows writes, and the slug of its run endpoint or a dash");
        Opa5.assert.strictEqual(
            viewOf(page).byId("odataUsedByTable")!.getDomRef()!.querySelectorAll("i").length, 0, "an agent name is text"
        );
        Opa5.assert.strictEqual(
            stripOf(page, "odataUsedByWarning").visible, false, "a technical-user service works from a run endpoint"
        );
    });
    Then.iStopTheApp();
});

opaTest("a signed-in-user service attached to an agent with a run endpoint shows the warning strip", function (Given: Common, When: Common, Then: Common) {
    let agent = "";
    iOpenPrepared(Given, When, USER, function () {
        const first = stored(USER).used_by[0];
        agent = first.agent;
        first.expose_api = true;
        first.api_slug = "nightly-check";
    });
    iSeeTheService(Then, USER, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.deepEqual(stripOf(page, "odataUsedByWarning"), {
            visible: true,
            text: `The agent ${agent} has a run endpoint; scheduled runs have no user, so calls to this service are refused there.`
        }, "the warning names the agent");
        Opa5.assert.deepEqual(usedByRows(page), [[agent, "", "No/None", "nightly-check"]]);
    });
    // The warning follows what Runs as holds on the page.
    iChoose(When, "odataRunsAs", "technical");
    iSee(Then, "no warning for the technical user", function (page: UI5Element) {
        return !stripOf(page, "odataUsedByWarning").visible;
    }, function () { Opa5.assert.ok(true, "as the technical user the run endpoint works"); });
    Then.iStopTheApp();
});

opaTest("a service that no agent uses says so, and a new service has no Used by section", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${UNUSED}`);
    iSeeTheService(Then, UNUSED, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.deepEqual(usedByRows(page), [], "no row");
        Opa5.assert.strictEqual(
            (viewOf(page).byId("odataUsedByTable") as unknown as { getNoDataText(): string }).getNoDataText(),
            "No agent uses this service."
        );
    });
    When.waitFor({ success: function () { HashChanger.getInstance().setHash("odata-services/new"); } });
    iSeeTheService(Then, "", "the new service", function (page: UI5Element) {
        Opa5.assert.strictEqual((viewOf(page).byId("odataUsedByPanel") as Panel).getVisible(), false, "no Used by yet");
        Opa5.assert.strictEqual(
            (viewOf(page).byId("odataTestCallButton") as unknown as { getEnabled(): boolean }).getEnabled(), false,
            "and nothing to test"
        );
    });
    Then.iStopTheApp();
});

opaTest("Test call shows the success strip with status, destination, duration, rows and identity", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(testStripOf(page).visible, false, "no result before a test");
    });
    iPressTestCall(When);
    iSee(Then, "the result", function (page: UI5Element) {
        return testStripOf(page).visible;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(testStripOf(page), { visible: true, type: "Success", text: TEST_OK, markup: false });
        Opa5.assert.strictEqual(backend.countRequests(TEST), 1, "one test call");
        Opa5.assert.deepEqual(backend.bodies[TEST], {}, "with an empty body: the stored service decides what is read");
        Opa5.assert.strictEqual(announced(), TEST_OK, "the result is announced");
        Opa5.assert.ok(inView(page, "odataTestStrip"), "and in view");
        Opa5.assert.strictEqual(backend.requests.filter((r) => /^PUT/.test(r)).length, 0, "nothing is saved by a test");
    });

    // What the server has to say besides the outcome is shown with it.
    When.waitFor({
        success: function () {
            backend.testResult = {
                ...backend.testResult, read: "metadata", target: "", rows: 0,
                warnings: [{ code: "no_list_entity_set", message: "No entity set has List enabled: only $metadata was fetched." }]
            };
        }
    });
    iPressTestCall(When);
    iSee(Then, "the result with a warning", function (page: UI5Element) {
        return testStripOf(page).type === "Warning";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            testStripOf(page).text,
            "Test call: 200 from S4_ODATA_TECH in 412 ms. Only the $metadata document was fetched as the technical user; "
            + "no row was read. No entity set has List enabled: only $metadata was fetched.",
            "a passed test with a warning is a warning, and says what was read"
        );
    });
    Then.iStopTheApp();
});

opaTest("a failed test call shows the error strip with SAP's message", function (Given: Common, When: Common, Then: Common) {
    const SAP = "HTTP 403 from the OData service: /IWFND/CM_BEC/026: No authorization <b>to read</b>";

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        backend.testResult = { ...backend.testResult, ok: false, code: "sap_error", status: 403, rows: 0, message: SAP };
    });
    iPressTestCall(When);
    iSee(Then, "the failed test", function (page: UI5Element) {
        return testStripOf(page).visible;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(testStripOf(page), {
            visible: true, type: "Error", markup: false,
            text: `Test call failed: 403 from S4_ODATA_TECH as the technical user. ${SAP}`
        }, "status, destination, identity and what SAP said, as text");
    });

    // Nothing reached SAP: no status, no identity.
    When.waitFor({
        success: function () {
            backend.testResult = {
                ...backend.testResult, ok: false, code: "unreachable", status: null, identity: "unknown",
                message: "The destination could not be reached."
            };
        }
    });
    iPressTestCall(When);
    iSee(Then, "the test without an answer", function (page: UI5Element) {
        return testStripOf(page).text.indexOf("without an answer") !== -1;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            testStripOf(page).text,
            "Test call failed without an answer from S4_ODATA_TECH. The destination could not be reached."
        );
    });

    // The route refuses the test: signed-in user required (424).
    When.waitFor({
        success: function () {
            backend.failNext = {
                path: `odata/services/${JOBS}/test`, status: 424, body: { detail: "signed-in user required" },
                headers: { "X-OData-Error": "user_token_required" }
            };
        }
    });
    iPressTestCall(When);
    iSee(Then, "the refused test", function (page: UI5Element) {
        return testStripOf(page).text.indexOf("not made") !== -1;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(testStripOf(page), {
            visible: true, type: "Error", markup: false,
            text: "Test call not made: this service runs as the signed-in user, and no user sign-in reached the server "
                + "with this request. Sign in again and retry."
        });
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "said on the page, not in a dialog");
        Opa5.assert.strictEqual(actionsOf(page).save, true, "the page is usable again");
    });
    Then.iStopTheApp();
});

opaTest("Test call on unsaved changes asks to save first", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");
    iEnter(When, "odataNotFor", "Contracts");
    iPressTestCall(When);
    iSee(Then, "the hint to save", function (page: UI5Element) {
        return testStripOf(page).visible;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(testStripOf(page), {
            visible: true, type: "Warning", markup: false,
            text: "Save the service before testing it: a test call goes through the saved service, not through what is on this page."
        });
        Opa5.assert.strictEqual(backend.countRequests(TEST), 0, "no test call is made for a form that is not saved");
    });
    // Saved, the strip about the old state goes and the test runs.
    iPress(When, "odataSaveButton");
    iSee(Then, "the save", function (page: UI5Element) {
        return stored(JOBS).not_for === "Contracts" && !testStripOf(page).visible;
    }, function () { Opa5.assert.ok(true, "a save takes the strip away"); });
    iPressTestCall(When);
    iSee(Then, "the result", function (page: UI5Element) {
        return testStripOf(page).type === "Success";
    }, function () { Opa5.assert.strictEqual(backend.countRequests(TEST), 1, "now the test call is made"); });
    Then.iStopTheApp();
});


// --- U6 review, fix round 1 ---------------------------------------------------------

const STRATEGY_ITEM = "the operation \"Release strategy\" (GetReleaseStrategy)";
const OPERATIONS_REFRESHED = "The list of operations was refreshed; this click was not applied. Check the boxes and tick again.";
const DELETED = "This service was deleted elsewhere. Your input is kept: you can save it as a new service, or go back to the list.";

/** A GET function import as an import leaves it: a write until an admin
 *  says that it only reads. */
function strategyOperation(over: Record<string, unknown> = {}) {
    return {
        name: "GetReleaseStrategy", qualified_name: "", title: "Release strategy", kind: "function_import" as const,
        http_method: "GET" as const, bound_to: null, parameters: [], description: "Reads the release strategy.",
        enabled: true, changes_data: true, ...over
    };
}

/** Presses the row of the operation titled `title`, which opens its dialog. */
function iOpenOperation(When: Common, title: string): void {
    When.waitFor({
        id: OPERATIONS_TABLE,
        viewName: VIEW,
        check: function (table: UI5Element) { return !!operationItem(table, title); },
        success: function (table: UI5Element) { new Press().executeOn(operationItem(table, title)); },
        errorMessage: `No row "${title}" in the operations table`
    });
}

/** Types into the business name or the description of the operation dialog. */
function iEnterInOperationDialog(When: Common, id: "operationTitle" | "operationDescription", text: string): void {
    When.waitFor({
        controlType: id === "operationTitle" ? "sap.m.Input" : "sap.m.TextArea",
        searchOpenDialogs: true,
        matchers: withId(id),
        actions: new EnterText({ text }),
        errorMessage: `No field ${id} in the operation dialog`
    });
}

opaTest("marking an enabled operation as only reading is said next to Save and asked about by Save, with the agents without Allow writes", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    const READERS = "Agents without \"Allow writes\" that use this service: reader-agent.";
    let writer = "";
    let before = "";
    let shown: UI5Element;

    iOpenPrepared(Given, When, JOBS, function () {
        const service = stored(JOBS);
        service.definition.operations.push(strategyOperation());
        before = JSON.stringify(service.definition.operations);
        writer = service.used_by[0].agent;
        service.used_by.push({ ...service.used_by[0], agent_id: 997, agent: "reader-agent", allow_write: false });
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        shown = page;
        Opa5.assert.strictEqual(operationRow(page, STRATEGY).changesData, true, "a write as stored");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "nothing pending");
    });

    // The question at the untick is not the statement: Save is.
    iTickOperation(When, STRATEGY, "changes");
    iPressInDialog(When, "It only reads");
    iSee(Then, "the pending read", function (page: UI5Element) {
        return !operationRow(page, STRATEGY).changesData;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(stripOf(page, "odataPendingWrites"), {
            visible: true, text: `${pendingRead(STRATEGY_ITEM)} ${READERS}`
        }, "the strip next to Save names the operation, what changes and the agents it newly reaches");
        Opa5.assert.strictEqual(
            announced(),
            `${STRATEGY_ITEM} will be marked as only reading by Save: callable without "Allow writes" and no longer recorded.`,
            "and it is announced"
        );
        Opa5.assert.strictEqual(backend.requests.filter((r) => /^(PUT|POST)/.test(r)).length, 0, "nothing is sent");
        Opa5.assert.strictEqual(stored(JOBS).definition.operations[1].changes_data, true, "nothing is stored");
    });

    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `These agents use this service: ${writer}, reader-agent.\n\n${readQuestion(STRATEGY_ITEM)}\n\n${READERS}`,
            "the Save question names the operation and the agents without Allow writes"
        );
        Opa5.assert.strictEqual(
            (dialog as unknown as { getTitle(): string }).getTitle(), "Save these operations as only reading?", "under its own title"
        );
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "nothing is sent before the answer");
        Opa5.assert.strictEqual(stripOf(shown, "odataPendingWrites").visible, true, "the strip stays until it is saved");
    }, "the confirmation of a call that is no longer recorded");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled", function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT), 0, "Cancel sends nothing");
        Opa5.assert.strictEqual(stored(JOBS).definition.operations[1].changes_data, true, "nothing is stored");
    });

    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function () { Opa5.assert.ok(true, "the next Save asks again"); }, "the confirmation, again");
    iPressInDialog(When, "Save");
    iSee(Then, "the saved read", function (page: UI5Element) {
        return backend.countRequests(PUT) === 1 && !stripOf(page, "odataPendingWrites").visible;
    }, function (page: UI5Element) {
        const sent = (backend.bodies[PUT]?.definition as { operations: Record<string, unknown>[] }).operations;
        const expected = JSON.parse(before) as Record<string, unknown>[];
        expected[1].changes_data = false;
        Opa5.assert.deepEqual(sent, expected, "the PUT carries both operations as stored, with only that flag changed");
        Opa5.assert.strictEqual(operationRow(page, STRATEGY).changesData, false, "the saved service shows it as a read");
    });
    // Stored as a read it stays one: the next save has nothing to ask.
    iEnter(When, "odataNotFor", "Contracts");
    iPress(When, "odataSaveButton");
    iSee(Then, "the second save", function () { return backend.countRequests(PUT) === 2; }, function () {
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "a stored read is not asked about again");
    });
    Then.iStopTheApp();
});

opaTest("a read that is all a save changes is asked about on a service nobody uses, when all agents may write, and with a new write", function (Given: Common, When: Common, Then: Common) {
    let agent = "";

    // Every agent has "Allow writes": the call still stops being recorded.
    iOpenPrepared(Given, When, JOBS, function () {
        stored(JOBS).definition.operations.push(strategyOperation({ enabled: false, changes_data: false }));
        stored(JOBS).definition.entity_sets[0].operations = ["list", "get"];
        agent = stored(JOBS).used_by[0].agent;
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "stored off: nothing pending");
    });
    iTickOperation(When, STRATEGY, "enabled");
    iTick(When, ITEM, "update");
    iSee(Then, "a pending write and a pending read", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").text.indexOf("only reading") !== -1 && entityRow(page, ITEM).ticked.indexOf("update") !== -1;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(
            stripOf(page, "odataPendingWrites").text,
            `${pending(`Update on "Requisition item" (A_PurchaseRequisitionItem); ${ITEM_FIELDS}`)} `
            + `${pendingRead(STRATEGY_ITEM)} Every agent that uses this service has "Allow writes" already.`,
            "one strip says both, each in its own words"
        );
    });
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `The agent ${agent} uses this service.\n\n`
            + `Saving enables these writes in SAP: Update on "Requisition item" (A_PurchaseRequisitionItem); ${ITEM_FIELDS}.\n\n`
            + `The agent ${agent} has "Allow writes" and will be able to run them.\n\n${AUDITED}\n\n`
            + `${readQuestion(STRATEGY_ITEM)}\n\nEvery agent that uses this service has "Allow writes" already.`,
            "one question for the write and the read"
        );
        Opa5.assert.strictEqual((dialog as unknown as { getTitle(): string }).getTitle(), "Confirm these changes");
    }, "the combined confirmation");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled", function () {
        Opa5.assert.strictEqual(backend.countRequests(`PUT odata/services/${JOBS}`), 0, "nothing was sent");
    });
    Then.iStopTheApp();
});

opaTest("a service saved as new asks about its operations that only read; Duplicate, a plain copy, lists none", function (Given: Common, When: Common, Then: Common) {
    const child = (dialog: UI5Element, id: string) => (dialog as unknown as {
        findAggregatedObjects(deep: boolean, filter: (c: UI5Element) => boolean): UI5Element[];
    }).findAggregatedObjects(true, withId(id))[0];

    iOpenPrepared(Given, When, JOBS, function () {
        stored(JOBS).definition.operations.push(strategyOperation({ changes_data: false }));
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "a stored read stays a read: no entry");
    });
    // A copy carries the read as the source has it: nothing is widened.
    iPress(When, "odataDuplicateButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        const text = (child(dialog, "odataDuplicateWrites") as MessageStrip).getText();
        Opa5.assert.ok(text.indexOf(RELEASE) !== -1, "the copy's writes are listed");
        Opa5.assert.strictEqual(text.indexOf("Release strategy"), -1, "the read it copies as it is is not");
    }, "the duplicate dialog");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "closed", function () {
        backend.odataServices = backend.odataServices.filter((service) => service.name !== JOBS);
    });

    iEnter(When, "odataTitle", "Kept input");
    iPress(When, "odataSaveButton");
    iPress(When, "odataSaveAsNew");
    iSee(Then, "the pending read of a new service", function (page: UI5Element) {
        return stripOf(page, "odataPendingWrites").text.indexOf("only reading") !== -1;
    }, function (page: UI5Element) {
        Opa5.assert.ok(
            stripOf(page, "odataPendingWrites").text.indexOf(`${pendingRead(STRATEGY_ITEM)} No agent uses this service yet.`) !== -1,
            "for a service that does not exist, an enabled read is new too"
        );
    });
    iPress(When, "odataSaveButton");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            "Saving enables these writes in SAP: Update on \"Requisition item\" (A_PurchaseRequisitionItem); "
            + `Create, Update on "Item text" (A_PurchaseReqnItemText); ${RELEASE}; ${ITEM_FIELDS}; ${TEXT_FIELD}.`
            + `\n\n${NO_AGENTS}\n\n${AUDITED}\n\n${readQuestion(STRATEGY_ITEM)}\n\nNo agent uses this service yet.`,
            "the question of a service saved as new names the read with the writes"
        );
        Opa5.assert.strictEqual(backend.countRequests("POST odata/services"), 0, "nothing is sent before the answer");
    }, "the confirmation of a service saved as new");
    iPressInDialog(When, "Cancel");
    iSeeNoDialog(Then, "cancelled");
    Then.iStopTheApp();
});

opaTest("a press on an operation opens its name and description for the agent; Apply writes only those, Cancel nothing", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;
    const NEW_TITLE = "Release one item";
    const NEW_DESCRIPTION = "Releases one item of a requisition. <b>Needs</b> the release code of the approver.";
    let before = "";
    let shown: UI5Element;

    iOpenPrepared(Given, When, JOBS, function () {
        const operation = stored(JOBS).definition.operations[0];
        operation.returns = { entity_set: "A_PurchaseRequisitionItem", collection: false };
        operation.parameters[2].required = false;
        before = JSON.stringify(operation);
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) { shown = page; });

    iOpenOperation(When, RELEASE_ROW);
    iSeeADialog(Then, function () {
        Opa5.assert.deepEqual(operationDialogOf(shown), {
            dialogTitle: "Operation: Release item",
            title: "Release item", description: "Releases one requisition item with a release code.",
            editable: [true, true],
            name: "ReleaseItem", method: "POST", kind: "Function import (V2)",
            boundTo: "\"Requisition item\" (A_PurchaseRequisitionItem)",
            returns: "One entry of \"Requisition item\" (A_PurchaseRequisitionItem)",
            parameters: [
                "PurchaseRequisition (Edm.String, required)", "PurchaseRequisitionItem (Edm.String, required)",
                "ReleaseCode (Edm.String, optional)"
            ],
            markup: false
        }, "the two texts to edit, and what SAP says about the operation as text to read");
    }, "the operation dialog");

    // Cancel with a change asks, and leaves the operation as it was.
    iEnterInOperationDialog(When, "operationTitle", "Not this");
    iPressInDialog(When, "Cancel");
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check: function (dialogs: UI5Element[]) { return dialogs.length === 2; },
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(messageOf(dialogs[1]), "Discard the changes to this operation?", "Cancel asks first");
        },
        errorMessage: "No question about the unapplied change"
    });
    iPressInDialog(When, "Discard");
    iSeeNoDialog(Then, "closed without Apply", function () {
        Opa5.assert.strictEqual(operationRow(shown, RELEASE_ROW).title, RELEASE_ROW, "the row is as it was");
        Opa5.assert.strictEqual(JSON.stringify(formOperation(shown, 0)), before, "and so is the form: nothing to save");
        Opa5.assert.strictEqual(actionsOf(shown).save, true);
    });

    // Apply writes the two texts into the form; Save stores them.
    iOpenOperation(When, RELEASE_ROW);
    iSeeADialog(Then, function () {
        Opa5.assert.strictEqual(operationDialogOf(shown).title, "Release item", "the dialog starts from the form, not from what was cancelled");
    }, "the operation dialog, again");
    iEnterInOperationDialog(When, "operationTitle", NEW_TITLE);
    iEnterInOperationDialog(When, "operationDescription", NEW_DESCRIPTION);
    When.waitFor({
        controlType: "sap.m.Button", searchOpenDialogs: true, matchers: withId("operationApplyButton"),
        actions: new Press(), errorMessage: "No Apply in the operation dialog"
    });
    iSee(Then, "the applied texts", function (page: UI5Element) {
        return !!operationItem(page, NEW_TITLE);
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "the dialog is closed");
        Opa5.assert.strictEqual(operationRow(page, NEW_TITLE).description, NEW_DESCRIPTION, "the row shows the new texts");
        Opa5.assert.strictEqual(operationRow(page, NEW_TITLE).technical, "ReleaseItem · POST", "of the same operation");
        Opa5.assert.strictEqual(
            viewOf(page).byId(OPERATIONS_TABLE)!.getDomRef()!.querySelectorAll("b").length, 0, "markup in a description is text"
        );
        Opa5.assert.strictEqual(backend.requests.filter((r) => /^(PUT|POST)/.test(r)).length, 0, "nothing is sent by Apply");
        Opa5.assert.strictEqual(stored(JOBS).definition.operations[0].title, "Release item", "and nothing is stored before Save");
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "a text is no widening: nothing pending");
    });
    iPress(When, "odataSaveButton");
    iSee(Then, "the save", function () { return backend.countRequests(PUT) === 1; }, function () {
        const sent = (backend.bodies[PUT]?.definition as { operations: Record<string, unknown>[] }).operations;
        Opa5.assert.deepEqual(
            sent, [{ ...JSON.parse(before) as Record<string, unknown>, title: NEW_TITLE, description: NEW_DESCRIPTION }],
            "the PUT carries the new texts and everything else of the operation unchanged, `returns` included"
        );
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "a changed text asks nothing");
    });
    Then.iStopTheApp();
});

opaTest("a reason why no agent can call an operation goes when its row was changed on the page", function (Given: Common, When: Common, Then: Common) {
    iOpenPrepared(Given, When, JOBS, function () {
        stored(JOBS).definition.operations[0].parameters.splice(1, 1);
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.ok(operationRow(page, RELEASE_ROW).uncallable !== "", "the reason, about the service as saved");
    });
    iOpenOperation(When, RELEASE_ROW);
    iEnterInOperationDialog(When, "operationDescription", "Changed on the page.");
    When.waitFor({
        controlType: "sap.m.Button", searchOpenDialogs: true, matchers: withId("operationApplyButton"),
        actions: new Press(), errorMessage: "No Apply in the operation dialog"
    });
    iSee(Then, "the changed row", function (page: UI5Element) {
        return operationRow(page, RELEASE_ROW).description === "Changed on the page.";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(operationRow(page, RELEASE_ROW).uncallable, "", "no reason on a row that is not as saved");
        Opa5.assert.strictEqual(stripOf(page, "odataUncallableStrip").visible, false, "and it is not counted");
    });
    Then.iStopTheApp();
});

opaTest("a click on an operation row that no longer is its operation changes nothing and says so", function (Given: Common, When: Common, Then: Common) {
    // What a later task (an import) could do: the operations change order
    // while the table still shows the old rows.
    const reorder = function (page: UI5Element): void {
        const model = viewOf(page).getModel("svc") as unknown as { getProperty(path: string): unknown[] };
        const operations = model.getProperty("/data/definition/operations");
        operations.unshift(operations.splice(1, 1)[0]);
    };
    let before = "";

    Given.iStartTheApp(`odata-services/${UNUSED}`);
    iSeeTheService(Then, UNUSED, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.deepEqual(operationsHeader(page).titles, ["Release", "Reject", STRATEGY]);
        reorder(page);
        before = JSON.stringify([formOperation(page, 0), formOperation(page, 1), formOperation(page, 2)]);
    });
    // The row says "Release", position 0 -- where Reject is now.
    iTickOperation(When, "Release", "enabled");
    iSee(Then, "the rows worked out again", function (page: UI5Element) {
        return operationsHeader(page).titles[0] === "Reject" && operationRow(page, "Release").note !== "";
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(operationsHeader(page).titles, ["Reject", "Release", STRATEGY], "the table shows the operations as they are");
        Opa5.assert.strictEqual(operationRow(page, "Release").note, OPERATIONS_REFRESHED, "the row says that the click was not applied");
        Opa5.assert.strictEqual(announced(), OPERATIONS_REFRESHED, "and a screen reader is told");
        Opa5.assert.strictEqual(
            JSON.stringify([formOperation(page, 0), formOperation(page, 1), formOperation(page, 2)]), before,
            "no operation was switched: neither the one the row named nor the one at its position"
        );
        Opa5.assert.strictEqual(operationRow(page, "Release").enabled, false, "the box shows what will be saved");
        Opa5.assert.strictEqual(operationRow(page, "Reject").enabled, false);
        Opa5.assert.strictEqual(stripOf(page, "odataPendingWrites").visible, false, "no write is pending");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "and nothing is asked");
    });
    Then.iStopTheApp();
});

opaTest("Writes allowed is Yes only for an agent whose entry holds exactly true", function (Given: Common, When: Common, Then: Common) {
    let agent = "";
    iOpenPrepared(Given, When, JOBS, function () {
        const first = stored(JOBS).used_by[0];
        agent = first.agent;
        // Not what the server sends; if it ever does, it is not a yes.
        (first as unknown as { allow_write: unknown }).allow_write = 1;
    });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) {
        Opa5.assert.deepEqual(
            usedByRows(page)[0].slice(0, 3), [agent, "", "No/None"], "`allow_write: 1` renders No, without the mark of a writer"
        );
    });
    Then.iStopTheApp();
});

opaTest("a test call the route refuses says why by its code, and a service that is gone gets its own strip", function (Given: Common, When: Common, Then: Common) {
    const path = `odata/services/${JOBS}/test`;
    const BUSY = "Test call not made: other test calls or metadata reads are running. Try again in a moment.";

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded", function () {
        backend.failNext = { path, status: 429, body: { detail: "busy" }, headers: { "X-OData-Error": "busy" } };
    });
    iPressTestCall(When);
    iSee(Then, "the busy test", function (page: UI5Element) {
        return testStripOf(page).visible;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(testStripOf(page), { visible: true, type: "Error", markup: false, text: BUSY }, "429 busy");
        Opa5.assert.strictEqual(actionsOf(page).save, true, "the page is usable again");
        // The same status from somewhere on the way, without the code.
        backend.failNext = { path, status: 429, body: { detail: "Too many requests <b>from a proxy</b>" } };
    });
    iPressTestCall(When);
    iSee(Then, "the test refused without a code", function (page: UI5Element) {
        return testStripOf(page).text.indexOf("proxy") !== -1;
    }, function (page: UI5Element) {
        Opa5.assert.deepEqual(testStripOf(page), {
            visible: true, type: "Error", markup: false, text: "Test call not made. Too many requests <b>from a proxy</b>"
        }, "a status alone is not taken for the server's refusal: what was answered is shown, as text");
        backend.failNext = { path, status: 404, body: { detail: "Service not found" } };
    });
    iPressTestCall(When);
    iSee(Then, "the service that is gone", function (page: UI5Element) {
        return stripOf(page, "odataDeletedElsewhere").visible;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(stripOf(page, "odataDeletedElsewhere").text, DELETED, "404: deleted elsewhere, with the way on");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "said on the page, not in a dialog");
        Opa5.assert.strictEqual(backend.countRequests(TEST), 3, "three calls, none repeated");
    });
    Then.iStopTheApp();
});

opaTest("the result of a test call goes with the first change of the form and when another service is shown", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");
    iPressTestCall(When);
    iSee(Then, "the result", function (page: UI5Element) {
        return testStripOf(page).type === "Success";
    }, function () {
        HashChanger.getInstance().setHash(`odata-services/${USER}`);
    });
    iSeeTheService(Then, USER, "another service", function (page: UI5Element) {
        Opa5.assert.strictEqual(testStripOf(page).visible, false, "the result of the other service is not shown over this one");
    });

    // A field of the form.
    iPressTestCall(When);
    iSee(Then, "the result for this service", function (page: UI5Element) {
        return testStripOf(page).type === "Success";
    }, function () { Opa5.assert.ok(true, "tested as saved"); });
    iEnter(When, "odataNotFor", "Contracts");
    iSee(Then, "no result after typing", function (page: UI5Element) {
        return !testStripOf(page).visible;
    }, function () { Opa5.assert.ok(true, "the first edit takes the result away: it is about the service before it"); });

    // A box of the definition.
    iPressTestCall(When);
    iSee(Then, "the hint to save", function (page: UI5Element) {
        return testStripOf(page).type === "Warning";
    }, function () { Opa5.assert.ok(true, "the strip is back, about the unsaved form"); });
    iTick(When, ITEM, "get");
    iSee(Then, "no strip after a tick", function (page: UI5Element) {
        return !testStripOf(page).visible;
    }, function () { Opa5.assert.ok(true, "a change of the definition takes it away too"); });
    Then.iStopTheApp();
});

opaTest("the operation dialog goes when another service is shown, keeps a title that is too long with its error, and Escape asks about a change", function (Given: Common, When: Common, Then: Common) {
    let shown: UI5Element;
    const title = () => viewOf(shown).byId("operationTitle") as Input;

    iOpenPrepared(Given, When, JOBS, function () { /* as seeded */ });
    iSeeTheService(Then, JOBS, "the service is loaded", function (page: UI5Element) { shown = page; });

    // Another service by the address: the dialog is closed, and that
    // service's form is as stored.
    iOpenOperation(When, RELEASE_ROW);
    iSeeADialog(Then, function () {
        HashChanger.getInstance().setHash(`odata-services/${UNUSED}`);
    }, "the operation dialog");
    iSeeTheService(Then, UNUSED, "the other service is shown", function () {
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "the dialog did not stay open over it");
        const model = viewOf(shown).getModel("svc") as unknown as { getProperty(path: string): unknown };
        Opa5.assert.strictEqual(
            JSON.stringify(model.getProperty("/data/definition")), JSON.stringify(stored(UNUSED).definition),
            "the form of the other service is unchanged"
        );
    });

    // A title of 121 characters: Apply is not taken, and the field says why.
    When.waitFor({
        id: PAGE, viewName: VIEW,
        success: function () { HashChanger.getInstance().setHash(`odata-services/${JOBS}`); }
    });
    iSeeTheService(Then, JOBS, "the first service again");
    iOpenOperation(When, RELEASE_ROW);
    iSeeADialog(Then, function () {
        title().setValue("x".repeat(121));
    }, "the operation dialog, again");
    When.waitFor({
        controlType: "sap.m.Button", searchOpenDialogs: true, matchers: withId("operationApplyButton"),
        actions: new Press(), errorMessage: "No Apply in the operation dialog"
    });
    iSeeADialog(Then, function () {
        Opa5.assert.strictEqual(title().getValueState(), "Error", "the dialog stays, with the title marked");
        Opa5.assert.ok(title().getValueStateText().length > 0, "and a text that says why");
        Opa5.assert.strictEqual(formOperation(shown, 0).title, "Release item", "nothing was applied");
    }, "the dialog after a refused Apply");

    // Escape with a change asks before the dialog closes.
    iSeeADialog(Then, function (dialog: UI5Element) {
        (dialog as unknown as { onsapescape(event: object): void }).onsapescape({
            preventDefault: function () { /* nothing to prevent */ }, stopPropagation: function () { /* nor to stop */ },
            originalEvent: {}
        });
    }, "the dialog before Escape");
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check: function (dialogs: UI5Element[]) { return dialogs.length === 2; },
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(messageOf(dialogs[1]), "Discard the changes to this operation?", "Escape asks first");
        },
        errorMessage: "No question after Escape"
    });
    iPressInDialog(When, "Discard");
    iSeeNoDialog(Then, "closed after the answer", function () {
        Opa5.assert.strictEqual(formOperation(shown, 0).title, "Release item", "and nothing of it is in the form");
    });
    Then.iStopTheApp();
});

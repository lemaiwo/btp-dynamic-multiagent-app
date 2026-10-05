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
    ENTITY_TABLE, PAGE, VIEW, accessibleName, actionsOf, counterState, entityHeader, entityItem, entityItems,
    entityRow, entityTitles, formOf, opBox, pageTitle, pressSegment, stateOf, stripOf, tagsOf, toasts, viewOf, withId,
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
        Opa5.assert.strictEqual(accessibleName(opBox(item, "update")), "Update Requisition item", "a checkbox is named by operation and entity set");
        Opa5.assert.strictEqual(accessibleName(opBox(entityItem(page, DELIVERY), "list")), "List Delivery address", "in every row");
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
            + `Saving enables these write operations in SAP: ${WRITES}.\n\n`
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
            pending("Update on \"Requisition item\" (A_PurchaseRequisitionItem)"), "against what is stored now"
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
            + "Saving enables these write operations in SAP: Delete on \"Requisition item\" (A_PurchaseRequisitionItem); "
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

opaTest("a service no agent uses is saved with its new writes without a question; the section still said what they mean", function (Given: Common, When: Common, Then: Common) {
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
    iSee(Then, "the save", function () {
        return backend.countRequests(PUT) === 1;
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "nobody to name, nothing to ask");
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
    iPress(When, "odataSaveButton");
    iSee(Then, "the marked row", function (page: UI5Element) {
        return entityRow(page, ACCOUNT).error !== "";
    }, function (page: UI5Element) {
        Opa5.assert.strictEqual(entityRow(page, ACCOUNT).error, NEEDS_SELECTABLE, "the row says what is wrong, in the user's words");
        Opa5.assert.strictEqual(entityRow(page, ITEM).error, "", "the other rows are not marked");
        Opa5.assert.strictEqual(
            stripOf(page, "odataSaveError").text, "Not saved. Check the marked entity sets: Account assignment.",
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
            stripOf(page, "odataSaveError").text, `The service was not saved. The server answered: ${DETAIL}`,
            "the whole answer is still above the form"
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

opaTest("Add appends an entity set with nothing enabled, and it is saved with the rest", function (Given: Common, When: Common, Then: Common) {
    const PUT = `PUT odata/services/${JOBS}`;

    Given.iStartTheApp(`odata-services/${JOBS}`);
    iSeeTheService(Then, JOBS, "the service is loaded");
    iEnter(When, "odataEntitySearch", "header");
    iPress(When, "odataAddEntitySetButton");
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
        return stripOf(page, "odataChangedElsewhere").visible;
    }, function () {
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

import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type Input from "sap/m/Input";
import type StandardListItem from "sap/m/StandardListItem";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";
import {
    DESTINATION, PAGE, VIEW, announced, destinationOf, formOf, pressSegment, stateOf, toasts,
    type DestinationField
} from "./pages/ODataDetail";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("OData destination dropdown journey");

/** Technical user through S4_ODATA_TECH, used by agents. */
const JOBS = "purchase-requisitions-jobs";
/** Signed-in user through S4_ODATA_USER, used by nobody: a save asks nothing. */
const UNUSED = "purchase-requisitions-v4";

const FIXED = "Signs in with one fixed account";
const AS_USER = "Signs in as the user";
const CHOICES = [
    ["S4_DEV_BASIC", `${FIXED} · internet · service instance`],
    ["S4_DEV_USER", `${AS_USER} · internet · subaccount`],
    ["S4_ODATA_TECH", `${FIXED} · on-premise · subaccount · Development system, technical user for jobs`],
    ["S4_ODATA_USER", `${AS_USER} · on-premise · subaccount · Development system, as the signed-in user`]
];
const UNUSABLE = "2 destinations cannot be used for OData and are not listed.";
const UNAVAILABLE = "The list of destinations could not be loaded. Type the name of the destination.";
const INCOMPLETE = "The list may be incomplete. A destination that is not shown can be typed.";
const FIXED_ACCOUNT = "This destination does not sign in as the user: every user would act as one and the same "
    + "account. Choose a destination that signs in as the user, or let the service run as a technical user.";
const NEEDS_USER = "This destination needs a signed-in user, so scheduled runs will be refused. Choose a "
    + "destination with a fixed account, or let the service run as the signed-in user.";
const NOT_LISTED = "Not in the list of destinations. Check the spelling, or create the destination before agents "
    + "use this service.";
const LIST_CALL = "GET odata/destinations";

function stored(name: string) {
    return backend.odataServices.filter((service) => service.name === name)[0];
}

/** Starts the app and opens the service `name` once `prepare` has set up
 *  the backend (the start itself resets it). */
function iOpen(Given: Common, name: string, prepare?: () => void): void {
    Given.iStartTheApp();
    Given.waitFor({
        id: "sideNavigation",
        viewName: "App",
        success: function () {
            if (prepare) {
                prepare();
            }
            HashChanger.getInstance().setHash(`odata-services/${name}`);
        },
        errorMessage: "The app did not start"
    });
}

function iEnter(When: Common, id: string, text: string): void {
    When.waitFor({ id, viewName: VIEW, actions: new EnterText({ text }), errorMessage: `No field ${id}` });
}

function iPress(When: Common, id: string): void {
    When.waitFor({ id, viewName: VIEW, actions: new Press(), errorMessage: `No control ${id}` });
}

function iRunAs(When: Common, key: "user" | "technical"): void {
    When.waitFor({
        id: "odataRunsAs",
        viewName: VIEW,
        actions: function (segmented: UI5Element | null) { pressSegment(segmented as UI5Element, key); },
        errorMessage: "No Runs as"
    });
}

/** Waits until the destination field is as `check` wants it, with the
 *  service `name` on the page, then asserts. */
function iSeeTheField(
    Then: Common, name: string, what: string,
    check: (field: DestinationField) => boolean, assert: (field: DestinationField, page: UI5Element) => void
): void {
    Then.waitFor({
        id: PAGE,
        viewName: VIEW,
        check: function (page: UI5Element) {
            return formOf(page).name === name && check(destinationOf(page));
        },
        success: function (page: UI5Element) { assert(destinationOf(page), page); },
        errorMessage: `Not seen: ${what}`
    });
}

function iSeeNoDialog(what: string): void {
    Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen, .sapMMessageBox").length, 0, what);
}

opaTest("the field offers the usable destinations in words and keeps the stored one", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED);

    iSeeTheField(Then, UNUSED, "the list in the dropdown", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.deepEqual(field.choices, CHOICES, "name and a line in words; the two unusable ones are left out");
        Opa5.assert.strictEqual(field.value, "S4_ODATA_USER", "the stored destination is still in the field");
        Opa5.assert.strictEqual(field.state, "None", "a user destination for the signed-in user: nothing to say");
        Opa5.assert.strictEqual(field.hint, UNUSABLE, "how many were left out");
        Opa5.assert.ok(field.hintDescribes, "the hint is the field's description");
        Opa5.assert.strictEqual(backend.countRequests(LIST_CALL), 1, "read once");
    });

    // Picked from the open dropdown, as a user does.
    iPress(When, DESTINATION);
    When.waitFor({
        controlType: "sap.m.StandardListItem",
        searchOpenDialogs: true,
        matchers: function (item: UI5Element) { return (item as StandardListItem).getTitle() === "S4_DEV_USER"; },
        success: function (items: UI5Element[]) {
            Opa5.assert.strictEqual(
                (items[0] as StandardListItem).getInfo(), `${AS_USER} · internet · subaccount`,
                "the open dropdown shows the line next to the name"
            );
        },
        actions: new Press(),
        errorMessage: "S4_DEV_USER is not in the open dropdown"
    });
    iSeeTheField(Then, UNUSED, "the picked destination", (field) => field.value === "S4_DEV_USER", function (field) {
        Opa5.assert.strictEqual(field.state, "None");
    });

    // Typing does not ask the server again.
    iEnter(When, DESTINATION, "S4_ODATA_USER");
    iEnter(When, DESTINATION, "S4_DEV_USER");
    iPress(When, "odataSaveButton");
    Then.waitFor({
        check: function () { return stored(UNUSED).destination === "S4_DEV_USER"; },
        success: function () {
            Opa5.assert.strictEqual(stored(UNUSED).destination, "S4_DEV_USER", "the value in the field is what is stored");
            Opa5.assert.strictEqual(backend.countRequests(LIST_CALL), 1, "still read once: not per keystroke, not per save");
        },
        errorMessage: "The destination was not saved"
    });
    Then.iStopTheApp();
});

opaTest("a destination that does not fit Runs as is a warning that follows both fields", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, JOBS);
    iSeeTheField(Then, JOBS, "the list", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.strictEqual(field.state, "None");
    });

    iEnter(When, DESTINATION, "S4_DEV_USER");
    iSeeTheField(Then, JOBS, "technical user, user destination", (field) => field.state === "Warning", function (field) {
        Opa5.assert.strictEqual(field.stateText, NEEDS_USER);
    });

    iRunAs(When, "user");
    iSeeTheField(Then, JOBS, "it fits now", (field) => field.state === "None", function (field) {
        Opa5.assert.strictEqual(field.stateText, "", "no text left behind");
        Opa5.assert.strictEqual(field.value, "S4_DEV_USER");
    });

    iEnter(When, DESTINATION, "S4_DEV_BASIC");
    iSeeTheField(Then, JOBS, "signed-in user, fixed account", (field) => field.state === "Warning", function (field) {
        Opa5.assert.strictEqual(field.stateText, FIXED_ACCOUNT);
    });

    iRunAs(When, "technical");
    iSeeTheField(Then, JOBS, "it fits again", (field) => field.state === "None", function () {
        Opa5.assert.ok(true, "the warning went with the choice of Runs as");
    });

    // Changed away from the field: said to a screen reader as well.
    iRunAs(When, "user");
    Then.waitFor({
        id: PAGE,
        viewName: VIEW,
        check: function (page: UI5Element) {
            return destinationOf(page).state === "Warning" && announced() === FIXED_ACCOUNT;
        },
        success: function (page: UI5Element) {
            Opa5.assert.strictEqual(destinationOf(page).stateText, FIXED_ACCOUNT, "the warning is back");
            Opa5.assert.strictEqual(announced(), FIXED_ACCOUNT, "and announced, since the focus is not on the field");
        },
        errorMessage: "The warning was not announced"
    });
    Then.iStopTheApp();
});

opaTest("the warning does not stand in the way of a save, and an error of the field comes first", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED);
    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.strictEqual(field.value, "S4_ODATA_USER");
        Opa5.assert.strictEqual(field.state, "None", "a user destination for the signed-in user");
    });

    iEnter(When, DESTINATION, "S4_ODATA_TECH");
    iSeeTheField(Then, UNUSED, "the mismatch", (field) => field.state === "Warning", function (field) {
        Opa5.assert.strictEqual(field.stateText, FIXED_ACCOUNT);
    });
    iPress(When, "odataSaveButton");
    Then.waitFor({
        check: function () { return stored(UNUSED).destination === "S4_ODATA_TECH"; },
        success: function () {
            Opa5.assert.strictEqual(stored(UNUSED).destination, "S4_ODATA_TECH", "saved with the warning showing");
            iSeeNoDialog("and nothing was asked about it");
        },
        errorMessage: "The save was held back by the warning"
    });
    iSeeTheField(Then, UNUSED, "the warning after the save", (field) => field.state === "Warning", function (field) {
        Opa5.assert.strictEqual(field.stateText, FIXED_ACCOUNT, "it is still true, so it is still there");
    });

    // A name the server refuses: the error, not the note about the list.
    iEnter(When, DESTINATION, "S4 DEV");
    iSeeTheField(Then, UNUSED, "not in the list", (field) => field.state === "Information", function (field) {
        Opa5.assert.strictEqual(field.stateText, NOT_LISTED);
    });
    iPress(When, "odataSaveButton");
    iSeeTheField(Then, UNUSED, "the refused name", (field) => field.state === "Error", function (field) {
        Opa5.assert.strictEqual(
            field.stateText, "Use letters, digits, dots, hyphens and underscores only.",
            "the error has the field"
        );
        Opa5.assert.strictEqual(stored(UNUSED).destination, "S4_ODATA_TECH", "nothing was stored");
    });

    // Typed again: the error goes, what the list says comes back.
    iEnter(When, DESTINATION, "S4_ODATA_USER");
    iSeeTheField(Then, UNUSED, "a fitting destination", (field) => field.state === "None", function (field) {
        Opa5.assert.strictEqual(field.stateText, "");
    });
    Then.iStopTheApp();
});

opaTest("a typed name that is not in a complete list is a note, and can be saved", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED);
    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function () {
        Opa5.assert.ok(true, "the list is there");
    });

    iEnter(When, DESTINATION, "S4_NEW_DESTINATION");
    iSeeTheField(Then, UNUSED, "the note", (field) => field.state === "Information", function (field) {
        Opa5.assert.strictEqual(field.stateText, NOT_LISTED);
        Opa5.assert.strictEqual(field.value, "S4_NEW_DESTINATION", "the typed name stays");
    });
    iRunAs(When, "technical");
    iSeeTheField(Then, UNUSED, "the note under the other identity", (field) => field.state === "Information", function (field) {
        Opa5.assert.strictEqual(field.stateText, NOT_LISTED, "no mismatch is claimed for a destination nobody knows");
    });

    iPress(When, "odataSaveButton");
    Then.waitFor({
        check: function () { return stored(UNUSED).destination === "S4_NEW_DESTINATION"; },
        success: function () {
            Opa5.assert.strictEqual(stored(UNUSED).destination, "S4_NEW_DESTINATION", "saved as typed");
            Opa5.assert.strictEqual(stored(UNUSED).user_context, false);
        },
        errorMessage: "The typed name was not saved"
    });

    // A listed destination that is not an HTTP destination.
    iEnter(When, DESTINATION, "S4_DEV_RFC");
    iSeeTheField(Then, UNUSED, "not usable", (field) => field.state === "Warning", function (field) {
        Opa5.assert.strictEqual(
            field.stateText, "This destination is not an HTTP destination and cannot be used for OData calls."
        );
    });
    Then.iStopTheApp();
});

opaTest("without a list the field is a text field with one quiet hint", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, JOBS, function () { backend.destinationsMode = "unavailable"; });

    iSeeTheField(Then, JOBS, "the hint", (field) => field.hint !== "", function (field) {
        Opa5.assert.strictEqual(field.hint, UNAVAILABLE);
        Opa5.assert.ok(field.hintDescribes, "the hint is the field's description");
        Opa5.assert.deepEqual(field.choices, [], "nothing to pick");
        Opa5.assert.strictEqual(field.value, "S4_ODATA_TECH", "the stored destination is untouched");
        Opa5.assert.strictEqual(field.state, "None", "no red field");
        iSeeNoDialog("no error dialog");
        Opa5.assert.deepEqual(toasts(), [], "no toast");
    });

    iEnter(When, DESTINATION, "S4_DEV_USER");
    iRunAs(When, "user");
    iRunAs(When, "technical");
    iSeeTheField(Then, JOBS, "a typed name", (field) => field.value === "S4_DEV_USER", function (field) {
        Opa5.assert.strictEqual(field.state, "None", "nothing is known, so nothing is said about the name");
        Opa5.assert.strictEqual(field.hint, UNAVAILABLE);
    });
    iEnter(When, "odataTitle", "Requisitions for jobs");
    iPress(When, "odataSaveButton");
    // The destination changed under agents that use the service: the page asks.
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: function (button: UI5Element) { return (button as unknown as { getText(): string }).getText() === "Save"; },
        actions: new Press(),
        errorMessage: "No question about the changed destination"
    });
    Then.waitFor({
        check: function () { return stored(JOBS).destination === "S4_DEV_USER"; },
        success: function () {
            Opa5.assert.strictEqual(stored(JOBS).destination, "S4_DEV_USER", "the typed name is saved");
        },
        errorMessage: "The typed name was not saved"
    });
    Then.iStopTheApp();
});

opaTest("a call that gets no answer at all leaves the same hint", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED, function () {
        backend.failNext = { path: "odata/destinations", status: 0, body: null, network: true };
    });

    iSeeTheField(Then, UNUSED, "the hint", (field) => field.hint !== "", function (field) {
        Opa5.assert.strictEqual(field.hint, UNAVAILABLE);
        Opa5.assert.strictEqual(field.value, "S4_ODATA_USER");
        Opa5.assert.strictEqual(field.state, "None");
        iSeeNoDialog("no error dialog");
    });
    Then.iStopTheApp();
});

opaTest("a cut list says it may be incomplete and blames no typed name", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, JOBS, function () { backend.destinationsMode = "truncated"; });

    iSeeTheField(Then, JOBS, "the cut list", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.deepEqual(field.choices.map((choice) => choice[0]), ["S4_DEV_BASIC", "S4_DEV_USER"]);
        Opa5.assert.strictEqual(field.hint, INCOMPLETE);
        Opa5.assert.strictEqual(field.value, "S4_ODATA_TECH");
        Opa5.assert.strictEqual(field.state, "None", "the stored name is not in the cut list, and that proves nothing");
    });

    iEnter(When, DESTINATION, "S4_DEV_USER");
    iSeeTheField(Then, JOBS, "what the cut list holds is judged", (field) => field.state === "Warning", function (field) {
        Opa5.assert.strictEqual(field.stateText, NEEDS_USER);
    });
    Then.iStopTheApp();
});

opaTest("a list of one level says it may be incomplete", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, JOBS, function () { backend.destinationsMode = "partial"; });

    iSeeTheField(Then, JOBS, "the instance level", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.deepEqual(field.choices.map((choice) => choice[0]), ["S4_DEV_BASIC"]);
        Opa5.assert.strictEqual(field.hint, INCOMPLETE);
        Opa5.assert.strictEqual(field.state, "None");
    });
    Then.iStopTheApp();
});

opaTest("a slow list does not hold the page up and never touches what was typed", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, JOBS, function () { backend.destinationsMode = "held"; });

    // The form is there and can be worked in while the list is on its way.
    Then.waitFor({
        id: "odataName",
        viewName: VIEW,
        check: function (input: UI5Element) { return (input as Input).getValue() === JOBS; },
        success: function (input: UI5Element) {
            const field = destinationOf(input);
            Opa5.assert.strictEqual(field.value, "S4_ODATA_TECH", "the form shows the service");
            Opa5.assert.deepEqual([field.choices, field.hint, field.state], [[], "", "None"], "no list yet, and no words about it");
            Opa5.assert.strictEqual(backend.countRequests(LIST_CALL), 1, "asked for");
        },
        errorMessage: "The form waited for the list"
    });
    iEnter(When, DESTINATION, "S4_DEV_USER");
    iSeeTheField(Then, JOBS, "typed while waiting", (field) => field.value === "S4_DEV_USER", function (field) {
        Opa5.assert.strictEqual(field.state, "None");
    });

    When.waitFor({ success: function () { backend.releaseDestinations(); } });
    Then.waitFor({
        id: PAGE,
        viewName: VIEW,
        check: function (page: UI5Element) {
            return destinationOf(page).choices.length > 0 && announced() === NEEDS_USER;
        },
        success: function (page: UI5Element) {
            const field = destinationOf(page);
            Opa5.assert.deepEqual(field.choices, CHOICES, "the list arrived");
            Opa5.assert.strictEqual(field.value, "S4_DEV_USER", "what was typed is still there");
            Opa5.assert.deepEqual([field.state, field.stateText], ["Warning", NEEDS_USER], "and is judged now");
            Opa5.assert.strictEqual(announced(), NEEDS_USER, "said, since it appeared by itself");
        },
        errorMessage: "The list did not arrive"
    });
    Then.iStopTheApp();
});

opaTest("each visit of the page reads the list once; a late answer for another visit is dropped", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, JOBS, function () { backend.destinationsMode = "held"; });
    Then.waitFor({
        id: "odataName",
        viewName: VIEW,
        check: function (input: UI5Element) { return (input as Input).getValue() === JOBS; },
        success: function () {
            // The second visit starts before the first list arrived.
            HashChanger.getInstance().setHash(`odata-services/${UNUSED}`);
        },
        errorMessage: "The first service did not open"
    });
    Then.waitFor({
        id: "odataName",
        viewName: VIEW,
        check: function (input: UI5Element) {
            return (input as Input).getValue() === UNUSED && backend.countRequests(LIST_CALL) === 2;
        },
        success: function () {
            Opa5.assert.strictEqual(backend.countRequests(LIST_CALL), 2, "one read per visit");
            // This visit's answer comes first ...
            backend.releaseDestinations("ok", true);
        },
        errorMessage: "The second service did not open"
    });
    iSeeTheField(Then, UNUSED, "the list of this visit", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.deepEqual(field.choices, CHOICES);
        // ... and then the one of the visit before, which says there is no list.
        backend.releaseDestinations("unavailable");
    });
    // The fake answers in a promise; give a wrongly taken answer the time to show.
    Then.waitFor({
        id: PAGE,
        viewName: VIEW,
        check: function () {
            const waited = (window as unknown as { destinationsWaited?: number });
            waited.destinationsWaited = (waited.destinationsWaited ?? 0) + 1;
            return waited.destinationsWaited > 3;
        },
        success: function (page: UI5Element) {
            delete (window as unknown as { destinationsWaited?: number }).destinationsWaited;
            const field = destinationOf(page);
            Opa5.assert.deepEqual(field.choices, CHOICES, "the late answer changed nothing");
            Opa5.assert.strictEqual(field.hint, UNUSABLE, "and its hint is not shown");
            Opa5.assert.strictEqual(field.value, "S4_ODATA_USER");
            Opa5.assert.strictEqual(field.state, "None");
        }
    });
    Then.iStopTheApp();
});

opaTest("a new service gets the list as well, and an empty field is not judged", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, "new");

    iSeeTheField(Then, "", "the list on an empty form", (field) => field.choices.length > 0, function (field, page) {
        Opa5.assert.deepEqual(field.choices, CHOICES);
        Opa5.assert.deepEqual([field.value, field.state], ["", "None"]);
        Opa5.assert.strictEqual(stateOf(page, DESTINATION).text, "");
    });
    iPress(When, "odataSaveButton");
    iSeeTheField(Then, "", "the required field", (field) => field.state === "Error", function (field) {
        Opa5.assert.strictEqual(field.stateText, "Enter the name of the destination.");
    });
    iEnter(When, DESTINATION, "S4_DEV_BASIC");
    iSeeTheField(Then, "", "a fitting choice", (field) => field.value === "S4_DEV_BASIC" && field.state === "None", function (field) {
        Opa5.assert.strictEqual(field.stateText, "", "a new service runs as a technical user: a fixed account fits");
    });
    Then.iStopTheApp();
});

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
    DESTINATION, PAGE, VIEW, announced, destinationOf, formOf, keyInDestination, leaveDestination, pressSegment,
    stateOf, toasts, typeDestination,
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
    ["S4_DEV", `${FIXED} · internet · subaccount`],
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
        Opa5.assert.deepEqual(field.choices.map((choice) => choice[0]), ["S4_DEV", "S4_DEV_BASIC", "S4_DEV_USER"]);
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

// --- what was typed is what is stored ---------------------------------------------

const PUT_UNUSED = `PUT odata/services/${UNUSED}`;

/** S4_NEW_PP is listed; S4_NEW, its beginning, is not. */
function withNewPp(): void {
    backend.odataDestinations.push({
        ...backend.odataDestinations.filter((item) => item.name === "S4_ODATA_USER")[0], name: "S4_NEW_PP",
        description: ""
    });
}

/** Types `text` as keystrokes do and waits until the field shows `completed`. */
function iType(When: Common, Then: Common, text: string, completed: string): void {
    When.waitFor({
        id: PAGE,
        viewName: VIEW,
        success: function (page: UI5Element) { typeDestination(page, text); },
        errorMessage: "No page to type in"
    });
    Then.waitFor({
        id: PAGE,
        viewName: VIEW,
        check: function (page: UI5Element) { return destinationOf(page).shown === completed; },
        success: function (page: UI5Element) {
            Opa5.assert.strictEqual(destinationOf(page).shown, completed, `while typing, the field shows ${completed}`);
        },
        errorMessage: `Typing ${text} did not show ${completed}`
    });
}

function iLeaveTheField(When: Common, how: "blur" | "enter"): void {
    When.waitFor({
        id: PAGE,
        viewName: VIEW,
        success: function (page: UI5Element) {
            if (how === "enter") {
                keyInDestination(page, "Enter");
            } else {
                leaveDestination(page);
            }
        },
        errorMessage: "No page"
    });
}

function iSaveAndSeeStored(When: Common, Then: Common, expected: string, what: string): void {
    iPress(When, "odataSaveButton");
    Then.waitFor({
        check: function () { return backend.countRequests(PUT_UNUSED) > 0; },
        success: function () {
            Opa5.assert.strictEqual(backend.bodies[PUT_UNUSED]?.destination, expected, `sent: ${what}`);
            Opa5.assert.strictEqual(stored(UNUSED).destination, expected, `stored: ${what}`);
        },
        errorMessage: "Nothing was saved"
    });
}

opaTest("a typed name that begins a listed one is stored as typed when the field is left", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED, withNewPp);
    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.ok(field.choices.some((choice) => choice[0] === "S4_NEW_PP"), "S4_NEW_PP is listed");
        Opa5.assert.notOk(field.choices.some((choice) => choice[0] === "S4_NEW"), "S4_NEW is not");
    });

    iType(When, Then, "S4_NEW", "S4_NEW_PP");
    iLeaveTheField(When, "blur");
    iSeeTheField(Then, UNUSED, "the typed name after leaving", (field) => field.state === "Information", function (field) {
        Opa5.assert.strictEqual(field.value, "S4_NEW", "the field holds what was typed, not the completion");
        Opa5.assert.strictEqual(field.shown, "S4_NEW", "and shows it");
        Opa5.assert.strictEqual(field.stateText, NOT_LISTED, "judged as the typed name");
    });
    iSaveAndSeeStored(When, Then, "S4_NEW", "the typed name");
    Then.iStopTheApp();
});

opaTest("a typed name that begins a listed one is stored as typed after Enter", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED, withNewPp);
    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function () {
        Opa5.assert.ok(true, "the list is there");
    });

    iType(When, Then, "S4_NEW", "S4_NEW_PP");
    iLeaveTheField(When, "enter");
    iSeeTheField(Then, UNUSED, "the typed name after Enter", (field) => field.state === "Information", function (field) {
        Opa5.assert.strictEqual(field.value, "S4_NEW", "the field holds what was typed, not the completion");
        Opa5.assert.strictEqual(field.shown, "S4_NEW", "and shows it");
        Opa5.assert.strictEqual(field.stateText, NOT_LISTED);
    });
    iSaveAndSeeStored(When, Then, "S4_NEW", "the typed name");
    Then.iStopTheApp();
});

opaTest("a name typed in another case than the listed one is kept as typed and said to be listed otherwise", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED);
    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function () {
        Opa5.assert.ok(true, "the list is there");
    });

    iType(When, Then, "s4_dev_basic", "S4_DEV_BASIC");
    iLeaveTheField(When, "blur");
    iSeeTheField(Then, UNUSED, "the lower-case name", (field) => field.state === "Information", function (field) {
        Opa5.assert.strictEqual(field.value, "s4_dev_basic", "kept as typed");
        Opa5.assert.strictEqual(field.shown, "s4_dev_basic");
        Opa5.assert.strictEqual(
            field.stateText,
            "Listed as S4_DEV_BASIC. Destination names are case-sensitive: the name is kept as you typed it. "
            + "Pick S4_DEV_BASIC from the list if that is the one you mean.",
            "the admin decides; no mismatch is claimed for the other name"
        );
    });
    iSaveAndSeeStored(When, Then, "s4_dev_basic", "the name as typed");
    Then.iStopTheApp();
});

opaTest("a listed name that begins another listed name stays when typed", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED);
    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.deepEqual(field.choices.slice(0, 3).map((choice) => choice[0]), ["S4_DEV", "S4_DEV_BASIC", "S4_DEV_USER"],
            "S4_DEV and names that start with it");
    });

    iType(When, Then, "S4_DEV", "S4_DEV");
    iLeaveTheField(When, "blur");
    iSeeTheField(Then, UNUSED, "the exact name", (field) => field.state === "Warning", function (field) {
        Opa5.assert.strictEqual(field.value, "S4_DEV");
        Opa5.assert.strictEqual(field.stateText, FIXED_ACCOUNT, "judged as S4_DEV, a fixed account, not as S4_DEV_USER");
    });
    iSaveAndSeeStored(When, Then, "S4_DEV", "the exact name");
    Then.iStopTheApp();
});

opaTest("a typed beginning and then the arrow key takes the listed name", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED);
    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function () {
        Opa5.assert.ok(true, "the list is there");
    });

    iType(When, Then, "S4_ODATA_T", "S4_ODATA_TECH");
    When.waitFor({
        id: PAGE,
        viewName: VIEW,
        success: function (page: UI5Element) { keyInDestination(page, "ArrowDown"); },
        errorMessage: "No page"
    });
    iLeaveTheField(When, "blur");
    iSeeTheField(Then, UNUSED, "the name moved to with the arrow key", (field) => field.state === "Warning", function (field) {
        Opa5.assert.strictEqual(field.value, "S4_ODATA_TECH", "going through the list is picking from it");
        Opa5.assert.strictEqual(field.stateText, FIXED_ACCOUNT);
    });
    iSaveAndSeeStored(When, Then, "S4_ODATA_TECH", "the picked name");
    Then.iStopTheApp();
});

opaTest("a typed beginning and then a click on the completed item takes the listed name", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED);
    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function () {
        Opa5.assert.ok(true, "the list is there");
    });

    iType(When, Then, "S4_ODATA_T", "S4_ODATA_TECH");
    When.waitFor({
        controlType: "sap.m.StandardListItem",
        searchOpenDialogs: true,
        matchers: function (item: UI5Element) { return (item as StandardListItem).getTitle() === "S4_ODATA_TECH"; },
        actions: new Press(),
        errorMessage: "S4_ODATA_TECH is not in the open dropdown"
    });
    iSeeTheField(Then, UNUSED, "the clicked name", (field) => field.state === "Warning", function (field) {
        Opa5.assert.strictEqual(field.value, "S4_ODATA_TECH", "the item that was clicked, though it was already the completion");
        Opa5.assert.strictEqual(field.stateText, FIXED_ACCOUNT);
    });
    iSaveAndSeeStored(When, Then, "S4_ODATA_TECH", "the clicked name");
    Then.iStopTheApp();
});

// --- rules of the brief, each with an assertion of its own ---------------------------

opaTest("the hint is the field's description for a screen reader and the label names the field", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, JOBS, function () { backend.destinationsMode = "unavailable"; });

    iSeeTheField(Then, JOBS, "the hint", (field) => field.hint !== "", function (field) {
        Opa5.assert.ok(field.hintDescribes, "aria-describedby of the input names the hint");
        Opa5.assert.strictEqual(field.describedText, UNAVAILABLE, "and the text it points to is the hint, in the page");
        Opa5.assert.strictEqual(field.label, "Destination", "the label is tied to the input");
    });
    Then.iStopTheApp();
});

const MARKUP = "{i18n>odataSaved} <b>bold</b> {= 1+1 } {dest>/hint}";

opaTest("a description is shown as the text it is, never as a binding or as markup", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED, function () {
        backend.odataDestinations.push({
            ...backend.odataDestinations.filter((item) => item.name === "S4_ODATA_TECH")[0], name: "S4_TEXT",
            description: MARKUP
        });
    });
    const line = `${FIXED} · on-premise · subaccount · ${MARKUP}`;

    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.deepEqual(
            field.choices.filter((choice) => choice[0] === "S4_TEXT"), [["S4_TEXT", line]],
            "the item's additional text is the description, character for character"
        );
    });
    iPress(When, DESTINATION);
    When.waitFor({
        controlType: "sap.m.StandardListItem",
        searchOpenDialogs: true,
        matchers: function (item: UI5Element) { return (item as StandardListItem).getTitle() === "S4_TEXT"; },
        success: function (items: UI5Element[]) {
            const dom = (items[0] as StandardListItem).getDomRef() as HTMLElement;
            Opa5.assert.ok((dom.textContent ?? "").indexOf(line) !== -1, "the open dropdown shows the same characters");
            Opa5.assert.strictEqual(dom.querySelectorAll("b").length, 0, "and no element was made of them");
        },
        actions: new Press(),
        errorMessage: "S4_TEXT is not in the open dropdown"
    });
    iSeeTheField(Then, UNUSED, "the picked destination", (field) => field.value === "S4_TEXT", function (field) {
        Opa5.assert.deepEqual([field.state, field.stateText], ["Warning", FIXED_ACCOUNT],
            "the state text is the application's own sentence; nothing of the description is in it");
    });
    Then.iStopTheApp();
});

opaTest("only destinations that can be used are offered, and the name of one that cannot is not taken for it", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED);

    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.deepEqual(
            field.choices.map((choice) => choice[0]),
            ["S4_DEV", "S4_DEV_BASIC", "S4_DEV_USER", "S4_ODATA_TECH", "S4_ODATA_USER"],
            "exactly the usable ones: not S4_DEV_RFC, not \"S4 DEV invalid\""
        );
        Opa5.assert.strictEqual(backend.odataDestinations.length, 7, "of seven that the server lists");
        Opa5.assert.strictEqual(field.hint, UNUSABLE);
    });
    iEnter(When, DESTINATION, "S4 DEV invalid");
    iSeeTheField(Then, UNUSED, "the shown form of an invalid name", (field) => field.value === "S4 DEV invalid" && field.state !== "None", function (field) {
        Opa5.assert.deepEqual([field.state, field.stateText], ["Information", NOT_LISTED],
            "the shown form of an invalid name is no listed destination");
    });
    iEnter(When, DESTINATION, "S4_DEV_RFC");
    iSeeTheField(Then, UNUSED, "the name of a non-HTTP destination", (field) => field.state === "Warning", function (field) {
        Opa5.assert.strictEqual(
            field.stateText, "This destination is not an HTTP destination and cannot be used for OData calls."
        );
    });
    Then.iStopTheApp();
});

opaTest("a change of Runs as says what holds for the destination, also when that is the same as before", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, UNUSED);
    iSeeTheField(Then, UNUSED, "the list", (field) => field.choices.length > 0, function () {
        Opa5.assert.ok(true, "the list is there");
    });
    iEnter(When, DESTINATION, "S4_NEW_DESTINATION");
    iSeeTheField(Then, UNUSED, "the note", (field) => field.state === "Information", function (field) {
        Opa5.assert.strictEqual(field.stateText, NOT_LISTED);
        Opa5.assert.notStrictEqual(announced(), NOT_LISTED, "typed in the field: read with the field, not announced");
    });

    iRunAs(When, "technical");
    Then.waitFor({
        id: PAGE,
        viewName: VIEW,
        check: function () { return announced() === NOT_LISTED; },
        success: function (page: UI5Element) {
            Opa5.assert.strictEqual(announced(), NOT_LISTED, "the same note, said again for the new identity");
            Opa5.assert.strictEqual(destinationOf(page).state, "Information");
        },
        errorMessage: "Nothing was announced after Runs as changed"
    });
    Then.iStopTheApp();
});

opaTest("a page that showed one service shows nothing of its list while the next one's is read", function (Given: Common, When: Common, Then: Common) {
    iOpen(Given, JOBS, function () {
        // The next service does not fit its destination: with a list, a warning.
        stored(UNUSED).destination = "S4_ODATA_TECH";
    });
    iSeeTheField(Then, JOBS, "the first list", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.strictEqual(field.hint, UNUSABLE);
        backend.destinationsMode = "held";
        HashChanger.getInstance().setHash(`odata-services/${UNUSED}`);
    });
    Then.waitFor({
        id: "odataName",
        viewName: VIEW,
        check: function (input: UI5Element) { return (input as Input).getValue() === UNUSED; },
        success: function (input: UI5Element) {
            const field = destinationOf(input);
            Opa5.assert.strictEqual(field.value, "S4_ODATA_TECH", "the next service is on the page");
            Opa5.assert.deepEqual(field.choices, [], "the list of the visit before is gone");
            Opa5.assert.strictEqual(field.hint, "", "with its hint");
            Opa5.assert.deepEqual([field.state, field.stateText], ["None", ""], "and nothing is said from it about this service");
            backend.releaseDestinations();
        },
        errorMessage: "The second service did not open"
    });
    iSeeTheField(Then, UNUSED, "this visit's list", (field) => field.choices.length > 0, function (field) {
        Opa5.assert.deepEqual([field.state, field.stateText], ["Warning", FIXED_ACCOUNT], "judged by its own list");
    });
    Then.iStopTheApp();
});

import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import QUnitUtils from "sap/ui/qunit/QUnitUtils";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { closeMessageBox } from "./pages/Shared";
import { OPTS, iSearch, iSeeRows, iSelectRow, onePerReason, rows, theHashIs } from "./pages/Worklist";

QUnit.module("Worklist journey");

const REASONS: Record<string, string> = {
    "Why is the order list slow?": "Trace request pending",
    "Unit tests for tax determination": "Comments answered",
    "Round amounts by currency decimals": "Changes to review",
    "New field on the order header": "Document to approve"
};

opaTest("the worklist shows every session with what it waits for", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, onePerReason);

    iSeeRows(Then, 5, "the seeded session and one session per waiting reason are listed");
    Then.waitFor({
        id: "worklistTable",
        ...OPTS,
        success: function (control: UI5Element) {
            const all = rows(control as Table);
            Object.keys(REASONS).forEach((title) => {
                const row = all.find((r) => r.title === title);
                Opa5.assert.strictEqual(row?.status, "Waiting for you", `"${title}" is marked waiting`);
                Opa5.assert.strictEqual(row?.statusState, "Warning", `"${title}": the marker is a warning`);
                Opa5.assert.strictEqual(row?.reason, REASONS[title], `"${title}": the reason is "${REASONS[title]}"`);
            });
            const idle = all.find((r) => r.title === "Explain the order class");
            Opa5.assert.strictEqual(idle?.status, "Idle", "a session waiting for nothing is idle");
            Opa5.assert.strictEqual(idle?.reason, "", "and carries no reason");
            Opa5.assert.strictEqual(idle?.system, "dev-system", "the system column shows the target");
            Opa5.assert.strictEqual(all.find((r) => r.title === "Why is the order list slow?")?.type, "Diagnose", "type column");
            Opa5.assert.strictEqual(idle?.stage, "Chat", "stage column, worded");
        }
    });
    Then.waitFor({
        id: "worklistTable",
        ...OPTS,
        check: function (control: UI5Element) {
            const row = rows(control as Table).find((r) => r.title === "Round amounts by currency decimals");
            return row?.objects === "ZCL_PRICE_CALC" && row.changed === "1";
        },
        success: function (control: UI5Element) {
            Opa5.assert.ok(true, "the object line and the changed-object count come from the session list");
            const diagnose = rows(control as Table).find((r) => r.title === "Why is the order list slow?");
            Opa5.assert.strictEqual(diagnose?.changed, "2 findings", "a diagnose row counts its findings");
            Opa5.assert.deepEqual(backend.requests.filter((r) => /^GET sessions\/[^/]+$/.test(r)), [],
                "no session is read one by one: the list carries the objects");
        },
        errorMessage: "The object line or the changed count is missing"
    });
    Then.waitFor({
        id: "worklistSummary",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "4 waiting for you · 0 running" }),
        success: function () {
            Opa5.assert.ok(true, "the header counts the sessions waiting and running");
        }
    });
    Then.waitFor({
        id: "worklistTable",
        ...OPTS,
        success: function (control: UI5Element) {
            const table = control as Table;
            Opa5.assert.ok(table.getAriaLabelledBy().length > 0, "the table has an accessible name");
            Opa5.assert.ok(table.getGrowing(), "the table grows instead of rendering every session at once");
        }
    });

    Then.iStopTheApp();
});

opaTest("search, the type filter and 'Waiting for me' narrow the list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, onePerReason);
    iSeeRows(Then, 5, "all sessions are listed");

    iSearch(When, "ORDER");
    iSeeRows(Then, 3, "the search matches titles case-insensitively");
    iSearch(When, "price_calc");
    iSeeRows(Then, 1, "the search matches an object name of the session");
    iSearch(When, "no such session");
    iSeeRows(Then, 0, "nothing matches");
    Then.waitFor({
        id: "worklistNoData",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "illustrationType", value: "sapIllus-NoSearchResults" }),
        success: function () {
            Opa5.assert.ok(true, "an empty result says that nothing matches the search");
        }
    });
    iSearch(When, "");
    iSeeRows(Then, 5, "clearing the search shows everything again");

    When.waitFor({
        id: "worklistWaitingToggle",
        ...OPTS,
        actions: new Press(),
        errorMessage: "No 'Waiting for me' toggle"
    });
    iSeeRows(Then, 4, "'Waiting for me' leaves the sessions that wait for the developer");

    When.waitFor({
        id: "worklistTypeDiagnose",
        ...OPTS,
        actions: new Press(),
        errorMessage: "No diagnose type filter"
    });
    iSeeRows(Then, 1, "type and waiting filters combine");

    When.waitFor({ id: "worklistWaitingToggle", ...OPTS, actions: new Press() });
    When.waitFor({ id: "worklistTypeAll", ...OPTS, actions: new Press() });
    When.waitFor({
        id: "worklistStageFilter",
        ...OPTS,
        actions: new Press()
    });
    When.waitFor({
        controlType: "sap.ui.core.Item",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "key", value: "propose" }),
        actions: new Press(),
        errorMessage: "No 'Propose' stage in the stage filter"
    });
    iSeeRows(Then, 1, "the stage filter leaves the session in propose");

    Then.iStopTheApp();
});

opaTest("an empty worklist says so", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.sessions = []; });
    iSeeRows(Then, 0, "no sessions");
    Then.waitFor({
        id: "worklistNoData",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "illustrationType", value: "sapIllus-NoEntries" }),
        success: function () {
            Opa5.assert.ok(true, "the empty state invites to start a session");
        }
    });
    Then.iStopTheApp();
});

opaTest("a new change session opens on its own route", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions");
    iSeeRows(Then, 1, "the seeded session is listed");

    When.waitFor({ id: "worklistNewButton", ...OPTS, actions: new Press(), errorMessage: "No 'New session' button" });
    When.waitFor({
        id: "newSessionTitle",
        ...OPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text: "Refactor the demo class" }),
        errorMessage: "The new-session dialog did not open"
    });
    Then.waitFor({
        id: "newSessionTarget",
        ...OPTS,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "selectedKey", value: "dev-system" }),
        success: function () {
            Opa5.assert.ok(true, "the only target is preselected");
        }
    });
    When.waitFor({ id: "newSessionCreate", ...OPTS, searchOpenDialogs: true, actions: new Press() });

    theHashIs(Then, /^sessions\/s-\d+$/, "the new session's route is open");
    Then.waitFor({
        success: function () {
            Opa5.assert.ok(backend.requests.includes("POST sessions"), "the session was created");
            const created = backend.sessions.find((d) => d.session.title === "Refactor the demo class");
            Opa5.assert.strictEqual(created?.session.type, "change", "as a change session");
        }
    });

    Then.iStopTheApp();
});

opaTest("rename and delete the selected session", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, onePerReason);
    iSeeRows(Then, 5, "all sessions are listed");

    Then.waitFor({
        id: "worklistRenameButton",
        ...OPTS,
        enabled: false,
        matchers: new PropertyStrictEquals({ name: "enabled", value: false }),
        success: function () {
            Opa5.assert.ok(true, "rename waits for a selected session");
        }
    });

    iSelectRow(When, "Explain the order class");
    When.waitFor({ id: "worklistRenameButton", ...OPTS, actions: new Press(), errorMessage: "Rename is not enabled" });
    When.waitFor({
        id: "renameInput",
        ...OPTS,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "value", value: "Explain the order class" }),
        actions: new EnterText({ text: "Explain the sales order class", clearTextFirst: true, keepFocus: true }),
        errorMessage: "The rename dialog did not open with the current title"
    });
    When.waitFor({ id: "renameSave", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: "worklistTable",
        ...OPTS,
        check: function (control: UI5Element) {
            return rows(control as Table).some((r) => r.title === "Explain the sales order class");
        },
        success: function () {
            const patch = backend.bodies.find((b) => b.key === "PATCH sessions/s-1");
            Opa5.assert.deepEqual(patch?.body, { title: "Explain the sales order class" }, "PATCH sends the new title only");
            Opa5.assert.ok(true, "the row shows the new title");
        },
        errorMessage: "The renamed title is not shown"
    });

    iSelectRow(When, "New field on the order header");
    When.waitFor({ id: "worklistDeleteButton", ...OPTS, actions: new Press(), errorMessage: "Delete is not enabled" });
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "Delete" }),
        check: function (buttons: UI5Element[]) {
            let parent = buttons[0]?.getParent() as UI5Element | null;
            while (parent && parent.getMetadata().getName() !== "sap.m.Dialog") {
                parent = parent.getParent() as UI5Element | null;
            }
            return (parent as unknown as { getTitle(): string } | null)?.getTitle() === "Delete Session";
        },
        actions: new Press(),
        errorMessage: "No 'Delete' action in a 'Delete Session' confirmation"
    });
    iSeeRows(Then, 4, "the deleted session is gone");
    Then.waitFor({
        id: "worklistTable",
        ...OPTS,
        success: function (control: UI5Element) {
            Opa5.assert.notOk(rows(control as Table).some((r) => r.title === "New field on the order header"), "not listed");
            Opa5.assert.ok(backend.requests.some((r) => /^DELETE sessions\//.test(r)), "the delete was sent");
        }
    });

    Then.iStopTheApp();
});

opaTest("keyboard: the rows are in the tab order and Enter opens one", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions");
    iSeeRows(Then, 1, "the seeded session is listed");

    When.waitFor({
        id: "worklistTable",
        ...OPTS,
        success: function (control: UI5Element) {
            const item = (control as Table).getItems()[0] as ColumnListItem;
            const dom = item.getDomRef() as HTMLElement;
            Opa5.assert.ok(dom.tabIndex >= 0 || dom.getAttribute("tabindex") === "-1", "the row is focusable");
            Opa5.assert.ok((control as Table).getDomRef()!.querySelector("[tabindex='0']"), "the table has a tab stop");
            Opa5.assert.strictEqual(item.getType(), "Navigation", "the row is announced as navigable");
            dom.focus();
            QUnitUtils.triggerKeydown(dom, "SPACE");
            QUnitUtils.triggerKeyup(dom, "SPACE");
        }
    });
    Then.waitFor({
        id: "worklistRenameButton",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "enabled", value: true }),
        success: function () {
            Opa5.assert.strictEqual(HashChanger.getInstance().getHash(), "sessions", "Space does not open the session");
            Opa5.assert.ok(true, "Space selects the focused row (Rename and Delete enable)");
        },
        errorMessage: "Space did not select the row"
    });
    When.waitFor({
        id: "worklistTable",
        ...OPTS,
        success: function (control: UI5Element) {
            const dom = ((control as Table).getItems()[0] as ColumnListItem).getDomRef() as HTMLElement;
            dom.focus();
            QUnitUtils.triggerKeydown(dom, "ENTER");
        }
    });
    theHashIs(Then, /^sessions\/s-1$/, "Enter opens the session");

    Then.iStopTheApp();
});

opaTest("a failed load shows the error and leaves the list empty", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", { path: "sessions", status: 500, body: { detail: "database down" } });
    Then.waitFor({
        check: function () {
            return Array.from(document.querySelectorAll(".sapMMessageBoxError"))
                .some((box) => (box.textContent ?? "").includes("The request failed: database down"));
        },
        success: function () {
            Opa5.assert.ok(true, "the error is shown through the shared error text");
        },
        errorMessage: "No error message box"
    });
    closeMessageBox(When);
    iSeeRows(Then, 0, "nothing is listed");
    Then.waitFor({
        id: "worklistNoData",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "illustrationType", value: "sapIllus-UnableToLoad" }),
        success: function (control: UI5Element) {
            Opa5.assert.ok(control.getDomRef(), "the failed-load state is rendered");
            const title = (control as unknown as { getTitle(): string }).getTitle();
            Opa5.assert.strictEqual(title, "Your sessions could not be loaded", "a failed load is not shown as 'no sessions'");
        },
        errorMessage: "The failed load looks like an empty worklist"
    });
    When.waitFor({ id: "worklistRetry", ...OPTS, actions: new Press(), errorMessage: "No 'Try again' action" });
    iSeeRows(Then, 1, "'Try again' loads the list");
    Then.waitFor({
        id: "worklistNoData",
        ...OPTS,
        // With a row listed the noData state is not rendered: check its property only.
        visible: false,
        matchers: new PropertyStrictEquals({ name: "illustrationType", value: "sapIllus-NoEntries" }),
        success: function () {
            Opa5.assert.ok(true, "the error state is gone after a successful load");
        }
    });
    Then.iStopTheApp();
});

opaTest("cancelling the delete confirmation sends nothing", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, onePerReason);
    iSeeRows(Then, 5, "all sessions are listed");
    iSelectRow(When, "New field on the order header");
    When.waitFor({ id: "worklistDeleteButton", ...OPTS, actions: new Press() });
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "Cancel" }),
        actions: new Press(),
        errorMessage: "No Cancel in the delete confirmation"
    });
    iSeeRows(Then, 5, "nothing was deleted");
    Then.waitFor({
        success: function () {
            Opa5.assert.notOk(backend.requests.some((r) => r.startsWith("DELETE ")), "no DELETE was sent");
        }
    });
    Then.iStopTheApp();
});

opaTest("a delete or rename of a session that is gone (404) reloads the list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, onePerReason);
    iSeeRows(Then, 5, "all sessions are listed");
    iSelectRow(When, "New field on the order header");
    Then.waitFor({
        success: function () {
            // Deleted elsewhere (another tab): the server no longer knows it.
            backend.sessions = backend.sessions.filter((d) => d.session.title !== "New field on the order header");
        }
    });
    When.waitFor({ id: "worklistDeleteButton", ...OPTS, actions: new Press() });
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "Delete" }),
        actions: new Press()
    });
    closeMessageBox(When);
    iSeeRows(Then, 4, "the list was read again and no longer shows the vanished session");

    iSelectRow(When, "Unit tests for tax determination");
    Then.waitFor({
        success: function () {
            backend.sessions = backend.sessions.filter((d) => d.session.title !== "Unit tests for tax determination");
        }
    });
    When.waitFor({ id: "worklistRenameButton", ...OPTS, actions: new Press() });
    When.waitFor({
        id: "renameInput",
        ...OPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text: "Renamed", clearTextFirst: true, keepFocus: true })
    });
    When.waitFor({ id: "renameSave", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    closeMessageBox(When);
    iSeeRows(Then, 3, "a rename answered 404 reloads the list too");
    Then.waitFor({
        success: function () {
            Opa5.assert.strictEqual(backend.requests.filter((r) => r === "GET sessions").length, 3, "two reloads after the first load");
        }
    });
    Then.iStopTheApp();
});

import {
    badgeText, itemKey, newestFinishedAt, newlyFinished, statusIcon, statusState, toastKey
} from "com/agent/admin/model/notifications";
import type { NotificationItem } from "com/agent/admin/service/types";

function item(kind: "agent" | "workflow", runId: string, status = "success"): NotificationItem {
    return {
        kind, run_id: runId, name: `n-${runId}`, status, trigger: "manual", created_by: null,
        finished_at: `2026-10-09T12:00:0${runId}+00:00`, unread: true
    };
}

QUnit.module("notifications: keys and new items");

QUnit.test("itemKey joins kind and run id, so an agent and a workflow run never collide", function (assert) {
    assert.strictEqual(itemKey(item("agent", "1")), "agent:1");
    assert.notStrictEqual(itemKey(item("agent", "1")), itemKey(item("workflow", "1")));
});

QUnit.test("the first poll (no previous keys) never reports anything as new", function (assert) {
    assert.deepEqual(newlyFinished(null, [item("agent", "1")]), []);
});

QUnit.test("newlyFinished returns the items whose key was not seen before", function (assert) {
    const a = item("agent", "1");
    const w = item("workflow", "2");
    assert.deepEqual(newlyFinished(new Set([itemKey(a)]), [w, a]), [w]);
    assert.deepEqual(newlyFinished(new Set([itemKey(a), itemKey(w)]), [w, a]), []);
    assert.deepEqual(newlyFinished(new Set(), []), []);
});

QUnit.test("newestFinishedAt is the first item's finished_at, null when empty", function (assert) {
    assert.strictEqual(newestFinishedAt([item("agent", "3"), item("agent", "1")]), "2026-10-09T12:00:03+00:00");
    assert.strictEqual(newestFinishedAt([]), null);
});

QUnit.module("notifications: badge and toast");

QUnit.test("badgeText hides 0, shows up to 99 and caps above", function (assert) {
    assert.strictEqual(badgeText(0), "");
    assert.strictEqual(badgeText(1), "1");
    assert.strictEqual(badgeText(99), "99");
    assert.strictEqual(badgeText(100), "99+");
    assert.strictEqual(badgeText(-1), "", "a negative count is nothing");
});

QUnit.test("toastKey names the run for one, counts for several, null for none", function (assert) {
    assert.deepEqual(toastKey([item("agent", "1", "failed")]),
        { key: "notificationOneFinished", args: ["n-1", "failed"] });
    assert.deepEqual(toastKey([item("agent", "1"), item("workflow", "2")]),
        { key: "notificationManyFinished", args: [2] });
    assert.strictEqual(toastKey([]), null);
});

QUnit.module("notifications: status look");

QUnit.test("statusState maps the terminal statuses", function (assert) {
    assert.strictEqual(statusState("success"), "Success");
    assert.strictEqual(statusState("partial"), "Warning");
    assert.strictEqual(statusState("interrupted"), "Warning");
    assert.strictEqual(statusState("failed"), "Error");
    assert.strictEqual(statusState("something-new"), "None");
});

QUnit.test("statusIcon gives each look its own icon", function (assert) {
    const icons = ["success", "partial", "failed", "other"].map(statusIcon);
    assert.strictEqual(new Set(icons).size, 4, "four distinct icons");
    assert.strictEqual(statusIcon("interrupted"), statusIcon("partial"));
    icons.forEach((i) => assert.ok(i.indexOf("sap-icon://") === 0, i));
});

import BaseController from "com/agent/ide/controller/BaseController";
import ResourceModel from "sap/ui/model/resource/ResourceModel";

/**
 * BaseController#text (fix round U11 #8): a text without arguments is taken
 * as it is (MessageFormat would drop a single apostrophe); a text with
 * arguments goes through MessageFormat, so an apostrophe in it is written
 * doubled ('') in the properties file.
 */
const i18n = new ResourceModel({ bundleName: "com.agent.ide.i18n.i18n" });

/** @namespace com.agent.ide.test.unit */
class Probe extends BaseController {
    public say(key: string, args?: (string | number)[]): string {
        return this.text(key, args);
    }
}

let probe: Probe;

/** The app's texts as written in the properties file (key -> raw value). */
async function rawTexts(): Promise<Map<string, string>> {
    const url = sap.ui.require.toUrl("com/agent/ide/i18n/i18n.properties");
    const text = await (await fetch(url)).text();
    const map = new Map<string, string>();
    for (const line of text.split(/\r?\n/)) {
        const m = /^([A-Za-z0-9_.]+)=(.*)$/.exec(line);
        if (m) {
            map.set(m[1], m[2]);
        }
    }
    return map;
}

QUnit.module("BaseController.text", {
    before: function () {
        probe = new Probe([] as never);
        // The owner component only lends its i18n model.
        probe.getOwnerComponent = (() => ({ getModel: (name?: string) => (name === "i18n" ? i18n : undefined) })) as never;
    },
    after: function () {
        probe.destroy();
    }
});

QUnit.test("a text with an apostrophe and no arguments keeps it, with or without an empty list", function (assert) {
    const expected = "The target system is not flagged as non-production, so this is not available there. "
        + "Ask an administrator to check the target's conventions.";
    assert.strictEqual(probe.say("targetNotNonProd"), expected, "no arguments");
    assert.strictEqual(probe.say("targetNotNonProd", []), expected, "an empty list is no arguments");
});

QUnit.test("arguments are filled in; an apostrophe written doubled next to a placeholder shows once", function (assert) {
    assert.strictEqual(probe.say("sourceFindingNote", [12, "A dump"]), "Line 12: A dump");
    assert.strictEqual(probe.say("sourceLineBeyond", [40, 5]),
        "Line 40 is beyond the source's last line (5): no line is highlighted.");
});

QUnit.test("every text with a placeholder keeps its apostrophes once formatted; none without one is doubled", async function (assert) {
    const texts = await rawTexts();
    let withArgs = 0;
    for (const [key, raw] of texts) {
        const holes = raw.match(/\{(\d+)\}/g);
        if (!holes) {
            assert.notOk(raw.includes("''"), `${key}: no placeholder, so a doubled apostrophe would show twice`);
            continue;
        }
        if (!raw.includes("'")) {
            continue;
        }
        const args = Array.from({ length: Math.max(...holes.map((h) => Number(h.slice(1, -1)))) + 1 }, (_, i) => `A${i}`);
        const shown = probe.say(key, args);
        assert.strictEqual((shown.match(/'/g) ?? []).length, (raw.replace(/''/g, "'").match(/'/g) ?? []).length,
            `${key}: "${shown}"`);
        assert.notOk(/\{\d+\}/.test(shown), `${key}: every placeholder filled`);
        withArgs++;
    }
    assert.ok(withArgs >= 1, `${withArgs} text(s) with an apostrophe and a placeholder checked`);
});

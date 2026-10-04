import Parameters from "sap/ui/core/theming/Parameters";
import { THEME_VAR_NAMES } from "com/agent/ide/model/themeVars";
import { decorateDiff } from "com/agent/ide/model/diffDecor";
import { renderUnifiedHtml, unifiedRows } from "com/agent/ide/model/diffModel";
import { ensureDiff } from "com/agent/ide/model/vendor";

/**
 * Task U17: accessibility rules that hold for the app's own markup and CSS as
 * written, independent of a journey: states are text (no UI5 value-state or
 * "Indication Color n" text added to a status that already says it), the
 * app CSS takes its colours from theme parameters only (so high-contrast
 * themes apply), and every parameter it reads is one the app publishes.
 */
QUnit.module("a11y (static)", {
    before: function () {
        return ensureDiff();
    }
});

const XML_FILES = [
    "view/App.view.xml", "view/Worklist.view.xml", "view/Session.view.xml",
    "fragment/ChangesView.fragment.xml", "fragment/CommentPopover.fragment.xml", "fragment/ConventionsDialog.fragment.xml",
    "fragment/Conversation.fragment.xml", "fragment/DocumentView.fragment.xml", "fragment/FindingDetailDialog.fragment.xml",
    "fragment/Findings.fragment.xml", "fragment/NewSessionDialog.fragment.xml", "fragment/RenameSessionDialog.fragment.xml",
    "fragment/RequestChangesDialog.fragment.xml", "fragment/SessionHeader.fragment.xml", "fragment/SourceView.fragment.xml"
];

async function load(path: string): Promise<string> {
    const response = await fetch(sap.ui.require.toUrl(`com/agent/ide/${path}`));
    if (!response.ok) {
        throw new Error(`${path}: ${response.status}`);
    }
    return response.text();
}

QUnit.test("every ObjectStatus with a state sets stateAnnouncementText: the state is in its text, never 'Indication Color n' or 'Invalid entry'", async function (assert) {
    const offenders: string[] = [];
    let seen = 0;
    for (const file of XML_FILES) {
        const xml = new DOMParser().parseFromString(await load(file), "application/xml");
        Array.from(xml.getElementsByTagName("*"))
            .filter((el) => el.localName === "ObjectStatus" && el.hasAttribute("state"))
            .forEach((el) => {
                seen++;
                if (!el.hasAttribute("stateAnnouncementText")) {
                    offenders.push(`${file}#${el.getAttribute("id") ?? "?"}`);
                }
            });
    }
    assert.ok(seen >= 15, `${seen} coloured ObjectStatus controls were checked`);
    assert.deepEqual(offenders, [], "each one says its state itself (stateAnnouncementText)");
});

QUnit.test("style.css: no hard-coded colour; every colour comes from a theme parameter", async function (assert) {
    const css = (await load("css/style.css")).replace(/\/\*[\s\S]*?\*\//g, "");
    const literal = css.match(/#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(|:\s*(?:white|black|red|green|blue|yellow|orange|gray|grey)\b/g) ?? [];
    assert.deepEqual(literal, [], "no #hex, rgb()/hsl() or named colour");
});

QUnit.test("every theme parameter style.css reads is published by themeVars (else a high-contrast theme falls back)", async function (assert) {
    const css = await load("css/style.css");
    const used = Array.from(new Set(Array.from(css.matchAll(/var\(--([A-Za-z0-9_]+)/g), (m) => m[1]))).sort();
    const missing = used.filter((name) => !THEME_VAR_NAMES.includes(name));
    assert.ok(used.length > 20, `${used.length} parameters are read`);
    assert.deepEqual(missing, [], "each one is copied onto :root");
});

QUnit.test("every published name is a parameter of the theme (an unknown one logs a FUTURE FATAL error)", function (assert) {
    const done = assert.async();
    let checked = false;
    const check = (): void => {
        if (checked) {
            return;
        }
        checked = true;
        const unknown = THEME_VAR_NAMES.filter((name) => Parameters.get({ name }) === undefined);
        assert.deepEqual(unknown, [], "all of them exist in the active theme");
        done();
    };
    // Answered at once when the theme is loaded, else through the callback.
    if (Parameters.get({ name: "sapTextColor", callback: check }) !== undefined) {
        check();
    }
});

QUnit.test("decorateDiff: only lines that can be selected and commented get the 'Space selects, Enter or C comments' hint", function (assert) {
    const old = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`).join("\n");
    const div = document.createElement("div");
    div.innerHTML = renderUnifiedHtml(unifiedRows(old, old.replace("line 5", "LINE 5"))!, { path: "p", revision: 1, idPrefix: "a11y" });
    document.body.appendChild(div);
    const root = div.firstElementChild as HTMLElement;
    const label = (r: { kind: string; line: number }): string => `${r.kind} ${r.line}`;
    decorateDiff(root, { selected: null, markers: [], rowLabel: label, rowHint: "hint" });
    const removed = root.querySelector("tr[data-side='old'][data-line='5']")!;
    const added = root.querySelector("tr[data-side='new'][data-line='5']")!;
    assert.ok((added.getAttribute("aria-describedby") ?? "").split(" ").includes("hint"), "an added line has the hint");
    assert.notOk((removed.getAttribute("aria-describedby") ?? "").split(" ").includes("hint"),
        "a removed line has not: Space and Enter do not select or comment it");
    removed.setAttribute("aria-describedby", "hint");
    decorateDiff(root, { selected: null, markers: [], rowLabel: label, rowHint: "hint" });
    assert.notOk(removed.hasAttribute("aria-describedby"), "a stale hint on a removed line is taken away");
    div.remove();
});

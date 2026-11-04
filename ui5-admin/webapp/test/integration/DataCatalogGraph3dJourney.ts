import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type SegmentedButton from "sap/m/SegmentedButton";
import type Graph from "sap/suite/ui/commons/networkgraph/Graph";
import type Line from "sap/suite/ui/commons/networkgraph/Line";
import type HTML from "sap/ui/core/HTML";
import type UI5Element from "sap/ui/core/Element";
import type JSONModel from "sap/ui/model/json/JSONModel";
import { hitTest, type Projected } from "com/agent/admin/model/graph3d";
import type Common from "./pages/Common";
import { pressSegment, viewOf } from "./pages/ODataDetail";

// The 3D overview of the data catalogue graph (the 2D network graph's own
// cases are in DataCatalogJourney). The math of the view is unit-tested in
// test/unit/graph3d.qunit.ts; this journey checks the page's wiring.
Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Data catalogue graph in 3D journey");

const DETAIL_VIEW = "DataCatalogDetail";
const GRAPH_VIEW = "DataCatalogGraph";

/** The canvas of the 3D view, or null while it is not rendered. */
function canvasOf(html: UI5Element): HTMLCanvasElement | null {
    const element = (html as HTML).getDomRef();
    return element instanceof HTMLCanvasElement ? element : null;
}

/** How many pixels of the canvas are painted (not fully transparent). */
function paintedPixels(canvas: HTMLCanvasElement | null): number {
    const context = canvas?.getContext("2d");
    if (!canvas || !context || !canvas.width || !canvas.height) {
        return 0;
    }
    const data = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let painted = 0;
    for (let alpha = 3; alpha < data.length; alpha += 4) {
        if (data[alpha] > 0) {
            painted++;
        }
    }
    return painted;
}

/** Where the graph page draws each node now (its own reading, which its pointer handlers use). */
function nodesOnScreen(html: UI5Element): Projected[] {
    return (viewOf(html).getController() as unknown as { nodesOnScreen(): Projected[] }).nodesOnScreen();
}

/** A pointer event on the canvas at (x, y) CSS px from its top left, as a mouse sends it. */
function pointer(canvas: HTMLCanvasElement, type: string, x: number, y: number): void {
    const box = canvas.getBoundingClientRect();
    const init = { bubbles: true, cancelable: true, clientX: box.left + x, clientY: box.top + y, button: 0, pointerId: 1, pointerType: "mouse" };
    canvas.dispatchEvent(type === "click" ? new MouseEvent(type, init) : new PointerEvent(type, init));
}

opaTest("the 3D toggle shows the canvas in place of the network graph, and back", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/graph");

    Then.waitFor({
        id: "dcGraph",
        viewName: GRAPH_VIEW,
        check: (graph: UI5Element) => (graph as Graph).getNodes().length === 15,
        success: function (graph: UI5Element) {
            const view = viewOf(graph);
            Opa5.assert.strictEqual((view.byId("dcGraphMode") as SegmentedButton).getSelectedKey(), "2d", "2D is the default");
            Opa5.assert.strictEqual((view.byId("dcGraph3d") as HTML).getVisible(), false, "no canvas in 2D");
            const lines = (graph as Graph).getLines();
            const drawn = lines.filter((line: Line) => line.getFrom() === "310" && line.getTo() === "210");
            Opa5.assert.strictEqual(drawn.length, 1, "the link an admin drew is drawn");
            Opa5.assert.strictEqual(drawn[0].getLineType(), "Dashed", "dashed");
            Opa5.assert.ok(lines.length > 1 && lines.filter((line: Line) => line !== drawn[0])
                .every((line: Line) => line.getLineType() === "Solid"), "every imported line solid");
        },
        errorMessage: "The graph does not open in 2D"
    });
    When.waitFor({
        id: "dcGraphMode",
        viewName: GRAPH_VIEW,
        success: (segmented: UI5Element) => pressSegment(segmented, "3d")
    });
    Then.waitFor({
        id: "dcGraph3d",
        viewName: GRAPH_VIEW,
        // The auto-rotation is a frame loop, which OPA's auto wait takes for
        // an animation that never ends: no auto wait while the view turns.
        autoWait: false,
        check: (html: UI5Element) => paintedPixels(canvasOf(html)) > 0,
        success: function (html: UI5Element) {
            const canvas = canvasOf(html) as HTMLCanvasElement;
            const ratio = window.devicePixelRatio || 1;
            Opa5.assert.strictEqual(canvas.getAttribute("role"), "img");
            Opa5.assert.strictEqual(canvas.getAttribute("aria-label"),
                "Three-dimensional overview of the data catalogue; the 2D view lists the same nodes", "it says what it shows");
            Opa5.assert.ok(canvas.clientWidth > 0 && canvas.clientHeight > 0, "it fills the page");
            Opa5.assert.strictEqual(canvas.width, Math.round(canvas.clientWidth * ratio), "as many pixels as the screen has across");
            Opa5.assert.strictEqual(canvas.height, Math.round(canvas.clientHeight * ratio), "and down");
            Opa5.assert.strictEqual((viewOf(html).byId("dcGraph") as Graph).getVisible(), false, "the network graph is hidden");
        },
        errorMessage: "3D did not draw the catalogue on the canvas"
    });
    When.waitFor({
        id: "dcGraphMode",
        viewName: GRAPH_VIEW,
        autoWait: false,
        success: (segmented: UI5Element) => pressSegment(segmented, "2d")
    });
    Then.waitFor({
        id: "dcGraph",
        viewName: GRAPH_VIEW,
        success: function (graph: UI5Element) {
            Opa5.assert.strictEqual((graph as Graph).getVisible(), true, "the network graph is back");
            Opa5.assert.strictEqual((viewOf(graph).byId("dcGraph3d") as HTML).getVisible(), false, "the canvas is gone");
            Opa5.assert.strictEqual((graph as Graph).getNodes().length, 15, "with the same nodes");
        },
        errorMessage: "2D did not bring the network graph back"
    });

    Then.iStopTheApp();
});

opaTest("on the canvas a drag stops the turning, a hover points and a click opens the source", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/graph");

    When.waitFor({
        id: "dcGraphMode",
        viewName: GRAPH_VIEW,
        success: (segmented: UI5Element) => pressSegment(segmented, "3d")
    });
    let spot: Projected | undefined;
    let expected = "";
    When.waitFor({
        id: "dcGraph3d",
        viewName: GRAPH_VIEW,
        // Turning until the drag below: no auto wait (see the test before).
        autoWait: false,
        check: (html: UI5Element) => paintedPixels(canvasOf(html)) > 0,
        success: function (html: UI5Element) {
            const canvas = canvasOf(html) as HTMLCanvasElement;
            pointer(canvas, "pointerdown", 20, 20);
            pointer(canvas, "pointermove", 60, 30);
            pointer(canvas, "pointerup", 60, 30);
            // A source that is the front-most node at its own centre: Business
            // partners when nothing is drawn over it.
            const shown = nodesOnScreen(html);
            const free = shown.filter((node) => node.kind === "source" && hitTest(shown, node.x, node.y)?.key === node.key);
            spot = free.filter((node) => node.key === "100")[0] ?? free[0];
            const nodes = (viewOf(html).getModel("graph") as JSONModel).getProperty("/nodes") as
                { key: string; source_kind: string; source_name: string }[];
            const source = nodes.filter((node) => node.key === spot?.key)[0];
            expected = source ? `data-catalog/${source.source_kind}/${source.source_name}` : "";
        },
        errorMessage: "3D did not draw the catalogue on the canvas"
    });
    Then.waitFor({
        id: "dcGraph3d",
        viewName: GRAPH_VIEW,
        // Three frames later the view has not moved: the drag stopped the auto-rotation.
        check: (html: UI5Element) => {
            const now = nodesOnScreen(html).filter((node) => node.key === spot?.key)[0];
            return !!spot && !!now && now.x === spot.x && now.y === spot.y;
        },
        success: function (html: UI5Element) {
            const canvas = canvasOf(html) as HTMLCanvasElement;
            const at = spot as Projected;
            pointer(canvas, "pointermove", at.x, at.y);
            Opa5.assert.strictEqual(canvas.style.cursor, "pointer", "a node under the pointer says it can be pressed");
            pointer(canvas, "pointermove", 1, 1);
            Opa5.assert.strictEqual(canvas.style.cursor, "", "the empty canvas does not");
            pointer(canvas, "pointerdown", at.x, at.y);
            pointer(canvas, "pointerup", at.x, at.y);
            pointer(canvas, "click", at.x, at.y);
        },
        errorMessage: "The drag did not stop the 3D view"
    });
    Then.waitFor({
        id: "dcEntitiesTable",
        viewName: DETAIL_VIEW,
        success: function () {
            Opa5.assert.ok(expected !== "", "a source to click on");
            Opa5.assert.strictEqual(HashChanger.getInstance().getHash(), expected, "the source the click was on");
        },
        errorMessage: "A click on a node did not open its source"
    });

    Then.iStopTheApp();
});

/** The animation frame the graph page has asked for, or null when nothing turns. */
function pendingFrame(html: UI5Element): number | null {
    return (viewOf(html).getController() as unknown as { frame: number | null }).frame;
}

opaTest("an address that matches no page stops the turning behind the not-found page", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/graph");

    When.waitFor({
        id: "dcGraphMode",
        viewName: GRAPH_VIEW,
        success: (segmented: UI5Element) => pressSegment(segmented, "3d")
    });
    let canvas: UI5Element | undefined;
    When.waitFor({
        id: "dcGraph3d",
        viewName: GRAPH_VIEW,
        // Turning: no auto wait (see the first test).
        autoWait: false,
        check: (html: UI5Element) => paintedPixels(canvasOf(html)) > 0,
        success: function (html: UI5Element) {
            canvas = html;
            Opa5.assert.notStrictEqual(pendingFrame(html), null, "the view turns");
            HashChanger.getInstance().setHash("no-such-page");
        },
        errorMessage: "3D did not draw the catalogue on the canvas"
    });
    let seen: Projected[] = [];
    let seenAt = 0;
    Then.waitFor({
        controlType: "sap.m.MessagePage",
        viewName: "NotFound",
        autoWait: false,
        success: function () {
            const html = canvas as UI5Element;
            Opa5.assert.strictEqual(pendingFrame(html), null, "no frame is asked for behind the not-found page");
            seen = nodesOnScreen(html);
            seenAt = Date.now();
        },
        errorMessage: "The not-found page did not open"
    });
    Then.waitFor({
        autoWait: false,
        // Half a second later (30 frames, had the loop gone on), nothing has moved.
        check: () => Date.now() - seenAt > 500
            && JSON.stringify(nodesOnScreen(canvas as UI5Element)) === JSON.stringify(seen),
        success: function () {
            Opa5.assert.ok(seen.length > 0, "the view stands still");
        },
        errorMessage: "The 3D view went on turning behind the not-found page"
    });

    Then.iStopTheApp();
});

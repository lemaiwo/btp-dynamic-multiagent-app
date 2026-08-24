import JSONModel from "sap/ui/model/json/JSONModel";
import BaseController from "./BaseController";
import ReportRenderer from "../service/ReportRenderer";
import formatter from "../model/formatter";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type HTML from "sap/ui/core/HTML";

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class RunDetail extends BaseController {

    public formatter = formatter;

    private runId = "";

    public onInit(): void {
        this.setModel(new JSONModel({ data: {}, reportHtml: "", hasReport: false }), "run");
        this.getRouter().getRoute("runDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            this.runId = (event.getParameter("arguments") as { runId: string }).runId;
            void this.load();
        });
    }

    private async load(): Promise<void> {
        const model = this.getModel("run") as JSONModel;
        model.setData({ data: {}, reportHtml: "", hasReport: false });

        const run = await this.run(
            this.getAdminService().getRun(this.runId),
            "Could not load the run."
        );
        if (!run) {
            return;
        }
        model.setProperty("/data", run);

        const bodyMd = run.report?.body_md ?? "";
        if (!bodyMd) {
            return;
        }
        await ReportRenderer.ensureLibraries();
        model.setProperty("/reportHtml", `<div class="agentAdminReport">${ReportRenderer.renderMarkdown(bodyMd)}</div>`);
        model.setProperty("/hasReport", true);

        // Diagrams are replaced after the HTML control has rendered, so the
        // fences exist in the DOM to be swapped.
        const html = this.byId("reportHtml") as HTML;
        html.attachEventOnce("afterRendering", () => {
            const dom = html.getDomRef();
            if (dom) {
                void ReportRenderer.renderMermaid(dom as HTMLElement, this.runId);
            }
        });
    }

    public onDownload(): void {
        window.open(this.getAdminService().runReportUrl(this.runId), "_blank", "noopener");
    }

    public onBack(): void {
        this.getRouter().navTo("runs");
    }
}

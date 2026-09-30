import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import BaseController from "./BaseController";
import ReportRenderer from "../service/ReportRenderer";
import ErrorHandler from "../service/ErrorHandler";
import formatter from "../model/formatter";
import Poller, { clockText, isLiveStatus, statusLine } from "../model/autoRefresh";
import type { PollOutcome } from "../model/autoRefresh";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type { Router$RouteMatchedEvent } from "sap/ui/core/routing/Router";
import type HTML from "sap/ui/core/HTML";
import type { JobRunDetail } from "../service/types";

/**
 * @namespace com.agent.admin.controller
 */
export default class RunDetail extends BaseController {

    public formatter = formatter;

    private runId = "";

    /** Re-fetches the run while it is live; see model/autoRefresh.ts. */
    private poller = new Poller(() => this.poll());
    private lastRefreshed = "";
    private lastPollFailed = false;

    public onInit(): void {
        this.setModel(new JSONModel({
            data: {}, reportHtml: "", hasReport: false,
            auto: { enabled: true, status: "" }
        }), "run");
        this.getRouter().getRoute("runDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            // Another run (or the same one again): whatever the previous
            // one was polling must not carry on against this id.
            this.poller.stop();
            this.runId = (event.getParameter("arguments") as { runId: string }).runId;
            this.lastRefreshed = "";
            this.lastPollFailed = false;
            void this.load();
        });
        // Leaving by any route -- back button, nav list, browser history --
        // stops the polling; the page is not visible, so there is nothing
        // to keep fresh.
        this.getRouter().attachRouteMatched((event: Router$RouteMatchedEvent) => {
            if (event.getParameter("name") !== "runDetail") {
                this.poller.stop();
            }
        });
    }

    public onExit(): void {
        this.poller.stop();
    }

    private load(): Promise<void> {
        return this.withBusy(async () => {
            const model = this.getModel("run") as JSONModel;
            model.setProperty("/data", {});
            model.setProperty("/reportHtml", "");
            model.setProperty("/hasReport", false);

            const run = await this.run(
                this.getAdminService().getRun(this.runId),
                "Could not load the run."
            );
            if (!run) {
                this.updateStatusLine();
                return;
            }
            await this.apply(run);
            this.syncPolling();
        });
    }

    /** Puts a fetched run into the model and renders its report. */
    private async apply(run: JobRunDetail): Promise<void> {
        const model = this.getModel("run") as JSONModel;
        model.setProperty("/data", run);
        this.lastRefreshed = clockText(new Date());
        this.updateStatusLine();

        const bodyMd = run.report?.body_md ?? "";
        if (!bodyMd) {
            model.setProperty("/reportHtml", "");
            model.setProperty("/hasReport", false);
            return;
        }
        let html = "";
        try {
            await ReportRenderer.ensureLibraries();
            html = `<div class="agentAdminReport">${ReportRenderer.renderMarkdown(bodyMd)}</div>`;
        } catch (error) {
            ErrorHandler.handle(error, "Could not render the run report.");
            return;
        }
        // Unchanged markup is left alone: re-setting it would re-render the
        // HTML control on every poll and lose the reader's scroll position.
        if (model.getProperty("/reportHtml") === html) {
            return;
        }
        model.setProperty("/reportHtml", html);
        model.setProperty("/hasReport", true);

        // Diagrams are replaced after the HTML control has rendered, so the
        // fences exist in the DOM to be swapped.
        const control = this.byId("reportHtml") as HTML;
        control.attachEventOnce("afterRendering", () => {
            const dom = control.getDomRef();
            if (dom) {
                void ReportRenderer.renderMermaid(dom as HTMLElement, this.runId);
            }
        });
    }

    // --- refresh ---

    /** One poll: fetch, apply, and tell the poller whether to continue.
     * No busy indicator, so the page does not flicker every few seconds. */
    private async poll(): Promise<PollOutcome> {
        const id = this.runId;
        try {
            const run = await this.getAdminService().getRun(id);
            if (id !== this.runId) {
                return "done";
            }
            this.lastPollFailed = false;
            await this.apply(run);
            return isLiveStatus(run.status) ? "live" : "done";
        } catch {
            if (!this.lastPollFailed) {
                MessageToast.show(this.text("autoRefreshFailed"));
            }
            this.lastPollFailed = true;
            this.updateStatusLine();
            return "failed";
        }
    }

    /** Starts or stops the poller from the toggle and the run's status. */
    private syncPolling(): void {
        const model = this.getModel("run") as JSONModel;
        const enabled = !!model.getProperty("/auto/enabled");
        const live = isLiveStatus((model.getProperty("/data/status") as string) || "");
        if (enabled && live) {
            if (!this.poller.isActive()) {
                this.poller.start("live");
            }
        } else {
            this.poller.stop();
        }
        this.updateStatusLine();
    }

    private updateStatusLine(): void {
        const model = this.getModel("run") as JSONModel;
        model.setProperty("/auto/status", statusLine({
            enabled: !!model.getProperty("/auto/enabled"),
            live: isLiveStatus((model.getProperty("/data/status") as string) || ""),
            failed: this.lastPollFailed,
            lastRefreshed: this.lastRefreshed,
            intervals: this.poller.intervals
        }));
    }

    public onToggleAutoRefresh(): void {
        // The two-way binding has already flipped /auto/enabled.
        this.syncPolling();
    }

    public onRefresh(): void {
        void this.load();
    }

    public onDownload(): void {
        window.open(this.getAdminService().runReportUrl(this.runId), "_blank", "noopener");
    }

    public onBack(): void {
        this.poller.stop();
        this.getRouter().navTo("runs");
    }
}

import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import { ValueState } from "sap/ui/core/library";
import BaseController from "../BaseController";
import ErrorHandler from "../../service/ErrorHandler";
import { AdminError } from "../../service/AdminService";
import type { ODataVersion } from "../../service/types";

/** What a delete needs to know of a service. */
export interface NamedService {
    name: string;
    title: string;
}

/**
 * How a delete ended. `refused`: the server answered, but not with a
 * success (still in use, already gone, an error) -- what the page shows may
 * no longer be true and should be read again. `unanswered`: no answer came,
 * or the session or the scope is the problem; reading again would not help.
 */
export type DeleteOutcome = "deleted" | "refused" | "unanswered";

/**
 * What the OData services list and the service detail page share: how a
 * version and an identity are worded, and how a service is deleted.
 *
 * @namespace com.agent.admin.controller.odata
 */
export default abstract class ODataController extends BaseController {

    // --- formatters ---------------------------------------------------------

    /** "V2" or "V4". */
    public formatVersion(version: ODataVersion | undefined): string {
        return this.text(version === "v4" ? "odataVersionV4" : "odataVersionV2");
    }

    public formatRunsAs(userContext: boolean | undefined): string {
        return this.text(userContext ? "odataRunsAsUser" : "odataRunsAsTechnical");
    }

    /** The signed-in user stands out; the technical user is the neutral case. */
    public formatRunsAsState(userContext: boolean | undefined): ValueState {
        return userContext ? ValueState.Information : ValueState.None;
    }

    // --- delete -------------------------------------------------------------

    /**
     * Asks before deleting `service` and calls `onConfirm` on a yes.
     *
     * Titles can repeat (the same API under two identities), so the question
     * also carries the technical name, which cannot.
     */
    protected askDelete(service: NamedService, onConfirm: () => void): void {
        const remove = this.text("delete");
        MessageBox.warning(this.text("odataDeleteConfirm", [service.title, service.name]), {
            title: remove,
            actions: [remove, MessageBox.Action.CANCEL],
            emphasizedAction: remove,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                if (action === remove) {
                    onConfirm();
                }
            }
        });
    }

    /**
     * Deletes `service` and says how it went: a toast for a success, the
     * failure otherwise. What happens to the page afterwards is the caller's.
     */
    protected async removeService(service: NamedService): Promise<DeleteOutcome> {
        try {
            await this.withBusy(() => this.getAdminService().deleteODataService(service.name));
        } catch (error) {
            const kind = ErrorHandler.classify(error);
            if (error instanceof AdminError && kind === "conflict") {
                // The central policy shows a 409 as a passing toast ("a run
                // is already in flight"). Here it is a refusal the admin has
                // to act on, and the server's text names the agents to
                // detach the service from, so it stays on screen as it came.
                MessageBox.error(error.detail || this.text("odataDeleteFailed", [service.title]));
            } else {
                ErrorHandler.handle(error, this.text("odataDeleteFailed", [service.title]));
            }
            return error instanceof AdminError && (kind === "conflict" || kind === "error")
                ? "refused" : "unanswered";
        }
        MessageToast.show(this.text("odataDeleted"));
        return "deleted";
    }
}

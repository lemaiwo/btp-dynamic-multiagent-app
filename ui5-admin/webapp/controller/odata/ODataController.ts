import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import { ValueState } from "sap/ui/core/library";
import BaseController from "../BaseController";
import ErrorHandler from "../../service/ErrorHandler";
import { AdminError } from "../../service/AdminService";
import type { ODataPending, ODataNewOperation } from "../../model/odataCatalog";
import type { ODataEntityOp, ODataVersion } from "../../service/types";

/** How many field names one capped entry of a write list spells out. */
const FIELD_CAP = 3;

/** The i18n key of each entity-set operation's name. */
const OP_TEXT: Record<ODataEntityOp, string> = {
    list: "odataOpList", get: "odataOpGet", create: "odataOpCreate", update: "odataOpUpdate", delete: "odataOpDelete"
};

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

    // --- what a definition opens -------------------------------------------

    /**
     * "Update on "Requisition item" (A_PurchaseRequisitionItem); the
     * operation "Release item" (ReleaseItem); the field Note writable on
     * ...": one entry per entity set, per operation and per entity set with
     * fields that become writable. With `cap`, at most that many entries
     * and the number of the others (`more` words that tail), and per entry
     * at most `FIELD_CAP` field names and the number of the others: an
     * entity set can make hundreds of fields writable at once. Without
     * `cap` (a question) every name is there.
     *
     * The one wording of a list of writes: the service page's strip and
     * Save question, the Duplicate dialog and the list page's file import.
     */
    protected writeList(pending: ODataPending, cap = Infinity, more = "odataWriteMore"): string {
        const entries = pending.entitySets.map((write) => this.text("odataWriteItem", [
            write.operations.map((op) => this.text(OP_TEXT[op])).join(", "), write.title, write.name
        ])).concat(pending.operations.map((operation) => (
            this.text("odataWriteOperationItem", [operation.title, operation.name])
        ))).concat((pending.fields ?? []).map((write) => {
            const short = cap !== Infinity && write.fields.length > FIELD_CAP;
            const names = short
                ? this.text("odataWriteFieldsMore", [
                    write.fields.slice(0, FIELD_CAP).join(", "), write.fields.length - FIELD_CAP
                ])
                : write.fields.join(", ");
            return this.text(write.fields.length === 1 ? "odataWriteFieldOne" : "odataWriteFieldMany", [
                names, write.title, write.name
            ]);
        }));
        return entries.length <= cap
            ? entries.join("; ")
            : this.text(more, [entries.slice(0, cap).join("; "), entries.length - cap]);
    }

    /** "the operation "Release strategy" (GetReleaseStrategy); ...": with
     *  `cap`, at most that many and the number of the others. */
    protected operationList(list: readonly ODataNewOperation[], cap = Infinity, more = "odataWriteMore"): string {
        const entries = list.map((operation) => this.text("odataWriteOperationItem", [operation.title, operation.name]));
        return entries.length <= cap
            ? entries.join("; ")
            : this.text(more, [entries.slice(0, cap).join("; "), entries.length - cap]);
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
     * Deletes `service` and says how it went: a toast for a success (a
     * warning that stays when the running agents could not be reloaded), the
     * failure otherwise. What happens to the page afterwards is the caller's.
     */
    protected async removeService(service: NamedService): Promise<DeleteOutcome> {
        let answer;
        try {
            answer = await this.withBusy(() => this.getAdminService().deleteODataService(service.name));
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
        // Deleted, but the running agents may still hold the service: that
        // is said in a box that stays, in place of the toast.
        if (!this.warnIfNotLive(answer, "reloadFailedServiceDeleted")) {
            MessageToast.show(this.text("odataDeleted"));
        }
        return "deleted";
    }
}

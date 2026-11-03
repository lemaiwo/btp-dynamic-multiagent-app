import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import ErrorHandler from "../../service/ErrorHandler";
import { AdminError } from "../../service/AdminService";
import type AdminService from "../../service/AdminService";
import type { Translate } from "../../model/odataDestinations";
import type { ReloadOutcome } from "../../service/types";

/** What `QueryDelete` needs of the page it works for. */
export interface QueryDeletePage {
    text: Translate;
    service(): AdminService;
    busy<T>(work: () => Promise<T>): Promise<T>;
    warnIfNotLive(outcome: ReloadOutcome | undefined, leadKey: string): boolean;
    /** The query is deleted: the page goes back to the list. */
    done(): void;
}

/**
 * Deletes an analytical query after asking, naming title and technical
 * name (titles can repeat), as the OData services do. A plain class.
 */
export default class QueryDelete {

    public constructor(private readonly page: QueryDeletePage) {}

    /** Asks with Cancel as the default, and deletes on a yes. */
    public ask(name: string, title: string): void {
        const remove = this.page.text("delete");
        MessageBox.warning(this.page.text("inaDeleteConfirm", [title, name]), {
            title: remove,
            actions: [remove, MessageBox.Action.CANCEL],
            emphasizedAction: remove,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                if (action === remove) {
                    void this.remove(name, title);
                }
            }
        });
    }

    private async remove(name: string, title: string): Promise<void> {
        const page = this.page;
        let answer: ReloadOutcome;
        try {
            answer = await page.busy(() => page.service().deleteInaQuery(name));
        } catch (error) {
            if (error instanceof AdminError && ErrorHandler.classify(error) === "conflict") {
                // Still used: the server's text names the agents to detach it
                // from, so it stays on screen as it came.
                MessageBox.error(error.detail || page.text("inaDeleteFailed", [title]));
            } else {
                ErrorHandler.handle(error, page.text("inaDeleteFailed", [title]));
            }
            return;
        }
        if (!page.warnIfNotLive(answer, "inaReloadFailedDeleted")) {
            MessageToast.show(page.text("inaDeleted"));
        }
        page.done();
    }
}

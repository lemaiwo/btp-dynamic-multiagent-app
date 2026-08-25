import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import { AdminError } from "./AdminService";

export type ErrorKind = "session" | "conflict" | "error";

/**
 * The application's single error policy.
 *
 * A 401/403 means the approuter session lapsed; reloading re-authenticates, so
 * that is the only offered action. A 409 from a run trigger means a run is
 * already in flight — informational, not a failure. Everything else is shown
 * with the server's own `detail`, never swallowed.
 */
export default {

    classify(error: unknown): ErrorKind {
        if (error instanceof AdminError) {
            if (error.status === 401 || error.status === 403) {
                return "session";
            }
            if (error.status === 409) {
                return "conflict";
            }
        }
        return "error";
    },

    messageFor(error: unknown, fallback: string): string {
        // AdminError is checked first and exhaustively: it extends Error, so
        // falling through would return the constructor's synthesised
        // "Request failed with status 500" instead of the caller's fallback
        // whenever the server sent no detail.
        if (error instanceof AdminError) {
            return error.detail || fallback;
        }
        if (error instanceof Error && error.message) {
            return error.message;
        }
        return fallback;
    },

    handle(error: unknown, fallback = "The request failed."): void {
        const kind = this.classify(error);
        const message = this.messageFor(error, fallback);

        if (kind === "session") {
            MessageBox.error(
                "Your session has expired. Reload the page to sign in again.",
                {
                    title: "Session expired",
                    actions: ["Reload"],
                    emphasizedAction: "Reload",
                    onClose: () => window.location.reload()
                }
            );
            return;
        }
        if (kind === "conflict") {
            MessageToast.show(message);
            return;
        }
        MessageBox.error(message);
    }
};

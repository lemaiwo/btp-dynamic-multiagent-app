import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import { AdminError } from "./AdminService";

export type ErrorKind = "session" | "forbidden" | "conflict" | "error";

/** The part of a resource bundle the handler uses. */
export interface TextSource {
    getText(key: string, args?: (string | number)[], ignoreKeyFallback?: boolean): string | undefined;
}

/**
 * English texts used until the component hands over its `i18n` bundle
 * (`useBundle`), and for a key the bundle does not hold: the handler is a
 * plain module, reached from unit tests without a component.
 */
const FALLBACK_TEXTS: Record<string, string> = {
    sessionExpiredTitle: "Session expired",
    sessionExpiredMessage: "Your session has expired. Reload the page to sign in again.",
    sessionExpiredReload: "Reload",
    accessDeniedTitle: "Access denied",
    accessDeniedMessage: "You are signed in, but your user has no access to this function. "
        + "Ask an administrator for the \"Agent Administrator\" role collection (the admin scope), "
        + "then sign in again.",
    requestFailedFallback: "The request failed."
};

let bundle: TextSource | undefined;

/**
 * The application's single error policy.
 *
 * A 401 means the approuter session lapsed; reloading re-authenticates, so
 * that is the only offered action. A 403 is a different thing: the user is
 * signed in but lacks the admin scope, and reloading would only sign them in
 * again to the same answer, so it is explained and left alone. A 409 from a
 * run trigger ("Run now" of an agent or a workflow, `AdminError.runTrigger`)
 * means a run is already in flight — informational, a toast. Every other
 * 409 (a name already taken, an agent a workflow step still runs, a rename
 * clash) is something the admin must act on, so it is shown like any other
 * failure: a MessageBox with the server's own `detail`, never swallowed.
 * `classify` still answers "conflict" for every 409: callers that handle a
 * 409 themselves (the OData pages) branch on it.
 */
export default {

    /** Hands over the component's `i18n` bundle (Component.init). */
    useBundle(source: TextSource | undefined): void {
        bundle = source;
    },

    /** A text of the bundle, or the English fallback when it has none. */
    text(key: string): string {
        const fromBundle = bundle ? bundle.getText(key, undefined, true) : undefined;
        return fromBundle || FALLBACK_TEXTS[key] || key;
    },

    classify(error: unknown): ErrorKind {
        if (error instanceof AdminError) {
            if (error.status === 401) {
                return "session";
            }
            if (error.status === 403) {
                return "forbidden";
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

    handle(error: unknown, fallback?: string): void {
        const kind = this.classify(error);
        const message = this.messageFor(error, fallback || this.text("requestFailedFallback"));

        if (kind === "session") {
            const reload = this.text("sessionExpiredReload");
            MessageBox.error(this.text("sessionExpiredMessage"), {
                title: this.text("sessionExpiredTitle"),
                actions: [reload],
                emphasizedAction: reload,
                onClose: () => window.location.reload()
            });
            return;
        }
        if (kind === "forbidden") {
            MessageBox.error(this.text("accessDeniedMessage"), { title: this.text("accessDeniedTitle") });
            return;
        }
        if (kind === "conflict" && error instanceof AdminError && error.runTrigger) {
            MessageToast.show(message);
            return;
        }
        // The text goes in as plain text (MessageBox renders it in a Text
        // control, no formatted HTML), so the server's detail is never markup.
        MessageBox.error(message);
    }
};

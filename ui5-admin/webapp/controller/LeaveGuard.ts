import HashChanger from "sap/ui/core/routing/HashChanger";
import type Component from "../Component";
import type Router from "sap/m/routing/Router";
import type { Router$RouteMatchedEvent } from "sap/ui/core/routing/Router";

/** What a `LeaveGuard` needs of its page. */
export interface LeaveGuardPage {
    /** The form holds changes that are not saved. */
    isDirty(): boolean;
    /** Asks whether to discard them; resolves true when the page may be
     *  left (the page has then dropped the changes). */
    askDiscard(): Promise<boolean>;
    /** Another page is shown: forget what was shown. */
    left(): void;
}

/**
 * Asks before a form with unsaved changes is left, the way the OData
 * service page does: the side navigation and the page's own back button
 * ask through `Component.canLeave`; the browser's back button or an edited
 * address (which the router cannot refuse) put the page's hash back, and
 * the question is asked from there; closing the tab gets the browser's own
 * question.
 *
 * The page calls `matched()` first in its route's `patternMatched` and
 * loads only when it answers true, and `destroy()` in `onExit`.
 */
export default class LeaveGuard {

    /** The hash the page is shown under. */
    private shownHash = "";
    /** The hash was just put back: the next match is the page again. */
    private restoring = false;
    /** Where the user was going when the hash was put back. */
    private wantedHash = "";

    private readonly onBeforeUnload = (event: BeforeUnloadEvent): void => {
        if (this.page.isDirty()) {
            // The browser's own "leave site?" question; its text is not ours.
            event.preventDefault();
            event.returnValue = "";
        }
    };

    public constructor(
        private readonly component: Component,
        private readonly router: Router,
        private readonly route: string,
        private readonly page: LeaveGuardPage
    ) {
        router.attachRouteMatched(this.onRouteMatched, this);
        router.attachBypassed(this.onLeft, this);
        window.addEventListener("beforeunload", this.onBeforeUnload);
    }

    /** The page's route matched. False: nothing is to be loaded (the
     *  page came back to ask, or keeps its unsaved changes). */
    public matched(): boolean {
        if (this.restoring) {
            // Back on the page the user tried to leave; it is as it was.
            this.restoring = false;
            this.component.setLeaveGuard(() => this.confirm());
            this.askLater();
            return false;
        }
        if (this.keepUnsaved()) {
            return false;
        }
        this.shownHash = HashChanger.getInstance().getHash();
        this.component.setLeaveGuard(() => this.confirm());
        return true;
    }

    /** Whether the page may be left: yes without changes, else the answer. */
    public confirm(): Promise<boolean> {
        return this.page.isDirty() ? this.page.askDiscard() : Promise.resolve(true);
    }

    public destroy(): void {
        window.removeEventListener("beforeunload", this.onBeforeUnload);
        this.router.detachRouteMatched(this.onRouteMatched, this);
        this.router.detachBypassed(this.onLeft, this);
        this.component.setLeaveGuard(undefined);
    }

    private onRouteMatched(event: Router$RouteMatchedEvent): void {
        if (event.getParameter("name") !== this.route) {
            this.onLeft();
        }
    }

    private onLeft(): void {
        if (this.keepUnsaved()) {
            return;
        }
        this.restoring = false;
        this.shownHash = "";
        this.component.setLeaveGuard(undefined);
        this.page.left();
    }

    /** The address changed under unsaved changes: the hash is put back,
     *  which shows the page again, and the question is asked from there. */
    private keepUnsaved(): boolean {
        const hashChanger = HashChanger.getInstance();
        const wanted = hashChanger.getHash();
        if (!this.shownHash || wanted === this.shownHash || !this.page.isDirty()) {
            return false;
        }
        this.restoring = true;
        this.wantedHash = wanted;
        hashChanger.replaceHash(this.shownHash);
        return true;
    }

    /** Not in the same tick: the router closes open dialogs while it is
     *  still navigating back here. */
    private askLater(): void {
        const wanted = this.wantedHash;
        setTimeout(() => {
            void this.confirm().then((leave) => {
                if (leave) {
                    HashChanger.getInstance().setHash(wanted);
                }
            });
        }, 0);
    }
}

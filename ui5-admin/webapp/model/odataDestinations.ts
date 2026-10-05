import type { ODataDestination, ODataDestinationList } from "../service/types";

/**
 * What the destination field of an OData service knows about the
 * destinations that exist (`GET odata/destinations`): what it offers, how it
 * words one, and what it says about the name in the field. Decisions only;
 * the controller puts them on the page.
 *
 * The list is a help, never a rule: a destination can be created after the
 * list was read and the list can be missing, so any name may be typed and
 * nothing here ever stands in the way of a save.
 */

/** No answer yet, no list at all, or the list. */
export type DestinationsState = "loading" | "unavailable" | ODataDestinationList;

/** Resource bundle lookup: the text of `key` with `args` filled in. */
export type Translate = (key: string, args?: (string | number)[]) => string;

/** One entry of the dropdown. */
export interface DestinationChoice {
    name: string;
    /** Sign-in, network, level and description, in words. */
    info: string;
}

/**
 * What there is to say about the name in the field:
 * `fixedAccount` -- the service runs as the signed-in user, the destination
 * signs in with one account; `needsUser` -- the reverse; `notListed` -- not
 * among the destinations of a complete list; `notUsable` -- listed, but not
 * an HTTP destination.
 */
export type DestinationNotice = "" | "fixedAccount" | "needsUser" | "notListed" | "notUsable";

const SEPARATOR = " · ";

const NOTICE_TEXT: Record<Exclude<DestinationNotice, "">, string> = {
    fixedAccount: "odataDestinationFixedAccount",
    needsUser: "odataDestinationNeedsUser",
    notListed: "odataDestinationNotListed",
    notUsable: "odataDestinationNotUsable"
};

function listOf(state: DestinationsState): ODataDestinationList | undefined {
    return typeof state === "string" ? undefined : state;
}

function authenticationKey(item: ODataDestination): string {
    if (item.user_propagating) {
        return "odataDestinationAuthUser";
    }
    if (item.authentication === "NoAuthentication") {
        return "odataDestinationAuthNone";
    }
    // "" (not stated) and "other" (not an identifier): nothing is known, so
    // nothing is claimed.
    return item.authentication && item.authentication !== "other"
        ? "odataDestinationAuthFixed" : "odataDestinationAuthUnknown";
}

function networkKey(item: ODataDestination): string {
    if (item.proxy_type === "OnPremise") {
        return "odataDestinationOnPremise";
    }
    return item.proxy_type === "Internet" ? "odataDestinationInternet" : "";
}

function usable(state: DestinationsState): ODataDestination[] {
    const seen: Record<string, boolean> = {};
    return (listOf(state)?.items ?? []).filter((item) => {
        if (!item.usable || seen[item.name]) {
            return false;
        }
        seen[item.name] = true;
        return true;
    });
}

function unusableCount(state: DestinationsState): number {
    return (listOf(state)?.items ?? []).filter((item) => !item.usable).length;
}

/** Whether every destination a service could name is in the list. */
function isComplete(list: ODataDestinationList): boolean {
    return !list.truncated && list.warnings.length === 0;
}

export default {

    /**
     * The answer of `GET odata/destinations` as a list, or `unavailable`
     * for anything that is not one. Entries without a name are dropped.
     */
    read(answer: unknown): DestinationsState {
        const body = answer as Partial<ODataDestinationList> | null | undefined;
        if (!body || !Array.isArray(body.items)) {
            return "unavailable";
        }
        return {
            items: body.items.filter((item) => !!item && typeof item.name === "string" && item.name !== ""),
            truncated: !!body.truncated,
            skipped: typeof body.skipped === "number" ? body.skipped : 0,
            warnings: Array.isArray(body.warnings) ? body.warnings : []
        };
    },

    /** "Signs in as the user · on-premise · subaccount · <description>". */
    describe(item: ODataDestination, text: Translate): string {
        const network = networkKey(item);
        const parts = [
            text(authenticationKey(item)),
            network ? text(network) : "",
            text(item.level === "instance" ? "odataDestinationLevelInstance" : "odataDestinationLevelSubaccount"),
            (item.description ?? "").trim()
        ];
        return parts.filter(Boolean).join(SEPARATOR);
    },

    /**
     * What the dropdown offers: the destinations a service can name. One
     * that cannot be used is left out rather than shown disabled -- its
     * shown name need not be its real one, and two of them can look alike --
     * and `hint` says how many there are.
     */
    choices(state: DestinationsState, text: Translate): DestinationChoice[] {
        return usable(state).map((item) => ({ name: item.name, info: this.describe(item, text) }));
    },

    unusableCount,

    /**
     * What to say about `name` for a service that runs as the signed-in
     * user (`userContext`) or as a technical user. Nothing without a list,
     * and nothing about a name the list does not hold unless the list is
     * complete.
     */
    notice(state: DestinationsState, name: string, userContext: boolean): DestinationNotice {
        const list = listOf(state);
        const wanted = (name ?? "").trim();
        if (!list || !wanted) {
            return "";
        }
        const found = usable(list).filter((item) => item.name === wanted)[0];
        if (found) {
            if (userContext && !found.user_propagating) {
                return "fixedAccount";
            }
            return !userContext && found.user_propagating ? "needsUser" : "";
        }
        // Only `not_http`: the shown name of an invalid one is a cleaned
        // form and proves nothing about the name that was typed.
        if (list.items.some((item) => !item.usable && item.reason === "not_http" && item.name === wanted)) {
            return "notUsable";
        }
        return isComplete(list) ? "notListed" : "";
    },

    /** None of the notices is an error: the admin may know better. */
    noticeState(notice: DestinationNotice): "None" | "Information" | "Warning" {
        if (!notice) {
            return "None";
        }
        return notice === "notListed" ? "Information" : "Warning";
    },

    noticeText(notice: DestinationNotice, text: Translate): string {
        return notice ? text(NOTICE_TEXT[notice]) : "";
    },

    /** The line under the field: why the dropdown may not be the whole truth. */
    hint(state: DestinationsState, text: Translate): string {
        if (state === "loading") {
            return "";
        }
        if (state === "unavailable") {
            return text("odataDestinationsUnavailable");
        }
        const parts: string[] = [];
        if (!isComplete(state)) {
            parts.push(text("odataDestinationsIncomplete"));
        }
        const unusable = unusableCount(state);
        if (unusable === 1) {
            parts.push(text("odataDestinationsUnusableOne"));
        } else if (unusable > 1) {
            parts.push(text("odataDestinationsUnusableMany", [unusable]));
        }
        if (parts.length === 0 && state.items.length === 0) {
            parts.push(text("odataDestinationsNone"));
        }
        return parts.join(" ");
    }
};

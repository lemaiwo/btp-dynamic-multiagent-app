/**
 * "Follow the conversation only when the reader is at its end."
 *
 * A reader at the bottom of the conversation sees a streamed answer grow
 * and the page keeps the end in view; a reader who scrolled up to read an
 * older message is left where they are. The state is taken from the
 * reader's own scrolling (`scroll` events), and the following is done by a
 * MutationObserver (any DOM change of the content: a re-rendered list, a
 * streamed answer injected by `core:HTML`) and a ResizeObserver (growth
 * without a mutation, e.g. a late layout), so it does not depend on when UI5
 * renders. A MutationObserver fires as a microtask, also where rendering
 * frames are throttled (a background test frame), where a ResizeObserver
 * alone would wait.
 *
 * No UI5 imports: the page hands in the scrolling element and its content.
 */

/** The element is scrolled to its end, give or take `slack` pixels. */
export function isAtBottom(el: HTMLElement, slack = 24): boolean {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= slack;
}

/** Scrolls the element to its end. */
export function follow(el: HTMLElement): void {
    el.scrollTop = el.scrollHeight;
}

export default class StickyScroll {

    private el?: HTMLElement;
    private observer?: ResizeObserver;
    private mutations?: MutationObserver;
    private content?: HTMLElement;
    private bottom = true;
    /** The first state is always reported, so the page starts in line with it. */
    private notified = false;
    private readonly onScroll = (): void => {
        if (this.el) {
            this.set(isAtBottom(this.el));
        }
    };

    /** `onChange` hears every change of {@link atBottom} (e.g. for a "Jump to latest" button). */
    public constructor(private readonly onChange?: (atBottom: boolean) => void) {}

    /** The reader is at the end: new content is followed. */
    public get atBottom(): boolean {
        return this.bottom;
    }

    /** Watches `el` (the scrolling element) and `content` (what grows inside it); replaces an earlier pair. */
    public attach(el: HTMLElement, content: HTMLElement): void {
        if (this.el === el && this.content === content) {
            return;
        }
        this.detach();
        this.el = el;
        this.content = content;
        el.addEventListener("scroll", this.onScroll, { passive: true });
        const keep = (): void => {
            if (this.bottom && this.el) {
                follow(this.el);
            }
        };
        this.observer = new ResizeObserver(keep);
        this.observer.observe(content);
        this.mutations = new MutationObserver(keep);
        this.mutations.observe(content, { childList: true, subtree: true, characterData: true, attributes: true });
        if (this.bottom) {
            follow(el);
        }
    }

    public detach(): void {
        this.observer?.disconnect();
        this.observer = undefined;
        this.mutations?.disconnect();
        this.mutations = undefined;
        this.content = undefined;
        this.el?.removeEventListener("scroll", this.onScroll);
        this.el = undefined;
    }

    /** Goes to the end and follows again (the reader sent a message, or chose "Jump to latest"). */
    public toBottom(): void {
        if (this.el) {
            follow(this.el);
        }
        this.set(true);
    }

    private set(atBottom: boolean): void {
        if (atBottom !== this.bottom || (this.onChange && !this.notified)) {
            this.bottom = atBottom;
            this.notified = true;
            this.onChange?.(atBottom);
        }
    }
}

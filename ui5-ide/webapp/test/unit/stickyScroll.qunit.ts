import StickyScroll, { follow, isAtBottom } from "com/agent/ide/model/stickyScroll";

interface Ctx { box: HTMLDivElement; content: HTMLDivElement; sticky?: StickyScroll }

/** Two animation frames: a ResizeObserver callback has run by then. */
function frames(): Promise<void> {
    return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
}

function grow(ctx: Ctx, px: number): void {
    ctx.content.style.height = `${ctx.content.offsetHeight + px}px`;
}

function scrollTo(ctx: Ctx, top: number): void {
    ctx.box.scrollTop = top;
    ctx.box.dispatchEvent(new Event("scroll"));
}

QUnit.module("stickyScroll", {
    beforeEach: function (this: Ctx) {
        this.box = document.createElement("div");
        this.box.style.cssText = "height:100px;overflow:auto;position:absolute;left:-2000px;top:0;width:100px";
        this.content = document.createElement("div");
        this.content.style.height = "300px";
        this.box.appendChild(this.content);
        document.body.appendChild(this.box);
    },
    afterEach: function (this: Ctx) {
        this.sticky?.detach();
        this.box.remove();
    }
});

QUnit.test("isAtBottom allows a small slack; follow scrolls to the end", function (this: Ctx, assert) {
    assert.notOk(isAtBottom(this.box), "at the top of a long list");
    follow(this.box);
    assert.ok(isAtBottom(this.box), "follow() goes to the end");
    this.box.scrollTop = 200 - 20;
    assert.ok(isAtBottom(this.box), "20 px above the end counts as the bottom (default slack 24)");
    assert.notOk(isAtBottom(this.box, 10), "not with a 10 px slack");
    this.box.scrollTop = 100;
    assert.notOk(isAtBottom(this.box), "100 px above the end is not");
});

QUnit.test("content that grows while the reader is at the bottom is followed", async function (this: Ctx, assert) {
    this.sticky = new StickyScroll();
    this.sticky.attach(this.box, this.content);
    this.sticky.toBottom();
    assert.ok(this.sticky.atBottom, "at the bottom");
    grow(this, 400);
    await frames();
    assert.ok(isAtBottom(this.box), "still at the bottom after the content grew");
    assert.strictEqual(this.box.scrollTop, this.box.scrollHeight - this.box.clientHeight, "scrolled to the very end");
});

QUnit.test("a reader who scrolled up stays where they are; jumping back resumes following", async function (this: Ctx, assert) {
    const changes: boolean[] = [];
    this.sticky = new StickyScroll((atBottom) => changes.push(atBottom));
    this.sticky.attach(this.box, this.content);
    this.sticky.toBottom();
    scrollTo(this, 50);
    assert.notOk(this.sticky.atBottom, "scrolled up");
    grow(this, 400);
    await frames();
    assert.strictEqual(this.box.scrollTop, 50, "the position is kept: no jump while reading");
    this.sticky.toBottom();
    grow(this, 100);
    await frames();
    assert.ok(isAtBottom(this.box), "following again");
    assert.deepEqual(changes, [true, false, true], "the page hears every change (for a 'Jump to latest' button)");
});

QUnit.test("detach stops following", async function (this: Ctx, assert) {
    this.sticky = new StickyScroll();
    this.sticky.attach(this.box, this.content);
    this.sticky.toBottom();
    this.sticky.detach();
    const before = this.box.scrollTop;
    grow(this, 400);
    await frames();
    assert.strictEqual(this.box.scrollTop, before, "no longer scrolled");
});

QUnit.test("growth without a DOM change of the content (a style rule) is followed through the ResizeObserver", async function (this: Ctx, assert) {
    const child = document.createElement("div");
    child.className = "ideStickyGrow";
    this.content.appendChild(child);
    const style = document.createElement("style");
    style.textContent = ".ideStickyGrow { height: 10px; }";
    document.head.appendChild(style);
    try {
        this.sticky = new StickyScroll();
        this.sticky.attach(this.box, this.content);
        this.content.style.height = "auto";
        await frames();
        this.sticky.toBottom();
        // Only a stylesheet outside the content changes: no mutation inside it.
        style.textContent = ".ideStickyGrow { height: 900px; }";
        const start = Date.now();
        while (!isAtBottom(this.box, 0) && Date.now() - start < 2000) {
            await frames();
        }
        assert.ok(this.box.scrollHeight > 800, "the content grew");
        assert.ok(isAtBottom(this.box, 0), "the page followed it to the end");
    } finally {
        style.remove();
    }
});

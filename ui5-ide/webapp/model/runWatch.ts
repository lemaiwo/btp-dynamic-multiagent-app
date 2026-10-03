/**
 * Polls a session whose run this page does not stream.
 *
 * A run goes on server-side when the page stops reading its stream (a
 * session switch) or never had it (a reload, another tab). The session then
 * says `running` and nothing would tell the page when it ends, so the
 * controller watches it: `poll(sid)` runs every `intervalMs` and answers
 * whether the run still goes on; `false` ends the watch. One session is
 * watched at a time; watching another, or `stop()`, cancels the pending
 * poll, and an answer that arrives after that is ignored. A failed poll is
 * treated as "still running" so a network blip does not end the watch.
 */
export default class RunWatch {

    /** The default interval; journeys shorten it (the instance reads it when created). */
    public static intervalMs = 3000;

    private timer?: ReturnType<typeof setTimeout>;
    private sid = "";
    /** Bumped by every watch()/stop(): a poll answers only for its own generation. */
    private generation = 0;

    public constructor(
        private readonly poll: (sid: string) => Promise<boolean>,
        private readonly intervalMs = RunWatch.intervalMs
    ) {}

    /** True while `sid` is watched (a poll scheduled or in flight). */
    public isWatching(sid: string): boolean {
        return !!sid && this.sid === sid;
    }

    /** Starts watching `sid`, unless it is watched already. */
    public watch(sid: string): void {
        if (this.isWatching(sid)) {
            return;
        }
        this.stop();
        this.sid = sid;
        this.schedule(this.generation);
    }

    public stop(): void {
        this.generation++;
        this.sid = "";
        if (this.timer !== undefined) {
            clearTimeout(this.timer);
            this.timer = undefined;
        }
    }

    private schedule(generation: number): void {
        this.timer = setTimeout(() => {
            this.timer = undefined;
            void this.tick(generation);
        }, this.intervalMs);
    }

    private async tick(generation: number): Promise<void> {
        const sid = this.sid;
        let running: boolean;
        try {
            running = await this.poll(sid);
        } catch {
            running = true;
        }
        if (generation !== this.generation) {
            return;
        }
        if (running) {
            this.schedule(generation);
        } else {
            this.sid = "";
        }
    }
}

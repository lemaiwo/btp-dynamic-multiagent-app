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
 * treated as "still running" so a network blip does not end the watch, with
 * two exceptions that end it and call `onGiveUp`: an auth failure (a 401 or
 * 403: the approuter session lapsed, so every later poll fails the same way),
 * at once, and {@link maxFailures} failed polls in a row.
 */
export default class RunWatch {

    /** The default interval; journeys shorten it (the instance reads it when created). */
    public static intervalMs = 3000;

    /** Failed polls in a row after which the watch gives up. */
    public static maxFailures = 5;

    private timer?: ReturnType<typeof setTimeout>;
    private sid = "";
    /** Bumped by every watch()/stop(): a poll answers only for its own generation. */
    private generation = 0;
    /** Failed polls in a row of the current watch. */
    private failures = 0;

    public constructor(
        private readonly poll: (sid: string) => Promise<boolean>,
        private readonly intervalMs = RunWatch.intervalMs,
        private readonly onGiveUp?: (sid: string, error: unknown) => void
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
        this.failures = 0;
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
        let failure: { error: unknown } | undefined;
        try {
            running = await this.poll(sid);
        } catch (error) {
            running = true;
            failure = { error };
        }
        if (generation !== this.generation) {
            return;
        }
        if (failure) {
            this.failures++;
            if (isAuthFailure(failure.error) || this.failures >= RunWatch.maxFailures) {
                this.stop();
                this.onGiveUp?.(sid, failure.error);
                return;
            }
        } else {
            this.failures = 0;
        }
        if (running) {
            this.schedule(generation);
        } else {
            this.sid = "";
        }
    }
}

/**
 * A failure no later poll can cure: the user is signed out or lacks the
 * role. Reads IdeError's own `isAuth` (which leaves out a refused CSRF
 * token) when the error has one, else the status.
 */
export function isAuthFailure(error: unknown): boolean {
    const e = error as { isAuth?: unknown; status?: unknown } | null | undefined;
    if (typeof e?.isAuth === "boolean") {
        return e.isAuth;
    }
    return e?.status === 401 || e?.status === 403;
}

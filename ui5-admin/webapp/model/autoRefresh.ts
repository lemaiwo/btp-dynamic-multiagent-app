/**
 * Auto-refresh for the run detail pages: which statuses are still moving,
 * how long to wait before the next poll, the status line under the toggle,
 * and a small `Poller` that chains `setTimeout`s so two requests never
 * overlap. Shared by RunDetail and WorkflowRunDetail; the pure parts are
 * unit-tested.
 */

/** Statuses a run can still leave on its own. Everything else is terminal.
 * `pending`/`queued` are not written by today's runners, but a status the
 * page has never seen must not stop the polling for a run that has not
 * started yet, so they are listed rather than inferred. */
const LIVE_STATUSES = ["running", "pending", "queued", "started"];

/** Whether a run with this status may still change. An empty status (the
 * page has not loaded yet) counts as live, so the first poll happens. */
export function isLiveStatus(status: string | null | undefined): boolean {
    if (!status) {
        return true;
    }
    return LIVE_STATUSES.indexOf(String(status).toLowerCase()) !== -1;
}

/** What one poll found out, which decides when the next one runs. */
export type PollOutcome = "live" | "done" | "failed";

export interface PollIntervals {
    /** Between polls while the run is live and the last poll worked. */
    intervalMs: number;
    /** Between polls after a failed poll: back off, but do not give up. */
    slowIntervalMs: number;
}

/** The delay before the next poll, or `null` to stop. */
export function nextDelay(outcome: PollOutcome, intervals: PollIntervals): number | null {
    switch (outcome) {
        case "live":
            return intervals.intervalMs;
        case "failed":
            return intervals.slowIntervalMs;
        default:
            return null;
    }
}

export interface StatusLineInput {
    /** The toggle's state. */
    enabled: boolean;
    /** Whether the run's current status is still live. */
    live: boolean;
    /** The last poll failed, so the slow interval is in force. */
    failed: boolean;
    /** Clock text of the last successful refresh ("12:01:07"), empty if none. */
    lastRefreshed: string;
    intervals: PollIntervals;
}

/** "12:01:07" for the status line. */
export function clockText(date: Date): string {
    const two = (n: number): string => (n < 10 ? `0${n}` : String(n));
    return `${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`;
}

/**
 * The text under the auto-refresh toggle. One line, so it names only the
 * state that matters: how often it polls while it does, that it stopped
 * once the run finished, or that it is off -- with the last refresh time
 * appended whenever polling is not the answer to "how fresh is this?".
 */
export function statusLine(input: StatusLineInput): string {
    const seconds = (ms: number): string => `${Math.round(ms / 100) / 10} s`.replace(".0 s", " s");
    const last = input.lastRefreshed ? ` · Last refreshed ${input.lastRefreshed}` : "";
    if (!input.enabled) {
        return `Auto-refresh off${last}`;
    }
    if (!input.live) {
        return `Finished, auto-refresh stopped${last}`;
    }
    if (input.failed) {
        return `Last refresh failed, retrying every ${seconds(input.intervals.slowIntervalMs)}${last}`;
    }
    return `Auto-refreshing every ${seconds(input.intervals.intervalMs)}`;
}

/**
 * A `setTimeout` chain around an async `tick`.
 *
 * The next timer is armed only after the previous tick has resolved, so a
 * slow backend never gets a second request while the first is out. `stop()`
 * wins over a tick already in flight: its result is applied by the caller,
 * but no further timer is armed.
 */
export default class Poller {

    /** Defaults for every poller. Static so a journey can shorten them:
     * OPA5's autoWait treats a timer of 1000 ms or less as something to
     * wait for, so a test override has to stay above that. */
    public static intervalMs = 3000;
    public static slowIntervalMs = 10000;

    private timer?: ReturnType<typeof setTimeout>;
    private active = false;
    private inFlight = false;

    public constructor(private readonly tick: () => Promise<PollOutcome>) {}

    public get intervals(): PollIntervals {
        return { intervalMs: Poller.intervalMs, slowIntervalMs: Poller.slowIntervalMs };
    }

    public isActive(): boolean {
        return this.active;
    }

    /** Arms the first timer with `outcome`'s delay; a `done` outcome means
     * there is nothing to poll and the poller stays stopped. */
    public start(outcome: PollOutcome = "live"): void {
        this.stop();
        this.active = true;
        this.arm(outcome);
    }

    public stop(): void {
        this.active = false;
        if (this.timer !== undefined) {
            clearTimeout(this.timer);
            this.timer = undefined;
        }
    }

    private arm(outcome: PollOutcome): void {
        const delay = nextDelay(outcome, this.intervals);
        if (delay === null) {
            this.active = false;
            return;
        }
        this.timer = setTimeout(() => {
            this.timer = undefined;
            void this.fire();
        }, delay);
    }

    private async fire(): Promise<void> {
        if (!this.active || this.inFlight) {
            return;
        }
        this.inFlight = true;
        let outcome: PollOutcome;
        try {
            outcome = await this.tick();
        } catch {
            outcome = "failed";
        } finally {
            this.inFlight = false;
        }
        if (this.active) {
            this.arm(outcome);
        }
    }
}

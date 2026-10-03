import type { SseEvent, SseEventType } from "./types";

const KNOWN: ReadonlySet<string> = new Set<SseEventType>([
    "run", "text", "tool", "plan", "file", "artifact", "usage", "error", "done"
]);

/**
 * Incremental parser for the IDE's `text/event-stream` (plan §1.3).
 *
 * Feed it decoded text in whatever pieces the network delivers; it keeps the
 * unfinished tail and returns only frames that ended with a blank line. A
 * frame is the WHATWG event-stream subset the server uses: `event:` names the
 * type, every `data:` line is joined with `\n` and parsed as JSON, and lines
 * starting with `:` are comments (the `: ping` heartbeat).
 *
 * Dropped silently, so a newer server cannot break an older UI: frames whose
 * event type is not in §1.3, frames without data, and data that is not a JSON
 * object. Call `flush()` at end of stream; an unterminated frame is then
 * discarded, as the spec says.
 */
export default class SseParser {

    private buffer = "";

    public push(chunk: string): SseEvent[] {
        this.buffer += chunk;
        return this.drain(false);
    }

    /**
     * End of stream: a CR held back by `push` is now known to be a line end,
     * so a final frame terminated by lone CRs is delivered. Whatever is left
     * after that is an unterminated frame and is discarded.
     */
    public flush(): SseEvent[] {
        const events = this.drain(true);
        this.buffer = "";
        return events;
    }

    private drain(final: boolean): SseEvent[] {
        // CRLF and lone CR are line ends too. A CR at the very end may be the
        // first half of a CRLF still in flight, so leave it for the next chunk
        // (or for flush(), which knows no LF is coming).
        const holdCr = !final && this.buffer.endsWith("\r");
        const text = (holdCr ? this.buffer.slice(0, -1) : this.buffer).replace(/\r\n?/g, "\n");

        const events: SseEvent[] = [];
        let start = 0;
        let end = text.indexOf("\n\n", start);
        while (end !== -1) {
            const event = SseParser.parseFrame(text.slice(start, end));
            if (event) {
                events.push(event);
            }
            start = end + 2;
            end = text.indexOf("\n\n", start);
        }
        this.buffer = text.slice(start) + (holdCr ? "\r" : "");
        return events;
    }

    private static parseFrame(frame: string): SseEvent | null {
        let type = "message";
        const data: string[] = [];
        for (const line of frame.split("\n")) {
            if (line === "" || line.startsWith(":")) {
                continue;
            }
            const colon = line.indexOf(":");
            const field = colon === -1 ? line : line.slice(0, colon);
            let value = colon === -1 ? "" : line.slice(colon + 1);
            if (value.startsWith(" ")) {
                value = value.slice(1);
            }
            if (field === "event") {
                type = value;
            } else if (field === "data") {
                data.push(value);
            }
        }
        if (!KNOWN.has(type) || data.length === 0) {
            return null;
        }
        let parsed: unknown;
        try {
            parsed = JSON.parse(data.join("\n"));
        } catch {
            return null;
        }
        // Every §1.3 payload is an object; `null`, a number or an array would
        // reach a consumer that reads `event.data.delta` and crash it.
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            return null;
        }
        return { type, data: parsed } as SseEvent;
    }
}

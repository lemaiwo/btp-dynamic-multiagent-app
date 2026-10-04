import SseParser from "com/agent/ide/service/SseParser";
import type { SseEvent } from "com/agent/ide/service/types";

QUnit.module("SseParser");

QUnit.test("parses one complete frame", function (assert) {
    const events = new SseParser().push('event: text\ndata: {"delta":"Hi"}\n\n');

    assert.deepEqual(events, [{ type: "text", data: { delta: "Hi" } }], "one text event");
});

QUnit.test("parses several frames from one chunk, in order", function (assert) {
    const events = new SseParser().push(
        'event: run\ndata: {"run_id":"r1","stage":"chat","message_id":"m1"}\n\n' +
        'event: text\ndata: {"delta":"a"}\n\n' +
        'event: done\ndata: {"message_id":"m2","stage":"chat","status":"idle"}\n\n'
    );

    assert.deepEqual(events.map((e) => e.type), ["run", "text", "done"], "all three, in order");
});

QUnit.test("a frame split mid-event across chunks is emitted once complete", function (assert) {
    const parser = new SseParser();
    const frame = 'event: text\ndata: {"delta":"split"}\n\n';
    const seen: SseEvent[] = [];
    for (let i = 0; i < frame.length; i += 3) {
        seen.push(...parser.push(frame.slice(i, i + 3)));
    }

    assert.deepEqual(seen, [{ type: "text", data: { delta: "split" } }], "exactly one event after the last chunk");
});

QUnit.test("a split between the two terminating newlines does not emit early", function (assert) {
    const parser = new SseParser();

    assert.deepEqual(parser.push('event: text\ndata: {"delta":"x"}\n'), [], "nothing before the blank line");
    assert.deepEqual(parser.push("\nevent: te"), [{ type: "text", data: { delta: "x" } }], "emitted on the blank line");
    assert.deepEqual(parser.push('xt\ndata: {"delta":"y"}\n\n'), [{ type: "text", data: { delta: "y" } }],
        "the next frame, split in its event name, still parses");
});

QUnit.test("multi-line data is joined with a newline", function (assert) {
    const events = new SseParser().push('event: text\ndata: {"delta":\ndata: "two lines"}\n\n');

    assert.deepEqual(events, [{ type: "text", data: { delta: "two lines" } }], "the data lines form one JSON document");
});

QUnit.test("CRLF line endings are accepted", function (assert) {
    const events = new SseParser().push('event: text\r\ndata: {"delta":"crlf"}\r\n\r\n');

    assert.deepEqual(events, [{ type: "text", data: { delta: "crlf" } }], "parsed despite CRLF");
});

QUnit.test("comment lines and heartbeats are ignored", function (assert) {
    const parser = new SseParser();

    assert.deepEqual(parser.push(": ping\n\n"), [], "a heartbeat yields nothing");
    assert.deepEqual(
        parser.push(': note\nevent: text\n: inner\ndata: {"delta":"z"}\n\n'),
        [{ type: "text", data: { delta: "z" } }],
        "comments inside a frame are skipped"
    );
});

QUnit.test("unknown event types and frames without data are ignored", function (assert) {
    const events = new SseParser().push(
        'event: mystery\ndata: {"a":1}\n\n' +
        'data: {"no":"event line"}\n\n' +
        "event: text\n\n" +
        'event: usage\ndata: {"requests_used":3,"request_cap":200}\n\n'
    );

    assert.deepEqual(events, [{ type: "usage", data: { requests_used: 3, request_cap: 200 } }],
        "only the known event survives");
});

QUnit.test("a frame whose data is not JSON is skipped, not thrown", function (assert) {
    const events = new SseParser().push('event: text\ndata: not json\n\nevent: text\ndata: {"delta":"ok"}\n\n');

    assert.deepEqual(events, [{ type: "text", data: { delta: "ok" } }], "the bad frame is dropped");
});

QUnit.test("error events carry message and optional code", function (assert) {
    const events = new SseParser().push(
        'event: error\ndata: {"message":"Gate refused","code":"run_in_progress"}\n\n' +
        'event: error\ndata: {"message":"Boom"}\n\n'
    );

    assert.deepEqual(events, [
        { type: "error", data: { message: "Gate refused", code: "run_in_progress" } },
        { type: "error", data: { message: "Boom" } }
    ], "both error frames, code only where sent");
});

QUnit.test("a single space after the colon is stripped, a missing one is tolerated", function (assert) {
    const events = new SseParser().push('event:text\ndata:{"delta":"  keep"}\n\n');

    assert.deepEqual(events, [{ type: "text", data: { delta: "  keep" } }], "value spacing inside JSON kept");
});

QUnit.test("an unterminated trailing frame is held back", function (assert) {
    const parser = new SseParser();

    assert.deepEqual(parser.push('event: text\ndata: {"delta":"tail"}'), [], "no blank line yet, no event");
    assert.deepEqual(parser.push("\n\n"), [{ type: "text", data: { delta: "tail" } }], "completed later");
});

// --- fix round 1 ------------------------------------------------------------

QUnit.test("a CRLF split between chunks (CR | LF) is one line end", function (assert) {
    const parser = new SseParser();

    assert.deepEqual(parser.push('event: text\r\ndata: {"delta":"a"}\r\n\r'), [], "the last CR may still be half a CRLF");
    assert.deepEqual(parser.push('\nevent: text\r\ndata: {"delta":"b"}\r\n\r\n'), [
        { type: "text", data: { delta: "a" } },
        { type: "text", data: { delta: "b" } }
    ], "no phantom blank line, both frames parsed");
});

QUnit.test("lone CR line endings are accepted", function (assert) {
    const events = new SseParser().push('event: text\rdata: {"delta":"cr"}\r\revent: text\rdata: {"delta":"cr2"}\r\r');

    assert.deepEqual(events, [{ type: "text", data: { delta: "cr" } }],
        "the first frame now; the second waits for the CR to be confirmed");
});

QUnit.test("flush delivers a final frame ended by a held-back CR", function (assert) {
    const parser = new SseParser();

    assert.deepEqual(parser.push('event: text\rdata: {"delta":"last"}\r\r'), [], "held back");
    assert.deepEqual(parser.flush(), [{ type: "text", data: { delta: "last" } }], "delivered by flush");
    assert.deepEqual(parser.flush(), [], "nothing left");
});

QUnit.test("flush discards an unterminated frame", function (assert) {
    const parser = new SseParser();
    parser.push('event: text\ndata: {"delta":"cut"}');

    assert.deepEqual(parser.flush(), [], "an incomplete frame is not dispatched");
});

QUnit.test("data that is not a JSON object is dropped", function (assert) {
    const events = new SseParser().push(
        "event: text\ndata: null\n\n" +
        "event: text\ndata: 42\n\n" +
        'event: text\ndata: "str"\n\n' +
        "event: text\ndata: [1]\n\n" +
        'event: text\ndata: {"delta":"obj"}\n\n'
    );

    assert.deepEqual(events, [{ type: "text", data: { delta: "obj" } }], "only the object survives");
});

QUnit.test("a comments frame is accepted", function (assert) {
    const events = new SseParser().push('event: comments\ndata: {"ids":["c1","c2"],"state":"addressed"}\n\n');

    assert.deepEqual(events, [{ type: "comments", data: { ids: ["c1", "c2"], state: "addressed" } }], "comments parsed");
});

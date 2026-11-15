"""Send one message to the web chat as the test user and print the answer,
the model and the token counts.   python3 test/chat_once.py "your question"
Uses the active model, so a real provider's key is billed for it."""
import json
import sys
import urllib.request
import uuid

import stack_api


def main(text):
    api = stack_api.Api()
    status, conv, _ = api.call("POST", "/chat/api/conversations", {})
    if status not in (200, 201):
        raise SystemExit("FAIL new conversation: HTTP %s" % status)
    boundary = uuid.uuid4().hex
    body = ("--%s\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n%s\r\n--%s--\r\n"
            % (boundary, text, boundary)).encode("utf-8")
    request = urllib.request.Request(
        "%s/chat/api/conversations/%s/messages" % (stack_api.BASE, conv["id"]), data=body, method="POST",
        headers={"Authorization": "Bearer " + api._token, "X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    answer, event = [], None
    with urllib.request.urlopen(request, timeout=600) as stream:
        for raw in stream:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[5:])
                except ValueError:
                    continue
                if event == "text":
                    answer.append(data.get("delta") or data.get("text") or "")
                elif event == "agent" and data.get("status") in ("done", "ok", "failed", "error"):
                    print("[agent %s: %s]" % (data.get("agent"), data.get("status")))
                elif event == "tool" and data.get("name"):
                    print("[tool %s]" % data.get("name"))
                elif event == "error":
                    print("RUN FAILED:", json.dumps(data)[:400])
                elif event == "usage":
                    print("[model %s | in %s out %s tokens | %s requests]" % (
                        data.get("model"), data.get("input_tokens"), data.get("output_tokens"),
                        data.get("requests")))
    print("".join(answer).strip()[:3000])


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "Hello. In one sentence: which model are you?")

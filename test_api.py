"""Quick test to verify GPT-4o works on SAP AI Core."""

from dotenv import load_dotenv

load_dotenv()

from gen_ai_hub.proxy import get_proxy_client
from gen_ai_hub.proxy.native.openai import OpenAI

proxy_client = get_proxy_client("gen-ai-hub")
client = OpenAI(proxy_client=proxy_client)

print("=== Test: GPT-4o simple chat ===")
try:
    response = client.chat.completions.create(
        model_name="gpt-4o",
        messages=[{"role": "user", "content": "hello"}],
    )
    print(f"OK: {response.choices[0].message.content}")
except Exception as e:
    print(f"ERROR: {e}")

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

os.environ["SCARF_NO_ANALYTICS"] = "true"

from harness import create_app  # noqa: E402


@pytest.fixture
def app(tmp_path):
    return create_app({
        "TESTING": True, "SECRET_KEY": "test-secret", "API_KEY": "",
        "ENDPOINT": "https://example.test/v1", "MODEL": "", "PROVIDER": "openai",
        "JOB_DIR": str(tmp_path),
    })


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def headers(client):
    return {"X-CSRF-Token": client.get("/api/state").json["csrf"]}


@pytest.fixture
def provider():
    state = {
        "requests": [], "models": ["test-model", "another-model"],
        "status": 200, "code": "result = {'type': 'number', 'value': dfs[0]['sales'].sum()}",
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state["requests"].append((self.path, self.headers.get("Authorization")))
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "data": [{"id": name} for name in state["models"]],
            }).encode())

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, self.headers.get("Authorization"), payload))
            content = "OK" if payload["messages"][-1]["content"] == "Reply with OK." else (
                f"```python\n{state['code']}\n```"
            )
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if state["status"] != 200:
                self.wfile.write(json.dumps({
                    "error": {"message": f"Invalid API key: {self.headers.get('Authorization')}"},
                }).encode())
                return
            self.wfile.write(json.dumps({
                "id": "test-completion", "object": "chat.completion", "created": 1,
                "model": payload["model"],
                "choices": [{
                    "index": 0, "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
            }).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

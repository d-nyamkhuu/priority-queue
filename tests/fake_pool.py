"""A stand-in for the router, the EPP's /metrics and Prometheus on one local port:
POST /v1/completions streams `max_tokens` SSE chunks then usage; GET
/api/v1/query(_range) answers with idle vLLM pods; any other GET serves idle
EPP gauges. Objective "batch" gets HTTP 429, like a full sheddable band."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

IDLE = (
    'llm_d_epp_flow_control_queue_size{priority="0"} 0\nllm_d_epp_flow_control_pool_saturation{stage="decode"} 0.2\n'
    "other_series 5\n"
)
PODS = {"prefill": "p-0", "decode": "d-0"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, code, body=b"", **headers):
        self.send_response(code)
        for k, v in {"Content-Length": str(len(body)), **headers}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        q = {k: float(v[0]) for k, v in parse_qs(url.query).items() if k != "query"}
        if url.path == "/api/v1/query":
            result = [{"metric": {}, "value": [time.time(), "0"]}]
        elif url.path == "/api/v1/query_range":
            ts = [q["start"] + i * q["step"] for i in range(int((q["end"] - q["start"]) // q["step"]) + 1)]
            result = [
                {"metric": {"llm_d_ai_role": role, "pod": pod}, "values": [[t, "0"] for t in ts]}
                for role, pod in PODS.items()
            ]
        else:
            return self.reply(200, IDLE.encode())
        self.reply(200, json.dumps({"status": "success", "data": {"result": result}}).encode())

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("x-llm-d-inference-objective") == "batch":
            return self.reply(429, b'{"error": "dropped"}')
        self.send_response(200)
        self.send_header("Connection", "close")
        self.end_headers()
        for _ in range(req["max_tokens"]):
            time.sleep(0.001)
            self.wfile.write(b'data: {"choices": [{"text": "x"}]}\n\n')
        usage = {"prompt_tokens": 1, "completion_tokens": req["max_tokens"]}
        self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\ndata: [DONE]\n\n".encode())
        self.close_connection = True


class FakePool:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

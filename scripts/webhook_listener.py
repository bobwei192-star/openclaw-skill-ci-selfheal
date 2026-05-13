import json
import logging
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

from .orchestrator import Orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

orchestrator = Orchestrator()


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook/ci-failure":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        logger.info("[WEBHOOK] 收到 POST 请求: path=%s Content-Length=%s client=%s",
                     self.path, content_length, self.client_address)
        try:
            payload = json.loads(body)
            logger.info("[WEBHOOK] 请求体 JSON: %s", json.dumps(payload, ensure_ascii=False))
        except json.JSONDecodeError:
            logger.warning("[WEBHOOK] JSON 解析失败: %s", body[:200])
            self._respond(400, {"status": "error", "reason": "Invalid JSON"})
            return

        required = ["job", "build"]
        missing = [k for k in required if k not in payload]
        if missing:
            logger.warning("[WEBHOOK] 缺少必要字段: %s", missing)
            self._respond(400, {"status": "error", "reason": f"Missing fields: {missing}"})
            return

        status = payload.get("status", "").upper()
        if status and status not in ("FAILURE", "FAILED"):
            logger.info("[WEBHOOK] 非失败状态 (%s)，跳过自愈", status)
            self._respond(200, {"status": "skipped", "reason": f"not a failure: {status}"})
            return

        logger.info("[WEBHOOK] ★ 触发自愈流程: job=%s build=%s branch=%s repo=%s status=%s",
                     payload.get("job"), payload.get("build"),
                     payload.get("branch", "?"), payload.get("repo", "?"),
                     status)

        try:
            orchestrator.handle(payload)
            logger.info("[WEBHOOK] 自愈流程 handle() 完成")
            self._respond(200, {"status": "accepted"})
        except Exception as e:
            logger.exception("[WEBHOOK] 自愈流程异常")
            self._respond(500, {"status": "error", "reason": str(e)})

    def do_GET(self):
        if self.path == "/health":
            status = orchestrator.get_status()
            self._respond(200, {
                "status": "ok",
                "circuit_breakers": {k: v for k, v in status.get("circuit_breaker", {}).items()},
                "active_chains": len(status.get("chains", {})),
            })
        elif self.path == "/admin/reset":
            orchestrator.state = {"version": "2.0.0", "chains": {}, "circuit_breaker": {}}
            orchestrator._save_state()
            self._respond(200, {"status": "reset", "message": "state cleared, all circuit breakers removed"})
        else:
            self.send_error(404)

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        logger.info("HTTP: %s", args[0] if args else format)


def start_server(host="0.0.0.0", port=None):
    if port is None:
        port = int(os.environ.get("BRIDGE_PORT", "8080"))
    server = HTTPServer((host, port), WebhookHandler)
    logger.info("ci-selfheal webhook listening on %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
        logger.info("Server stopped")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ci-selfheal webhook server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("BRIDGE_PORT", "8080")))
    args = parser.parse_args()
    start_server(args.host, args.port)

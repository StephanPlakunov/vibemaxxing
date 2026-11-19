"""Fake Messages API + refusing CONNECT proxy for the token broker experiment. Fake tokens only."""
import hashlib, http.server, json, os, socketserver, subprocess, sys, threading, time

EXP = sys.argv[1]            # experiment dir (logs go here)
API_PORT, PROXY_PORT = int(sys.argv[2]), int(sys.argv[3])
CONFIG_DIR = sys.argv[4]
SERVICE = "Claude Code-credentials-" + hashlib.sha256(CONFIG_DIR.encode()).hexdigest()[:8]
log_lock = threading.Lock()

def log(name, entry):
    with log_lock, open(os.path.join(EXP, name), "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **entry}) + "\n")

def swap_item(token):
    # What vibe does when it replaces a mirror: a new access token, no refresh token.
    blob = {"claudeAiOauth": {"accessToken": token, "expiresAt": int(time.time()*1000) + 8*3600*1000,
            "scopes": ["user:inference", "user:profile", "user:sessions:claude_code", "user:mcp_servers"],
            "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}}
    subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-a", os.environ["USER"],
                    "-s", SERVICE, "-w", json.dumps(blob)], check=True)

SSE = [
 ("message_start", {"type":"message_start","message":{"id":"msg_fake","type":"message","role":"assistant","model":"claude-opus-5-5","content":[],"stop_reason":None,"stop_sequence":None,"usage":{"input_tokens":1,"output_tokens":1}}}),
 ("content_block_start", {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}),
 ("content_block_delta", {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"pong"}}),
 ("content_block_stop", {"type":"content_block_stop","index":0}),
 ("message_delta", {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":None},"usage":{"output_tokens":1}}),
 ("message_stop", {"type":"message_stop"}),
]
swapped = {"done": False}

class Api(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _token(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ("x-api-key" if self.headers.get("x-api-key") else None)
    def do_GET(self):
        log("api.log", {"m": "GET", "path": self.path, "token": self._token()})
        self.send_response(404); self.end_headers()
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n)
        tok = self._token()
        try: stream = json.loads(body or b"{}").get("stream", False)
        except ValueError: stream = False
        log("api.log", {"m": "POST", "path": self.path, "token": tok, "stream": stream})
        if not self.path.startswith("/v1/messages"):
            self.send_response(404); self.end_headers(); return
        if tok == os.environ.get("EXP_REJECT"):
            if os.environ.get("EXP_SWAP_TO") and not swapped["done"]:
                swap_item(os.environ["EXP_SWAP_TO"]); swapped["done"] = True
                log("api.log", {"event": "swapped keychain item to EXP_SWAP_TO"})
            out = json.dumps({"type":"error","error":{"type":"authentication_error","message":"OAuth token has expired."}}).encode()
            self.send_response(401); self.send_header("Content-Type","application/json"); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        if self.path.startswith("/v1/messages/count_tokens"):
            out = b'{"input_tokens": 1}'
            self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        if stream:
            self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.end_headers()
            for ev, data in SSE:
                self.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()); self.wfile.flush()
        else:
            msg = dict(SSE[0][1]["message"]); msg["content"] = [{"type":"text","text":"pong"}]; msg["stop_reason"] = "end_turn"
            out = json.dumps(msg).encode()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().decode(errors="replace").strip()
        log("proxy.log", {"request": line})
        self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")

class T(socketserver.ThreadingMixIn, http.server.HTTPServer): daemon_threads = True
class TP(socketserver.ThreadingTCPServer): daemon_threads = True; allow_reuse_address = True
api = T(("127.0.0.1", API_PORT), Api); proxy = TP(("127.0.0.1", PROXY_PORT), Proxy)
threading.Thread(target=proxy.serve_forever, daemon=True).start()
print("ready", SERVICE, flush=True)
api.serve_forever()

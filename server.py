#!/usr/bin/env python3
"""
Tiny local dashboard server. Two jobs:
  1. Serves the dashboard folder as static files at http://localhost:8765/
  2. Handles POST /refresh — runs aemet_fetch.py in a subprocess and returns
     JSON so the dashboard's Refresh button can trigger a fresh fetch.

Started by double-clicking "Start Dashboard.command". Ctrl-C in Terminal to stop.
"""
import http.server, socketserver, subprocess, sys, json, webbrowser
from pathlib import Path

PORT = 8765
HERE = Path(__file__).resolve().parent


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def log_message(self, format, *args):
        # Compact log: strip the leaked spam SimpleHTTPRequestHandler emits
        sys.stdout.write(f"[server] {self.command} {self.path} → {args[1] if len(args)>1 else ''}\n")

    def _send_json(self, code: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path.rstrip("/") == "/refresh":
            try:
                # Run the fetcher — allow up to 5 min for retries + AEMET fetch
                result = subprocess.run(
                    [sys.executable, "aemet_fetch.py"],
                    cwd=str(HERE), capture_output=True, timeout=300,
                )
                stdout = result.stdout.decode(errors="replace")
                stderr = result.stderr.decode(errors="replace")
                self._send_json(200, {
                    "ok": result.returncode == 0,
                    "returncode": result.returncode,
                    "stdout_tail": stdout[-4000:],
                    "stderr_tail": stderr[-1500:],
                })
            except subprocess.TimeoutExpired:
                self._send_json(504, {"ok": False, "error": "Fetch timed out after 5 minutes."})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(404, {"ok": False, "error": f"Unknown path: {self.path}"})


def main():
    # Try our default port; if busy, try up to 10 nearby
    for p in range(PORT, PORT + 10):
        try:
            httpd = socketserver.ThreadingTCPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    else:
        print(f"Could not bind to any port in {PORT}..{PORT+9}. Stop other servers and retry.")
        sys.exit(1)
    url = f"http://localhost:{p}/index.html"
    print("──────────────────────────────────────────────────")
    print(f"  Spain Weather Dashboard — local server")
    print(f"  Open: {url}")
    print(f"  Refresh button on the page runs aemet_fetch.py")
    print(f"  Press Ctrl-C to stop this server")
    print("──────────────────────────────────────────────────")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server. Bye.")


if __name__ == "__main__":
    main()

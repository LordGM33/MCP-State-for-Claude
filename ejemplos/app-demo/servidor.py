import os
from http.server import HTTPServer, BaseHTTPRequestHandler

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain"); self.end_headers()
        self.wfile.write(b"app dinamica de ejemplo, viva en su sandbox\n")
    def log_message(self, *a): pass

HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()

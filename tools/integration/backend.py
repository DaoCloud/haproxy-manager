"""HTTP/HTTPS fixture. Only this container holds the TLS private key."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import ssl
import subprocess
import tempfile
import threading


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/certificate":
            body = certificate.read_bytes()
            content_type = "application/x-pem-file"
        else:
            body = json.dumps({"service": "lab-backend",
                               "protocol": self.server.protocol}).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ham-backend-") as directory:
        certificate = Path(directory) / "certificate.pem"
        key = Path(directory) / "private.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3",
            "-subj", "/CN=backend.lab.test", "-addext", "subjectAltName=DNS:backend.lab.test",
            "-keyout", str(key), "-out", str(certificate),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        http = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
        http.protocol = "http"
        https = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
        https.protocol = "https"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        https.socket = context.wrap_socket(https.socket, server_side=True)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        https.serve_forever()

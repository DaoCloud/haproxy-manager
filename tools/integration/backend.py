"""HTTP/HTTPS fixtures. Only this container holds the TLS private keys."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import ssl
import subprocess
import tempfile
import threading


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/certificate", "/monitor-certificate"):
            service = "lab-monitor" if self.path == "/monitor-certificate" else "lab-backend"
            body = certificates[service].read_bytes()
            content_type = "application/x-pem-file"
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "session=fixture; Path=/; HttpOnly")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            result = {"service": self.server.service, "protocol": self.server.protocol}
            if self.path.startswith("/echo"):
                result.update(path=self.path, host=self.headers.get("Host"))
            body = json.dumps(result).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ham-backend-") as directory:
        root = Path(directory)
        certificates = {}
        for service in ("lab-backend", "lab-monitor"):
            certificate = root / (service + ".crt")
            key = root / (service + ".key")
            sans = "DNS:backend.lab.test," + ",".join(
                "IP:" + os.environ[name] for name in ("LAB_BACKEND", "LAB_VIP", "LAB_VIP_2"))
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3",
                "-subj", "/CN=" + service, "-addext", "subjectAltName=" + sans,
                "-keyout", str(key), "-out", str(certificate),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            certificates[service] = certificate
        http = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
        http.protocol, http.service = "http", "lab-backend"
        threading.Thread(target=http.serve_forever, daemon=True).start()
        for port, service in ((9443, "lab-monitor"), (8443, "lab-backend")):
            https = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            https.protocol, https.service = "https", service
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificates[service], root / (service + ".key"))
            https.socket = context.wrap_socket(https.socket, server_side=True)
            if port == 8443:
                https.serve_forever()
            threading.Thread(target=https.serve_forever, daemon=True).start()

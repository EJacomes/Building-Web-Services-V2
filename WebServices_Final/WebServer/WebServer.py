"""
WebServer.py - Origin web server for the "Building Web Services" project.

Runs on the SERVER virtual machine. It serves our own web pages and the
resume from the site/ folder, and it can listen for HTTP and HTTPS at the
same time:

    HTTP  on port 8000
    HTTPS on port 4443  (self-signed certificate, created automatically)

Supported request methods: GET, HEAD, POST, PUT.

Every response carries a Cache-Control header. Each test page gets a
different directive (see CACHE_RULES), so the web cache can be shown
honoring max-age, no-store, no-cache, must-revalidate, private, public and
s-maxage. Responses also carry an ETag and Last-Modified header, and the
server answers conditional requests (If-None-Match / If-Modified-Since)
with "304 Not Modified", which is what the cache uses to revalidate.

Usage:
    python3 WebServer.py            # HTTP (8000) and HTTPS (4443) together
    python3 WebServer.py --http     # HTTP only
    python3 WebServer.py --https    # HTTPS only
"""

import argparse
import email.utils
import hashlib
import mimetypes
import os
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
LISTEN_ADDRESS = '0.0.0.0'   # listen on every network interface of the VM
HTTP_PORT = 8000
HTTPS_PORT = 4443

# All paths are relative to this file, so the server works no matter which
# folder it is started from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(BASE_DIR, 'site')            # our web pages + resume
UPLOAD_DIR = os.path.join(SITE_DIR, 'uploads')       # where PUT saves files
CERT_FILE = os.path.join(BASE_DIR, 'cert.pem')
KEY_FILE = os.path.join(BASE_DIR, 'key.pem')

# Cache-Control header sent for each URL path. Anything not listed here
# (for example files uploaded with PUT) gets DEFAULT_CACHE_RULE.
CACHE_RULES = {
    '/index.html':                 'public, max-age=20',
    '/resume/Resume.pdf':          'public, max-age=60',
    '/pages/max-age.html':         'public, max-age=20',
    '/pages/no-store.html':        'no-store',
    '/pages/no-cache.html':        'no-cache',
    '/pages/must-revalidate.html': 'max-age=10, must-revalidate',
    '/pages/private.html':         'private, max-age=60',
    '/pages/s-maxage.html':        'public, max-age=5, s-maxage=30',
}
DEFAULT_CACHE_RULE = 'public, max-age=20'
NO_STORE = 'no-store'   # used for errors and POST/PUT replies


def ensure_certificate():
    """Creates a self-signed certificate (cert.pem/key.pem) if it is missing."""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    print('No certificate found, creating a self-signed one with openssl...')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', KEY_FILE, '-out', CERT_FILE, '-days', '365',
                    '-subj', '/CN=web-server'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WebServer(BaseHTTPRequestHandler):
    """Handles one HTTP request. A new instance is created for every request."""

    server_version = 'TeamWebServer/1.0'
    protocol_version = 'HTTP/1.1'   # answer as HTTP/1.1 (keep-alive supported)

    # -- helpers ------------------------------------------------------------
    def scheme(self):
        """'HTTPS' or 'HTTP', depending on which listener received the request."""
        return 'HTTPS' if self.server.server_port == HTTPS_PORT else 'HTTP'

    def url_path(self):
        """Request path without the query string; '/' means the homepage."""
        path = self.path.split('?', 1)[0]
        return '/index.html' if path == '/' else path

    def local_file(self, path):
        """
        Maps a URL path to a file inside site/. Returns None if the path tries
        to leave the site folder (path traversal such as /../../etc/passwd).
        """
        full = os.path.realpath(os.path.join(SITE_DIR, path.lstrip('/')))
        if not full.startswith(os.path.realpath(SITE_DIR) + os.sep):
            return None
        return full

    def send_simple(self, code, text, cache_rule=NO_STORE, extra=None):
        """Sends a short plain-text response (used for errors, POST and PUT)."""
        body = (text + '\n').encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache_rule)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def read_body(self):
        """Returns the request body, or None after sending 411/400."""
        length = self.headers.get('Content-Length')
        if length is None:
            self.send_simple(411, 'Error 411: Length Required (no Content-Length header).')
            return None
        try:
            length = int(length)
            if length < 0:
                raise ValueError
        except ValueError:
            self.send_simple(400, 'Error 400: Invalid Content-Length header.')
            return None
        return self.rfile.read(length)

    # -- GET and HEAD -------------------------------------------------------
    def serve_file(self, include_body):
        path = self.url_path()
        filename = self.local_file(path)
        if filename is None or not os.path.isfile(filename):
            self.send_simple(404, f'Error 404: File Not Found ({path})')
            return

        with open(filename, 'rb') as f:
            data = f.read()

        cache_rule = CACHE_RULES.get(path, DEFAULT_CACHE_RULE)
        etag = '"' + hashlib.sha1(data).hexdigest()[:16] + '"'
        mtime = int(os.path.getmtime(filename))
        last_modified = email.utils.formatdate(mtime, usegmt=True)

        # Conditional request: the cache asks "has this changed since my copy?"
        # If the ETag (or date) still matches, answer 304 with no body.
        if_none_match = self.headers.get('If-None-Match')
        if_modified_since = self.headers.get('If-Modified-Since')
        not_modified = False
        if if_none_match is not None:
            not_modified = etag in [t.strip() for t in if_none_match.split(',')]
        elif if_modified_since:
            try:
                since = email.utils.parsedate_to_datetime(if_modified_since).timestamp()
                not_modified = mtime <= since
            except (TypeError, ValueError):
                pass

        if not_modified:
            self.send_response(304, 'Not Modified')
            self.send_header('Cache-Control', cache_rule)
            self.send_header('ETag', etag)
            self.send_header('Last-Modified', last_modified)
            self.end_headers()
            return

        content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        if content_type.startswith('text/'):
            content_type += '; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', cache_rule)
        self.send_header('ETag', etag)
        self.send_header('Last-Modified', last_modified)
        self.end_headers()
        if include_body:            # HEAD sends the same headers, no body
            self.wfile.write(data)

    def do_GET(self):
        self.serve_file(include_body=True)

    def do_HEAD(self):
        self.serve_file(include_body=False)

    # -- POST and PUT -------------------------------------------------------
    def do_POST(self):
        data = self.read_body()
        if data is None:
            return
        print(f'    POST data received: {data.decode("utf-8", errors="replace")}')
        self.send_simple(200, f'Server: POST received ({len(data)} bytes)')

    def do_PUT(self):
        name = os.path.basename(self.url_path())
        if not name:
            self.send_simple(400, 'Error 400: Missing filename in the URL, e.g. PUT /uploads/notes.txt')
            return
        data = self.read_body()
        if data is None:
            return
        # basename() keeps only the file name, so a PUT can never write outside
        # the uploads/ folder (blocks path traversal).
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        with open(os.path.join(UPLOAD_DIR, name), 'wb') as f:
            f.write(data)
        print(f'    PUT saved {len(data)} bytes to site/uploads/{name}')
        self.send_simple(201, f'Created: saved to /uploads/{name}',
                         extra={'Location': f'/uploads/{name}'})

    # -- logging ------------------------------------------------------------
    def log_request(self, code='-', size='-'):
        """One readable line per request: who asked, what, and what we answered."""
        rule = CACHE_RULES.get(self.url_path(), DEFAULT_CACHE_RULE)
        if self.command in ('POST', 'PUT') or str(code) not in ('200', '304'):
            rule = NO_STORE
        via = self.headers.get('X-Forwarded-For') if hasattr(self, 'headers') else None
        source = f'{self.client_address[0]} (web cache, for client {via})' if via else self.client_address[0]
        print(f'[{self.scheme():5}] {self.command:4} {self.path}  from {source}  ->  {code}'
              f'   (Cache-Control: {rule})')

    def log_message(self, fmt, *args):
        # Errors from the base class (e.g. a client disconnecting) go to stderr.
        sys.stderr.write(f'[{self.scheme()}] {self.client_address[0]} {fmt % args}\n')


def start(port, use_https):
    httpd = ThreadingHTTPServer((LISTEN_ADDRESS, port), WebServer)
    if use_https:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f'{"HTTPS" if use_https else "HTTP"} server listening on {LISTEN_ADDRESS}:{port}')
    return httpd


def main():
    parser = argparse.ArgumentParser(description='Origin web server (HTTP and HTTPS).')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--http', action='store_true', help='serve HTTP only (port 8000)')
    group.add_argument('--https', action='store_true', help='serve HTTPS only (port 4443)')
    args = parser.parse_args()
    run_http = not args.https
    run_https = not args.http

    servers = []
    if run_https:
        ensure_certificate()
        servers.append(start(HTTPS_PORT, use_https=True))
    if run_http:
        servers.append(start(HTTP_PORT, use_https=False))
    print('Press Ctrl+C to stop.\n')

    try:
        threading.Event().wait()   # keep the main thread alive
    except KeyboardInterrupt:
        print('\nShutting down.')
        for s in servers:
            s.shutdown()


if __name__ == '__main__':
    main()

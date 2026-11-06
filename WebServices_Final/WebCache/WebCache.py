"""
WebCache.py - Caching proxy for the "Building Web Services" project.

Runs on the CACHE virtual machine, between the client VM and the server VM:

    client VM  --->  web cache VM  --->  web server VM
               HTTP  port 8080        HTTP  port 8000
               HTTPS port 8443        HTTPS port 4443

The client sends its request to the cache. If the cache has a fresh copy it
answers by itself (HIT). Otherwise it forwards the request to the origin
server, sends the answer back to the client, and stores it if the server's
Cache-Control header allows it.

Requirement 4 (cache control): caching can be turned on or off separately
for HTTP and HTTPS, either at startup (--http-cache off, --https-cache off)
or while it is running by typing a command in this terminal:
    http on | http off | https on | https off | status | list | clear | help
When caching is off for a protocol, the cache still forwards the traffic but
never stores or reuses anything (X-Cache: BYPASS).

Requirement 5 (cache directives) - honored from the server's response:
    max-age=N        fresh for N seconds, then fetched or revalidated again
    s-maxage=N       overrides max-age for shared caches like this one
    no-store         never stored
    no-cache         stored, but revalidated with the server before every use
    must-revalidate  once stale, never served without revalidating; if the
                     server cannot be reached the cache answers 504
    private          never stored, because this is a shared cache
    public           may be stored by a shared cache
and from the client's request:
    no-cache         skip the stored copy and revalidate with the server
    no-store         do not store this response

Every response the cache sends back gets an "X-Cache" header (HIT, MISS,
REVALIDATED, STALE or BYPASS) and an "Age" header on cached answers, so the
client can see what the cache did.

Usage:
    python3 WebCache.py --origin <SERVER_VM_IP>
    python3 WebCache.py --origin <SERVER_VM_IP> --http-cache off --https-cache on
"""

import argparse
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
LISTEN_ADDRESS = '0.0.0.0'     # accept clients on every interface of the VM
CACHE_PORT_HTTP = 8080         # clients connect here for HTTP
CACHE_PORT_HTTPS = 8443        # clients connect here for HTTPS
ORIGIN_PORT_HTTP = 8000        # WebServer.py HTTP port
ORIGIN_PORT_HTTPS = 4443       # WebServer.py HTTPS port
ORIGIN_TIMEOUT = 5             # seconds to wait for the origin server

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'cache_storage')       # stored responses
METADATA_FILE = os.path.join(CACHE_DIR, 'cache_metadata.json')
CERT_FILE = os.path.join(BASE_DIR, 'cert.pem')             # for port 8443
KEY_FILE = os.path.join(BASE_DIR, 'key.pem')

# Set from the command line in main()
ORIGIN_IP = '127.0.0.1'
CACHING_ON = {'HTTP': True, 'HTTPS': True}   # requirement 4: on/off switch

lock = threading.Lock()   # protects the cache index and the console output
index = {}                # cache key -> metadata of the stored response


def log(*lines):
    """Prints a block of lines without other threads cutting in."""
    with lock:
        for line in lines:
            print(line)
        sys.stdout.flush()


# ----------------------------------------------------------------------------
# Reading and parsing HTTP messages
# ----------------------------------------------------------------------------
def recv_http_message(sock, has_body=True):
    """
    Reads one complete HTTP message: the headers, then exactly Content-Length
    bytes of body. If there is no Content-Length, a response is read until the
    sender closes the connection. Reading the whole message (not just the first
    4096 bytes) is what lets the 209 KB resume PDF pass through the cache.
    """
    data = b''
    while b'\r\n\r\n' not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return data
        data += chunk
    header_end = data.index(b'\r\n\r\n') + 4
    if not has_body:
        return data[:header_end]
    match = re.search(rb'(?im)^content-length:\s*(\d+)', data[:header_end])
    if match:
        total = header_end + int(match.group(1))
        while len(data) < total:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return data[:total]
    if data[:5] == b'HTTP/':          # a response with no length: read to close
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    return data


def split_message(raw):
    """Returns (first_line, headers_dict_lowercase, header_lines, body)."""
    head, _, body = raw.partition(b'\r\n\r\n')
    lines = head.decode('iso-8859-1').split('\r\n')
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            name, value = line.split(':', 1)
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers, lines[1:], body


def parse_cache_control(value):
    """'public, max-age=20' -> {'public': None, 'max-age': '20'}"""
    directives = {}
    for part in (value or '').split(','):
        part = part.strip().lower()
        if not part:
            continue
        name, _, arg = part.partition('=')
        directives[name.strip()] = arg.strip().strip('"') or None
    return directives


def status_of(raw):
    try:
        return int(raw.split(b' ', 2)[1])
    except (IndexError, ValueError):
        return 0


def error_response(code, reason, text):
    """Builds a small plain-text error response with a correct Content-Length."""
    body = (text + '\n').encode('utf-8')
    return (f'HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n'
            f'Content-Length: {len(body)}\r\nCache-Control: no-store\r\n\r\n').encode() + body


def add_headers(raw, extra):
    """Inserts extra headers (dict) into a raw HTTP response."""
    head, sep, body = raw.partition(b'\r\n\r\n')
    added = ''.join(f'\r\n{k}: {v}' for k, v in extra.items()).encode('iso-8859-1')
    return head + added + sep + body


# ----------------------------------------------------------------------------
# Talking to the origin server
# ----------------------------------------------------------------------------
def build_origin_request(first_line, header_lines, body, client_ip, conditional=None):
    """
    Rebuilds the client's request for the origin server. Adds Via and
    X-Forwarded-For so the server log shows the request came through the cache
    for this client, and adds If-None-Match / If-Modified-Since when the cache
    is revalidating a stored copy.
    """
    skip = {'connection', 'proxy-connection', 'keep-alive'}
    if conditional is not None:
        skip |= {'if-none-match', 'if-modified-since'}
    lines = [first_line]
    for line in header_lines:
        if line.split(':', 1)[0].strip().lower() not in skip:
            lines.append(line)
    lines.append('Via: 1.1 web-cache')
    lines.append(f'X-Forwarded-For: {client_ip}')
    for name, value in (conditional or {}).items():
        lines.append(f'{name}: {value}')
    lines.append('Connection: close')
    return ('\r\n'.join(lines) + '\r\n\r\n').encode('iso-8859-1') + body


def ask_origin(scheme, request_bytes, method):
    """Sends a request to the origin server and returns the full raw response."""
    port = ORIGIN_PORT_HTTPS if scheme == 'HTTPS' else ORIGIN_PORT_HTTP
    with socket.create_connection((ORIGIN_IP, port), timeout=ORIGIN_TIMEOUT) as sock:
        if scheme == 'HTTPS':
            # The origin uses a self-signed certificate, so it is not verified.
            context = ssl._create_unverified_context()
            with context.wrap_socket(sock, server_hostname=ORIGIN_IP) as tls:
                tls.sendall(request_bytes)
                return recv_http_message(tls, has_body=(method != 'HEAD'))
        sock.sendall(request_bytes)
        return recv_http_message(sock, has_body=(method != 'HEAD'))


# ----------------------------------------------------------------------------
# The cache itself
# ----------------------------------------------------------------------------
def cache_key(scheme, target):
    return hashlib.md5(f'{scheme} GET {target}'.encode()).hexdigest()


def save_index():
    with open(METADATA_FILE, 'w') as f:
        json.dump(index, f, indent=2)


def load_index():
    global index
    try:
        with open(METADATA_FILE) as f:
            index = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        index = {}


def storage_decision(response, request_cc):
    """
    Decides whether a response may be stored, based on the Cache-Control
    directives. Returns (store, reason, metadata).
    """
    if status_of(response) != 200:
        return False, f'status {status_of(response)} is not cacheable', None
    _, headers, _, _ = split_message(response)
    cc = parse_cache_control(headers.get('cache-control'))
    if 'no-store' in request_cc:
        return False, 'client sent Cache-Control: no-store', None
    if 'no-store' in cc:
        return False, 'no-store: the server forbids storing it', None
    if 'private' in cc:
        return False, 'private: only for one user, a shared cache must not store it', None

    if 's-maxage' in cc:
        lifetime = int(cc['s-maxage'])
        why = f's-maxage={lifetime} (overrides max-age for a shared cache)'
    elif 'max-age' in cc:
        lifetime = int(cc['max-age'])
        why = f'max-age={lifetime}'
    else:
        lifetime = 0
        why = 'no max-age, so it is revalidated every time'
    meta = {
        'lifetime': lifetime,
        'no_cache': 'no-cache' in cc,
        'must_revalidate': 'must-revalidate' in cc or 'proxy-revalidate' in cc,
        'etag': headers.get('etag'),
        'last_modified': headers.get('last-modified'),
        'cache_control': headers.get('cache-control', ''),
    }
    if meta['no_cache']:
        why = 'no-cache: stored, but must be revalidated before every use'
    elif meta['must_revalidate']:
        why += ', must-revalidate: never served stale'
    if 'public' in cc:
        why += ', public'
    return True, f'stored ({why})', meta


def store(key, scheme, target, response, meta):
    with open(os.path.join(CACHE_DIR, key), 'wb') as f:
        f.write(response)
    meta.update({'url': f'{scheme.lower()}://{target}', 'stored_at': time.time()})
    with lock:
        index[key] = meta
        save_index()


def cached_response(key):
    path = os.path.join(CACHE_DIR, key)
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return f.read()


def handle_get(scheme, first_line, header_lines, req_headers, body, client_ip, target, notes):
    """GET with caching on. Returns (response_bytes, x_cache_value, extra_headers)."""
    request_cc = parse_cache_control(req_headers.get('cache-control'))
    if 'no-cache' in (req_headers.get('pragma') or '').lower():
        request_cc['no-cache'] = None

    key = cache_key(scheme, target)
    with lock:
        meta = dict(index[key]) if key in index else None
    stored = cached_response(key) if meta else None

    if meta and stored:
        age = int(time.time() - meta['stored_at'])
        fresh = (age < meta['lifetime'] and not meta['no_cache']
                 and 'no-cache' not in request_cc)
        if fresh:
            notes.append(f'cached copy is fresh (age {age}s of {meta["lifetime"]}s) -> answering from the cache')
            return stored, 'HIT', {'Age': str(age)}

        # Stale, no-cache, or the client asked for no-cache: revalidate.
        if meta['no_cache']:
            notes.append('cached copy has no-cache -> must ask the server first')
        elif 'no-cache' in request_cc:
            notes.append('client sent Cache-Control: no-cache -> asking the server first')
        else:
            notes.append(f'cached copy is stale (age {age}s, limit {meta["lifetime"]}s) -> revalidating')
        conditional = {}
        if meta.get('etag'):
            conditional['If-None-Match'] = meta['etag']
        if meta.get('last_modified'):
            conditional['If-Modified-Since'] = meta['last_modified']
        notes.append(f'conditional request to origin {ORIGIN_IP} with '
                     + ', '.join(f'{k}: {v}' for k, v in conditional.items()))
        try:
            response = ask_origin(scheme, build_origin_request(
                first_line, header_lines, body, client_ip, conditional), 'GET')
        except OSError as e:
            if meta['must_revalidate'] or meta['no_cache']:
                notes.append(f'origin unreachable ({e}); must-revalidate forbids a stale copy -> 504')
                return error_response(504, 'Gateway Timeout',
                                      'Error 504: the server is unreachable and this page has '
                                      'must-revalidate, so a stale copy cannot be used.'), 'MISS', {}
            notes.append(f'origin unreachable ({e}) -> serving the stale copy with a warning')
            return stored, 'STALE', {'Age': str(age), 'Warning': '110 web-cache "Response is Stale"'}

        if status_of(response) == 304:
            notes.append('origin answered 304 Not Modified -> stored copy is still valid, sending it')
            with lock:
                if key in index:
                    index[key]['stored_at'] = time.time()
                    save_index()
            return stored, 'REVALIDATED', {'Age': '0'}
        notes.append(f'origin sent a new copy ({status_of(response)})')
    else:
        notes.append(f'not in the cache -> forwarding to origin {ORIGIN_IP}')
        try:
            response = ask_origin(scheme, build_origin_request(
                first_line, header_lines, body, client_ip), 'GET')
        except OSError as e:
            notes.append(f'origin unreachable ({e}) -> 502')
            return error_response(502, 'Bad Gateway', 'Error 502: the server is unreachable.'), 'MISS', {}

    ok, reason, new_meta = storage_decision(response, request_cc)
    notes.append(('cache: ' if ok else 'not stored: ') + reason)
    if ok:
        store(key, scheme, target, response, new_meta)
    return response, 'MISS', {}


def handle_client(conn, client_ip, scheme):
    try:
        request = recv_http_message(conn, has_body=True)
        if not request:
            return
        first_line, headers, header_lines, body = split_message(request)
        try:
            method, target, _ = first_line.split(' ', 2)
        except ValueError:
            return
        method = method.upper()
        host = headers.get('host', '')
        notes = []

        if not CACHING_ON[scheme]:
            notes.append(f'caching is OFF for {scheme} -> forwarding to origin {ORIGIN_IP}, nothing stored')
            x_cache, extra = 'BYPASS', {}
            try:
                response = ask_origin(scheme, build_origin_request(
                    first_line, header_lines, body, client_ip), method)
            except OSError as e:
                notes.append(f'origin unreachable ({e}) -> 502')
                response = error_response(502, 'Bad Gateway', 'Error 502: the server is unreachable.')
        elif method == 'GET':
            response, x_cache, extra = handle_get(scheme, first_line, header_lines, headers,
                                                  body, client_ip, host + target, notes)
        else:
            notes.append(f'{method} is never cached -> forwarding to origin {ORIGIN_IP}')
            x_cache, extra = 'BYPASS', {}
            try:
                response = ask_origin(scheme, build_origin_request(
                    first_line, header_lines, body, client_ip), method)
            except OSError as e:
                notes.append(f'origin unreachable ({e}) -> 502')
                response = error_response(502, 'Bad Gateway', 'Error 502: the server is unreachable.')

        extra = dict(extra)
        extra['X-Cache'] = x_cache
        response = add_headers(response, extra)
        conn.sendall(response)

        status_line = response.split(b'\r\n', 1)[0].decode('iso-8859-1')
        log(f'[{scheme:5}] {client_ip:15} {method:4} {target}',
            *[f'        {n}' for n in notes],
            f'        -> sent "{status_line}" to {client_ip}  (X-Cache: {x_cache})', '')
    except Exception as e:
        log(f'[{scheme}] error with client {client_ip}: {e}', '')
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Listeners and the on/off console
# ----------------------------------------------------------------------------
def ensure_certificate():
    """Creates a self-signed certificate for the cache's HTTPS port if missing."""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    print('No certificate found, creating a self-signed one with openssl...')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', KEY_FILE, '-out', CERT_FILE, '-days', '365',
                    '-subj', '/CN=web-cache'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def listen(port, scheme):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_ADDRESS, port))
    server.listen(20)
    context = None
    if scheme == 'HTTPS':
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERT_FILE, KEY_FILE)

    def loop():
        while True:
            conn, addr = server.accept()
            if context is not None:
                try:
                    conn.settimeout(10)
                    conn = context.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError) as e:
                    log(f'[HTTPS] TLS handshake with {addr[0]} failed: {e}', '')
                    conn.close()
                    continue
            threading.Thread(target=handle_client, args=(conn, addr[0], scheme),
                             daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()


def print_status():
    log(f'Caching for HTTP  (port {CACHE_PORT_HTTP}):  {"ON" if CACHING_ON["HTTP"] else "OFF"}',
        f'Caching for HTTPS (port {CACHE_PORT_HTTPS}): {"ON" if CACHING_ON["HTTPS"] else "OFF"}',
        f'Origin server: {ORIGIN_IP} (HTTP {ORIGIN_PORT_HTTP}, HTTPS {ORIGIN_PORT_HTTPS})',
        f'Stored responses: {len(index)}', '')


def console():
    """Reads on/off commands typed into this terminal while the cache runs."""
    help_text = ('Commands: http on | http off | https on | https off | '
                 'status | list | clear | help')
    log(help_text, '')
    while True:
        try:
            command = input().strip().lower()
        except EOFError:              # no keyboard (e.g. started in background)
            threading.Event().wait()
        parts = command.split()
        if len(parts) == 2 and parts[0] in ('http', 'https') and parts[1] in ('on', 'off'):
            CACHING_ON[parts[0].upper()] = parts[1] == 'on'
            log(f'>>> Caching for {parts[0].upper()} is now {parts[1].upper()}')
            print_status()
        elif command == 'status':
            print_status()
        elif command == 'list':
            with lock:
                rows = [f'  {m["url"]}  age {int(time.time() - m["stored_at"])}s, '
                        f'limit {m["lifetime"]}s, Cache-Control: {m["cache_control"]}'
                        for m in index.values()] or ['  (empty)']
            log('Stored responses:', *rows, '')
        elif command == 'clear':
            with lock:
                for key in list(index):
                    try:
                        os.remove(os.path.join(CACHE_DIR, key))
                    except FileNotFoundError:
                        pass
                index.clear()
                save_index()
            log('>>> Cache cleared.', '')
        elif command:
            log(help_text, '')


def main():
    global ORIGIN_IP
    parser = argparse.ArgumentParser(description='Caching proxy between the client and the web server.')
    parser.add_argument('--origin', default='127.0.0.1',
                        help='IP address of the web server VM (default 127.0.0.1)')
    parser.add_argument('--http-cache', choices=['on', 'off'], default='on',
                        help='turn caching on or off for HTTP traffic')
    parser.add_argument('--https-cache', choices=['on', 'off'], default='on',
                        help='turn caching on or off for HTTPS traffic')
    args = parser.parse_args()
    ORIGIN_IP = args.origin
    CACHING_ON['HTTP'] = args.http_cache == 'on'
    CACHING_ON['HTTPS'] = args.https_cache == 'on'

    os.makedirs(CACHE_DIR, exist_ok=True)
    load_index()
    ensure_certificate()
    listen(CACHE_PORT_HTTP, 'HTTP')
    listen(CACHE_PORT_HTTPS, 'HTTPS')
    print(f'Web cache listening on {LISTEN_ADDRESS}: HTTP port {CACHE_PORT_HTTP}, HTTPS port {CACHE_PORT_HTTPS}')
    print_status()
    try:
        console()
    except KeyboardInterrupt:
        print('\nCache stopping.')


if __name__ == '__main__':
    main()

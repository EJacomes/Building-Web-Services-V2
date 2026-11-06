"""
WebClient.py - Command-line HTTP/HTTPS client for the "Building Web Services"
project.

Runs on the CLIENT virtual machine. It can send GET, POST, HEAD and PUT
requests (requirement 3) over HTTP or HTTPS, either directly to the web
server or through the web cache, and prints the full response: status line,
every header (including the cache's X-Cache and Age headers) and the body.

Usage:
    python3 WebClient.py GET  http://<CACHE_IP>:8080/
    python3 WebClient.py GET  https://<CACHE_IP>:8443/ -k
    python3 WebClient.py HEAD https://<SERVER_IP>:4443/ -k
    python3 WebClient.py POST http://<CACHE_IP>:8080/ '{"name": "Ourania"}'
    python3 WebClient.py PUT  http://<CACHE_IP>:8080/uploads/notes.txt 'hello'
    python3 WebClient.py GET  http://<CACHE_IP>:8080/pages/max-age.html -H "Cache-Control: no-cache"

Options:
    -k / --insecure   accept the self-signed HTTPS certificates of our VMs
    -H / --header     add a request header (can be used more than once)
"""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request


class Client:
    def __init__(self, insecure=False):
        # Self-signed certificates on our VMs need verification turned off.
        if insecure:
            self.ssl_context = ssl._create_unverified_context()
            print("(!) Warning: SSL certificate verification disabled (-k).", file=sys.stderr)
        else:
            self.ssl_context = ssl.create_default_context()
        # ProxyHandler({}) ignores any system proxy settings, so requests always
        # go straight to the VM we name in the URL.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=self.ssl_context))

    def print_response_details(self, response, method):
        print(f"\n<- Response: {response.status} {response.reason}")
        print("--- Headers ---")
        for header, value in response.getheaders():
            print(f"{header}: {value}")
        print("--- Body ---")
        body = response.read()
        if method == 'HEAD':
            print("(no body: HEAD returns headers only)")
            return
        content_type = response.getheader('Content-Type', '')
        if not body:
            print("(empty)")
        elif 'application/json' in content_type:
            print(json.dumps(json.loads(body), indent=2))
        elif content_type.startswith('text/'):
            print(body.decode('utf-8', errors='replace'))
        else:
            # Binary files such as the resume PDF are summarized, not printed.
            print(f"[{len(body)} bytes of {content_type or 'binary data'}]")

    def request(self, method, url, data=None, headers=None):
        headers = dict(headers or {})
        data_bytes = None
        if data is not None:
            if isinstance(data, (dict, list)):
                data_bytes = json.dumps(data).encode('utf-8')
                headers.setdefault('Content-Type', 'application/json')
            else:
                data_bytes = str(data).encode('utf-8')
                headers.setdefault('Content-Type', 'text/plain')

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        print(f"-> Sending {method} request to {url}")
        for name, value in headers.items():
            print(f"   {name}: {value}")
        if data is not None:
            print(f"   with data: {data if isinstance(data, str) else json.dumps(data)}")

        try:
            with self.opener.open(req, timeout=15) as response:
                self.print_response_details(response, method)
            return True
        except urllib.error.HTTPError as e:
            # Error statuses (404, 411, 502, 504...) still have headers and a body.
            print(f"\n<- Response: {e.code} {e.reason}")
            print("--- Headers ---")
            for header, value in e.headers.items():
                print(f"{header}: {value}")
            body = e.read().decode('utf-8', errors='replace')
            print("--- Body ---")
            print(body or "(empty)")
            print(f"(!) HTTP Error: {e.code} {e.reason}", file=sys.stderr)
        except urllib.error.URLError as e:
            print(f"(!) Could not connect: {e.reason}", file=sys.stderr)
        except Exception as e:
            print(f"(!) Unexpected error: {e}", file=sys.stderr)
        return False


def parse_headers(values):
    headers = {}
    for item in values or []:
        if ':' not in item:
            sys.exit(f'Bad header "{item}". Use the form -H "Name: value".')
        name, value = item.split(':', 1)
        headers[name.strip()] = value.strip()
    return headers


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A simple Python HTTP client for web service testing.")
    parser.add_argument("method", type=str.upper, choices=['GET', 'POST', 'HEAD', 'PUT'],
                        help="The HTTP method to use.")
    parser.add_argument("url", help="The full URL (http:// or https://) to request.")
    parser.add_argument("data", nargs='?', help="Data for POST or PUT requests (text or JSON).")
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="Allow HTTPS servers with self-signed certificates.")
    parser.add_argument("-H", "--header", action="append",
                        help='Extra request header, e.g. -H "Cache-Control: no-cache"')
    args = parser.parse_args()

    data = args.data
    if data and data[:1] in ('{', '['):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            pass
    if args.method in ('POST', 'PUT') and data is None:
        parser.error(f"Data is required for {args.method} requests.")

    client = Client(insecure=args.insecure)
    ok = client.request(args.method, args.url, data, parse_headers(args.header))
    sys.exit(0 if ok else 1)

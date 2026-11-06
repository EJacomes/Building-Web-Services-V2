# Building Web Services

Team: Ourania Rahman and Erick Jacomes

A working web service environment on a virtual network of three Ubuntu
virtual machines: a **client**, a **web cache**, and a **web server**. All
three programs are written in Python 3 using only the standard library.

```
 web-client VM                web-cache VM                  web-server VM
 WebClient.py   --------->    WebCache.py    --------->     WebServer.py
                HTTP  :8080                  HTTP  :8000
                HTTPS :8443                  HTTPS :4443
```

| Folder | Program | Runs on | What it does |
|---|---|---|---|
| `WebServer/` | `WebServer.py` | web-server VM | Hosts our web pages and both team members' resumes (`site/`). Serves HTTP (8000) and HTTPS (4443) at the same time. Supports GET, HEAD, POST, PUT, ETag and 304 Not Modified. |
| `WebCache/` | `WebCache.py` | web-cache VM | Caching proxy. Accepts HTTP (8080) and HTTPS (8443). Caching can be switched on or off separately for HTTP and HTTPS. Honors 7 cache directives. |
| `WebClient/` | `WebClient.py` | web-client VM | Command-line client that sends GET, HEAD, POST and PUT over HTTP or HTTPS and prints the full response. |

## How the project meets each requirement

| # | Requirement | How it is met |
|---|---|---|
| 1 | Three Linux VMs: client, web server, web cache; server hosts our pages and resumes | Three Ubuntu 24.04 VMs (`web-client`, `web-cache`, `web-server`) on one virtual network. `WebServer/site/` holds our homepage, six test pages, and both resumes in `resume/` (Ourania_Rahman_Resume.pdf and Erick's Resume.pdf). |
| 2 | HTTP and HTTPS | The server listens on HTTP 8000 and HTTPS 4443 at the same time (`--http` or `--https` to run only one). The cache accepts HTTP on 8080 and HTTPS on 8443 and talks to the server with the same protocol. Self-signed certificates are created automatically. |
| 3 | GET, POST, HEAD, PUT | `WebClient.py GET/POST/HEAD/PUT <url> [data]`, works directly against the server or through the cache. |
| 4 | Cache on/off for HTTP and HTTPS | Startup flags `--http-cache on/off` and `--https-cache on/off`, or type `http off`, `http on`, `https off`, `https on` in the cache terminal while it runs. When off, traffic is forwarded with `X-Cache: BYPASS` and nothing is stored. |
| 5 | At least five cache directives | `max-age`, `s-maxage`, `no-store`, `no-cache`, `must-revalidate`, `private`, `public` from the server, plus `no-cache` and `no-store` sent by the client. Each has its own test page. |
| 6 | Live demo | See `DEMO_GUIDE` (separate document). |

## Cache directive test pages

| Page | Cache-Control sent by the server | What the cache does |
|---|---|---|
| `/` (index.html) | `public, max-age=20` | HIT for 20 s, then revalidates |
| `/pages/max-age.html` | `public, max-age=20` | HIT for 20 s, then revalidates (304) |
| `/pages/s-maxage.html` | `public, max-age=5, s-maxage=30` | s-maxage overrides max-age in a shared cache: HIT for 30 s |
| `/pages/no-store.html` | `no-store` | never stored: MISS every time |
| `/pages/no-cache.html` | `no-cache` | stored, but revalidated before every use: REVALIDATED |
| `/pages/must-revalidate.html` | `max-age=10, must-revalidate` | HIT for 10 s; when stale and the server is down: 504, never a stale copy |
| `/pages/private.html` | `private, max-age=60` | never stored by a shared cache: MISS every time |
| `/resume/Resume.pdf` (Erick) | `public, max-age=60` | HIT for 60 s |
| `/resume/Ourania_Rahman_Resume.pdf` | `public, max-age=20` | HIT for 20 s |

Errors (404, 411), POST and PUT replies are sent with `no-store`.

The cache adds `X-Cache: HIT | MISS | REVALIDATED | STALE | BYPASS` and
`Age` to every response so the client shows what happened. When a page
without `must-revalidate` is stale and the server is unreachable, the cache
serves the stale copy with `Warning: 110` (X-Cache: STALE).

## Step-by-step build guide

### 1. Create the three Ubuntu VMs (on an Ubuntu host with Multipass)

```bash
sudo snap install multipass
multipass launch 24.04 --name web-server
multipass launch 24.04 --name web-cache
multipass launch 24.04 --name web-client
multipass list            # shows each VM's IPv4 address
```

Write down the IP addresses. Below they are called `SERVER_IP`, `CACHE_IP`
and `CLIENT_IP`.

### 2. Copy the project into each VM

From the host, in the folder that contains `WebServices_Final.zip`:

```bash
for vm in web-server web-cache web-client; do multipass transfer WebServices_Final.zip $vm:/home/ubuntu/ ; done
```

Then inside each VM (`multipass shell <name>`):

```bash
python3 -m zipfile -e WebServices_Final.zip ~/project
cd ~/project
```

### 3. Start the web server (web-server VM)

```bash
cd ~/project/WebServer
python3 WebServer.py
```

The first run creates `cert.pem` and `key.pem` with openssl. Expected:
`HTTPS server listening on 0.0.0.0:4443` and `HTTP server listening on 0.0.0.0:8000`.

### 4. Start the web cache (web-cache VM)

```bash
cd ~/project/WebCache
python3 WebCache.py --origin SERVER_IP
```

Expected: `Web cache listening on 0.0.0.0: HTTP port 8080, HTTPS port 8443`
and `Caching for HTTP ... ON`, `Caching for HTTPS ... ON`.

### 5. Send requests (web-client VM)

```bash
cd ~/project/WebClient
python3 WebClient.py GET  http://CACHE_IP:8080/
python3 WebClient.py GET  https://CACHE_IP:8443/ -k
python3 WebClient.py HEAD https://SERVER_IP:4443/ -k
python3 WebClient.py POST http://CACHE_IP:8080/ '{"name": "Ourania"}'
python3 WebClient.py PUT  http://CACHE_IP:8080/uploads/notes.txt 'hello from the client VM'
```

`-k` accepts our self-signed certificates. `-H "Name: value"` adds a request
header, for example `-H "Cache-Control: no-cache"`.

### 6. Watch the network traffic (web-cache VM, second terminal)

```bash
sudo tcpdump -i any -nn 'port 8080 or port 8443 or port 8000 or port 4443'
```

## Files created while running

- `WebServer/cert.pem`, `WebServer/key.pem`, `WebCache/cert.pem`, `WebCache/key.pem`: self-signed certificates (never commit the keys)
- `WebCache/cache_storage/`: stored responses and `cache_metadata.json`
- `WebServer/site/uploads/`: files saved by PUT

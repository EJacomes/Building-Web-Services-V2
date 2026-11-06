# Statement of Work: who did which tasks

Team: Ourania Rahman and Erick Jacomes

> DRAFT: both team members should read this, correct anything that is not
> accurate, and agree on it before it is submitted with the final report.

## Erick Jacomes

- Wrote the first versions of the three programs: the web server
  (`WebServer.py`), the web cache (`WebCache.py`) and the web client
  (`WebClient.py`).
- Created the team GitHub repository and the original resume webpage.
- Presents the three-VM setup, the web server, HTTP/HTTPS, and the client's
  request methods in the live demo.

## Ourania Rahman

- Tested all three programs together and found and fixed 11 bugs (see the
  progress report of November 3, 2026), including the cache's wrong origin
  port, the 4,096-byte read limit that broke the resume PDF, caching of POST,
  PUT and error responses, missing expiration, the resume path, and the
  server crash on POST without Content-Length.
- Made the server multithreaded, made HEAD return the same headers as GET,
  blocked path traversal in PUT, and wrote the README and `.gitignore`.
- Built the remaining requirements for the final version:
  - the server serving HTTP (8000) and HTTPS (4443) at the same time, with
    ETag/Last-Modified and 304 Not Modified;
  - the cache accepting HTTPS (8443) as well as HTTP (8080), with an on/off
    switch for each protocol (startup flags and live commands);
  - seven cache directives (max-age, s-maxage, no-store, no-cache,
    must-revalidate, private, public) plus client-side no-cache/no-store,
    with a test page for each;
  - client support for custom request headers and binary responses.
- Wrote the step-by-step build guide and the demo guide, and tested the system
  on a three-machine network.
- Presents the cache on/off switch, the cache directives and the packet
  capture in the live demo.

## Tools

As the project rules allow, Claude (Anthropic) was used to help test the
programs, find and fix bugs, and write and test the final features. All code
was reviewed, run and demonstrated by the team.

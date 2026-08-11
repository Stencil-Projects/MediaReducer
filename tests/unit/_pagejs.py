"""What the browser actually ends up with for a page: its markup, plus the
script files that markup pulls in.

A test that greps a rendered page for a JavaScript marker used to work because
the JavaScript WAS the page — every line of it sat inside a <script> tag in the
template. It does not any more: the templates carry a short preamble of
server-rendered values and a <script src> for the rest. Grepping the HTML alone
finds the values and none of the code, so a whole class of check reads as a
regression the moment it is asked about behaviour that lives in a file.

`page(client, "/config")` puts it back together. It follows only this app's own
scripts — a marker is never being looked for inside Bootstrap, and 233 KB of
minified vendor code in the haystack is a false match waiting to happen.
"""
import re

_SRC = re.compile(r"<script[^>]*\bsrc=\"(/static/[^\"]+)\"")


def page(client, path: str) -> str:
    """The page at `path` followed by the source of every app script it loads."""
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    html = r.get_data(as_text=True)
    parts = [html]
    for src in _SRC.findall(html):
        if not src.startswith("/static/js/"):
            continue                      # vendored code is not ours to grep
        js = client.get(src)
        assert js.status_code == 200, (src, js.status_code)
        parts.append(js.get_data(as_text=True))
    return "\n".join(parts)

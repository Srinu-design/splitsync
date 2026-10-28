import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


def test_js_files_safety():
    """static/*.js must contain none of the unsafe DOM / eval constructs or absolute URLs."""
    js_files = list(STATIC_DIR.glob("*.js"))
    assert len(js_files) >= 2, f"Expected at least app.js and graph.js in {STATIC_DIR}"

    forbidden_patterns = [
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "setAttribute('style'",
        'setAttribute("style"',
    ]

    for js_file in js_files:
        content = js_file.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert pattern not in content, f"Forbidden '{pattern}' found in {js_file.name}"

        # Check for crypto.randomUUID without fallback
        if "crypto.randomUUID" in content:
            pytest.fail(f"crypto.randomUUID found in {js_file.name} without fallback")

        # Check for absolute "/api/", "/ws/", "/r/" string literals
        # Matches quotes followed by /api/, /ws/, or /r/
        abs_url_match = re.search(r"""['"`]/(?:api|ws|r|static)/""", content)
        assert abs_url_match is None, (
            f"Absolute URL pattern found in {js_file.name}: {abs_url_match.group(0)}"
        )


def test_html_files_safety():
    """static/*.html must contain no inline <script>, no <style>, no style=, no on*= handlers."""
    html_files = list(STATIC_DIR.glob("*.html"))
    assert len(html_files) >= 2, f"Expected at least index.html and room.html in {STATIC_DIR}"

    for html_file in html_files:
        content = html_file.read_text(encoding="utf-8")

        # No <style> tags
        assert not re.search(r"<style\b", content, re.IGNORECASE), (
            f"Inline <style> tag found in {html_file.name}"
        )

        # No style= attributes
        assert not re.search(r"""\bstyle\s*=\s*['"]""", content, re.IGNORECASE), (
            f"Inline style attribute found in {html_file.name}"
        )

        # No on*= event handlers (e.g. onclick, onload, etc.)
        assert not re.search(r"""\bon[a-z]+\s*=\s*['"]""", content, re.IGNORECASE), (
            f"Inline event handler found in {html_file.name}"
        )

        # No <script> tags without src
        # Check all <script ...> tags
        script_tags = re.findall(r"<script\b([^>]*)>", content, re.IGNORECASE)
        for attrs in script_tags:
            assert re.search(r"""\bsrc\s*=\s*['"]""", attrs, re.IGNORECASE), (
                f"Inline script without src found in {html_file.name}: <script {attrs}>"
            )


class Markup(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.tags = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.mark.parametrize("filename", ["index.html", "room.html"])
def test_redesign_markup_assets_and_metadata(filename):
    page = Markup((STATIC_DIR / filename).read_text())
    ids = [attrs["id"] for _, attrs in page.tags if "id" in attrs]
    assert len(ids) == len(set(ids)), "Duplicate IDs break labels and app event handlers"
    if filename == "index.html":
        assert {"about", "how-it-works", "try"} <= set(ids)
    prefix = "static/" if filename == "index.html" else "../static/"
    scripts = [attrs["src"] for tag, attrs in page.tags if tag == "script"]
    assert scripts[0] == prefix + "theme.js"
    assert any(
        tag == "link" and attrs.get("href") == prefix + "favicon.svg" for tag, attrs in page.tags
    )
    meta = {
        attrs.get("name", attrs.get("property")): attrs.get("content")
        for tag, attrs in page.tags
        if tag == "meta"
    }
    assert all(meta.get(name) for name in ("description", "og:title", "og:description"))
    for tag, attrs in page.tags:
        for key in ("src", "href"):
            assert not re.match(r"/(?:api|ws|r|static)/", attrs.get(key, ""))
        if tag == "script" or (tag == "link" and attrs.get("rel") == "stylesheet"):
            assert not attrs.get("src", attrs.get("href", "")).startswith(("http", "//"))


def test_theme_and_favicon_are_local_and_safe():
    theme = (STATIC_DIR / "theme.js").read_text()
    assert "splitsync.theme" in theme
    assert "prefers-color-scheme: dark" in theme
    favicon = ET.parse(STATIC_DIR / "favicon.svg").getroot()
    assert favicon.tag == "{http://www.w3.org/2000/svg}svg"
    for element in favicon.iter():
        assert element.tag.rsplit("}", 1)[-1] not in {"script", "style", "foreignObject"}
        assert all(not key.lower().startswith("on") and key != "style" for key in element.attrib)
        assert not any(
            value.startswith(("http:", "https:", "//")) for value in element.attrib.values()
        )

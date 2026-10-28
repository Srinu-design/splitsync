import pytest

EXPECTED_CSP = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def assert_security_headers(response):
    assert response.headers.get("Content-Security-Policy") == EXPECTED_CSP
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("Referrer-Policy") == "same-origin"
    assert response.headers.get("Cache-Control") == "no-cache"


@pytest.mark.asyncio
async def test_pages_and_static_files(client):
    """GET / and GET /r/anything return 200 text/html; /static/app.js returns 200."""
    # GET / (Landing page)
    res_index = await client.get("/")
    assert res_index.status_code == 200
    assert "text/html" in res_index.headers.get("content-type", "")
    assert "Start demo" in res_index.text
    assert "Create a group" in res_index.text
    for panel in ("about", "how-it-works", "try"):
        assert f'id="{panel}"' in res_index.text
    assert_security_headers(res_index)

    # GET /r/{slug} (Room page, always 200 for any slug)
    res_room = await client.get("/r/anything")
    assert res_room.status_code == 200
    assert "text/html" in res_room.headers.get("content-type", "")
    assert "Settlement Graphs" in res_room.text
    assert "Balances" in res_room.text
    assert_security_headers(res_room)

    # Static assets
    res_app_js = await client.get("/static/app.js")
    assert res_app_js.status_code == 200
    assert "javascript" in res_app_js.headers.get(
        "content-type", ""
    ) or "text/" in res_app_js.headers.get("content-type", "")
    assert_security_headers(res_app_js)

    res_graph_js = await client.get("/static/graph.js")
    assert res_graph_js.status_code == 200
    assert_security_headers(res_graph_js)

    res_css = await client.get("/static/styles.css")
    assert res_css.status_code == 200
    assert "css" in res_css.headers.get("content-type", "")
    assert_security_headers(res_css)

    for asset in ("theme.js", "site.js", "favicon.svg"):
        response = await client.get(f"/static/{asset}")
        assert response.status_code == 200
        assert_security_headers(response)


@pytest.mark.asyncio
async def test_security_headers_on_all_responses(client):
    """Every response (HTML, JSON, 404) carries the CSP, nosniff and Referrer-Policy headers."""
    # 404 Not Found route
    res_404 = await client.get("/nonexistent-page-404")
    assert res_404.status_code == 404
    assert_security_headers(res_404)

    # JSON response from API (e.g. 422 validation error)
    res_api = await client.post("/api/rooms", json={})
    assert res_api.status_code == 422
    assert_security_headers(res_api)

    # Healthcheck
    res_health = await client.get("/healthz")
    assert res_health.status_code == 200
    assert_security_headers(res_health)

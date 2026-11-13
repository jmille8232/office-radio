"""One-time login. Run on your laptop (it needs a browser):

    python -m office_radio.cli login

Starts a tiny server on the redirect URI's host/port, prints the Spotify sign-in
link, waits for the callback, checks `state`, exchanges the code and saves the
refresh token to the token file (mode 600). Copy that token into a Chalk secret
named SPOTIFY_REFRESH_TOKEN; don't paste it anywhere else.
"""
from __future__ import annotations

import http.server
import webbrowser
from urllib.parse import parse_qs, urlparse

from .auth import AuthError, authorize_url, exchange_code, new_state, save_token_file


def run_login(client_id: str, client_secret: str, redirect_uri: str, token_path: str,
              open_browser: bool = True, timeout: int = 300) -> None:
    parsed = urlparse(redirect_uri)
    if parsed.hostname not in ("127.0.0.1", "[::1]", "::1"):
        raise AuthError("For local login, SPOTIFY_REDIRECT_URI must use 127.0.0.1, "
                        "e.g. http://127.0.0.1:8888/callback")
    state = new_state()
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(url.query)
            result["state"] = (qs.get("state") or [None])[0]
            result["code"] = (qs.get("code") or [None])[0]
            result["error"] = (qs.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Office Radio login received. You can close this tab.")

        def log_message(self, *args):  # keep the code out of terminal logs
            pass

    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    server.timeout = timeout
    link = authorize_url(client_id, redirect_uri, state)
    print("Open this link and approve access:\n\n" + link + "\n")
    if open_browser:
        webbrowser.open(link)
    server.handle_request()
    server.server_close()

    if result.get("error"):
        raise AuthError(f"Spotify returned an error: {result['error']}")
    if not result.get("code"):
        raise AuthError("No authorization code received (timed out?)")
    if result.get("state") != state:
        raise AuthError("State mismatch; possible CSRF. Try again.")

    tokens = exchange_code(result["code"], client_id, client_secret, redirect_uri)
    save_token_file(token_path, tokens["refresh_token"])
    print(f"Saved refresh token to {token_path} (readable only by you).")
    print("Next: copy the refresh_token value from that file into a Chalk secret "
          "named SPOTIFY_REFRESH_TOKEN, then delete the file from your laptop.")

#!/usr/bin/env python3
"""
One-time setup: authorize the Spotify account that will own the Weedian
playlists, for weedian_playlists.py.

Run this yourself (not in CI). It:
  1. asks for that app's client ID and secret (unless already saved),
  2. starts a tiny local server on the app's redirect URI
     (default http://127.0.0.1:8888/callback; set WEEDIAN_SPOTIFY_REDIRECT_URI
     to use another one registered in the Spotify app),
  3. opens Spotify's consent page; log in there as the account that should
     own the playlists,
  4. shows which account was authorized and saves the client ID, secret and
     refresh token as Windows user environment variables:
       WEEDIAN_SPOTIFY_CLIENT_ID, WEEDIAN_SPOTIFY_CLIENT_SECRET,
       WEEDIAN_SPOTIFY_REFRESH_TOKEN

At the end it prints the refresh token once, for the GitHub secret of the same
name. Treat it like a password.
"""

import base64
import getpass
import os
import secrets
import subprocess
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

REDIRECT_URI = os.environ.get("WEEDIAN_SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
PORT = urllib.parse.urlparse(REDIRECT_URI).port or 80
# ugc-image-upload: the playlists get the compilation's cover art.
SCOPES = "playlist-modify-public playlist-modify-private ugc-image-upload"


def user_env(name: str) -> str | None:
    """Read a variable from the process, or (Windows) the saved user env."""
    value = os.environ.get(name)
    if value or sys.platform != "win32":
        return value
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def save_user_env(name: str, value: str) -> None:
    if sys.platform == "win32":
        subprocess.run(["setx", name, value], check=True, capture_output=True)


def main() -> int:
    client_id = user_env("WEEDIAN_SPOTIFY_CLIENT_ID")
    client_secret = user_env("WEEDIAN_SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Enter the credentials of the Spotify app you made for this")
        print("(Spotify dashboard -> the app -> Settings).")
        client_id = input("Client ID: ").strip()
        client_secret = getpass.getpass("Client secret (hidden): ").strip()
    if not client_id or not client_secret:
        print("Client ID and secret are required.", file=sys.stderr)
        return 1

    state = secrets.token_urlsafe(16)
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        # Always show the consent page, with a "Not you?" link to switch account.
        "show_dialog": "true",
    })

    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                body = "State mismatch, please run the script again."
            elif "error" in query:
                result["error"] = query["error"][0]
                body = f"Spotify said: {result['error']}. You can close this tab."
            else:
                result["code"] = query.get("code", [""])[0]
                body = "Done! You can close this tab and go back to the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} is busy. Free it and try again.", file=sys.stderr)
        return 1

    print("\nOpening Spotify in your browser.")
    print("Log in as the account that should own the playlists (use 'Not you?' or")
    print("a private window to switch). If no browser opens, visit:\n")
    print(auth_url + "\n")
    webbrowser.open(auth_url)

    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()

    if "error" in result:
        print(f"Authorization failed: {result['error']}", file=sys.stderr)
        return 1

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "authorization_code", "code": result["code"],
              "redirect_uri": REDIRECT_URI},
        timeout=20,
    )
    if not resp.ok:
        print(f"Token exchange failed: {resp.status_code} {resp.text[:200]}",
              file=sys.stderr)
        return 1
    tokens = resp.json()

    me = requests.get("https://api.spotify.com/v1/me",
                      headers={"Authorization": f"Bearer {tokens['access_token']}"},
                      timeout=20)
    me.raise_for_status()
    profile = me.json()
    print(f"\nAuthorized Spotify account: {profile.get('display_name')} (id {profile['id']})")
    print("If that is NOT the account you meant, run the script again.\n")

    refresh = tokens["refresh_token"]
    save_user_env("WEEDIAN_SPOTIFY_CLIENT_ID", client_id)
    save_user_env("WEEDIAN_SPOTIFY_CLIENT_SECRET", client_secret)
    save_user_env("WEEDIAN_SPOTIFY_REFRESH_TOKEN", refresh)
    if sys.platform == "win32":
        print("Saved WEEDIAN_SPOTIFY_CLIENT_ID, WEEDIAN_SPOTIFY_CLIENT_SECRET and")
        print("WEEDIAN_SPOTIFY_REFRESH_TOKEN as Windows user environment variables.")

    print("\nAdd this as the GitHub repository secret WEEDIAN_SPOTIFY_REFRESH_TOKEN:\n")
    print(refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())

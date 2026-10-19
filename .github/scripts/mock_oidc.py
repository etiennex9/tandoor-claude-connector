"""Stands in for Authentik in smoke-test.sh: serves an OIDC discovery document and an empty JWKS on port 8080.

The gateway reads the discovery document when it starts. Signing in would need the other endpoints, which aren't served.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # URLs on whatever host the gateway reached this server by (its network alias, oidc).
        base = f"http://{self.headers['Host']}"
        docs = {
            "/.well-known/openid-configuration": {
                "issuer": f"{base}/",
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "userinfo_endpoint": f"{base}/userinfo",
                "jwks_uri": f"{base}/jwks",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "email", "profile", "offline_access"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "code_challenge_methods_supported": ["S256"],
            },
            "/jwks": {"keys": []},
        }
        doc = docs.get(self.path)
        if doc is None:
            self.send_error(404)
            return
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("", 8080), Handler).serve_forever()

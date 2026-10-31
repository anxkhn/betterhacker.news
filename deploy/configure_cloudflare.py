"""Publish the named tunnel using a private API-token file supplied at deployment."""

import argparse
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

HOSTNAME = "betterhacker.anaskhan.me"


def configure(token_file):
    path = Path(token_file)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("API token file must be owner-only")
    token = path.read_text().strip()

    def api(method, resource, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            "https://api.cloudflare.com/client/v4" + resource,
            data=payload,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if not result.get("success"):
            raise RuntimeError("Cloudflare API rejected " + resource)
        return result["result"]

    zones = api("GET", "/zones?name=anaskhan.me")
    if len(zones) != 1:
        raise ValueError("Expected exactly one anaskhan.me zone")
    zone = zones[0]
    account_id = zone["account"]["id"]
    existing = api("GET", f"/zones/{zone['id']}/dns_records?name={HOSTNAME}")
    tunnels = api("GET", f"/accounts/{account_id}/cfd_tunnel?is_deleted=false&name=betterhacker-azure")
    matching = [tunnel for tunnel in tunnels if tunnel["name"] == "betterhacker-azure"]
    if existing and not matching:
        raise ValueError("Hostname already has DNS records; refusing to replace them")
    tunnel = (
        matching[0]
        if matching
        else api(
            "POST", f"/accounts/{account_id}/cfd_tunnel", {"name": "betterhacker-azure", "config_src": "cloudflare"}
        )
    )
    tunnel_id = tunnel["id"]
    target = f"{tunnel_id}.cfargotunnel.com"
    if existing and any(record["type"] != "CNAME" or record["content"] != target for record in existing):
        raise ValueError("Hostname has unrelated DNS records; refusing to replace them")
    api(
        "PUT",
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        {
            "config": {
                "ingress": [{"hostname": HOSTNAME, "service": "http://127.0.0.1:8791"}, {"service": "http_status:404"}]
            }
        },
    )
    if not existing:
        api(
            "POST",
            f"/zones/{zone['id']}/dns_records",
            {"type": "CNAME", "name": HOSTNAME, "content": target, "proxied": True, "ttl": 1},
        )
    tunnel_token = api("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
    directory = Path.home() / ".config" / "betterhacker"
    directory.mkdir(mode=0o700, exist_ok=True)
    destination = directory / "tunnel-token"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(tunnel_token)
    print("Tunnel configured for https://" + HOSTNAME)
    print("Tunnel token saved privately to " + str(destination))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    arguments = parser.parse_args()
    try:
        configure(arguments.token_file)
    except (OSError, ValueError, RuntimeError, urllib.error.HTTPError):
        raise SystemExit("Cloudflare configuration failed. Check token permissions and existing DNS records.") from None

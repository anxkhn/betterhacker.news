import hashlib
import http.client
import io
import ipaddress
import os
import random
import socket
import ssl
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from PIL import Image, ImageDraw, ImageFont, ImageOps

HTML_LIMIT = 512 * 1024
IMAGE_LIMIT = 3 * 1024 * 1024
PIXEL_LIMIT = 12_000_000
SIZE = (960, 540)


class UnsafeURL(ValueError):
    pass


def public_ip(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise UnsafeURL("Non-public IP address")
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None
    ):
        raise UnsafeURL("IPv6 translation address")
    return address


def validate_url(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise UnsafeURL("Invalid URL")
    if "\\" in url:
        raise UnsafeURL("Ambiguous URL")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        if (
            parts.scheme not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
        ):
            raise UnsafeURL("Invalid URL authority")
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            raise UnsafeURL("Custom port")
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if len(labels) < 2 or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in label)
                for label in labels
            ):
                raise UnsafeURL("Invalid hostname")
            if labels[-1] in {"localhost", "local", "internal", "test", "invalid", "example"}:
                raise UnsafeURL("Local hostname")
        else:
            public_ip(str(address))
        authority = f"[{host}]" if ":" in host else host
        return urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError) as exc:
        raise UnsafeURL(str(exc)) from exc


def resolve_public(host, port):
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers:
        raise UnsafeURL("No DNS answers")
    addresses = []
    for family, _, _, _, sockaddr in answers:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise UnsafeURL("Unsupported address family")
        public_ip(sockaddr[0])
        entry = (family, sockaddr)
        if entry not in addresses:
            addresses.append(entry)
    return addresses


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout, tls=False):
        super().__init__(host, port, timeout=timeout)
        self.address = address
        self.tls = tls

    def connect(self):
        family, sockaddr = self.address
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            # Connect to the validated numeric sockaddr, never resolve the host again.
            sock.connect(sockaddr)
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except BaseException:
            sock.close()
            raise


def fetch_public(url, limit, content_types, deadline=None):
    deadline = min(deadline or float("inf"), time.monotonic() + 8)
    for _ in range(4):
        url = validate_url(url)
        parts = urlsplit(url)
        port = 443 if parts.scheme == "https" else 80
        addresses = resolve_public(parts.hostname, port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Preview deadline")
        connection = PinnedHTTPConnection(parts.hostname, port, addresses[0], min(3, remaining), port == 443)
        try:
            connection.request(
                "GET",
                urlunsplit(("", "", parts.path, parts.query, "")),
                headers={
                    "User-Agent": "BetterHackerNews/1.0 article-preview",
                    "Accept": ", ".join(content_types),
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Redirect without location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError("Preview HTTP status")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if (
                content_type not in content_types
                or response.getheader("Content-Encoding", "identity").lower() != "identity"
            ):
                raise ValueError("Unsupported content type or encoding")
            length = response.getheader("Content-Length")
            if length is not None and (int(length) < 0 or int(length) > limit):
                raise ValueError("Response too large")
            chunks = []
            size = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Preview deadline")
                if connection.sock is not None and connection.sock.fileno() >= 0:
                    connection.sock.settimeout(min(3, remaining))
                chunk = response.read1(min(65536, limit + 1 - size))
                if not chunk:
                    return b"".join(chunks), url
                size += len(chunk)
                if size > limit:
                    raise ValueError("Response too large")
                chunks.append(chunk)
        finally:
            connection.close()
    raise ValueError("Too many redirects")


class OpenGraphParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.image = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "meta":
            attrs = {key.lower(): value for key, value in attrs}
            name = attrs.get("property") or attrs.get("name") or ""
            if name.lower() == "og:image" and attrs.get("content") and self.image is None:
                self.image = attrs["content"].strip()


def parse_og_image(html, article_url):
    parser = OpenGraphParser()
    parser.feed(html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html)
    return validate_url(urljoin(article_url, parser.image)) if parser.image else None


def article_preview(url, deadline=None):
    deadline = min(deadline or float("inf"), time.monotonic() + 10)
    html, final_url = fetch_public(url, HTML_LIMIT, {"text/html", "application/xhtml+xml"}, deadline)
    image_url = parse_og_image(html, final_url)
    if not image_url:
        raise ValueError("No Open Graph image")
    raw, _ = fetch_public(image_url, IMAGE_LIMIT, {"image/png", "image/jpeg", "image/webp", "image/gif"}, deadline)
    try:
        decoded = Image.open(io.BytesIO(raw), formats=["PNG", "JPEG", "WEBP", "GIF"])
    except Image.DecompressionBombError as exc:
        raise ValueError("Image decompression limit") from exc
    with decoded as image:
        width, height = image.size
        if width < 32 or height < 32 or max(width, height) > 8192 or width * height > PIXEL_LIMIT:
            raise ValueError("Image dimensions outside limits")
        image.seek(0)
        image.load()
        return ImageOps.fit(image.convert("RGB"), SIZE, method=Image.Resampling.LANCZOS)


def editorial_thumbnail(story):
    seed = hashlib.sha256(f"{story['id']}:{story['title']}:{story['domain']}".encode()).digest()
    rng = random.Random(seed)
    palettes = [
        ("#172e3b", "#f1e6ce", "#ee9565"),
        ("#352c45", "#f3e9db", "#b4a9de"),
        ("#25382c", "#f2ebd9", "#c5d18c"),
        ("#472c29", "#f3e6d5", "#e8ae79"),
    ]
    background, foreground, accent = palettes[seed[0] % len(palettes)]
    image = Image.new("RGB", SIZE, background)
    draw = ImageDraw.Draw(image)
    for i in range(6):
        x, y = rng.randrange(590, 980), rng.randrange(-50, 540)
        radius = rng.randrange(35, 130)
        if i % 2:
            draw.rectangle((x, y, x + radius, y + radius), outline=accent, width=3)
        else:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=accent, width=4)
    try:
        title_font = ImageFont.truetype("DejaVuSans.ttf", 38)
        small_font = ImageFont.truetype("DejaVuSans.ttf", 19)
    except OSError:
        title_font = ImageFont.load_default(size=38)
        small_font = ImageFont.load_default(size=19)
    # Render editorial text without pictographs, keeping the original title in the snapshot.
    title = "".join(c for c in story["title"][:2048] if ord(c) < 0x2600 or 0x3000 <= ord(c) < 0xFE00)
    lines, current = [], ""
    for word in title.split():
        while draw.textlength(word, font=title_font) > 550:
            cut = 1
            while cut < len(word) and draw.textlength(word[: cut + 1], font=title_font) <= 550:
                cut += 1
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:cut])
            word = word[cut:]
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=title_font) > 550:
            lines.append(current)
            current = word
        else:
            current = candidate
        if len(lines) >= 7:
            break
    if current:
        lines.append(current)
    for i, line in enumerate(lines[:7]):
        draw.text((48, 105 + i * 49), line, font=title_font, fill=foreground)
    draw.rectangle((48, 57, 130, 63), fill=accent)
    domain = story["domain"][:72]
    draw.text((48, 484), domain, font=small_font, fill=accent)
    return image


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_thumbnail(path, image):
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=82, method=3)
    atomic_write(path, buffer.getvalue())

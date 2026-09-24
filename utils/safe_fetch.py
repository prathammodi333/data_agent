"""Small public HTTP(S) data downloads with DNS-validated, pinned connections."""

import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, quote

import urllib3

from utils.errors import ETLRejected
from utils.request_context import request_id

logger = logging.getLogger(__name__)
MAX_DOWNLOAD_BYTES = 1_000_000
MAX_DOWNLOAD_SECONDS = 20
MAX_REDIRECTS = 3


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or "%" in value:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Transition/translation ranges can tunnel to a private IPv4 destination.
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if address in ipaddress.ip_network("64:ff9b::/96") or address in ipaddress.ip_network("64:ff9b:1::/48"):
            return False
    return value != "168.63.129.16"  # Azure's virtual platform service address.


def source_url(message: str) -> str | None:
    urls = re.findall(r"https?://[^\s<>\"']+", message, re.IGNORECASE)
    if len(urls) > 1:
        raise ETLRejected("Provide one public data URL per request.")
    return urls[0].rstrip(".,;)") if urls else None


def checked_target(url: str):
    if len(url) > 2048 or re.search(r"[\x00-\x20\\]", url):
        raise ETLRejected("Invalid source URL.")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (ValueError, UnicodeError):
        raise ETLRejected("Invalid source URL.") from None
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ETLRejected("Use a public HTTP or HTTPS JSON/CSV data URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ETLRejected("URLs containing login credentials are not supported.")
    if port != (443 if parsed.scheme.lower() == "https" else 80):
        raise ETLRejected("Only standard HTTP/HTTPS ports are supported.")
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        # Validate *every* DNS answer, then connect to one exact validated IP.
        if not addresses or any(not public_address(ip) for ip in addresses):
            raise ETLRejected("The URL must resolve only to public internet addresses.")
    except ETLRejected:
        raise
    except (socket.gaierror, ValueError):
        raise ETLRejected("The source hostname could not be safely resolved.") from None
    return parsed, host, port, addresses[0]


def fetch_public_data(url: str) -> tuple[bytes, str, str]:
    """GET only; no ambient proxy, cookies, credentials, retries, or automatic redirects."""
    started = time.monotonic()
    for redirect in range(MAX_REDIRECTS + 1):
        parsed, host, port, ip = checked_target(url)
        if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
            raise ETLRejected("Source download exceeded the time limit.")
        host_header = f"[{host}]" if ":" in host else host
        pool_type = urllib3.HTTPSConnectionPool if parsed.scheme.lower() == "https" else urllib3.HTTPConnectionPool
        options = {"server_hostname": host, "assert_hostname": host, "cert_reqs": "CERT_REQUIRED"} if parsed.scheme.lower() == "https" else {}
        target = urlunsplit(("", "", quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"), parsed.query, ""))
        response = None
        try:
            with pool_type(ip, port=port, timeout=urllib3.Timeout(connect=3, read=3), **options) as pool:
                response = pool.urlopen("GET", target, headers={"Host": host_header,
                    "Accept": "application/json, text/csv, application/x-ndjson, text/tab-separated-values, text/plain",
                    "Accept-Encoding": "identity", "User-Agent": "DataAgentDemo/1.5"},
                    redirect=False, retries=False, preload_content=False, assert_same_host=False)
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location or redirect == MAX_REDIRECTS:
                        raise ETLRejected("The source redirected too many times or supplied no destination.")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ETLRejected(f"The data source returned HTTP {response.status}.")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ETLRejected("Compressed downloads are not supported; provide an uncompressed JSON/CSV endpoint.")
                length = response.headers.get("Content-Length")
                if length and (not length.isdigit() or int(length) > MAX_DOWNLOAD_BYTES):
                    raise ETLRejected("Source exceeds the 1 MB download limit.")
                content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
                if content_type in {"text/html", "application/xhtml+xml"}:
                    raise ETLRejected("This URL serves a web page. Provide its JSON/CSV download or API URL.")
                chunks, size = [], 0
                while True:
                    if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                        raise ETLRejected("Source download exceeded the time limit.")
                    chunk = response.read1(8192, decode_content=False)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ETLRejected("Source exceeds the 1 MB download limit.")
                    chunks.append(chunk)
                logger.info("request=%s url_fetch=completed host=%s bytes=%d redirects=%d",
                            request_id.get(), host, size, redirect)
                return b"".join(chunks), content_type, f"public URL {host}"
        except (urllib3.exceptions.HTTPError, OSError):
            raise ETLRejected("Could not fetch the public data source securely within the time limit.") from None
        finally:
            if response is not None:
                response.close()
    raise ETLRejected("The source redirected too many times.")

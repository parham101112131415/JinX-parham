"""Parham link tuner: makes every config in the subscription faster and steadier on all carriers.

Applied to VLESS / Trojan links over WebSocket only, and only adds what is missing:
  fp=chrome      real Chrome TLS fingerprint (Iranian DPI throttles the default Go fingerprint)
  alpn=http/1.1  WebSocket always runs on HTTP/1.1; no ALPN guessing at connect time
  path ?ed=2560  WebSocket early data: the first request rides on the handshake (one round-trip less)
Anything it cannot parse is returned unchanged, so a subscription can never break because of it.
"""
import base64, binascii, os
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote

ED = os.environ.get("JX_EARLY_DATA", "2560")
FP = os.environ.get("JX_FINGERPRINT", "chrome")
ON = os.environ.get("JX_LINK_TUNE", "on").lower() not in ("0", "off", "false", "no")


def tune_link(link):
    try:
        s = link.strip()
        if not (s.startswith("vless://") or s.startswith("trojan://")):
            return link
        u = urlsplit(s)
        q = parse_qsl(u.query, keep_blank_values=True)
        d = dict(q)
        if d.get("type") != "ws":
            return link
        add = []
        if d.get("security") == "tls":
            if not d.get("fp") and FP:
                add.append(("fp", FP))
            if not d.get("alpn"):
                add.append(("alpn", "http/1.1"))
        out = []
        for k, v in q:
            if k == "path" and ED and ED != "0" and "ed=" not in v:
                v = v + ("&" if "?" in v else "?") + "ed=" + ED
            out.append((k, v))
        out += add
        query = urlencode(out, quote_via=quote, safe="")
        return urlunsplit((u.scheme, u.netloc, u.path, query, u.fragment))
    except Exception:
        return link


def _tune_text(text):
    lines = text.split("\n")
    return "\n".join(tune_link(l) if "://" in l else l for l in lines)


def tune_body(body):
    """body: raw subscription bytes (base64 or plain). Returns tuned bytes, or the original on any doubt."""
    if not ON or not body:
        return body
    try:
        raw = body.strip()
        try:
            txt = base64.b64decode(raw + b"=" * (-len(raw) % 4), validate=True).decode("utf-8")
            if "://" not in txt:
                return body
            return base64.b64encode(_tune_text(txt).encode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            txt = body.decode("utf-8")
            if "vless://" not in txt and "trojan://" not in txt:
                return body
            return _tune_text(txt).encode("utf-8")
    except Exception:
        return body

"""Parham speed bot: measures the tunnel from the inside and keeps it fast.

  tunnel   - real WebSocket handshake through nginx -> xray (not just "port open")
  direct   - same handshake straight to xray, to tell an nginx problem from an xray problem
  outbound - TCP connect from the server to the internet (1.1.1.1:443)
  memory   - container memory; xray is restarted gently before the whole container could be killed
Results are written to /run/jinx/speed.json and shown on /jx-status.
"""
import os, json, time, socket, base64, glob
from common import *

OUT = os.path.join(RUN, "speed.json")


def ws_handshake(port, path, host="localhost", t=4.0):
    """ms for a full WebSocket upgrade (101), or None."""
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nUser-Agent: jinx-speed\r\n\r\n") % (path, host, key)
    t0 = time.monotonic()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=t) as s:
            s.settimeout(t)
            s.sendall(req.encode())
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 4096:
                d = s.recv(1024)
                if not d:
                    break
                buf += d
        if buf.startswith(b"HTTP/1.1 101"):
            return round((time.monotonic() - t0) * 1000, 1)
    except OSError:
        pass
    return None


def outbound_ms(t=4.0):
    best = None
    for host in ("1.1.1.1", "8.8.8.8"):
        t0 = time.monotonic()
        try:
            with socket.create_connection((host, 443), timeout=t):
                ms = round((time.monotonic() - t0) * 1000, 1)
                best = ms if best is None else min(best, ms)
        except OSError:
            pass
    return best


def _read(p):
    try:
        return open(p).read().strip()
    except OSError:
        return ""


def memory():
    """(used_bytes, limit_bytes or 0, xray_rss_bytes)"""
    used = _read("/sys/fs/cgroup/memory.current") or _read("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    lim = _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    try:
        used = int(used)
    except ValueError:
        used = 0
    try:
        lim = int(lim)
        if lim > 1 << 50:
            lim = 0
    except ValueError:
        lim = 0
    rss = 0
    for st in glob.glob("/proc/[0-9]*/status"):
        try:
            txt = open(st).read()
        except OSError:
            continue
        if txt.startswith("Name:\txray"):
            for line in txt.split("\n"):
                if line.startswith("VmRSS:"):
                    rss = max(rss, int(line.split()[1]) * 1024)
    return used, lim, rss


def measure(st):
    path = st.get("ws") or "/"
    r = {
        "time": int(time.time()),
        "tunnel": ws_handshake(PUBLIC_PORT, path, domain(st) or "localhost"),
        "direct": ws_handshake(WS_PORT, path),
        "outbound": outbound_ms(),
    }
    used, lim, rss = memory()
    r.update({"mem_used": used, "mem_limit": lim, "xray_rss": rss})
    try:
        os.makedirs(RUN, exist_ok=True)
        tmp = OUT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(r, f)
        os.replace(tmp, OUT)
    except OSError:
        pass
    return r


def last():
    try:
        return json.load(open(OUT))
    except Exception:
        return {}

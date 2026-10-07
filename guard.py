#!/usr/bin/env python3
"""Parham guardian: watches every part of the panel and repairs it automatically.

checks (each independent, each with its own failure counter):
  panel   - panel HTTP port answers           -> restart x-ui
  sub     - subscription port answers         -> restart x-ui
  xray    - VLESS/WS inbound port answers     -> restart x-ui (x-ui relaunches xray)
  nginx   - public port answers               -> restart nginx
  assets  - panel scripts arrive complete      -> fix nginx temp dirs, re-render, restart nginx
  helper  - QR/health helper answers          -> restart helper
  inbound - locked inbound exists and intact  -> rewrite it, reload x-ui
  config  - critical settings still correct   -> rewrite them, reload x-ui
  speed   - xray speed template intact        -> re-apply, restart xray core
  tunnel  - real WebSocket handshake works    -> restart nginx or xray core (whichever is at fault)
  memory  - container close to its RAM limit  -> restart xray core before the container is killed
  domain  - public domain discovered/changed  -> update links, reload x-ui
  db      - integrity check + rolling backups -> restore last good copy
  disk    - log/backup growth                 -> prune
"""
import os, sys, time, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
import settings as S, inbound as IB, render, xraytpl as XT, speed as SP

FAST = turbo()
TICK = 5 if FAST else 8
LIMIT = 3              # consecutive failures before acting
COOLDOWN = 45          # seconds to leave a service alone after restarting it
GRACE = 90             # startup grace period
fails = {}
last_restart = {}
started = time.time()


def bad(name, ok):
    if ok:
        fails[name] = 0
        return False
    fails[name] = fails.get(name, 0) + 1
    return fails[name] >= LIMIT


streak = {}            # consecutive repairs per service -> growing back-off (45s .. 10min)


def _allowed(svc):
    now = time.time()
    wait = min(600, COOLDOWN * (2 ** max(0, streak.get(svc, 0) - 1)))
    if now - last_restart.get(svc, 0) < wait:
        return False
    if now - last_restart.get(svc, 0) > 900:
        streak[svc] = 0        # healthy for a long time -> forget old failures
    streak[svc] = streak.get(svc, 0) + 1
    last_restart[svc] = now
    return True


def restart(svc, why):
    if not _allowed(svc):
        return
    log("repair:", why, "->", svc)
    svc_restart(svc)
    for k in list(fails):
        fails[k] = 0


def restart_xray(why):
    if not _allowed("xray-core"):
        return
    log("repair:", why, "-> xray core")
    svc_signal("x-ui", "-1")
    fails["xray"] = 0


def reload_xui(why):
    restart("x-ui", why)


def check_ports():
    if time.time() - started < GRACE:
        return
    if bad("panel", port_open(PANEL_PORT)): reload_xui("panel not answering")
    if bad("sub", port_open(SUB_PORT)): reload_xui("subscription not answering")
    if bad("xray", port_open(WS_PORT)): restart_xray("xray inbound not answering")
    if bad("nginx", port_open(PUBLIC_PORT)): restart("nginx", "public port down")
    if bad("helper", port_open(HELPER_PORT)): restart("jx-helper", "helper down")


def check_assets():
    if time.time() - started < GRACE:
        return
    ok = panel_assets_ok()
    if ok is None:
        return
    try:
        import helper
        ok = ok and helper.redirect_ok(load_state())
    except Exception:
        pass
    if bad("assets", ok):
        st = load_state()
        fix_nginx_tmp()
        if streak.get("nginx", 0) >= 2 and not st.get("nginx_safe"):
            st["nginx_safe"] = True; save_state(st)     # tuned config keeps failing -> minimal one
        render.nginx(st)
        restart("nginx", "panel scripts not delivered completely")


def check_speed():
    if time.time() - started < GRACE:
        return
    st = load_state()
    r = SP.measure(st)
    direct_ok = r["direct"] is not None
    tunnel_ok = r["tunnel"] is not None
    if bad("ws-direct", direct_ok):
        restart_xray("xray does not accept WebSocket handshakes")
    elif direct_ok and bad("ws-tunnel", tunnel_ok):
        fix_nginx_tmp(); render.nginx(st)
        restart("nginx", "WebSocket handshake fails through nginx")
    used, lim, rss = r["mem_used"], r["mem_limit"], r["xray_rss"]
    if lim and rss and bad("memory", not (used > lim * 0.92 and rss > lim * 0.4)):
        restart_xray("memory at %d%% of the limit (xray %d MB)" % (used * 100 // lim, rss >> 20))


def check_config():
    st = load_state()
    dom = domain(st)
    # domain learned from a real visit (panel page beacon) if Railway did not give one
    if not dom:
        try:
            seen = open(os.path.join(RUN, "seen-host")).read().strip()
            if seen:
                dom = seen
        except OSError:
            pass
    if not integrity_ok():
        return
    c = db()
    with c:
        ch1 = S.ensure(c, st["base"], dom)
        iid, ch2 = IB.ensure(c, dom, st["ws"], FAST, st.get("clients"))
        ch2 = XT.ensure(c, FAST) or ch2       # speed template (DNS, IPv4, QUIC block, policy) stays in place
        snap = IB.snapshot(c, iid)
    c.close()
    if snap and snap != st.get("clients"):
        st["clients"] = snap
        save_state(st)
    if dom != st.get("domain") or iid != st.get("inbound_id"):
        st["domain"] = dom
        old = st.get("inbound_id")
        st["inbound_id"] = iid
        save_state(st)
        if iid != old:
            render.nginx(st); restart("nginx", "inbound id changed")
    if ch1:
        log("repair: panel settings restored -> reload web + sub")
        svc_signal("x-ui", "-h")
    if ch2:
        log("repair: inbound restored -> restart xray core")
        svc_signal("x-ui", "-1")


def check_db():
    if os.path.exists(DB) and not integrity_ok():
        log("database check failed -> restore")
        subprocess.run(["s6-svc", "-d", os.path.join(SVC, "x-ui")], check=False)
        time.sleep(3)
        if restore_backup():
            subprocess.run(["s6-svc", "-u", os.path.join(SVC, "x-ui")], check=False)
        else:
            subprocess.run(["s6-svc", "-u", os.path.join(SVC, "x-ui")], check=False)
        return
    backup()


def prune():
    for f in glob.glob(DB + ".broken*")[:-2]:
        try: os.remove(f)
        except OSError: pass
    for f in glob.glob("/var/log/x-ui/*"):
        try:
            if os.path.getsize(f) > 20 * 1024 * 1024:
                open(f, "w").close()
        except OSError:
            pass


def main():
    log("guardian online | tick %ss | turbo %s" % (TICK, "ON" if FAST else "off"))
    n = 0
    while True:
        try:
            check_ports()
            if n % max(1, 60 // TICK) == 0:
                check_assets()
            if n % max(1, 20 // TICK) == 0:
                check_speed()
            if n % max(1, 30 // TICK) == 0:
                check_config()
            if n % max(1, 900 // TICK) == 0 and n:
                check_db()
            if n % max(1, 3600 // TICK) == 0:
                prune()
        except Exception as e:
            log("guardian error (continuing):", e)
        n += 1
        time.sleep(TICK)


if __name__ == "__main__":
    main()

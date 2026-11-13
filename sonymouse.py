# /// script
# requires-python = ">=3.10"
# dependencies = ["hidapi", "pyobjc-framework-Quartz"]
# ///
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import threading
import time
from pathlib import Path

import hid

CONFIG = Path.home() / ".config" / "sonymouse" / "axes.json"
MARKER = b"#AndroidHeadTracker#"
ROT_SCALE = math.pi / 32767.0
DEFAULT_AXES = {"yaw": [0.0, 0.0, 1.0], "pitch": [0.0, -1.0, 0.0]}


def rotvec_to_quat(v):
    angle = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    if angle < 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    s = math.sin(angle / 2) / angle
    return (math.cos(angle / 2), v[0] * s, v[1] * s, v[2] * s)


def qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz, aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw)


def delta_deg(q_from, q_to):
    w, x, y, z = qmul((q_from[0], -q_from[1], -q_from[2], -q_from[3]), q_to)
    if w < 0:
        w, x, y, z = -w, -x, -y, -z
    n = math.sqrt(x * x + y * y + z * z)
    if n < 1e-12:
        return (0.0, 0.0, 0.0)
    k = math.degrees(2 * math.atan2(n, w)) / n
    return (x * k, y * k, z * k)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


class HeadTracker:
    def __init__(self):
        self.dev = None
        if not self.open():
            raise SystemExit("No head-tracker headset found. Are the earbuds connected to this Mac?")

    def open(self):
        for c in hid.enumerate():
            if c["usage_page"] != 0x20 or c["usage"] != 0xE1:
                continue
            d = hid.device()
            try:
                d.open_path(c["path"])
                if MARKER in bytes(d.get_feature_report(2, 64)):
                    d.send_feature_report([0x01, 0x03])
                    self.dev, self.name = d, c["product_string"]
                    return True
                d.close()
            except (OSError, ValueError):
                pass
        return False

    def reconnect(self, give_up=lambda: False, every_s=2.0):
        try:
            self.dev.close()
        except (OSError, ValueError):
            pass
        self.dev = None
        while not give_up():
            time.sleep(every_s)
            if self.open():
                return True
        return False

    def read(self, timeout_ms=500):
        r = bytes(self.dev.read(64, timeout_ms))
        if len(r) < 14 or r[0] != 0x01:
            return None
        v = struct.unpack_from("<3h", r, 1)
        return rotvec_to_quat(tuple(x * ROT_SCALE for x in v)), r[13]

    def close(self):
        if self.dev is None:
            return
        try:
            self.dev.send_feature_report([0x01, 0x00])
            self.dev.close()
        except (OSError, ValueError):
            pass


def load_axes():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return None


def hold(tracker, text, seconds):
    end, q = time.monotonic() + seconds, None
    while (left := end - time.monotonic()) > 0:
        s = tracker.read()
        if s:
            q = s[0]
        print(f"\r{text}  {left:3.1f}s ", end="", flush=True)
    print()
    if q is None:
        raise SystemExit("No data from the earbuds. Put them in your ears and try again.")
    return q


def calibrate(tracker):
    print("Wear both earbuds and face the screen.\n")
    c1 = hold(tracker, "Look at the CENTER of the screen", 3)
    r = hold(tracker, "Turn your head to the RIGHT (about 30°) and hold", 3)
    c2 = hold(tracker, "Back to the CENTER", 3)
    u = hold(tracker, "Look UP (about 20°) and hold", 3)
    hold(tracker, "Back to the CENTER", 1.5)
    axes, message = axes_from(c1, r, c2, u)
    print(message)
    if axes is None:
        raise SystemExit(1)


CALIB_STEPS = (("Look at the CENTER of the screen", 3.0), ("Turn your head to the RIGHT (about 30°) and hold", 3.0),
               ("Back to the CENTER", 3.0), ("Look UP (about 20°) and hold", 3.0))


def axes_from(c1, r, c2, u):
    axes, seen = {}, []
    for key, a, b in (("yaw", c1, r), ("pitch", c2, u)):
        d = delta_deg(a, b)
        n = math.sqrt(dot(d, d))
        if n < 8:
            return None, f"Only saw {n:.0f}° of movement for {key}; nothing saved. Try again with a bigger turn."
        axes[key] = [x / n for x in d]
        seen.append(f"{key} {n:.0f}°")
    if abs(dot(axes["yaw"], axes["pitch"])) > 0.5:
        return None, "'Right' and 'up' looked like the same movement; nothing saved. Try again."
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(axes))
    return axes, f"Calibrated ({', '.join(seen)}). Saved to {CONFIG}"


class Cursor:

    def __init__(self, dwell, smooth_s):
        import Quartz as Q
        self.Q = Q
        self.can_post = bool(Q.CGPreflightPostEventAccess())
        self.dwell, self.smooth_s = dwell, smooth_s
        self.lock = threading.Lock()
        self.pending = [0.0, 0.0]
        self.frac = [0.0, 0.0]
        self.running = True
        self.anchor, self.anchor_t, self.armed = None, 0.0, False
        _err, ids, n = Q.CGGetActiveDisplayList(16, None, None)
        self.bounds = [Q.CGDisplayBounds(i) for i in ids[:n]]

    def add(self, dx, dy):
        with self.lock:
            self.pending[0] += dx
            self.pending[1] += dy

    def position(self):
        p = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        return p.x, p.y

    def clamp(self, x, y):
        best = None
        for b in self.bounds:
            cx = min(max(x, b.origin.x), b.origin.x + b.size.width - 1)
            cy = min(max(y, b.origin.y), b.origin.y + b.size.height - 1)
            d = (cx - x) ** 2 + (cy - y) ** 2
            if best is None or d < best[0]:
                best = (d, cx, cy)
        return best[1], best[2]

    def move(self, x, y):
        Q = self.Q
        if self.can_post:
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (x, y), 0))
        else:
            Q.CGWarpMouseCursorPosition((x, y))

    def click(self, x, y):
        Q = self.Q
        for kind in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, kind, (x, y), Q.kCGMouseButtonLeft))

    def run(self):
        dt = 1 / 120
        alpha = 1 - math.exp(-dt / self.smooth_s)
        while self.running:
            time.sleep(dt)
            with self.lock:
                sx, sy = self.pending[0] * alpha, self.pending[1] * alpha
                self.pending[0] -= sx
                self.pending[1] -= sy
            self.frac[0] += sx
            self.frac[1] += sy
            ix, iy = round(self.frac[0]), round(self.frac[1])
            x, y = self.position()
            if ix or iy:
                self.frac[0] -= ix
                self.frac[1] -= iy
                x, y = self.clamp(x + ix, y + iy)
                self.move(x, y)
            if self.dwell and self.can_post:
                self.dwell_step(x, y, moved_by_head=bool(ix or iy))

    def dwell_step(self, x, y, moved_by_head):
        now = time.monotonic()
        if self.anchor is None or math.hypot(x - self.anchor[0], y - self.anchor[1]) > 8:
            self.anchor, self.anchor_t, self.armed = (x, y), now, moved_by_head
        elif self.armed and now - self.anchor_t >= self.dwell:
            self.click(x, y)
            self.armed = False


def rotate(q, v):
    w, x, y, z = q
    return qmul(qmul(q, (0.0, *v)), (w, -x, -y, -z))[1:]


def head_basis(axes):
    z = [-c for c in axes["yaw"]]
    k = dot(axes["pitch"], z)
    x = [p - k * c for p, c in zip(axes["pitch"], z)]
    n = math.sqrt(dot(x, x)) or 1.0
    x = [c / n for c in x]
    y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]]
    return x, y, z


def head_pose(ref, q, basis):
    d = delta_deg(ref, q)
    qh = rotvec_to_quat(tuple(math.radians(dot(d, b)) for b in basis))
    f, r = rotate(qh, (0.0, 1.0, 0.0)), rotate(qh, (1.0, 0.0, 0.0))
    yaw = math.degrees(math.atan2(f[0], f[1]))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, f[2]))))
    roll = math.degrees(math.asin(max(-1.0, min(1.0, -r[2]))))
    return qh, yaw, pitch, roll


class Viz:

    def __init__(self, port, can_mouse):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.cond = threading.Condition()
        self.seq, self.payload = 0, ""
        self.can_mouse = self.mouse_on = can_mouse
        self.want_recenter = self.want_calibrate = self.quit = False
        viz, page = self, Path(__file__).with_name("viz.html")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path in ("/", "/parallax"):
                    body = page.with_name("viz.html" if self.path == "/" else "parallax.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    seen = 0
                    try:
                        while not viz.quit:
                            with viz.cond:
                                fresh = viz.cond.wait_for(lambda: viz.seq != seen, timeout=1.0)
                                seen, data = viz.seq, viz.payload
                            self.wfile.write(f"data: {data}\n\n".encode() if fresh else b": idle\n\n")
                            self.wfile.flush()
                    except OSError:
                        pass
                else:
                    self.send_error(404)

            def do_POST(self):
                if self.headers.get("X-Sonymouse") != "1":
                    return self.send_error(403)
                if self.path == "/recenter":
                    viz.want_recenter = True
                elif self.path == "/calibrate":
                    viz.want_calibrate = True
                elif self.path.startswith("/mouse"):
                    viz.mouse_on = viz.can_mouse and self.path.endswith("on=1")
                elif self.path == "/quit":
                    viz.quit = True
                else:
                    return self.send_error(404)
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        print(f"Live view: http://127.0.0.1:{port}")

    def publish(self, **sample):
        with self.cond:
            self.payload = json.dumps(sample)
            self.seq += 1
            self.cond.notify_all()


def run(tracker, args, axes, calibrated=True):
    cursor = None
    viz = Viz(args.viz, can_mouse=not args.diag) if args.viz else None
    if viz and not calibrated:
        viz.mouse_on = False
    basis, ref = head_basis(axes), None
    calib, note = None, (None, 0.0)
    if not args.diag:
        cursor = Cursor(args.dwell, args.smooth)
        threading.Thread(target=cursor.run, daemon=True).start()
        if not cursor.can_post:
            print("Note: this terminal has no Accessibility permission, so the cursor is moved by 'warping':\n"
                  "it moves, but hover effects may lag and --dwell clicks are off. To fix: System Settings >\n"
                  "Privacy & Security > Accessibility, enable your terminal app, then restart it.")
    print(f"{tracker.name}: head tracking on. Ctrl+C to stop.")
    prev, prev_n, last, dt_avg = None, None, None, 0.045
    end = time.monotonic() + args.seconds if args.seconds else None
    count = 0
    while (end is None or time.monotonic() < end) and not (viz and viz.quit):
        try:
            s = tracker.read()
        except OSError:
            print("Earbuds disconnected. Waiting for them to reconnect…", flush=True)
            if not tracker.reconnect(give_up=lambda: bool(viz and viz.quit) or (end and time.monotonic() > end)):
                break
            print(f"{tracker.name}: reconnected.", flush=True)
            prev = ref = None
            continue
        if s is None:
            prev = None
            continue
        q, n = s
        now = time.monotonic()
        if viz and viz.want_recenter:
            viz.want_recenter, ref = False, None
        if viz and viz.want_calibrate:
            viz.want_calibrate = False
            calib = {"step": 0, "until": now + CALIB_STEPS[0][1], "poses": []}
        if calib and now >= calib["until"]:
            calib["poses"].append(q)
            calib["step"] += 1
            if calib["step"] < len(CALIB_STEPS):
                calib["until"] = now + CALIB_STEPS[calib["step"]][1]
            else:
                new_axes, message = axes_from(*calib["poses"])
                if new_axes:
                    axes, basis, ref = new_axes, head_basis(new_axes), None
                    viz.mouse_on = viz.can_mouse
                calib, note = None, (message, now + 8)
        if ref is None or (prev_n is not None and n != prev_n):
            ref = q
        if prev is None or n != prev_n:
            prev, prev_n, last = q, n, now
            continue
        dt_avg += 0.1 * (min(max(now - last, 0.005), 0.2) - dt_avg)
        d = delta_deg(prev, q)
        prev, last = q, now
        yaw, pitch = dot(d, axes["yaw"]), dot(d, axes["pitch"])
        speed = math.hypot(yaw, pitch) / dt_avg
        keep = max(0.0, speed - args.deadzone) / speed if speed > 0 else 0.0
        gain = args.speed * min(max(math.sqrt(speed / 40.0), 0.5), 2.0) * keep
        count += 1
        mouse_on = cursor is not None and calib is None and (viz is None or viz.mouse_on)
        if viz:
            qh, hy, hp, hr = head_pose(ref, q, basis)
            step = calib and {"text": CALIB_STEPS[calib["step"]][0], "left": round(calib["until"] - now, 1),
                              "step": calib["step"] + 1, "of": len(CALIB_STEPS)}
            viz.publish(calib=step, note=note[0] if now < note[1] else None,t=round(now, 3), yaw=round(hy, 2), pitch=round(hp, 2), roll=round(hr, 2),
                        q=[round(c, 5) for c in qh], speed=round(speed, 1), hz=round(1 / dt_avg, 1),
                        dx=round(yaw * gain / dt_avg, 1), dy=round(-pitch * gain / dt_avg, 1),
                        mouse=mouse_on, can_mouse=viz.can_mouse, name=tracker.name)
        if args.diag:
            if count % 3 == 0:
                print(f"\ryaw {yaw / dt_avg:+7.1f}°/s  pitch {pitch / dt_avg:+7.1f}°/s  "
                      f"{1 / dt_avg:4.0f} Hz  -> {yaw * gain:+6.1f} px, {-pitch * gain:+6.1f} px   ", end="", flush=True)
        elif mouse_on:
            cursor.add(yaw * gain, -pitch * gain)
    if args.diag:
        print()
    print(f"{count} samples.")


def main():
    ap = argparse.ArgumentParser(description="Move the Mac cursor with your head, using Sony earbuds.")
    ap.add_argument("--calibrate", action="store_true", help="learn the 'right' and 'up' directions (run once)")
    ap.add_argument("--diag", action="store_true", help="print motion, do not move the cursor")
    ap.add_argument("--speed", type=float, default=30.0, help="pixels per degree at medium head speed (default 30)")
    ap.add_argument("--deadzone", type=float, default=2.0, help="ignore head movement slower than this, °/s (default 2)")
    ap.add_argument("--smooth", type=float, default=0.05, help="smoothing time constant in seconds (default 0.05)")
    ap.add_argument("--dwell", type=float, default=0.0, help="click after holding still this many seconds (0 = off)")
    ap.add_argument("--viz", type=int, nargs="?", const=8765, default=0, metavar="PORT",
                    help="serve a live view of the tracking at http://127.0.0.1:PORT (default port 8765)")
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after this many seconds (0 = run until Ctrl+C)")
    args = ap.parse_args()

    tracker = HeadTracker()
    try:
        if args.calibrate:
            calibrate(tracker)
            return
        axes = load_axes()
        if axes is None:
            print("Not calibrated yet: using the default axes. Run with --calibrate, or press Calibrate in the --viz page.")
        run(tracker, args, axes or DEFAULT_AXES, calibrated=axes is not None)
    except KeyboardInterrupt:
        print()
    finally:
        tracker.close()


if __name__ == "__main__":
    sys.exit(main())

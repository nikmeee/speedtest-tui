"""
⚡ Speedtest TUI — стильный замер скорости интернета прямо в терминале.

Замер идёт через сеть Cloudflare (speed.cloudflare.com):
  • пинг и джиттер по HTTP,
  • скачивание и отдача в несколько параллельных потоков,
  • задержка под нагрузкой (bufferbloat).

Запуск:  speedtest
Опции:   --duration 10  --streams 6  --no-upload
"""

import argparse
import http.client
import json
import math
import os
import re
import statistics
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from rich import box

from . import __version__
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

HOST = "speed.cloudflare.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (speedtest-tui)",
    "Referer": "https://speed.cloudflare.com/",
}
HISTORY_FILE = Path.home() / ".speedtest_tui_history.json"
SAMPLE_EVERY = 0.1  # сек между замерами мгновенной скорости

THEMES = {
    "ping": ["#00ffa3", "#00d1ff"],
    "download": ["#00e5ff", "#3d7bff", "#b388ff"],
    "upload": ["#ff4ecd", "#ff8a3d", "#ffd84d"],
    "title": ["#00e5ff", "#b388ff", "#ff4ecd"],
}
DIM = "grey42"
FAINT = "grey23"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BLOCKS = " ▁▂▃▄▅▆▇█"

# ─────────────────────────────── цвета ────────────────────────────────


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def grad(stops, t):
    """Цвет из градиента stops в точке t ∈ [0, 1]."""
    t = max(0.0, min(1.0, t))
    n = len(stops) - 1
    i = min(int(t * n), n - 1)
    lt = t * n - i
    a, b = _rgb(stops[i]), _rgb(stops[i + 1])
    return "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * lt) for k in range(3))


def gradient_text(s, stops, bold=True):
    t = Text()
    n = max(1, len(s) - 1)
    for i, ch in enumerate(s):
        t.append(ch, style=("bold " if bold else "") + grad(stops, i / n))
    return t


# ───────────────────────────── большие цифры ────────────────────────────

FONT = {
    "0": ["███", "█ █", "█ █", "█ █", "███"],
    "1": ["██ ", " █ ", " █ ", " █ ", "███"],
    "2": ["███", "  █", "███", "█  ", "███"],
    "3": ["███", "  █", "███", "  █", "███"],
    "4": ["█ █", "█ █", "███", "  █", "  █"],
    "5": ["███", "█  ", "███", "  █", "███"],
    "6": ["███", "█  ", "███", "█ █", "███"],
    "7": ["███", "  █", "  █", "  █", "  █"],
    "8": ["███", "█ █", "███", "█ █", "███"],
    "9": ["███", "█ █", "███", "  █", "███"],
    ".": [" ", " ", " ", " ", "█"],
    "-": ["   ", "   ", "███", "   ", "   "],
}


def big_text(s, stops):
    rows = ["", "", "", "", ""]
    for ch in s:
        glyph = FONT.get(ch, FONT["-"])
        for r in range(5):
            rows[r] += glyph[r] + " "
    rows = [r[:-1] for r in rows]
    width = max(1, len(rows[0]) - 1)
    out = Text()
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            out.append(ch, style=grad(stops, c / width) if ch != " " else None)
        if r < 4:
            out.append("\n")
    return out


def fmt(v):
    if v is None:
        return "-"
    if v >= 100:
        return "%.0f" % v
    if v >= 10:
        return "%.1f" % v
    return "%.2f" % v


# ─────────────────────────────── состояние ──────────────────────────────


class State:
    def __init__(self, duration, do_upload):
        self.duration = duration
        self.do_upload = do_upload
        self.phase = "init"  # init → ping → download → upload → done
        self.phase_start = time.perf_counter()
        self.meta = {}
        self.ping_samples = []
        self.ping = None
        self.jitter = None
        self.current = None
        self.series = {"download": [], "upload": []}
        self.loaded = {"download": [], "upload": []}
        self.result = {"download": None, "upload": None}
        self.error = None

    def set_phase(self, p):
        self.phase = p
        self.phase_start = time.perf_counter()
        self.current = None

    @property
    def elapsed(self):
        return time.perf_counter() - self.phase_start


# ──────────────────────────────── сеть ──────────────────────────────────


def fetch_meta():
    try:
        c = http.client.HTTPSConnection(HOST, timeout=6)
        c.request("GET", "/meta", headers=HEADERS)
        data = json.loads(c.getresponse().read() or b"{}")
        c.close()
        return data
    except Exception:
        return {}


def one_rtt(conn):
    """RTT одного HTTP-запроса в мс (минус время обработки на сервере)."""
    t0 = time.perf_counter()
    conn.request("GET", "/__down?bytes=0", headers=HEADERS)
    r = conn.getresponse()
    r.read()
    rtt = (time.perf_counter() - t0) * 1000
    m = re.search(r"dur=([\d.]+)", r.getheader("Server-Timing") or "")
    if m:
        rtt -= float(m.group(1))
    return max(rtt, 0.1)


def jitter_of(samples):
    if len(samples) < 2:
        return 0.0
    return statistics.mean(abs(a - b) for a, b in zip(samples, samples[1:]))


def measure_ping(state, n=20):
    conn = http.client.HTTPSConnection(HOST, timeout=10)
    one_rtt(conn)  # прогрев: TCP + TLS
    for _ in range(n):
        rtt = one_rtt(conn)
        state.ping_samples.append(rtt)
        state.current = statistics.median(state.ping_samples)
        time.sleep(0.08)
    conn.close()
    state.ping = statistics.median(state.ping_samples)
    state.jitter = jitter_of(state.ping_samples)


class Meter:
    def __init__(self):
        self.bytes = 0
        self._lock = threading.Lock()

    def add(self, n):
        with self._lock:
            self.bytes += n


def download_worker(meter, stop, size=25_000_000):
    while not stop.is_set():
        conn = http.client.HTTPSConnection(HOST, timeout=10)
        try:
            while not stop.is_set():
                conn.request("GET", "/__down?bytes=%d" % size, headers=HEADERS)
                r = conn.getresponse()
                while not stop.is_set():
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    meter.add(len(chunk))
        except Exception:
            stop.wait(0.2)
        finally:
            conn.close()


PAYLOAD = memoryview(os.urandom(1 << 20))


class UploadBody:
    """Файлоподобное тело POST-запроса: считает отданные байты."""

    def __init__(self, size, meter, stop):
        self.left, self.meter, self.stop = size, meter, stop

    def read(self, n=-1):
        if self.stop.is_set():
            raise IOError("stopped")
        if self.left <= 0:
            return b""
        n = min(n if n and n > 0 else 65536, self.left, len(PAYLOAD))
        self.left -= n
        self.meter.add(n)
        return PAYLOAD[:n]


def upload_worker(meter, stop, size=8_000_000):
    while not stop.is_set():
        conn = http.client.HTTPSConnection(HOST, timeout=10, blocksize=65536)
        try:
            while not stop.is_set():
                h = dict(HEADERS)
                h["Content-Type"] = "application/octet-stream"
                h["Content-Length"] = str(size)
                conn.request("POST", "/__up", body=UploadBody(size, meter, stop), headers=h)
                conn.getresponse().read()
        except Exception:
            stop.wait(0.2)
        finally:
            conn.close()


def loaded_ping_worker(out, stop):
    try:
        conn = http.client.HTTPSConnection(HOST, timeout=5)
        one_rtt(conn)
        while not stop.is_set():
            out.append(one_rtt(conn))
            stop.wait(0.25)
        conn.close()
    except Exception:
        pass


def measure_transfer(state, kind, streams):
    worker = download_worker if kind == "download" else upload_worker
    meter, stop = Meter(), threading.Event()
    threads = [threading.Thread(target=worker, args=(meter, stop), daemon=True) for _ in range(streams)]
    threads.append(threading.Thread(target=loaded_ping_worker, args=(state.loaded[kind], stop), daemon=True))
    for t in threads:
        t.start()

    warmup = min(2.0, state.duration * 0.2)  # разгон TCP не учитываем
    t0 = time.perf_counter()
    window = deque()
    mark = None
    try:
        while True:
            time.sleep(SAMPLE_EVERY)
            now, b = time.perf_counter(), meter.bytes
            window.append((now, b))
            while now - window[0][0] > 0.75:
                window.popleft()
            if len(window) >= 2 and window[-1][0] > window[0][0]:
                inst = (window[-1][1] - window[0][1]) * 8 / (window[-1][0] - window[0][0]) / 1e6
                state.current = inst
                state.series[kind].append(inst)
            if mark is None and now - t0 >= warmup:
                mark = (now, b)
            if now - t0 >= state.duration:
                break
    finally:
        stop.set()
    if mark and now > mark[0]:
        state.result[kind] = (b - mark[1]) * 8 / (now - mark[0]) / 1e6
    else:
        state.result[kind] = state.current or 0.0


# ───────────────────────────────── UI ───────────────────────────────────


def mask_ip(ip):
    if not ip:
        return "?"
    if "." in ip:
        return ".".join(ip.split(".")[:3]) + ".•••"
    return ":".join(ip.split(":")[:3]) + ":••••"


def header(state):
    m = state.meta
    title = Text("  ") + gradient_text("⚡ S P E E D T E S T", THEMES["title"])
    title.append("   терминальный замер скорости", style=DIM)
    info = Text("  ")
    if m:
        colo = m.get("colo") or {}
        info.append(mask_ip(m.get("clientIp")), style="bold")
        info.append("  ·  ", style=FAINT)
        info.append(m.get("asOrganization", "?"))
        info.append("  ·  ", style=FAINT)
        info.append("%s, %s" % (m.get("city", "?"), m.get("country", "?")))
        info.append("  →  ", style=DIM)
        info.append("Cloudflare %s (%s)" % (colo.get("city", "?"), colo.get("iata", "?")), style="bold #b388ff")
    else:
        info.append("определяю сеть…", style=DIM)
    return Group(title, info)


def steps(state):
    order = [("ping", "ПИНГ"), ("download", "СКАЧИВАНИЕ")]
    if state.do_upload:
        order.append(("upload", "ОТДАЧА"))
    names = [k for k, _ in order]
    cur = names.index(state.phase) if state.phase in names else (len(names) if state.phase == "done" else -1)
    spin = SPINNER[int(time.time() * 12) % len(SPINNER)]
    t = Text("  ")
    for i, (key, label) in enumerate(order):
        color = THEMES[key][0]
        if i < cur:
            t.append("✔ " + label, style=color)
        elif i == cur:
            t.append(spin + " " + label, style="bold " + color)
        else:
            t.append("○ " + label, style=FAINT)
        if i < len(order) - 1:
            t.append("  ─────  ", style=FAINT)
    return t


def gauge(value, stops, width):
    top = 1000 if (value or 0) <= 1000 else 10000
    t = math.log10(max(value or 1, 1)) / math.log10(top)
    filled = int(round(t * width))
    bar = Text()
    for i in range(width):
        bar.append("━", style=grad(stops, i / max(1, width - 1)) if i < filled else FAINT)
    ticks = [" "] * width
    for v in (1, 10, 100, 1000, 10000):
        if v > top:
            break
        label = str(v) if v < 1000 else "%dG" % (v // 1000)
        pos = min(int(math.log10(v) / math.log10(top) * (width - 1)), width - len(label))
        for j, ch in enumerate(label):
            ticks[pos + j] = ch
    return Group(bar, Text("".join(ticks), style=DIM))


def chart(values, slots, width, height, stops, show_axis=True):
    cols = []
    for i in range(width):
        idx = int(i * slots / width)
        cols.append(values[idx] if idx < len(values) else None)
    known = [v for v in values if v is not None]
    vmax = max(known + [1e-9]) * 1.15
    label_w = 6 if show_axis else 0
    out = Text()
    for row in range(height - 1, -1, -1):
        if show_axis:
            lbl = fmt(vmax) if row == height - 1 else ("0" if row == 0 else "")
            out.append(lbl.rjust(label_w - 1) + " ", style=DIM)
            out.append("│", style=FAINT)
        for v in cols:
            if v is None:
                out.append("·" if row == 0 else " ", style=FAINT)
                continue
            fill = int(max(0, min(8, v / vmax * height * 8 - row * 8)))
            out.append(BLOCKS[fill], style=grad(stops, (row + 0.5) / height))
        if row:
            out.append("\n")
    return out


def big_with_unit(value, unit, label, stops):
    g = Table.grid(padding=(0, 2))
    g.add_column(vertical="bottom")
    g.add_column(vertical="bottom")
    side = Text()
    side.append(label + "\n", style=DIM)
    side.append(unit, style="bold " + stops[-1])
    g.add_row(big_text(fmt(value), stops), side)
    return g


def card(title, value, unit, key, active=False, sub=None):
    stops = THEMES[key]
    body = Text(justify="center")
    if value is None:
        if active:
            body.append(SPINNER[int(time.time() * 12) % len(SPINNER)], style=stops[0])
        else:
            body.append("—", style=FAINT)
    else:
        body.append(fmt(value), style="bold " + stops[0])
        body.append(" " + unit, style=DIM)
    return Panel(
        body,
        title=Text(title, style=stops[0] if (active or value is not None) else DIM),
        subtitle=Text(sub, style=DIM) if sub else None,
        box=box.ROUNDED,
        border_style=stops[0] if active else FAINT,
        padding=(0, 1),
    )


def loaded_sub(state, kind):
    s = state.loaded[kind]
    return "нагр. %s мс" % fmt(statistics.median(s)) if len(s) >= 3 else None


def cards(state):
    ph = state.phase
    ping = state.ping if state.ping is not None else (state.current if ph == "ping" else None)
    row = [
        card("ПИНГ", ping, "мс", "ping", ph == "ping"),
        card("ДЖИТТЕР", state.jitter, "мс", "ping", ph == "ping"),
        card("СКАЧИВАНИЕ", state.result["download"], "Мбит/с", "download", ph == "download", loaded_sub(state, "download")),
    ]
    if state.do_upload:
        row.append(card("ОТДАЧА", state.result["upload"], "Мбит/с", "upload", ph == "upload", loaded_sub(state, "upload")))
    g = Table.grid(expand=True, padding=(0, 1))
    for _ in row:
        g.add_column(ratio=1)
    g.add_row(*row)
    return g


def live_view(state, console):
    width = min(console.width, 90)
    chart_h = max(3, min(8, console.height - 21))
    inner = width - 4
    parts = [header(state), Text(""), steps(state), Text("")]
    ph = state.phase
    if ph == "ping":
        parts.append(big_with_unit(state.current, "мс", "задержка", THEMES["ping"]))
        parts.append(Text(""))
        parts.append(chart(state.ping_samples, 20, inner - 7, chart_h, THEMES["ping"]))
    elif ph in ("download", "upload"):
        stops = THEMES[ph]
        label = "скачивание" if ph == "download" else "отдача"
        label += "  %.1f / %d с" % (min(state.elapsed, state.duration), state.duration)
        parts.append(big_with_unit(state.current, "Мбит/с", label, stops))
        parts.append(gauge(state.current, stops, inner))
        slots = int(state.duration / SAMPLE_EVERY)
        parts.append(chart(state.series[ph], slots, inner - 7, chart_h, stops))
    else:
        parts.append(Text("  подключаюсь к Cloudflare…", style=DIM))
    parts += [Text(""), cards(state)]
    return Panel(Group(*parts), box=box.HEAVY, border_style=FAINT, width=width, padding=(0, 1))


# ─────────────────────────── итоговый экран ─────────────────────────────


def verdicts(state):
    dl = state.result["download"] or 0
    ul = state.result["upload"] if state.do_upload else None
    ping, jit = state.ping or 999, state.jitter or 999
    checks = [
        ("Видеозвонки HD", dl >= 5 and (ul is None or ul >= 3) and ping < 100),
        ("4K стриминг", dl >= 25),
        ("Онлайн-игры", ping <= 40 and jit <= 10),
        ("Облачный гейминг", dl >= 35 and ping <= 30),
        ("Стрим 1080p60", ul is not None and ul >= 8),
        ("Семья / 5+ устройств", dl >= 100),
    ]
    g = Table.grid(padding=(0, 3))
    g.add_column()
    g.add_column()
    g.add_column()
    cells = []
    for name, ok in checks:
        t = Text()
        t.append("✔ " if ok else "✖ ", style="bold #00ffa3" if ok else "#ff5c7a")
        t.append(name, style=None if ok else DIM)
        cells.append(t)
    for i in range(0, len(cells), 3):
        g.add_row(*cells[i : i + 3])
    return g


def bufferbloat(state):
    loaded = state.loaded["download"] + state.loaded["upload"]
    if not loaded or state.ping is None:
        return None
    delta = max(0.0, statistics.median(loaded) - state.ping)
    for limit, grade, color in ((5, "A+", "#00ffa3"), (30, "A", "#00ffa3"), (60, "B", "#c6ff4d"),
                                (200, "C", "#ffd84d"), (400, "D", "#ff8a3d")):
        if delta < limit:
            break
    else:
        grade, color = "F", "#ff5c7a"
    t = Text("  Bufferbloat: ")
    t.append(" %s " % grade, style="bold black on " + color)
    t.append("  задержка под нагрузкой +%s мс" % fmt(delta), style=DIM)
    return t


def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []


def save_history(state):
    hist = load_history()
    hist.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "ping": state.ping, "jitter": state.jitter,
        "download": state.result["download"], "upload": state.result["upload"],
        "isp": state.meta.get("asOrganization"),
        "colo": (state.meta.get("colo") or {}).get("iata"),
    })
    try:
        HISTORY_FILE.write_text(json.dumps(hist[-200:], ensure_ascii=False, indent=1))
    except Exception:
        pass
    return hist[:-1]


def delta_text(now, before, higher_is_better=True):
    if now is None or not before:
        return Text("")
    pct = (now - before) / before * 100
    good = pct >= 0 if higher_is_better else pct <= 0
    if abs(pct) < 3:
        return Text(" ≈", style=DIM)
    return Text(" %s%.0f%%" % ("▲" if pct > 0 else "▼", abs(pct)), style="#00ffa3" if good else "#ff5c7a")


def history_line(prev):
    if not prev:
        return None
    p = prev[-1]
    ts = datetime.fromisoformat(p["ts"]).strftime("%d.%m %H:%M")
    t = Text("  Прошлый замер %s:  " % ts, style=DIM)
    t.append("↓ %s" % fmt(p.get("download")), style=THEMES["download"][0])
    if p.get("upload"):
        t.append("  ↑ %s" % fmt(p["upload"]), style=THEMES["upload"][0])
    t.append("  ⟳ %s мс" % fmt(p.get("ping")), style=THEMES["ping"][0])
    return t


def final_view(state, console, prev):
    width = min(console.width, 90)
    inner = width - 4
    last = prev[-1] if prev else {}
    parts = [header(state), Text(""), steps(state), Text("")]

    heroes = Table.grid(expand=True, padding=(0, 2))
    col_w = inner // (2 if state.do_upload else 1) - 2
    blocks = []
    for kind in ("download", "upload") if state.do_upload else ("download",):
        stops = THEMES[kind]
        lbl = ("↓ скачивание" if kind == "download" else "↑ отдача")
        cap = Text(lbl, style="bold " + stops[0])
        cap.append_text(delta_text(state.result[kind], last.get(kind)))
        slots = int(state.duration / SAMPLE_EVERY)
        blocks.append(Group(
            cap,
            big_with_unit(state.result[kind], "Мбит/с", "", stops),
            Text(""),
            chart(state.series[kind], slots, col_w, 3, stops, show_axis=False),
        ))
        heroes.add_column(ratio=1)
    heroes.add_row(*blocks)
    parts += [heroes, Text(""), cards(state), Text(""), verdicts(state), Text("")]
    bb = bufferbloat(state)
    if bb:
        parts.append(bb)
    hl = history_line(prev)
    if hl:
        parts.append(hl)
    return Panel(Group(*parts), box=box.HEAVY, border_style=FAINT, width=width, padding=(0, 1),
                 subtitle=Text(" история: ~/%s " % HISTORY_FILE.name, style=FAINT))


# ──────────────────────────────── main ──────────────────────────────────


def main():
    ap = argparse.ArgumentParser(prog="speedtest", description="Стильный замер скорости интернета в терминале")
    ap.add_argument("-V", "--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("-d", "--duration", type=int, default=10, help="длительность каждого теста, сек (10)")
    ap.add_argument("-s", "--streams", type=int, default=6, help="параллельных потоков (6)")
    ap.add_argument("--no-upload", action="store_true", help="не мерить отдачу")
    ap.add_argument("--json", action="store_true", help="вывести результат в JSON без интерфейса")
    args = ap.parse_args()

    console = Console()
    state = State(max(3, args.duration), not args.no_upload)

    def run():
        state.meta = fetch_meta()
        state.set_phase("ping")
        measure_ping(state)
        state.set_phase("download")
        measure_transfer(state, "download", args.streams)
        if state.do_upload:
            state.set_phase("upload")
            measure_transfer(state, "upload", args.streams)
        state.set_phase("done")

    try:
        if args.json:
            run()
        else:
            with Live(get_renderable=lambda: live_view(state, console), console=console,
                      refresh_per_second=20, transient=True, screen=False):
                run()
    except KeyboardInterrupt:
        console.print("\n  [grey42]Прервано.[/]")
        return 130
    except Exception as e:
        console.print("\n  [bold #ff5c7a]Ошибка сети:[/] %s" % e)
        return 1

    prev = save_history(state)
    if args.json:
        print(json.dumps({"ping_ms": state.ping, "jitter_ms": state.jitter,
                          "download_mbps": state.result["download"], "upload_mbps": state.result["upload"],
                          "server": (state.meta.get("colo") or {}).get("iata")}, indent=2))
    else:
        console.print(final_view(state, console, prev))
    return 0


def cli():
    sys.exit(main())


if __name__ == "__main__":
    cli()

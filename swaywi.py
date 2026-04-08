#!/usr/bin/env python3
"""
swaytop.py — a "top"-like TUI for Sway windows (swaymsg -t get_tree)

Columns (default):
WS  | F | FL | ID | APP/CLASS | PID | GEOM (WxH+X+Y) | TITLE

Keys:
  q / ESC        : quit
  r              : refresh now
  +/-            : increase/decrease refresh interval
  SPACE          : pause/resume auto-refresh
  f              : filter by substring (app/class/title/workspace)
  c              : clear filter
  s              : cycle sort (ws, app, title, pid, id)
  t              : toggle show title column
  h              : toggle help footer
  j/k or ↓/↑     : move selection
  PgUp / PgDn    : move selection by page
  Home / End     : jump to top / bottom
"""

import curses
import json
import locale
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

locale.setlocale(locale.LC_ALL, "")


@dataclass
class WinRow:
    ws: str
    focused: bool
    floating: bool
    con_id: int
    app: str
    pid: Optional[int]
    geom: str
    title: str


SORT_MODES = ["ws", "app", "title", "pid", "id"]

CW_WS = 8
CW_F = 1
CW_FL = 2
CW_ID = 10
CW_APPCLASS = 30
CW_ID = 7
CW_PEOM = 18


def run_swaymsg_tree() -> Dict[str, Any]:
    p = subprocess.run(
        ["swaymsg", "-t", "get_tree", "-r"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "swaymsg failed")
    return json.loads(p.stdout)


def is_real_window(node: Dict[str, Any]) -> bool:
    t = node.get("type")
    if t not in ("con", "floating_con"):
        return False

    if node.get("app_id"):
        return True

    wp = node.get("window_properties") or {}
    if wp.get("class"):
        return True

    if node.get("window") is not None:
        return True

    if (node.get("nodes") == [] and node.get("floating_nodes") == []) and node.get("name"):
        return True

    return False


def get_app_label(node: Dict[str, Any]) -> str:
    if node.get("app_id"):
        return str(node["app_id"])

    wp = node.get("window_properties") or {}
    if wp.get("class"):
        return str(wp["class"])

    if node.get("window") is not None:
        return f"x11:{node['window']}"

    return node.get("type", "-")


def get_title(node: Dict[str, Any]) -> str:
    name = node.get("name")
    return str(name) if name else "-"


def rect_str(node: Dict[str, Any]) -> str:
    r = node.get("rect") or {}
    w = r.get("width", 0)
    h = r.get("height", 0)
    x = r.get("x", 0)
    y = r.get("y", 0)
    return f"{w}x{h}+{x}+{y}"


def walk(node: Dict[str, Any], fn):
    fn(node)
    for child in node.get("nodes", []) or []:
        walk(child, fn)
    for child in node.get("floating_nodes", []) or []:
        walk(child, fn)


def find_focused_workspace(tree: Dict[str, Any]) -> Optional[str]:
    focused_ws = None

    def _fn(n: Dict[str, Any]):
        nonlocal focused_ws
        if n.get("type") == "workspace" and n.get("focused"):
            focused_ws = n.get("name")

    walk(tree, _fn)
    return focused_ws


def collect_windows(tree: Dict[str, Any]) -> List[WinRow]:
    focused_ws = find_focused_workspace(tree) or "-"
    rows: List[WinRow] = []

    def walk2(n: Dict[str, Any], current_ws: str, floating_ctx: bool):
        if n.get("type") == "workspace":
            current_ws = n.get("name", "-")
            floating_ctx = False

        if is_real_window(n):
            rows.append(
                WinRow(
                    ws=current_ws,
                    focused=bool(n.get("focused")),
                    floating=floating_ctx,
                    con_id=int(n.get("id", 0)),
                    app=get_app_label(n),
                    pid=n.get("pid"),
                    geom=rect_str(n),
                    title=get_title(n),
                )
            )

        for ch in n.get("nodes", []) or []:
            walk2(ch, current_ws, floating_ctx)

        for ch in n.get("floating_nodes", []) or []:
            walk2(ch, current_ws, True)

    walk2(tree, "-", False)

    if all(r.ws == "-" for r in rows):
        for i in range(len(rows)):
            rows[i].ws = focused_ws

    return rows


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def safe_addnstr(win, y: int, x: int, s: str, maxw: int, attr: int = 0):
    if maxw <= 0:
        return
    try:
        win.addnstr(y, x, s, maxw, attr)
    except curses.error:
        pass


def get_body_height(screen_h: int, help_on: bool) -> int:
    body_h = screen_h - 3 - (1 if help_on else 0)
    return max(body_h, 1)


def ensure_visible(selected: int, scroll: int, body_h: int, total: int) -> int:
    if total <= 0:
        return 0

    max_scroll = max(0, total - body_h)

    if selected < scroll:
        scroll = selected
    elif selected >= scroll + body_h:
        scroll = selected - body_h + 1

    return clamp(scroll, 0, max_scroll)


def apply_filter(rows: List[WinRow], filt: str) -> List[WinRow]:
    if not filt:
        return rows
    f = filt.lower()
    out = []
    for r in rows:
        blob = f"{r.ws} {r.app} {r.title} {r.con_id} {r.pid}"
        if f in blob.lower():
            out.append(r)
    return out


def apply_sort(rows: List[WinRow], mode: str) -> List[WinRow]:
    if mode == "ws":
        return sorted(rows, key=lambda r: (r.ws, not r.focused, r.app, r.title))
    if mode == "app":
        return sorted(rows, key=lambda r: (r.app, r.ws, not r.focused, r.title))
    if mode == "title":
        return sorted(rows, key=lambda r: (r.title, r.ws, not r.focused, r.app))
    if mode == "pid":
        return sorted(rows, key=lambda r: (r.pid is None, r.pid or 0, r.ws, r.app))
    if mode == "id":
        return sorted(rows, key=lambda r: (r.con_id,))
    return rows


class Theme:
    def __init__(self):
        self.bg_fill = curses.A_NORMAL
        self.header = curses.A_REVERSE | curses.A_BOLD
        self.col_header_bar = curses.A_REVERSE
        self.col_header_text = curses.A_REVERSE | curses.A_BOLD
        self.footer = curses.A_REVERSE
        self.help = curses.A_DIM
        self.normal = curses.A_NORMAL
        self.selected = curses.A_REVERSE
        self.selected_focused = curses.A_REVERSE | curses.A_BOLD
        self.prompt = curses.A_REVERSE | curses.A_BOLD
        self.empty = curses.A_DIM
        self.match = curses.A_UNDERLINE | curses.A_BOLD
        self.marker_focused = curses.A_BOLD
        self.marker_floating = curses.A_NORMAL


def build_row_segments(r: WinRow, show_title: bool) -> List[Tuple[str, str]]:
    ws = (r.ws or "-")[:CW_WS].ljust(CW_WS)
    focused = "●" if r.focused else " "
    fl = "Y" if r.floating else "N"
    conid = str(r.con_id).ljust(CW_ID)[:CW_ID]
    app = (r.app or "-")[:CW_APPCLASS].ljust(CW_APPCLASS)
    pid = (str(r.pid) if r.pid is not None else "-").rjust(CW_ID)[:CW_ID]
    geom = (r.geom or "-")[:CW_PEOM].ljust(CW_PEOM)

    segments: List[Tuple[str, str]] = [
        (ws, "search"),
        (" ", "plain"),
        (focused, "focused_marker"),
        (" ", "plain"),
        (fl.ljust(2), "floating_marker"),
        (" ", "plain"),
        (conid, "search"),
        (" ", "plain"),
        (app, "search"),
        (" ", "plain"),
        (pid, "search"),
        (" ", "plain"),
        (geom, "plain"),
    ]

    if show_title:
        segments.append((" ", "plain"))
        segments.append((r.title or "-", "search"))

    return segments


def fit_segments_to_width(segments: List[Tuple[str, str]], width: int) -> List[Tuple[str, str]]:
    if width <= 0:
        return []

    out: List[Tuple[str, str]] = []
    remaining = width

    for text, kind in segments:
        if remaining <= 0:
            break
        piece = text[:remaining]
        if piece:
            out.append((piece, kind))
            remaining -= len(piece)

    if remaining > 0:
        out.append((" " * remaining, "plain"))

    return out


def find_matches(text: str, needle: str) -> List[Tuple[int, int]]:
    if not text or not needle:
        return []

    hay = text.lower()
    ndl = needle.lower()
    matches: List[Tuple[int, int]] = []
    start = 0

    while True:
        idx = hay.find(ndl, start)
        if idx == -1:
            break
        matches.append((idx, idx + len(needle)))
        start = idx + len(needle)

    return matches


def draw_segment_with_matches(
    win,
    y: int,
    x: int,
    text: str,
    kind: str,
    base_attr: int,
    theme: Theme,
    needle: str,
    allow_match_highlight: bool,
):
    if not text:
        return 0

    if kind == "focused_marker":
        attr = base_attr if (base_attr & curses.A_REVERSE) else theme.marker_focused
        safe_addnstr(win, y, x, text, len(text), attr)
        return len(text)

    if kind == "floating_marker":
        attr = base_attr if (base_attr & curses.A_REVERSE) else theme.marker_floating
        safe_addnstr(win, y, x, text, len(text), attr)
        return len(text)

    if kind != "search" or not needle or not allow_match_highlight:
        safe_addnstr(win, y, x, text, len(text), base_attr)
        return len(text)

    matches = find_matches(text, needle)
    if not matches:
        safe_addnstr(win, y, x, text, len(text), base_attr)
        return len(text)

    cursor = 0
    for start, end in matches:
        if start > cursor:
            piece = text[cursor:start]
            safe_addnstr(win, y, x + cursor, piece, len(piece), base_attr)
        piece = text[start:end]
        safe_addnstr(win, y, x + start, piece, len(piece), theme.match)
        cursor = end

    if cursor < len(text):
        piece = text[cursor:]
        safe_addnstr(win, y, x + cursor, piece, len(piece), base_attr)

    return len(text)


def draw_row(
    win,
    y: int,
    width: int,
    r: WinRow,
    show_title: bool,
    filter_text: str,
    theme: Theme,
    is_selected: bool,
):
    if width <= 0:
        return

    base_attr = theme.selected_focused if (is_selected and r.focused) else theme.selected if is_selected else theme.normal

    safe_addnstr(win, y, 0, " " * width, width, base_attr)

    segments = build_row_segments(r, show_title)
    segments = fit_segments_to_width(segments, width)

    x = 0
    allow_match_highlight = not is_selected

    for text, kind in segments:
        consumed = draw_segment_with_matches(
            win=win,
            y=y,
            x=x,
            text=text,
            kind=kind,
            base_attr=base_attr,
            theme=theme,
            needle=filter_text,
            allow_match_highlight=allow_match_highlight,
        )
        x += consumed
        if x >= width:
            break


def draw(
    stdscr,
    theme: Theme,
    rows: List[WinRow],
    status: str,
    help_on: bool,
    paused: bool,
    interval: float,
    sort_mode: str,
    filter_text: str,
    show_title: bool,
    scroll: int,
    selected: int,
):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    content_w = max(0, w - 1)

    for yy in range(h):
        safe_addnstr(stdscr, yy, 0, " " * content_w, content_w, theme.bg_fill)

    header = (
        f" swaytop | windows: {len(rows)} | interval: {interval:.1f}s | "
        f"sort: {sort_mode} | filter: {filter_text or '-'} | "
        f"{'PAUSED' if paused else 'LIVE'} "
    )
    safe_addnstr(stdscr, 0, 0, header.ljust(content_w), content_w, theme.header)

    safe_addnstr(stdscr, 1, 0, " " * content_w, content_w, theme.col_header_bar)

    cols = [
        ("WS", CW_WS),
        ("F", CW_F),
        ("FL", CW_FL),
        ("ID", CW_ID),
        ("APP/CLASS", CW_APPCLASS),
        ("PID", CW_ID),
        ("GEOM", CW_PEOM),
    ]
    if show_title:
        cols.append(("TITLE", content_w))

    x = 0
    for name, width in cols:
        if x >= content_w:
            break
        if name == "TITLE":
            safe_addnstr(stdscr, 1, x, name, max(0, content_w - x), theme.col_header_text)
        else:
            safe_addnstr(stdscr, 1, x, name.ljust(width)[:width], width, theme.col_header_text)
            x += width + 1

    y = 2
    body_h = get_body_height(h, help_on)
    visible = rows[scroll:scroll + body_h]

    if not visible:
        empty_msg = "No windows"
        safe_addnstr(stdscr, y, 0, empty_msg.ljust(content_w), content_w, theme.empty)

    for i, r in enumerate(visible):
        line_y = y + i
        if line_y >= h - 1:
            break

        absolute_idx = scroll + i
        is_selected = absolute_idx == selected

        draw_row(
            win=stdscr,
            y=line_y,
            width=content_w,
            r=r,
            show_title=show_title,
            filter_text=filter_text,
            theme=theme,
            is_selected=is_selected,
        )

    if help_on and h >= 4:
        help_line = (
            " q/ESC quit | r refresh | +/- interval | SPACE pause | "
            "f filter | c clear | s sort | t title | j/k move | PgUp/PgDn page | h help "
        )
        safe_addnstr(stdscr, h - 2, 0, help_line.ljust(content_w), content_w, theme.help)

    safe_addnstr(stdscr, h - 1, 0, status.ljust(content_w), content_w, theme.footer)
    stdscr.refresh()


def prompt(stdscr, theme: Theme, prompt_text: str) -> str:
    curses.echo()
    stdscr.nodelay(False)

    h, w = stdscr.getmaxyx()
    content_w = max(0, w - 1)

    try:
        stdscr.move(h - 1, 0)
        stdscr.clrtoeol()
    except curses.error:
        pass

    safe_addnstr(stdscr, h - 1, 0, prompt_text.ljust(content_w), content_w, theme.prompt)

    x = min(len(prompt_text), max(0, w - 2))
    try:
        stdscr.move(h - 1, x)
    except curses.error:
        pass

    stdscr.refresh()

    try:
        buf = stdscr.getstr(h - 1, x, max(1, w - x - 1))
    except curses.error:
        buf = b""

    stdscr.nodelay(True)
    curses.noecho()

    try:
        return buf.decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def main(stdscr):
    try:
        curses.curs_set(0)
    except curses.error:
        pass

    stdscr.nodelay(True)
    stdscr.keypad(True)

    theme = Theme()

    interval = 1.0
    paused = False
    help_on = True
    show_title = True

    sort_idx = 0
    sort_mode = SORT_MODES[sort_idx]
    filter_text = ""

    scroll = 0
    selected = 0

    status = "loading..."
    last_refresh = 0.0
    rows_all: List[WinRow] = []
    rows_view: List[WinRow] = []

    def refresh():
        nonlocal status, rows_all, rows_view, last_refresh, scroll, selected
        try:
            tree = run_swaymsg_tree()
            rows_all = collect_windows(tree)
            rows_view = apply_sort(apply_filter(rows_all, filter_text), sort_mode)
            last_refresh = time.time()
            status = f"OK  {len(rows_view)}/{len(rows_all)} windows"

            if rows_view:
                selected = clamp(selected, 0, len(rows_view) - 1)
            else:
                selected = 0
                scroll = 0

            h, _ = stdscr.getmaxyx()
            body_h = get_body_height(h, help_on)
            scroll = ensure_visible(selected, scroll, body_h, len(rows_view))
        except Exception as e:
            status = f"ERR {e}"

    refresh()

    while True:
        now = time.time()
        if not paused and (now - last_refresh) >= interval:
            refresh()

        h, _ = stdscr.getmaxyx()
        body_h = get_body_height(h, help_on)
        scroll = ensure_visible(selected, scroll, body_h, len(rows_view))

        draw(
            stdscr=stdscr,
            theme=theme,
            rows=rows_view,
            status=status,
            help_on=help_on,
            paused=paused,
            interval=interval,
            sort_mode=sort_mode,
            filter_text=filter_text,
            show_title=show_title,
            scroll=scroll,
            selected=selected,
        )

        try:
            k = stdscr.getch()
        except Exception:
            k = -1

        if k == -1:
            time.sleep(0.03)
            continue

        if k in (27, ord("q")):
            break

        if k == ord("r"):
            refresh()

        elif k == ord(" "):
            paused = not paused
            status = "PAUSED" if paused else "LIVE"

        elif k in (ord("+"), ord("=")):
            interval = min(10.0, interval + 0.5)
            status = f"interval: {interval:.1f}s"

        elif k in (ord("-"), ord("_")):
            interval = max(0.2, interval - 0.5)
            status = f"interval: {interval:.1f}s"

        elif k == ord("h"):
            help_on = not help_on
            body_h = get_body_height(stdscr.getmaxyx()[0], help_on)
            scroll = ensure_visible(selected, scroll, body_h, len(rows_view))

        elif k == ord("t"):
            show_title = not show_title

        elif k == ord("c"):
            filter_text = ""
            rows_view = apply_sort(apply_filter(rows_all, filter_text), sort_mode)
            selected = 0
            scroll = 0
            status = "filter cleared"

        elif k == ord("f"):
            s = prompt(stdscr, theme, "Filter: ")
            filter_text = s
            rows_view = apply_sort(apply_filter(rows_all, filter_text), sort_mode)
            selected = 0
            scroll = 0
            status = f"filter: {filter_text or '-'}"

        elif k == ord("s"):
            sort_idx = (sort_idx + 1) % len(SORT_MODES)
            sort_mode = SORT_MODES[sort_idx]
            rows_view = apply_sort(apply_filter(rows_all, filter_text), sort_mode)
            if rows_view:
                selected = clamp(selected, 0, len(rows_view) - 1)
            else:
                selected = 0
                scroll = 0
            status = f"sort: {sort_mode}"

        elif k in (ord("j"), curses.KEY_DOWN):
            if rows_view:
                selected = clamp(selected + 1, 0, len(rows_view) - 1)

        elif k in (ord("k"), curses.KEY_UP):
            if rows_view:
                selected = clamp(selected - 1, 0, len(rows_view) - 1)

        elif k == curses.KEY_NPAGE:
            if rows_view:
                selected = clamp(selected + body_h, 0, len(rows_view) - 1)

        elif k == curses.KEY_PPAGE:
            if rows_view:
                selected = clamp(selected - body_h, 0, len(rows_view) - 1)

        elif k == curses.KEY_HOME:
            if rows_view:
                selected = 0

        elif k == curses.KEY_END:
            if rows_view:
                selected = len(rows_view) - 1


if __name__ == "__main__":
    curses.wrapper(main)

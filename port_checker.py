"""PortWatch Hormuz monitor with a configurable market window.

Run `python port_checker.py --help` for full usage and examples.
"""

import argparse
import atexit
import platform
import subprocess
import sys
import time
from datetime import datetime, date, timedelta

import requests

PORTWATCH_URL = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
    "Daily_Chokepoints_Data/FeatureServer/0/query"
)
# Same layer, but the bare endpoint returns service metadata including
# editingInfo.dataLastEditDate / lastEditDate (ms-since-epoch timestamps).
PORTWATCH_LAYER_URL = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
    "Daily_Chokepoints_Data/FeatureServer/0"
)

# ANSI escape codes
CLEAR_SCREEN = '\033[2J\033[3J\033[H'  # Clear screen + scrollback, move cursor home
HOME = '\033[H'                         # Move cursor to top-left (no clear)
CLEAR_LINE = '\033[K'                   # Clear from cursor to end of line
HIDE_CURSOR = '\033[?25l'
SHOW_CURSOR = '\033[?25h'
BOLD = '\033[1m'
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
CYAN = '\033[96m'
MAGENTA = '\033[95m'
RESET = '\033[0m'
DIM = '\033[2m'

# Validation bounds
MIN_DAYS = 1
MAX_DAYS = 99            # cap on both --window-days and --pull-days
MIN_INTERVAL = 1
MAX_INTERVAL = 3600
DEFAULT_WINDOW_DAYS = 7
DEFAULT_PULL_BUFFER = 7  # extra days fetched beyond the window length
LAG_TOLERANCE_DAYS = 14  # how far behind today PortWatch may be publishing


# ---------------------------------------------------------------------------
# CLI parsing & validation
# ---------------------------------------------------------------------------

def _date_arg(s):
    """argparse type for YYYY-MM-DD dates."""
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r}. Expected YYYY-MM-DD (e.g. 2026-05-04)."
        )


def _bounded_int(name, lo, hi):
    """Build an argparse type that accepts integers in [lo, hi]."""
    def _parser(s):
        try:
            n = int(s)
        except (ValueError, TypeError):
            raise argparse.ArgumentTypeError(
                f"invalid {name} {s!r}: must be an integer between {lo} and {hi}."
            )
        if n < lo or n > hi:
            raise argparse.ArgumentTypeError(
                f"invalid {name} {n}: must be between {lo} and {hi} (inclusive)."
            )
        return n
    return _parser


_days_arg = _bounded_int('number of days', MIN_DAYS, MAX_DAYS)
_interval_arg = _bounded_int('interval (seconds)', MIN_INTERVAL, MAX_INTERVAL)


HELP_EPILOG = f"""\
Examples:
  # Watch the Apr 27 – May 3 window with default pull
  python port_checker.py --start 2026-04-27

  # This week (Mon–Sun) with a 5-day window
  python port_checker.py --start 2026-05-04 --end 2026-05-08

  # Same as above but specifying length instead of end date
  python port_checker.py --start 2026-05-04 --window-days 5

  # Pull a wider history so older windows are still in the response
  python port_checker.py --start 2026-04-20 --window-days 7 --pull-days 30

  # Run a single fetch and exit (useful for cron / CI)
  python port_checker.py --start 2026-05-04 --once

  # Group rows into 7-day chunks with a Σ summary after each
  python port_checker.py --start 2026-05-04 --pull-days 21 --show-week-totals

Validation rules:
  * --start is required and must be a valid YYYY-MM-DD date.
  * --end is mutually exclusive with --window-days. If neither is given,
    the window defaults to {DEFAULT_WINDOW_DAYS} days starting at --start.
  * --end must be on or after --start.
  * --window-days and --pull-days are integers in [{MIN_DAYS}, {MAX_DAYS}].
  * The window must fit inside the pulled days (--pull-days ≥ window length),
    AND the start date must lie within the most recent --pull-days days, since
    the API returns the latest N days only.
  * --interval is in [{MIN_INTERVAL}, {MAX_INTERVAL}] seconds.

Change signaling:
  * Single beep the first time every day in the window has data.
  * Double beep if the total then changes (data was revised).
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog='port_checker.py',
        description=(
            'Live PortWatch monitor for a configurable market window. '
            'Fetches the latest N days from the ArcGIS Daily_Chokepoints feed '
            'and highlights ships transiting on a target date range.'
        ),
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        '--start', type=_date_arg, required=True,
        help='Start date of the market window (YYYY-MM-DD). REQUIRED.',
    )

    end_group = p.add_mutually_exclusive_group()
    end_group.add_argument(
        '--end', type=_date_arg, default=None,
        help='End date of the market window, inclusive (YYYY-MM-DD). '
             'Mutually exclusive with --window-days.',
    )
    end_group.add_argument(
        '--window-days', type=_days_arg, default=None, dest='window_days',
        help=f'Length of the window in days, {MIN_DAYS}-{MAX_DAYS}. '
             f'Mutually exclusive with --end. Default: {DEFAULT_WINDOW_DAYS}.',
    )

    p.add_argument(
        '--pull-days', type=_days_arg, default=None, dest='pull_days',
        help=f'How many days to fetch from the API, {MIN_DAYS}-{MAX_DAYS}. '
             f'Default: window length + {DEFAULT_PULL_BUFFER} (min 14).',
    )

    p.add_argument(
        '--interval', type=_interval_arg, default=20,
        help=f'Refresh interval in seconds, {MIN_INTERVAL}-{MAX_INTERVAL}. '
             f'Default: 20.',
    )

    p.add_argument(
        '--once', action='store_true',
        help='Run a single fetch and exit (no live loop). Handy for cron.',
    )

    p.add_argument(
        '--show-week-totals', action='store_true', dest='week_totals',
        help='Group rows into 7-day chunks with a Σ summary line after each. '
             'A trailing partial chunk is summarized over the days it contains.',
    )

    return p


def fail(message):
    """Print a red error, point the user at --help, and exit non-zero."""
    sys.stderr.write(f"{RED}error: {message}{RESET}\n")
    sys.stderr.write(
        f"Run with {BOLD}--help{RESET} (or {BOLD}-h{RESET}) for usage and examples.\n"
    )
    sys.exit(2)


def resolve_window(args, today=None):
    """Apply defaults and cross-field validation. Returns (start, end, pull_days).

    `today` is injectable for testing.
    """
    today = today or date.today()
    start = args.start

    if args.end is not None:
        end = args.end
    elif args.window_days is not None:
        end = start + timedelta(days=args.window_days - 1)
    else:
        end = start + timedelta(days=DEFAULT_WINDOW_DAYS - 1)

    if end < start:
        fail(f"--end ({end}) must be on or after --start ({start}).")

    window_len = (end - start).days + 1
    if window_len > MAX_DAYS:
        fail(
            f"window length is {window_len} days; maximum is {MAX_DAYS}. "
            f"Shorten --end or --window-days."
        )

    if args.pull_days is not None:
        pull_days = args.pull_days
    else:
        pull_days = min(MAX_DAYS, max(14, window_len + DEFAULT_PULL_BUFFER))

    if pull_days < window_len:
        fail(
            f"--pull-days ({pull_days}) must be at least the window length "
            f"({window_len}). The window must fit inside the pulled days."
        )

    # The API returns the latest N records ordered by date DESC. The newest
    # record may not be today — PortWatch typically publishes a few days
    # behind. So the reachable range is roughly
    #   [latest_published - pull_days + 1, latest_published]
    # and `latest_published` ≈ today minus some lag (often 1-5 days, but we
    # allow up to LAG_TOLERANCE_DAYS to be safe). Reject only when the start
    # is clearly outside that envelope; warn for borderline cases.
    if start <= today:
        days_back = (today - start).days
        if days_back > pull_days + LAG_TOLERANCE_DAYS:
            fail(
                f"--start is {days_back} days ago and --pull-days={pull_days}; "
                f"even allowing {LAG_TOLERANCE_DAYS} days of publishing lag the "
                f"window can't appear in the API response. Increase --pull-days "
                f"(max {MAX_DAYS}) or pick a more recent --start."
            )
        elif days_back >= pull_days:
            sys.stderr.write(
                f"{YELLOW}warning: --start is {days_back} days ago, at or past "
                f"--pull-days={pull_days}. If PortWatch is publishing close to "
                f"today the window may not appear; if it's running a few days "
                f"behind (the usual case), it should be fine. Increase "
                f"--pull-days if you want a hard guarantee.{RESET}\n"
            )

    if start > today + timedelta(days=14):
        sys.stderr.write(
            f"{YELLOW}warning: --start {start} is more than 14 days in the "
            f"future; PortWatch usually publishes with a 1-2 day delay.{RESET}\n"
        )

    return start, end, pull_days


# ---------------------------------------------------------------------------
# Terminal & beeper helpers
# ---------------------------------------------------------------------------

def beep(times=1):
    """Play an audible alert. Falls back to terminal bell if system sound unavailable."""
    system = platform.system()
    for _ in range(times):
        try:
            if system == 'Darwin':  # macOS
                subprocess.run(
                    ['afplay', '/System/Library/Sounds/Glass.aiff'],
                    check=False, timeout=2,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            elif system == 'Windows':
                import winsound
                winsound.Beep(1000, 300)
            else:  # Linux and other
                sys.stdout.write('\a')
                sys.stdout.flush()
        except Exception:
            sys.stdout.write('\a')
            sys.stdout.flush()


def cleanup_terminal():
    """Restore cursor visibility on exit."""
    sys.stdout.write(SHOW_CURSOR)
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# API & parsing
# ---------------------------------------------------------------------------

def fetch_data(pull_days):
    """Fetch the latest `pull_days` records from PortWatch."""
    params = {
        'where': "portid='chokepoint6'",
        'outFields': '*',
        'f': 'json',
        'resultRecordCount': pull_days,
        'orderByFields': 'date DESC',
        'returnGeometry': 'false',
    }
    try:
        response = requests.get(PORTWATCH_URL, params=params, timeout=10)
        return response.json()
    except Exception as e:
        return {'error': str(e)}


def fetch_layer_meta(timeout=5):
    """Best-effort fetch of layer metadata. Returns (datetime_utc | None, error | None).

    Reads editingInfo.dataLastEditDate when available (the time the data was
    last refreshed) with editingInfo.lastEditDate as a fallback (any layer
    edit, including schema changes).
    """
    try:
        r = requests.get(PORTWATCH_LAYER_URL, params={'f': 'json'}, timeout=timeout)
        info = (r.json().get('editingInfo') or {})
        ms = info.get('dataLastEditDate') or info.get('lastEditDate')
        if not ms:
            return None, None
        return datetime.utcfromtimestamp(ms / 1000), None
    except Exception as e:
        return None, str(e)


def humanize_age(dt_utc, now_utc=None):
    """Format a UTC datetime as a relative age like '40m ago', '16h ago', '3d ago'."""
    now_utc = now_utc or datetime.utcnow()
    secs = (now_utc - dt_utc).total_seconds()
    if secs < 0:
        return "in the future"
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def parse_records(data):
    """Parse API response into list of records sorted ascending by date."""
    if 'features' not in data:
        return []

    records = []
    for feature in data['features']:
        attrs = feature['attributes']

        # Handle date - API may return millisecond timestamp (int/numeric string)
        # or pre-formatted date string like '2026-04-26'.
        date_raw = attrs['date']
        if isinstance(date_raw, str):
            try:
                date_ms = int(date_raw)
                date_str = datetime.utcfromtimestamp(date_ms / 1000).strftime('%Y-%m-%d')
            except ValueError:
                date_str = date_raw[:10]
        else:
            date_str = datetime.utcfromtimestamp(date_raw / 1000).strftime('%Y-%m-%d')

        container = attrs.get('n_container') or 0
        dry_bulk = attrs.get('n_dry_bulk') or 0
        general_cargo = attrs.get('n_general_cargo') or 0
        roro = attrs.get('n_roro') or 0
        tanker = attrs.get('n_tanker') or 0
        total = container + dry_bulk + general_cargo + roro + tanker

        records.append({
            'date': date_str,
            'container': container,
            'dry_bulk': dry_bulk,
            'general_cargo': general_cargo,
            'roro': roro,
            'tanker': tanker,
            'total': total,
        })

    records.sort(key=lambda x: x['date'])
    return records


def determine_bucket(total):
    """Determine Polymarket bucket."""
    if total < 25:
        return "<25"
    elif total < 50:
        return "25-49"
    elif total < 75:
        return "50-74"
    elif total < 100:
        return "75-99"
    elif total < 125:
        return "100-124"
    elif total < 150:
        return "125-149"
    else:
        return "150+"


def get_bucket_color(bucket):
    """Color code buckets."""
    colors = {
        '<25': RED,
        '25-49': YELLOW,
        '50-74': GREEN,
        '75-99': CYAN,
        '100-124': BLUE,
        '125-149': MAGENTA,
        '150+': MAGENTA,
    }
    return colors.get(bucket, RESET)


def format_window_title(start, end):
    """Human-friendly title like 'Apr 27 - May 3' or 'May 4' for a single day."""
    def fmt(d):
        return d.strftime('%b %d').replace(' 0', ' ')
    if start == end:
        return fmt(start)
    return f"{fmt(start)} – {fmt(end)}"


def window_dates(start, end):
    """Return list of YYYY-MM-DD strings from start to end inclusive."""
    out = []
    d = start
    while d <= end:
        out.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)
    return out


def chunk_records(records, size=7):
    """Split records into consecutive groups of `size`. Last group may be shorter."""
    return [records[i:i + size] for i in range(0, len(records), size)]


def summarize_chunk(chunk):
    """Sum each ship-type column across a chunk. Returns a dict of totals + meta."""
    keys = ('container', 'dry_bulk', 'general_cargo', 'roro', 'tanker', 'total')
    sums = {k: sum(r[k] for r in chunk) for k in keys}
    sums['n_days'] = len(chunk)
    sums['first_date'] = chunk[0]['date']
    sums['last_date'] = chunk[-1]['date']
    sums['partial'] = len(chunk) < 7
    return sums


def _short_date(yyyy_mm_dd):
    """'2026-04-25' -> 'Apr 25' (no leading zero)."""
    d = datetime.strptime(yyyy_mm_dd, '%Y-%m-%d').date()
    return d.strftime('%b %d').replace(' 0', ' ')


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def display(records, window_start, window_end, pull_days, interval,
            show_loop_hint=True, last_total=None, last_check_time=None,
            error=None, week_totals=False, portwatch_updated=None):
    """Render the dashboard. Returns the market_total when window is complete, else None.

    The whole frame is buffered to a string and written in one syscall after
    a full screen-clear. This avoids the artifact where, when the dashboard
    is taller than the terminal viewport, an in-place HOME-only redraw lands
    inside scrolled content and successive frames stack.
    """
    buf = [CLEAR_SCREEN]  # full clear (viewport + scrollback) every frame

    def line(text=''):
        buf.append(text + CLEAR_LINE + '\n')

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    title = f"PORTWATCH HORMUZ MONITOR - Last {pull_days} Days"
    line(f"{BOLD}{CYAN}╔═══════════════════════════════════════════════════════════════════╗{RESET}")
    line(f"{BOLD}{CYAN}║  🚢 {title:<62}║{RESET}")
    line(f"{BOLD}{CYAN}╚═══════════════════════════════════════════════════════════════════╝{RESET}")
    line(f"{DIM}Last check:        {now}{RESET}")

    if portwatch_updated is not None:
        age = humanize_age(portwatch_updated)
        stamp = portwatch_updated.strftime('%Y-%m-%d %H:%M UTC')
        line(f"{DIM}PortWatch updated: {stamp} ({age}){RESET}")
    else:
        line(f"{DIM}PortWatch updated: unknown{RESET}")

    if records:
        latest = max(r['date'] for r in records)
        try:
            latest_dt = datetime.strptime(latest, '%Y-%m-%d').date()
            days_old = (date.today() - latest_dt).days
            age_label = "today" if days_old == 0 else (
                "1d ago" if days_old == 1 else f"{days_old}d ago"
            )
            line(f"{DIM}Data through:      {latest} ({age_label}){RESET}")
        except ValueError:
            line(f"{DIM}Data through:      {latest}{RESET}")
    line()

    if error:
        line(f"{RED}⚠️  ERROR: {error}{RESET}")
        buf.append('\033[J')
        sys.stdout.write(''.join(buf))
        sys.stdout.flush()
        return None

    if not records:
        line(f"{YELLOW}No data received{RESET}")
        buf.append('\033[J')
        sys.stdout.write(''.join(buf))
        sys.stdout.flush()
        return None

    line(f"{BOLD}{'Date':<12} {'Cont':>5} {'DryBlk':>7} {'GenCgo':>7} {'RoRo':>5} {'Tank':>5} │ {'Total':>6}{RESET}")
    line(f"{DIM}{'─' * 12} {'─' * 5} {'─' * 7} {'─' * 7} {'─' * 5} {'─' * 5} ─ {'─' * 6}{RESET}")

    market_window = window_dates(window_start, window_end)
    window_set = set(market_window)
    window_len = len(market_window)

    market_total = 0
    market_dates_present = []

    chunks = chunk_records(records, 7) if week_totals else [records]

    for ci, chunk in enumerate(chunks):
        for r in chunk:
            is_market_day = r['date'] in window_set
            color = GREEN if is_market_day else ''
            marker = '►' if is_market_day else ' '

            if is_market_day:
                market_total += r['total']
                market_dates_present.append(r['date'])

            line(f"{color}{marker} {r['date']:<10} "
                 f"{r['container']:>5} "
                 f"{r['dry_bulk']:>7} "
                 f"{r['general_cargo']:>7} "
                 f"{r['roro']:>5} "
                 f"{r['tanker']:>5} │ "
                 f"{BOLD}{r['total']:>6}{RESET}")

        if week_totals and chunk:
            s = summarize_chunk(chunk)
            partial_tag = f" {YELLOW}(partial){RESET}" if s['partial'] else ""
            range_label = f"{_short_date(s['first_date'])}–{_short_date(s['last_date'])}"
            date_col = f"Σ {s['n_days']}d"
            line(f"{DIM}{'─' * 12} {'─' * 5} {'─' * 7} {'─' * 7} "
                 f"{'─' * 5} {'─' * 5} ─ {'─' * 6}{RESET}")
            line(f"{BOLD}{MAGENTA}{date_col:<12}{RESET} "
                 f"{BOLD}{MAGENTA}{s['container']:>5} "
                 f"{s['dry_bulk']:>7} "
                 f"{s['general_cargo']:>7} "
                 f"{s['roro']:>5} "
                 f"{s['tanker']:>5} │ "
                 f"{s['total']:>6}{RESET}  "
                 f"{DIM}{range_label}{RESET}{partial_tag}")
            if ci < len(chunks) - 1:
                line()

    line(f"{DIM}{'─' * 60}{RESET}")
    line()
    line(
        f"{BOLD}{CYAN}═══ POLYMARKET WINDOW "
        f"({format_window_title(window_start, window_end)}) ═══{RESET}"
    )
    line()

    missing_count = window_len - len(market_dates_present)

    if missing_count == 0:
        bucket = determine_bucket(market_total)
        color = get_bucket_color(bucket)

        line(f"  Days collected: {GREEN}{window_len}/{window_len} ✅{RESET}")
        line(f"  {BOLD}TOTAL: {market_total} ships{RESET}")
        line(f"  {BOLD}{color}🎯 BUCKET: {bucket}{RESET}")
        line()

        if last_total is not None and last_total != market_total:
            line(
                f"{BOLD}{RED}🚨🚨🚨 TOTAL CHANGED! "
                f"Was {last_total}, now {market_total} 🚨🚨🚨{RESET}"
            )
        else:
            line(
                f"{GREEN}✓ All {window_len} days available - "
                f"this could be the final answer{RESET}"
            )
    else:
        line(f"  Days collected: {YELLOW}{window_len - missing_count}/{window_len}{RESET}")
        line(f"  Missing days: {RED}{missing_count}{RESET}")
        line(f"  Current received total: {market_total}")

        missing_dates = [d for d in market_window if d not in market_dates_present]
        line(f"  Waiting for: {', '.join(missing_dates)}")

    line()
    line(f"{DIM}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    if last_check_time and show_loop_hint:
        line(f"{DIM}Refreshing every {interval} seconds. Press Ctrl+C to stop.{RESET}")

    buf.append('\033[J')
    sys.stdout.write(''.join(buf))
    sys.stdout.flush()

    return market_total if missing_count == 0 else None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    start, end, pull_days = resolve_window(args)

    print(
        f"Starting PortWatch monitor for {format_window_title(start, end)} "
        f"(pulling latest {pull_days} days)..."
    )
    if not args.once:
        print(f"Refresh every {args.interval}s. Press Ctrl+C to stop.")
    time.sleep(0.5)

    atexit.register(cleanup_terminal)
    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()

    last_total = None

    try:
        while True:
            data = fetch_data(pull_days)
            portwatch_updated, _ = fetch_layer_meta()

            if 'error' in data:
                display([], start, end, pull_days, args.interval,
                        show_loop_hint=not args.once,
                        error=data['error'],
                        week_totals=args.week_totals,
                        portwatch_updated=portwatch_updated)
            else:
                records = parse_records(data)
                current_total = display(
                    records, start, end, pull_days, args.interval,
                    show_loop_hint=not args.once,
                    last_total=last_total,
                    last_check_time=datetime.now(),
                    week_totals=args.week_totals,
                    portwatch_updated=portwatch_updated,
                )

                # Change signaling: single beep on first complete window,
                # double beep if total changes after settling.
                if current_total is not None:
                    if last_total is None:
                        beep(times=1)
                        last_total = current_total
                    elif current_total != last_total:
                        beep(times=2)
                        last_total = current_total

            if args.once:
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        cleanup_terminal()
        print(f"\n{YELLOW}Monitor stopped by user{RESET}")
        sys.exit(0)

if __name__ == "__main__":
    main()


#!/usr/bin/env python3
"""Vikunja Alfred workflow backend. Subcommands are invoked by Info.plist objects."""
import calendar
import difflib
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TIMEOUT = 8
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


class ConfigError(Exception):
    pass


class ApiError(Exception):
    pass


class NetworkError(ApiError):
    """Server unreachable, timed out, or answering 5xx: worth retrying later."""


# ---------------------------------------------------------------------------
# Config / env

def env(name, default=""):
    return os.environ.get(name, default) or default


LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def api_base():
    url = env("VIKUNJA_URL").strip()
    if not url:
        raise ConfigError(
            "VIKUNJA_URL is not set. Open this workflow's configuration in "
            "Alfred Preferences and fill in VIKUNJA_URL and VIKUNJA_API_TOKEN."
        )
    if "://" not in url:
        url = "https://" + url
    url = url.rstrip("/")

    scheme, _, rest = url.partition("://")
    host = rest.split("/", 1)[0].split(":", 1)[0].lower()
    if scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ConfigError(
            "VIKUNJA_URL uses http://, which would send your API token "
            "unencrypted over the network. Use https:// instead (http:// is "
            "only allowed for localhost)."
        )

    if url.endswith("/api/v1"):
        return url
    return url + "/api/v1"


def web_base():
    return api_base()[: -len("/api/v1")]


def api_token():
    token = env("VIKUNJA_API_TOKEN").strip()
    if not token:
        raise ConfigError(
            "VIKUNJA_API_TOKEN is not set. Create a token in Vikunja under "
            "Settings > API Tokens, then add it to this workflow's configuration."
        )
    return token


def redact(text):
    """Strip the live API token (and any 'Bearer ...' credential) out of text
    before it reaches Alfred's UI or stdout, in case a future error path ever
    stringifies something that contains it."""
    text = str(text)
    token = env("VIKUNJA_API_TOKEN").strip()
    if token:
        text = text.replace(token, "[REDACTED]")
    return re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text)


def cache_dir():
    d = env("alfred_workflow_cache") or env("TMPDIR", "/tmp") + "/vikunja-alfred-cache"
    os.makedirs(d, exist_ok=True)
    return d


def cache_ttl():
    try:
        return max(5, int(env("VIKUNJA_CACHE_TTL", "60")))
    except ValueError:
        return 60


def normalize_range(val):
    if not val:
        return None
    val = val.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if val in ("today", "todays", "day"):
        return "today"
    if val in ("tomorrow", "tmrw", "tmw"):
        return "tomorrow"
    if val in ("week", "thisweek", "currentweek"):
        return "week"
    if val in ("all", "any", "everything"):
        return "all"

    # Match d/w/m/y patterns (e.g. 7d, 8d, 1w, 2w, 3m, 6m, 1y)
    m_days = re.match(r"^(\d+)(?:d|days?)$", val)
    if m_days:
        return f"{m_days.group(1)}d"

    m_weeks = re.match(r"^(\d+)(?:w|weeks?)$", val)
    if m_weeks:
        return f"{m_weeks.group(1)}w"

    m_months = re.match(r"^(\d+)(?:m|months?|mons?)$", val)
    if m_months:
        return f"{m_months.group(1)}m"

    m_years = re.match(r"^(\d+)(?:y|years?|yrs?)$", val)
    if m_years:
        return f"{m_years.group(1)}y"

    return None


def configured_range():
    raw = env("VIKUNJA_RANGE", "today")
    norm = normalize_range(raw)
    return norm if norm else "today"


def local_timezone_name():
    try:
        link = os.readlink("/etc/localtime")
        idx = link.find("zoneinfo/")
        if idx != -1:
            return link[idx + len("zoneinfo/"):]
    except OSError:
        pass
    return "UTC"


# ---------------------------------------------------------------------------
# HTTP

def _api_call(method, path, body=None, query=None):
    """Perform one API call and return (parsed_json_or_None, response_headers)."""
    url = api_base() + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None
    headers = {
        "Authorization": "Bearer " + api_token(),
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            resp_headers = resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw).get("message", raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, AttributeError):
            msg = raw.decode("utf-8", "replace")
        err = NetworkError if e.code >= 500 else ApiError
        raise err(f"{method} {path} -> {e.code}: {msg}") from None
    except urllib.error.URLError as e:
        raise NetworkError(f"Could not reach {api_base()}: {e.reason}") from None
    except socket.timeout:
        raise NetworkError(f"Vikunja did not respond within {TIMEOUT} s ({api_base()})") from None
    return (json.loads(raw) if raw else None), resp_headers


def api_request(method, path, body=None, query=None):
    return _api_call(method, path, body=body, query=query)[0]


MAX_PAGES = 20


def api_request_all(path, query=None):
    """GET every page of a list endpoint. The server caps per_page (Vikunja
    defaults to 50), so one request silently truncates larger lists."""
    items = []
    page, total_pages = 1, 1
    while page <= min(total_pages, MAX_PAGES):
        data, headers = _api_call("GET", path, query={**(query or {}), "page": str(page)})
        items.extend(data or [])
        try:
            total_pages = int(headers.get("x-pagination-total-pages", 1))
        except (TypeError, ValueError):
            total_pages = 1
        page += 1
    return items


# ---------------------------------------------------------------------------
# Caching

def cache_path(name):
    return os.path.join(cache_dir(), name)


def read_cache_entry(name):
    path = cache_path(name)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, True
    is_stale = (time.time() - st.st_mtime > cache_ttl())
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), is_stale
    except (OSError, json.JSONDecodeError):
        return None, True


def read_cache(name):
    data, _ = read_cache_entry(name)
    return data


def write_cache(name, data):
    path = cache_path(name)
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def trigger_bg_refresh(target, *args):
    lock_name = f".lock_{target}" + (f"_{'_'.join(args)}" if args else "")
    lock_file = cache_path(lock_name)
    now = time.time()
    try:
        st = os.stat(lock_file)
        if now - st.st_mtime < 15:
            return
    except FileNotFoundError:
        pass

    try:
        with open(lock_file, "w") as f:
            f.write(str(now))
    except OSError:
        pass

    cmd = [sys.executable, os.path.abspath(__file__), "bg-refresh", target, *args]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def cmd_bg_refresh(*args):
    if not args:
        return
    target = args[0]
    target_args = args[1:]
    lock_name = f".lock_{target}" + (f"_{'_'.join(target_args)}" if target_args else "")
    lock_file = cache_path(lock_name)
    try:
        if target == "tasks":
            range_key = target_args[0] if target_args else "today"
            fetch_tasks(range_key, force=True, allow_bg=False)
        elif target == "projects":
            fetch_projects(force=True, allow_bg=False)
        elif target == "user":
            fetch_user(force=True, allow_bg=False)
        elif target == "scope-tasks":
            if target_args:
                fetch_scope_tasks(target_args[0], force=True, allow_bg=False)
        elif target == "project-detail":
            if target_args:
                fetch_project_detail(target_args[0], force=True, allow_bg=False)
        elif target == "all":
            range_key = configured_range()
            fetch_tasks(range_key, force=True, allow_bg=False)
            fetch_projects(force=True, allow_bg=False)
            fetch_user(force=True, allow_bg=False)
    except Exception:
        pass
    finally:
        try:
            if os.path.exists(lock_file):
                os.remove(lock_file)
        except OSError:
            pass


def optimistic_remove_task(task_id):
    try:
        tid = int(task_id)
    except (ValueError, TypeError):
        return
    for pattern in ("tasks_*.json", "scope_tasks_*.json"):
        for path in glob.glob(cache_path(pattern)):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    tasks = json.load(f)
                if isinstance(tasks, list):
                    new_tasks = [t for t in tasks if t.get("id") != tid]
                    if len(new_tasks) != len(tasks):
                        write_cache(os.path.basename(path), new_tasks)
            except Exception:
                pass


def optimistic_update_task(task_id, new_due_str):
    try:
        tid = int(task_id)
    except (ValueError, TypeError):
        return
    for pattern in ("tasks_*.json", "scope_tasks_*.json"):
        for path in glob.glob(cache_path(pattern)):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    tasks = json.load(f)
                if isinstance(tasks, list):
                    updated = False
                    for t in tasks:
                        if t.get("id") == tid:
                            if new_due_str == "CLEAR":
                                t["due_date"] = "0001-01-01T00:00:00Z"
                            else:
                                t["due_date"] = new_due_str
                            updated = True
                    if updated:
                        write_cache(os.path.basename(path), tasks)
            except Exception:
                pass


def optimistic_add_task(task):
    if not isinstance(task, dict) or "id" not in task:
        return
    tid = task["id"]
    for pattern in ("tasks_*.json", "scope_tasks_*.json"):
        for path in glob.glob(cache_path(pattern)):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    tasks = json.load(f)
                if isinstance(tasks, list):
                    tasks = [t for t in tasks if t.get("id") != tid]
                    tasks.insert(0, task)
                    write_cache(os.path.basename(path), tasks)
            except Exception:
                pass


def invalidate_cache(*names):
    for name in names:
        try:
            os.remove(cache_path(name))
        except FileNotFoundError:
            pass


def invalidate_task_caches():
    for path in glob.glob(cache_path("tasks_*.json")):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    for path in glob.glob(cache_path("scope_tasks_*.json")):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Data fetch (cached)

def fetch_projects(force=False, allow_bg=True):
    if not force:
        cached, is_stale = read_cache_entry("projects.json")
        if cached is not None:
            if is_stale and allow_bg:
                trigger_bg_refresh("projects")
            return cached
    projects = api_request_all("/projects", query={"per_page": "250"})
    write_cache("projects.json", projects)
    return projects


def project_map():
    return {p["id"]: p for p in fetch_projects()}


def fetch_tasks(range_key, force=False, allow_bg=True):
    norm = normalize_range(range_key) or "today"
    cache_name = f"tasks_{norm}.json"
    if not force:
        cached, is_stale = read_cache_entry(cache_name)
        if cached is not None:
            if is_stale and allow_bg:
                trigger_bg_refresh("tasks", norm)
            return cached
    tz = local_timezone_name()
    if norm == "all":
        filt = "done = false"
    else:
        end = range_end(norm)
        filt = f"done = false && due_date <= '{end.strftime('%Y-%m-%dT%H:%M:%S')}'"
    tasks = api_request_all(
        "/tasks",
        query={
            "filter": filt,
            "filter_timezone": tz,
            "sort_by": "due_date",
            "order_by": "asc",
            "per_page": "250",
        },
    )
    if norm != "all":
        tasks = [t for t in tasks if t.get("due_date", "0001-01-01T00:00:00Z") > "0001-01-01T00:00:01Z"]
    write_cache(cache_name, tasks)
    return tasks


def fetch_project_detail(project_id, force=False, allow_bg=True):
    cache_name = f"project_detail_{project_id}.json"
    if not force:
        cached, is_stale = read_cache_entry(cache_name)
        if cached is not None:
            if is_stale and allow_bg:
                trigger_bg_refresh("project-detail", str(project_id))
            return cached
    detail = api_request("GET", f"/projects/{project_id}")
    write_cache(cache_name, detail)
    return detail


def fetch_scope_tasks(scope_id, force=False, allow_bg=True):
    cache_name = f"scope_tasks_{scope_id}.json"
    if not force:
        cached, is_stale = read_cache_entry(cache_name)
        if cached is not None:
            if is_stale and allow_bg:
                trigger_bg_refresh("scope-tasks", str(scope_id))
            return cached
    detail = fetch_project_detail(scope_id, allow_bg=allow_bg)
    list_view = next((v for v in detail.get("views", []) if v.get("view_kind") == "list"), None)
    if list_view is None:
        return []
    tasks = api_request_all(
        f"/projects/{scope_id}/views/{list_view['id']}/tasks",
        query={"sort_by": "due_date", "order_by": "asc", "per_page": "100"},
    )
    tasks = [t for t in tasks if not t.get("done")]
    write_cache(cache_name, tasks)
    return tasks


def fetch_user(force=False, allow_bg=True):
    if not force:
        cached, is_stale = read_cache_entry("user.json")
        if cached is not None:
            if is_stale and allow_bg:
                trigger_bg_refresh("user")
            return cached
    user = api_request("GET", "/user")
    write_cache("user.json", user)
    return user


def fetch_task(task_id):
    return api_request("GET", f"/tasks/{task_id}")


def verify_task_write(task_id, checks):
    """Re-fetch a task after a write and confirm the server reflects the expected fields."""
    fresh = fetch_task(task_id)
    for field, expected in checks.items():
        actual = fresh.get(field)
        if actual != expected:
            raise ApiError(
                f"Task {task_id} update did not persist: expected {field}={expected!r}, "
                f"got {actual!r}. Check Vikunja directly."
            )
    return fresh


# ---------------------------------------------------------------------------
# Date helpers

def now_local():
    return datetime.now().astimezone()


def range_end(range_key):
    norm = normalize_range(range_key) or "today"
    now = now_local()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    if norm == "today":
        return today_end
    if norm == "tomorrow":
        return today_end + timedelta(days=1)
    if norm == "week":
        days_left = 6 - now.weekday()  # Monday=0 .. Sunday=6
        return today_end + timedelta(days=days_left)

    m = re.match(r"^(\d+)([dwmy])$", norm)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        if unit == "d":
            return today_end + timedelta(days=num)
        if unit == "w":
            return today_end + timedelta(days=num * 7)
        if unit == "m":
            return today_end + timedelta(days=num * 30)
        if unit == "y":
            return today_end + timedelta(days=num * 365)

    return today_end


def range_human_label(range_key):
    norm = normalize_range(range_key) or "today"
    if norm == "today":
        return "today"
    if norm == "tomorrow":
        return "tomorrow"
    if norm == "week":
        return "this week"
    if norm == "all":
        return "all open tasks"

    m = re.match(r"^(\d+)([dwmy])$", norm)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        unit_names = {
            "d": ("day", "days"),
            "w": ("week", "weeks"),
            "m": ("month", "months"),
            "y": ("year", "years"),
        }
        singular, plural = unit_names.get(unit, ("unit", "units"))
        name = singular if num == 1 else plural
        return f"the next {num} {name}"

    return norm


def range_due_target(range_key):
    norm = normalize_range(range_key) or "today"
    now = now_local()
    today_evening = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if norm == "today":
        return today_evening
    if norm == "tomorrow":
        return today_evening + timedelta(days=1)
    if norm == "week":
        days_left = 6 - now.weekday()
        return today_evening + timedelta(days=days_left)

    m = re.match(r"^(\d+)([dwmy])$", norm)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        if unit == "d":
            return today_evening + timedelta(days=num)
        if unit == "w":
            return today_evening + timedelta(days=num * 7)
        if unit == "m":
            return today_evening + timedelta(days=num * 30)
        if unit == "y":
            return today_evening + timedelta(days=num * 365)

    return today_evening


def parse_time_component(time_str):
    """Parses strings like '5pm', '5:30pm', '17:00', '9am', 'noon', 'midnight'."""
    s = time_str.strip().lower()
    if s in ("noon", "midday"):
        return (12, 0)
    if s in ("midnight",):
        return (23, 59)
    if s in ("morning",):
        return (9, 0)
    if s in ("afternoon",):
        return (14, 0)
    if s in ("evening",):
        return (19, 0)
    if s in ("tonight",):
        return (20, 0)

    # 12-hour format: 5pm, 5:30pm, 5.30pm, 11:45am, 9am
    m = re.match(r"^(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)$", s)
    if m:
        h = int(m.group(1))
        min_val = int(m.group(2)) if m.group(2) else 0
        meridiem = m.group(3)
        if meridiem == "pm" and h < 12:
            h += 12
        elif meridiem == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= min_val <= 59:
            return (h, min_val)

    # 24-hour format: 17:00, 09:30, 14.30
    m = re.match(r"^(\d{1,2})[:.](\d{2})$", s)
    if m:
        h = int(m.group(1))
        min_val = int(m.group(2))
        if 0 <= h <= 23 and 0 <= min_val <= 59:
            return (h, min_val)

    return None


def parse_relative_duration(text):
    """
    Parses relative duration expressions like:
    - 'in 9 hours', '9hours', '9h', '+9h', 'in 9 hrs'
    - 'in 30 mins', '30m', '+30m', 'in 30 minutes'
    - 'in 2 hours 30 mins', '2h30m', '1h 45min'
    - 'in 3 days', '3d', '+3d'
    - 'in 2 weeks', '2w'
    - 'in 1 month', '3m'
    - 'in 1 year', '1y'
    """
    s = text.strip().lower()
    if s.startswith("in "):
        s = s[3:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()

    now = now_local()

    # Composite hours + minutes: 2h30m, 2h 30min, 1 hour 45 minutes
    m_hm = re.match(r"^(\d+)\s*(?:h|hrs?|hours?)\s*(?:and\s*)?(\d+)\s*(?:m|mins?|minutes?)$", s)
    if m_hm:
        hrs = int(m_hm.group(1))
        mins = int(m_hm.group(2))
        return now + timedelta(hours=hrs, minutes=mins)

    # Single hours: 9h, 9 hours, 9 hrs, 1.5h
    m_h = re.match(r"^(\d+(?:\.\d+)?)\s*(?:h|hrs?|hours?)$", s)
    if m_h:
        hrs = float(m_h.group(1))
        return now + timedelta(hours=hrs)

    # Single minutes: 30m, 30 mins, 45 minutes
    m_min = re.match(r"^(\d+)\s*(?:m|mins?|minutes?)$", s)
    if m_min:
        mins = int(m_min.group(1))
        return now + timedelta(minutes=mins)

    # Single days: 3d, 3 days, 3 day
    m_d = re.match(r"^(\d+)\s*(?:d|days?)$", s)
    if m_d:
        days = int(m_d.group(1))
        return now.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=days)

    # Single weeks: 2w, 2 weeks
    m_w = re.match(r"^(\d+)\s*(?:w|weeks?)$", s)
    if m_w:
        weeks = int(m_w.group(1))
        return now.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=weeks * 7)

    # Single months: 1m, 3 months
    m_m = re.match(r"^(\d+)\s*(?:m|months?|mons?)$", s)
    if m_m:
        months = int(m_m.group(1))
        return now.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=months * 30)

    # Single years: 1y, 2 years
    m_y = re.match(r"^(\d+)\s*(?:y|years?|yrs?)$", s)
    if m_y:
        years = int(m_y.group(1))
        return now.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=years * 365)

    return None


def strip_ordinal(tok):
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)$", tok)
    return m.group(1) if m else tok


def parse_month_name_date(tokens):
    """Parses 'June 12 [year]' or '12 June [year]' style dates (ordinals like '21st' allowed)."""
    if not tokens:
        return None
    if len(tokens) == 1 or (tokens[0] in MONTH_NAMES and not strip_ordinal(tokens[1]).isdigit()):
        # Bare month ("dec", "december 9am"): 1st of the next occurrence of that month.
        if tokens[0] not in MONTH_NAMES:
            return None
        now = now_local()
        month = MONTH_NAMES[tokens[0]]
        year = now.year if datetime(now.year, month, 1).date() >= now.date() else now.year + 1
        return datetime(year, month, 1).date(), tokens[1:]
    first, second = strip_ordinal(tokens[0]), strip_ordinal(tokens[1])

    if tokens[0] in MONTH_NAMES and second.isdigit():
        month, day = MONTH_NAMES[tokens[0]], int(second)
        rest = tokens[2:]
    elif first.isdigit() and tokens[1] in MONTH_NAMES:
        month, day = MONTH_NAMES[tokens[1]], int(first)
        rest = tokens[2:]
    else:
        return None

    year = now_local().year
    if rest and rest[0].isdigit() and len(rest[0]) in (2, 4):
        year = int(rest[0])
        if year < 100:
            year += 2000
        rest = rest[1:]

    try:
        return datetime(year, month, day).date(), rest
    except ValueError:
        return None


def parse_numeric_date(tokens):
    """Parses 'D/M[/Y]' dates, falling back to M/D when D/M is invalid (e.g. '12/25').

    Returns (date, remaining_tokens) or None.
    """
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?$", tokens[0])
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    year = now_local().year
    if m.group(3):
        year = int(m.group(3))
        if year < 100:
            year += 2000
    for day, month in ((a, b), (b, a)):
        try:
            return datetime(year, month, day).date(), tokens[1:]
        except ValueError:
            continue
    return None


def parse_due_date(text):
    """Best-effort parse of free text into a local datetime. Returns None if unparseable."""
    raw = text.strip()
    if not raw:
        return None
    text_lower = raw.lower()
    for prefix in ("on ", "at "):
        if text_lower.startswith(prefix):
            raw = raw[len(prefix):].strip()
            text_lower = raw.lower()
            break
    now = now_local()

    if text_lower in ("none", "clear", "no date", "remove", "nodate"):
        return "CLEAR"

    # 1. Relative duration (e.g. "in 9 hours", "30m", "2h30m", "+3d")
    rel_dt = parse_relative_duration(text_lower)
    if rel_dt is not None:
        return rel_dt

    # 2. Pure time of day (e.g. "5pm", "17:00", "9am", "noon", "midnight")
    time_comp = parse_time_component(text_lower)
    if time_comp is not None:
        h, m = time_comp
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target < now:
            target += timedelta(days=1)
        return target

    # 3. Keywords with optional time (e.g. "tomorrow 9am", "today 14:00", "friday 2pm", "tonight")
    tokens = text_lower.replace(" at ", " ").split()
    if tokens:
        target_date = None
        remaining_time_tokens = []

        if tokens[0] in ("today", "todays"):
            target_date = now.date()
            remaining_time_tokens = tokens[1:]
        elif tokens[0] in ("tomorrow", "tmrw", "tmw"):
            target_date = (now + timedelta(days=1)).date()
            remaining_time_tokens = tokens[1:]
        elif tokens[0] in ("tonight",):
            target_date = now.date()
            if len(tokens) > 1:
                remaining_time_tokens = tokens[1:]
            else:
                return now.replace(hour=20, minute=0, second=0, microsecond=0)
        elif tokens[0] in ("weekend", "thisweekend"):
            days_left = (5 - now.weekday()) % 7 or 7
            target_date = (now + timedelta(days=days_left)).date()
            remaining_time_tokens = tokens[1:]
        elif tokens[0] == "this" and len(tokens) > 1 and tokens[1] in ("week", "weekend"):
            if tokens[1] == "week":
                days_left = 6 - now.weekday()
                target_date = (now + timedelta(days=days_left)).date()
            else:
                days_left = (5 - now.weekday()) % 7 or 7
                target_date = (now + timedelta(days=days_left)).date()
            remaining_time_tokens = tokens[2:]
        elif tokens[0] == "next" and len(tokens) > 1:
            if tokens[1] in ("week",):
                days_left = (7 - now.weekday()) % 7 or 7
                target_date = (now + timedelta(days=days_left)).date()
                remaining_time_tokens = tokens[2:]
            else:
                for i, wd in enumerate(WEEKDAYS):
                    if wd.startswith(tokens[1]) and len(tokens[1]) >= 3:
                        days_left = ((i - now.weekday()) % 7 or 7) + 7
                        target_date = (now + timedelta(days=days_left)).date()
                        remaining_time_tokens = tokens[2:]
                        break
        elif tokens[0] in MONTH_NAMES or (len(tokens) > 1 and strip_ordinal(tokens[0]).isdigit() and tokens[1] in MONTH_NAMES):
            parsed = parse_month_name_date(tokens)
            if parsed is not None:
                target_date, remaining_time_tokens = parsed
        elif re.match(r"^\d{1,2}/\d{1,2}", tokens[0]):
            parsed = parse_numeric_date(tokens)
            if parsed is not None:
                target_date, remaining_time_tokens = parsed
        else:
            for i, wd in enumerate(WEEKDAYS):
                if wd.startswith(tokens[0]) and len(tokens[0]) >= 3:
                    days_left = (i - now.weekday()) % 7 or 7
                    target_date = (now + timedelta(days=days_left)).date()
                    remaining_time_tokens = tokens[1:]
                    break

        if target_date is not None:
            if remaining_time_tokens:
                time_str = " ".join(remaining_time_tokens)
                t_comp = parse_time_component(time_str)
                if t_comp is None:
                    # Trailing tokens didn't parse as a time - the input isn't a
                    # clean date expression, so don't silently drop them.
                    return None
                h, m = t_comp
                return datetime(target_date.year, target_date.month, target_date.day, h, m, tzinfo=now.tzinfo)
            return datetime(target_date.year, target_date.month, target_date.day, 18, 0, tzinfo=now.tzinfo)

    # 4. Standard explicit date formats
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %I:%M%p",
        "%Y-%m-%d %I%p",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(raw, fmt)
            parsed = parsed.replace(
                year=parsed.year if parsed.year != 1900 else now.year,
                hour=parsed.hour if ":" in raw or "am" in text_lower or "pm" in text_lower else 18,
                minute=parsed.minute,
            )
            return parsed.replace(tzinfo=now.tzinfo)
        except ValueError:
            continue

    return None


# Vikunja repeat modes: 0 = add repeat_after to the due date, 1 = same day each calendar
# month (ignores repeat_after), 2 = add repeat_after to the completion date.
REPEAT_MODE_FIXED = 0
REPEAT_MODE_MONTHLY = 1
REPEAT_MODE_AFTER_DONE = 2


def add_months(dt, n):
    year, month = divmod(dt.year * 12 + dt.month - 1 + n, 12)
    month += 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


_REPEAT_UNITS = {
    "d": "day", "day": "day", "days": "day",
    "w": "week", "week": "week", "weeks": "week",
    "m": "month", "month": "month", "months": "month",
    "y": "year", "yr": "year", "yrs": "year", "year": "year", "years": "year",
}


def parse_repeat(tokens, after_done=False):
    """Parses 'every wednesday [9am]', 'every 3 days [at 5pm]', 'every other week', 'every month'.

    Returns {"seconds", "mode", "first_due", "label"} or None. The first due date is the
    next matching weekday, or one interval from now for 'every N <unit>'; time defaults to 18:00.
    By default a repeat counts from the due date; after_done=True counts from completion instead.
    """
    toks = [t.lower() for t in tokens]
    if len(toks) < 2 or toks[0] != "every":
        return None
    rest = toks[1:]
    n, unit, weekday = 1, None, None

    if rest[0] == "other":
        n, rest = 2, rest[1:]
    elif rest[0].isdigit():
        n, rest = int(rest[0]), rest[1:]
    else:
        m = re.match(r"^(\d+)([a-z]+)$", rest[0])
        if m and m.group(2) in _REPEAT_UNITS:
            n, rest = int(m.group(1)), [m.group(2)] + rest[1:]
    if not rest or n < 1 or n > 1000:
        return None

    if rest[0] in _REPEAT_UNITS:
        unit = _REPEAT_UNITS[rest[0]]
    elif n == 1 and len(rest[0]) >= 3:
        for i, wd in enumerate(WEEKDAYS):
            if wd.startswith(rest[0]):
                weekday = i
                break
    if unit is None and weekday is None:
        return None

    time_toks = [t for t in rest[1:] if t != "at"]
    if time_toks:
        hm = parse_time_component(" ".join(time_toks))
        if hm is None:
            return None
    else:
        hm = (18, 0)

    now = now_local()
    base = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    mode = REPEAT_MODE_AFTER_DONE if after_done else REPEAT_MODE_FIXED
    if weekday is not None:
        seconds = 7 * 86400
        first = base + timedelta(days=(weekday - now.weekday()) % 7)
        if first <= now:
            first += timedelta(days=7)
        label = f"Every {WEEKDAYS[weekday].capitalize()}"
    elif unit == "day":
        seconds, first = n * 86400, base + timedelta(days=n)
        label = "Every day" if n == 1 else f"Every {n} days"
    elif unit == "week":
        seconds, first = n * 7 * 86400, base + timedelta(days=7 * n)
        label = "Every week" if n == 1 else f"Every {n} weeks"
    elif unit == "month":
        # Calendar-month repeat only exists for one month; longer spans are 30-day multiples.
        seconds = n * 30 * 86400
        if n == 1 and not after_done:
            seconds, mode = 0, REPEAT_MODE_MONTHLY
        first = add_months(base, n)
        label = "Every month" if n == 1 else f"Every {n} months"
    else:
        seconds, first = n * 365 * 86400, add_months(base, 12 * n)
        label = "Every year" if n == 1 else f"Every {n} years"
    return {"seconds": seconds, "mode": mode, "first_due": first, "label": label}


def extract_repeat_from_title(title, after_done=False):
    """Pull a trailing 'every ...' expression out of a task title.

    Returns (title_without_it, repeat_dict_or_None, phrase). The title must keep at
    least one word before the expression.
    """
    tokens = title.split()
    for start in range(1, len(tokens)):
        if tokens[start].lower() != "every":
            continue
        rep = parse_repeat(tokens[start:], after_done=after_done)
        if rep is not None:
            return " ".join(tokens[:start]), rep, " ".join(tokens[start:])
    return title, None, ""


def relative_due_label(due_date_str):
    if not due_date_str or due_date_str.startswith("0001-01-01"):
        return "No due date"
    due = datetime.strptime(due_date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    due_local = due.astimezone()
    now = now_local()
    delta_days = (due_local.date() - now.date()).days
    time_part = due_local.strftime("%H:%M")
    if delta_days < 0:
        n = -delta_days
        return f"Overdue {n} day{'s' if n != 1 else ''} ({due_local.strftime('%b %d')})"
    if delta_days == 0:
        return f"Today {time_part}"
    if delta_days == 1:
        return f"Tomorrow {time_part}"
    if delta_days < 7:
        return due_local.strftime("%A") + f" {time_part}"
    return due_local.strftime("%b %d")


def format_datetime_str(dt_str):
    if not dt_str or dt_str.startswith("0001-01-01"):
        return None
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt_str


def format_repeat(seconds, mode=0):
    if mode == 1:
        return "Every month"
    if not seconds:
        return None
    if seconds % 86400 == 0:
        days = seconds // 86400
        if days % 365 == 0:
            years = days // 365
            interval = "Every year" if years == 1 else f"Every {years} years"
        elif days % 30 == 0 and days >= 30 and days % 7 != 0:
            months = days // 30
            interval = "Every month" if months == 1 else f"Every {months} months"
        elif days % 7 == 0:
            weeks = days // 7
            interval = "Every week" if weeks == 1 else f"Every {weeks} weeks"
        elif days == 1:
            interval = "Every day"
        else:
            interval = f"Every {days} days"
    elif seconds % 3600 == 0:
        hours = seconds // 3600
        interval = "Every hour" if hours == 1 else f"Every {hours} hours"
    elif seconds % 60 == 0:
        minutes = seconds // 60
        interval = f"Every {minutes} minutes"
    else:
        interval = f"Every {seconds}s"

    if mode == 2:
        interval += " (after completion)"
    return interval


def task_details_text(t, project_title=""):
    lines = []
    title = str(t.get("title", ""))
    identifier = t.get("identifier")
    if identifier:
        lines.append(f"Title: {title} ({identifier})")
    else:
        lines.append(f"Title: {title}")

    if project_title:
        lines.append(f"Project: {project_title}")

    status = "Completed" if t.get("done") else "Open"
    percent = t.get("percent_done", 0)
    if percent and not t.get("done"):
        status += f" ({int(percent * 100 if percent <= 1 else percent)}%)"
    lines.append(f"Status: {status}")

    due_str = format_datetime_str(t.get("due_date"))
    if due_str:
        rel = relative_due_label(t.get("due_date"))
        lines.append(f"Due: {due_str} ({rel})")

    start_str = format_datetime_str(t.get("start_date"))
    if start_str:
        lines.append(f"Start: {start_str}")

    end_str = format_datetime_str(t.get("end_date"))
    if end_str:
        lines.append(f"End: {end_str}")

    rep = format_repeat(t.get("repeat_after", 0), t.get("repeat_mode", 0))
    if rep:
        lines.append(f"Repeat: {rep}")

    priority = t.get("priority", 0)
    if priority:
        p_names = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent", 5: "DO NOW"}
        lines.append(f"Priority: {p_names.get(priority, str(priority))}")

    labels = [l.get("title") for l in t.get("labels") or [] if l.get("title")]
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")

    assignees = [a.get("name", "").strip() or a.get("username", "") for a in t.get("assignees") or [] if a]
    assignees = [a for a in assignees if a]
    if assignees:
        lines.append(f"Assignees: {', '.join(assignees)}")

    task_id = t.get("id")
    if task_id:
        lines.append(f"URL: {web_base()}/tasks/{task_id}")

    desc = (t.get("description") or "").strip()
    if desc:
        lines.append(f"\nDescription:\n{desc}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Alfred output

def alfred_output(items, cache_seconds=None):
    payload = {"items": items}
    if cache_seconds:
        payload["cache"] = {"seconds": cache_seconds, "loosereload": True}
    print(json.dumps(payload))


def error_item(message):
    return {
        "title": "Vikunja error",
        "subtitle": message,
        "valid": False,
        "icon": {"path": "icon.png"},
    }


def run_guarded(fn):
    try:
        fn()
    except (ConfigError, ApiError) as e:
        alfred_output([error_item(redact(e))])
    except Exception as e:  # noqa: BLE001 - last resort so Alfred always gets valid JSON
        alfred_output([error_item(redact(f"Unexpected error: {e}"))])


# ---------------------------------------------------------------------------
# Subcommands

_FULLWIDTH_ASCII_OFFSET = 0xFEE0


def normalize_fullwidth(text):
    """Convert full-width ASCII punctuation/letters/digits to normal ASCII.

    CJK input methods commonly substitute full-width forms for plain ASCII
    (e.g. '@' -> '＠'), which silently fails to match syntax like '@Project'
    with no error. Actual CJK characters (kanji/kana) sit outside this block
    and are left untouched.
    """
    if not text:
        return text
    chars = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - _FULLWIDTH_ASCII_OFFSET))
        elif ch == "　":
            chars.append(" ")
        else:
            chars.append(ch)
    return "".join(chars)


# Zero-width space, ZWNJ, ZWJ, LRM, RLM, word joiner, BOM, soft hyphen.
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\xad]")


def strip_invisible(text):
    """Remove invisible Unicode formatting characters (zero-width space/joiners,
    word joiner, BOM, directional marks, soft hyphen).

    Text copied from share sheets (e.g. social apps) sometimes carries these as
    line-wrap hints. Left in place, they can silently break whitespace-boundary
    parsing like '@Project' / '/Project' tag detection without being visible.
    This only ever deletes characters - it never inserts a space - so it can't
    turn part of a URL into a false tag match.
    """
    if not text:
        return text
    return _INVISIBLE_RE.sub("", text)


def normalize_query_text(text):
    return normalize_fullwidth(strip_invisible(text or ""))


def add_repeat_alternatives(items):
    """For each create row whose title has a trailing 'every ...' repeat, say how it will repeat
    and add a second row that repeats after completion instead of from the due date."""
    out = []
    for item in items:
        variables = item.get("variables") or {}
        if not item.get("uid", "").startswith("fallback-create") or variables.get("action") != "create":
            out.append(item)
            continue
        _, from_due, _ = extract_repeat_from_title(variables["new_title"])
        _, after_done, _ = extract_repeat_from_title(variables["new_title"], after_done=True)
        if from_due is None:
            out.append(item)
            continue
        from_label = format_repeat(from_due["seconds"], from_due["mode"])
        after_label = format_repeat(after_done["seconds"], after_done["mode"])
        out.append({**item, "subtitle": f"{item['subtitle']} · {from_label} from due date"})
        out.append(
            {
                **item,
                "uid": item["uid"] + "-after-done",
                "title": f"{item['title']} · repeat after completion",
                "subtitle": f"{after_label} · next due counts from when you complete it · ↩ to create",
                "variables": {**variables, "new_repeat_mode": "after_done"},
            }
        )
    return out


def add_project_preview(items):
    """On each create row whose title carries a '@tag' or '/tag', show which project it resolves to
    (or why it doesn't) and drop the tag from the row title."""
    out = []
    for item in items:
        variables = item.get("variables") or {}
        if not item.get("uid", "").startswith("fallback-create") or variables.get("action") != "create":
            out.append(item)
            continue
        bare, tag = extract_project_tag(variables["new_title"])
        if not tag or not bare:
            out.append(item)
            continue
        try:
            projects = fetch_projects()
        except (ConfigError, ApiError):
            out.append(item)
            continue
        try:
            project = match_project(tag, projects)
            problem = None if project else f"No project matches '{tag}'"
        except ConfigError as e:
            project, problem = None, str(e)
        title = item["title"].replace(variables["new_title"], bare)
        if project:
            subtitle = item["subtitle"].replace("default project", f"'{project['title']}'")
            if "default project" not in item["subtitle"]:
                subtitle = f"→ '{project['title']}' · {item['subtitle']}"
            out.append({**item, "title": title, "subtitle": subtitle})
        else:
            out.append({**item, "title": title, "subtitle": problem, "valid": False})
    return out


def cmd_list(query):
    raw_query = normalize_query_text((query or "").strip())
    parts = raw_query.split(None, 1)
    first = parts[0].lower() if parts else ""
    raw_rest = parts[1].strip() if len(parts) > 1 else ""

    norm_first = normalize_range(first)
    if norm_first is not None:
        active_range = norm_first
        task_title = raw_rest
        text_filter = raw_rest.lower()
        is_scoped_range = (norm_first != "all")
    elif not raw_query:
        active_range = configured_range()
        task_title = ""
        text_filter = ""
        is_scoped_range = (active_range != "all")
    else:
        # Generic search query (e.g. 'v Call Optometrist') searches across ALL open tasks!
        active_range = "all"
        task_title = raw_query
        text_filter = raw_query.lower()
        is_scoped_range = False

    pending_rows = pending_items() if not raw_query else []
    try:
        tasks = fetch_tasks(active_range)
        projects = project_map()
    except ApiError as e:
        if not pending_rows:
            raise
        alfred_output(pending_rows + [error_item(redact(e))])
        return

    items = []
    for t in tasks:
        if text_filter:
            title_match = text_filter in t["title"].lower()
            label_match = any(text_filter in (l.get("title") or "").lower() for l in t.get("labels") or [])
            if not (title_match or label_match):
                continue
        proj = projects.get(t["project_id"], {})
        proj_title = proj.get("title", f"Project {t['project_id']}")
        due_label = relative_due_label(t.get("due_date"))
        rep = format_repeat(t.get("repeat_after", 0), t.get("repeat_mode", 0))
        parts = [due_label] + ([rep] if rep else []) + [proj_title]
        subtitle = " · ".join(parts)
        details = task_details_text(t, proj_title)
        items.append(
            {
                "uid": f"task-{t['id']}",
                "title": t["title"],
                "subtitle": subtitle,
                "arg": str(t["id"]),
                "autocomplete": t["title"],
                "variables": {"action": "open", "task_id": str(t["id"]), "task_title": t["title"]},
                "mods": {
                    "cmd": {
                        "subtitle": f"Complete task: '{t['title']}'",
                        "variables": {"action": "complete", "task_id": str(t["id"]), "task_title": t["title"]},
                    },
                    "alt": {
                        "subtitle": f"Reschedule task: '{t['title']}'",
                        "arg": "",
                        "variables": {"task_id": str(t["id"]), "task_title": t["title"]},
                    },
                },
                "text": {"copy": details, "largetype": details},
            }
        )

    if not items:
        if is_scoped_range:
            label = range_human_label(active_range)
            if text_filter:
                parsed_title, parsed_due = extract_due_from_title(task_title)
                if parsed_due is not None:
                    create_title = parsed_title
                    due_iso = parsed_due.isoformat()
                    due_label = parsed_due.strftime("%a %b %d %H:%M")
                else:
                    create_title = task_title
                    due_target = range_due_target(active_range)
                    due_iso = due_target.isoformat() if isinstance(due_target, datetime) else ""
                    due_label = label
                items.append(
                    {
                        "uid": f"fallback-create-{active_range}-{text_filter}",
                        "title": f"Create task: '{create_title}'",
                        "subtitle": f"Add to default project due {due_label} · ↩ to create",
                        "arg": "create",
                        "variables": {"action": "create", "new_title": create_title, "new_due": due_iso},
                        "valid": True,
                    }
                )
                items.append(
                    {
                        "uid": f"fallback-search-all-{text_filter}",
                        "title": f"Search all open tasks for '{task_title}'",
                        "subtitle": f"Widen search beyond tasks due {label}",
                        "autocomplete": text_filter,
                        "valid": False,
                    }
                )
            else:
                items.append(
                    {
                        "title": f"No open tasks due {label}",
                        "subtitle": "Type 'v 1w', 'v 2w', 'v 1m' to switch range, or type to search/create",
                        "valid": False,
                    }
                )
        else:
            if text_filter:
                parsed_title, parsed_due = extract_due_from_title(task_title)
                create_title = parsed_title if parsed_due is not None else task_title
                variables = {"action": "create", "new_title": create_title}
                if parsed_due is not None:
                    variables["new_due"] = parsed_due.isoformat()
                    subtitle = f"Press ↩ to create, due {parsed_due.strftime('%a %b %d %H:%M')}"
                else:
                    subtitle = "No matching tasks found · Press ↩ to create in default project"
                items.append(
                    {
                        "uid": f"fallback-create-{text_filter}",
                        "title": f"Create task: '{create_title}'",
                        "subtitle": subtitle,
                        "arg": "create",
                        "variables": variables,
                        "valid": True,
                    }
                )
            else:
                items.append(
                    {
                        "title": "No open tasks",
                        "subtitle": "You have no open tasks across all projects",
                        "valid": False,
                    }
                )
    elif text_filter and not is_scoped_range:
        # If tasks matched a generic search, also offer a create item at the bottom
        parsed_title, parsed_due = extract_due_from_title(task_title)
        create_title = parsed_title if parsed_due is not None else task_title
        variables = {"action": "create", "new_title": create_title}
        if parsed_due is not None:
            variables["new_due"] = parsed_due.isoformat()
            subtitle = f"Press ↩ to create, due {parsed_due.strftime('%a %b %d %H:%M')}"
        else:
            subtitle = "Press ↩ to create in default project"
        items.append(
            {
                "uid": f"fallback-create-extra-{text_filter}",
                "title": f"Create new task: '{create_title}'",
                "subtitle": subtitle,
                "arg": "create",
                "variables": variables,
                "valid": True,
            }
        )

    items = add_project_preview(add_repeat_alternatives(items))
    # Don't let Alfred cache the list while saved tasks are shown: they change on retry/discard.
    alfred_output(pending_rows + items, cache_seconds=None if pending_rows else cache_ttl())


def cmd_dispatch():
    action = env("action", "open")
    task_id = env("task_id")
    task_title = env("task_title")
    new_title = env("new_title")
    new_due = env("new_due")

    if action == "create":
        if not new_title:
            print("No task title provided.")
            return
        task, project_id = create_task_or_queue(
            new_title, due_date=new_due if new_due else None, repeat_mode=env("new_repeat_mode") or None
        )
        rep_lbl = format_repeat(task.get("repeat_after", 0), task.get("repeat_mode", 0))
        due_lbl = f" (due: {relative_due_label(task.get('due_date'))}{' · ' + rep_lbl if rep_lbl else ''})" if new_due else ""
        print(f"Created: {task.get('title')}{due_lbl}")
        return

    if action in ("retry-pending", "discard-pending"):
        entries = load_pending()
        entry = next((e for e in entries if e["id"] == env("pending_id")), None)
        if entry is None:
            print("That saved task no longer exists.")
            return
        if action == "discard-pending":
            save_pending([e for e in entries if e["id"] != entry["id"]])
            print(f"Discarded: {entry['title']}")
            return
        task, _ = create_task(entry["title"], due_date=entry.get("due") or None, repeat_mode=entry.get("repeat_mode") or None)
        save_pending([e for e in load_pending() if e["id"] != entry["id"]])
        print(f"Created: {task.get('title')}")
        return

    if not task_id:
        print("No task selected.")
        return

    if action == "complete":
        task = fetch_task(task_id)
        title = task.get("title") or task_title or task_id
        # Vikunja never leaves a repeating task done: completing it rolls the due
        # date forward and resets done=False, so "done" can't be the check there.
        repeating = bool(task.get("repeat_after")) or task.get("repeat_mode") == 1
        old_due = task.get("due_date")
        task["done"] = True
        # Verify against the POST response rather than re-fetching: one round
        # trip fewer, and it carries the server-computed state (incl. the rolled
        # due date for repeating tasks).
        fresh = api_request("POST", f"/tasks/{task_id}", body=task) or {}
        if repeating:
            if fresh.get("done") or fresh.get("due_date") == old_due:
                raise ApiError(
                    f"Task {task_id} is repeating but its due date did not advance. "
                    "Check Vikunja directly."
                )
            invalidate_task_caches()
            trigger_bg_refresh("tasks", configured_range())
            print(f"Completed: {title} (next due: {relative_due_label(fresh.get('due_date'))})")
            return
        if not fresh.get("done"):
            raise ApiError(
                f"Task {task_id} update did not persist: expected done=True, "
                f"got {fresh.get('done')!r}. Check Vikunja directly."
            )
        optimistic_remove_task(task_id)
        trigger_bg_refresh("tasks", configured_range())
        print(f"Completed: {title}")
        return

    # default: open in browser
    url = f"{web_base()}/tasks/{task_id}"
    os.system(f"open {json.dumps(url)}")
    title = task_title or f"Task {task_id}"
    print(f"Opened in browser: {title}")


def cmd_due_tasks(query):
    # Searches every open task (not just the configured range) so you can
    # reschedule anything, including tasks only surfaced via a filter/project.
    tasks = fetch_tasks("all")
    projects = project_map()
    q = (query or "").strip().lower()

    items = []
    for t in tasks:
        if q and q not in t["title"].lower():
            continue
        proj = projects.get(t["project_id"], {})
        proj_title = proj.get("title", "")
        rep = format_repeat(t.get("repeat_after", 0), t.get("repeat_mode", 0))
        parts = [relative_due_label(t.get("due_date"))] + ([rep] if rep else []) + [proj_title]
        subtitle = " · ".join(parts)
        details = task_details_text(t, proj_title)
        items.append(
            {
                "uid": f"duetask-{t['id']}",
                "title": t["title"],
                "subtitle": subtitle,
                "arg": "",
                "autocomplete": t["title"],
                "variables": {"task_id": str(t["id"]), "task_title": t["title"]},
                "text": {"copy": details, "largetype": details},
            }
        )
    if not items:
        items.append({"title": "No matching tasks", "valid": False})
    alfred_output(items, cache_seconds=cache_ttl())


def cmd_due_options(query):
    task_title = env("task_title", "task")
    task_id = env("task_id", "")
    q = (query or "").strip()

    # Omit random task ID numbers in input if passed through from Alfred query
    if q and task_id and q == task_id:
        q = ""

    items = []

    parsed = parse_due_date(q) if q else None
    if parsed == "CLEAR":
        items.append(
            {
                "title": "Clear due date",
                "subtitle": f"Remove the due date from '{task_title}'",
                "arg": "CLEAR",
            }
        )
    elif parsed is not None:
        items.append(
            {
                "title": f"Set due date to {parsed.strftime('%A, %b %d %H:%M')}",
                "subtitle": f"For '{task_title}'",
                "arg": parsed.isoformat(),
            }
        )

    quick = [
        ("in 1 hour", "In 1 hour"),
        ("in 3 hours", "In 3 hours"),
        ("tonight", "Tonight (20:00)"),
        ("tomorrow", "Tomorrow (18:00)"),
        ("weekend", "This weekend (Saturday 18:00)"),
        ("next week", "Next week (Monday 18:00)"),
        ("none", "Clear due date"),
    ]
    for key, label in quick:
        if q and q.lower() not in key and q.lower() not in label.lower():
            continue
        resolved = parse_due_date(key)
        arg = "CLEAR" if resolved == "CLEAR" else resolved.isoformat()
        items.append(
            {
                "title": label,
                "subtitle": f"For '{task_title}'",
                "arg": arg,
            }
        )

    if not items:
        items.append(
            {
                "title": "Type a time or date",
                "subtitle": "e.g. in 9 hours, 5pm, 17:30, tomorrow 9am, friday 2pm, +3d, none",
                "valid": False,
            }
        )

    alfred_output(items)


def cmd_apply_due():
    task_id = env("task_id")
    new_due = sys.argv[2] if len(sys.argv) > 2 else ""
    if not task_id or not new_due:
        print("Missing task or date.")
        return
    task = fetch_task(task_id)
    if new_due == "CLEAR":
        task["due_date"] = "0001-01-01T00:00:00Z"
        iso_due = "CLEAR"
    else:
        dt = datetime.fromisoformat(new_due).astimezone(timezone.utc)
        task["due_date"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        iso_due = task["due_date"]
    api_request("POST", f"/tasks/{task_id}", body=task)
    verify_task_write(task_id, {"due_date": task["due_date"]})
    optimistic_update_task(task_id, iso_due)
    trigger_bg_refresh("tasks", configured_range())
    if new_due == "CLEAR":
        print(f"Cleared due date on: {task.get('title')}")
    else:
        print(f"Due date updated: {task.get('title')} -> {relative_due_label(task['due_date'])}")


def cmd_projects(query):
    projects = fetch_projects()
    q = (query or "").strip().lower()
    default_id = None
    try:
        default_id = fetch_user().get("settings", {}).get("default_project_id")
    except (ConfigError, ApiError):
        pass

    items = []
    for p in sorted(projects, key=lambda p: p["title"].lower()):
        if p["id"] < 0:  # saved filters are not valid task-creation targets
            continue
        if p.get("is_archived"):
            continue
        if q and q not in p["title"].lower():
            continue
        star = " (default)" if p["id"] == default_id else ""
        items.append(
            {
                "uid": f"project-{p['id']}",
                "title": p["title"] + star,
                "subtitle": f"Set as default project for new tasks (id {p['id']})",
                "arg": str(p["id"]),
                "autocomplete": p["title"],
                "variables": {"project_id": str(p["id"]), "project_title": p["title"]},
            }
        )
    if not items:
        items.append({"title": "No matching projects", "valid": False})
    alfred_output(items, cache_seconds=cache_ttl())


def cmd_set_default_project():
    project_id = env("project_id")
    project_title = env("project_title", project_id)
    if not project_id:
        print("No project selected.")
        return
    user = fetch_user(force=True, allow_bg=False)
    settings = user.get("settings", {})
    settings["default_project_id"] = int(project_id)
    api_request("POST", "/user/settings/general", body=settings)
    write_cache("user.json", user)
    print(f"Default project for new tasks set to: {project_title}")


def cmd_scopes(query):
    """List projects and saved filters together, to browse into either one's tasks."""
    scopes = fetch_projects()
    q = (query or "").strip().lower()

    items = []
    for s in sorted(scopes, key=lambda s: s["title"].lower()):
        if s.get("is_archived"):
            continue
        if q and q not in s["title"].lower():
            continue
        kind = "Filter" if s["id"] < 0 else "Project"
        items.append(
            {
                "uid": f"scope-{s['id']}",
                "title": s["title"],
                "subtitle": f"{kind} · browse open tasks",
                "arg": "",
                "autocomplete": s["title"],
                "variables": {"scope_id": str(s["id"]), "scope_title": s["title"]},
            }
        )
    if not items:
        items.append({"title": "No matching projects or filters", "valid": False})
    alfred_output(items, cache_seconds=cache_ttl())


def cmd_scope_tasks(query):
    scope_id = env("scope_id")
    scope_title = env("scope_title", "this scope")
    if not scope_id:
        alfred_output([error_item("No project or filter selected.")])
        return

    tasks = fetch_scope_tasks(scope_id)
    q = (query or "").strip().lower()

    items = []
    for t in tasks:
        if q and q not in t["title"].lower():
            continue
        details = task_details_text(t, scope_title)
        items.append(
            {
                "uid": f"scopetask-{t['id']}",
                "title": t["title"],
                "subtitle": relative_due_label(t.get("due_date")),
                "arg": str(t["id"]),
                "autocomplete": t["title"],
                "variables": {"action": "open", "task_id": str(t["id"]), "task_title": t["title"]},
                "mods": {
                    "cmd": {
                        "subtitle": f"Complete task: '{t['title']}'",
                        "variables": {"action": "complete", "task_id": str(t["id"]), "task_title": t["title"]},
                    },
                    "alt": {
                        "subtitle": f"Reschedule task: '{t['title']}'",
                        "arg": "",
                        "variables": {"task_id": str(t["id"]), "task_title": t["title"]},
                    },
                },
                "text": {"copy": details, "largetype": details},
            }
        )
    if not items:
        items.append({"title": f"No open tasks in '{scope_title}'", "valid": False})
    alfred_output(items, cache_seconds=cache_ttl())


PROJECT_TAG_RE = re.compile(r"(?:^|\s)[@/](\S+)")


def extract_project_tag(title):
    """Pull a trailing/inline '@ProjectName' or '/ProjectName' tag out of a task title, if present."""
    match = None
    for match in PROJECT_TAG_RE.finditer(title):
        pass  # keep the last match so "@" only takes effect once
    if not match:
        return title, None
    start, end = match.span()
    new_title = re.sub(r"\s+", " ", (title[:start] + title[end:])).strip()
    return new_title, match.group(1)


def _norm_project_name(name):
    """Casefold and keep only letters/digits, so 'To Watch', 'to-watch' and 'towatch' agree."""
    return re.sub(r"[\W_]+", "", name.casefold())


def match_project(tag, projects):
    """Fuzzy-match a project tag to a project. Returns the project, or None if nothing fits.

    Tiers, best first: exact, prefix, substring, close spelling (all on normalised names).
    Two projects tied in the best tier raise ConfigError rather than guess.
    """
    key = _norm_project_name(tag)
    if not key:
        return None
    named = [(p, _norm_project_name(p["title"])) for p in projects]
    named = [(p, n) for p, n in named if n]
    tiers = (
        [p for p, n in named if n == key],
        [p for p, n in named if n.startswith(key)],
        [p for p, n in named if key in n],
    )
    close = difflib.get_close_matches(key, [n for _, n in named], n=len(named), cutoff=0.8)
    tiers += ([p for p, n in named if n in close],)
    for candidates in tiers:
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            titles = ", ".join(f"'{p['title']}'" for p in candidates)
            raise ConfigError(f"'{tag}' matches several projects: {titles}. Type more of the name.")
    return None


# Single trailing word that's safe to treat as a date on its own, without a
# "next"/"this" or digit alongside it. Excludes 3-letter weekday abbreviations
# (e.g. "sat", "sun", "fri") since those collide with common English words.
_TITLE_DATE_SAFE_WORDS = {
    "today", "todays", "tomorrow", "tmrw", "tmw", "tonight",
    "weekend", "thisweekend", "noon", "midnight",
} | set(WEEKDAYS) | {
    # Full month names only; "may"/"march" double as ordinary words and abbreviations
    # ("dec", "mar", "jun") are too easily part of a title.
    name for name in MONTH_NAMES if len(name) > 4 and name not in ("march", "sept")
}


def extract_due_from_title(title):
    """Like _extract_due_from_title, but understands a trailing 'every ...' repeat.

    With a repeat, the phrase stays in the returned title (create_task strips it and sets
    repeat_after) and the due date is any explicit date before it, else the first occurrence.
    """
    bare, rep, phrase = extract_repeat_from_title(title)
    if rep is None:
        return _extract_due_from_title(title)
    rest, due = _extract_due_from_title(bare)
    return f"{rest} {phrase}", due or rep["first_due"]


def _extract_due_from_title(title):
    """Pull a trailing natural-language date/time expression out of a task title, if present.

    Tries the longest trailing word-sequence first so multi-word expressions
    like 'next friday 2pm' are captured whole rather than partially.
    """
    tokens = title.split()
    for start in range(1, len(tokens)):
        suffix_tokens = tokens[start:]
        candidate = " ".join(suffix_tokens)
        if len(suffix_tokens) == 1:
            word = candidate.lower()
            looks_like_time_or_date = bool(re.search(r"[\d:/+-]", word)) or word.endswith(("am", "pm"))
            if word not in _TITLE_DATE_SAFE_WORDS and not looks_like_time_or_date:
                continue
        parsed = parse_due_date(candidate)
        if isinstance(parsed, datetime):
            new_title = " ".join(tokens[:start]).strip()
            if new_title:
                return new_title, parsed
    return title, None


def pending_path():
    # Prefer Alfred's data dir (survives cache clears); fall back to the cache dir.
    d = env("alfred_workflow_data")
    if d:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "pending_tasks.json")
    return cache_path("pending_tasks.json")


def load_pending():
    try:
        with open(pending_path(), "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("id") and e.get("title")]


def save_pending(entries):
    path = pending_path()
    if not entries:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    os.replace(tmp, path)


def queue_pending(title, due_date=None, repeat_mode=None):
    """Save a task that couldn't be sent. Resolves any trailing due date now, so a
    relative date ('tomorrow') keeps its original meaning when retried later."""
    title = normalize_query_text((title or "").strip())
    due_iso = ""
    if isinstance(due_date, datetime):
        due_iso = due_date.isoformat()
    elif isinstance(due_date, str) and due_date != "CLEAR":
        due_iso = due_date
    else:
        bare, tag = extract_project_tag(title)
        bare, parsed_due = extract_due_from_title(bare)
        if parsed_due is not None:
            title = f"{bare} @{tag}" if tag else bare
            due_iso = parsed_due.isoformat()
    entries = load_pending()
    if any(e["title"] == title and e.get("due", "") == due_iso and e.get("repeat_mode", "") == (repeat_mode or "") for e in entries):
        return
    entries.append({"id": f"{int(time.time() * 1000):x}", "title": title, "due": due_iso, "repeat_mode": repeat_mode or "", "saved_at": time.time()})
    save_pending(entries)


def create_task_or_queue(title, due_date=None, repeat_mode=None):
    """create_task, but keep the title for a later retry if the server is unreachable."""
    try:
        return create_task(title, due_date, repeat_mode)
    except NetworkError as e:
        queue_pending(title, due_date, repeat_mode)
        raise NetworkError(f"{e}. Saved the task; open the task list to retry it.") from None


def pending_items():
    items = []
    now = time.time()
    for e in load_pending():
        mins = max(0, int((now - e.get("saved_at", now)) // 60))
        ago = "just now" if mins < 1 else f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago" if mins < 1440 else f"{mins // 1440} d ago"
        due = f" · due {e['due'][:10]}" if e.get("due") else ""
        items.append(
            {
                "uid": f"pending-{e['id']}",
                "title": f"Retry unsent task: {e['title']}",
                "subtitle": f"Saved {ago}{due} · ↩ to retry · ⌥ to discard",
                "arg": "retry",
                "variables": {"action": "retry-pending", "pending_id": e["id"]},
                "mods": {
                    "alt": {
                        "subtitle": f"Discard saved task: '{e['title']}'",
                        "variables": {"action": "discard-pending", "pending_id": e["id"]},
                    }
                },
                "valid": True,
            }
        )
    return items


def create_task(title, due_date=None, repeat_mode=None):
    title = normalize_query_text((title or "").strip())
    if not title:
        raise ConfigError("Task title cannot be empty.")

    title, project_tag = extract_project_tag(title)
    if not title:
        raise ConfigError("Task title cannot be empty.")

    title, repeat, _ = extract_repeat_from_title(title, after_done=repeat_mode == "after_done")
    if not due_date:
        parsed_title, parsed_due = _extract_due_from_title(title)
        if parsed_due is not None:
            title, due_date = parsed_title, parsed_due
        elif repeat is not None:
            due_date = repeat["first_due"]

    if project_tag:
        match = match_project(project_tag, fetch_projects())
        if match is None:
            raise ConfigError(
                f"No project found matching '{project_tag}'. Try 'vproject' to list project titles."
            )
        project_id = match["id"]
    else:
        override = env("VIKUNJA_DEFAULT_PROJECT").strip()
        if override:
            if override.lstrip("-").isdigit():
                project_id = int(override)
            else:
                match = next(
                    (p for p in fetch_projects() if p["title"].lower() == override.lower()),
                    None,
                )
                if match is None:
                    raise ConfigError(
                        f"VIKUNJA_DEFAULT_PROJECT is set to '{override}', but no project with that "
                        f"exact title exists. Use a project ID instead, or fix the name."
                    )
                project_id = match["id"]
        else:
            user = fetch_user()
            project_id = user.get("settings", {}).get("default_project_id") or 0
            if not project_id:
                projects = fetch_projects()
                if not projects:
                    raise ConfigError("No default project set and no projects exist. Use 'vproject' first.")
                project_id = min(projects, key=lambda p: p["id"])["id"]

    body = {"title": title}
    if repeat is not None:
        body["repeat_after"] = repeat["seconds"]
        body["repeat_mode"] = repeat["mode"]
    if due_date:
        if isinstance(due_date, datetime):
            dt = due_date.astimezone(timezone.utc)
            body["due_date"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        elif isinstance(due_date, str) and due_date != "CLEAR":
            try:
                dt = datetime.fromisoformat(due_date).astimezone(timezone.utc)
                body["due_date"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass

    task = api_request("PUT", f"/projects/{project_id}/tasks", body=body)
    if not isinstance(task, dict) or "id" not in task:
        raise ApiError("Vikunja did not return a valid task after creation.")
    checks = {"title": title}
    if "due_date" in body:
        checks["due_date"] = body["due_date"]
    try:
        task = verify_task_write(task["id"], checks)
    except NetworkError:
        pass  # the PUT succeeded; don't let a failed re-read make callers retry (duplicate)
    optimistic_add_task(task)
    trigger_bg_refresh("tasks", configured_range())
    return task, project_id


def cmd_add_task(query):
    title = (query or "").strip()
    if not title:
        print("Type a task title after the keyword.")
        return
    task, project_id = create_task_or_queue(title)
    print(f"Created: {task.get('title')} (project {project_id})")


# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: vikunja.py <subcommand> [query]", file=sys.stderr)
        sys.exit(1)

    sub = sys.argv[1]
    query = sys.argv[2] if len(sys.argv) > 2 else ""

    dispatch = {
        "list": lambda: cmd_list(query),
        "dispatch": cmd_dispatch,
        "due-tasks": lambda: cmd_due_tasks(query),
        "due-options": lambda: cmd_due_options(query),
        "apply-due": cmd_apply_due,
        "projects": lambda: cmd_projects(query),
        "set-default-project": cmd_set_default_project,
        "add-task": lambda: cmd_add_task(query),
        "scopes": lambda: cmd_scopes(query),
        "scope-tasks": lambda: cmd_scope_tasks(query),
        "bg-refresh": lambda: cmd_bg_refresh(*sys.argv[2:]),
    }

    fn = dispatch.get(sub)
    if fn is None:
        print(f"Unknown subcommand: {sub}", file=sys.stderr)
        sys.exit(1)

    if sub in ("list", "due-tasks", "due-options", "projects", "scopes", "scope-tasks"):
        run_guarded(fn)
    elif sub == "bg-refresh":
        fn()
    else:
        try:
            fn()
        except (ConfigError, ApiError) as e:
            print(f"Error: {redact(e)}")
        except Exception as e:  # noqa: BLE001
            print(f"Unexpected error: {redact(e)}")


if __name__ == "__main__":
    main()

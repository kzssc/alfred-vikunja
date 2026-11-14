# Vikunja Alfred Workflow

Manage, search, reschedule, and create [Vikunja](https://vikunja.io) tasks directly from Alfred with instant response times, zero-latency caching, and natural language date/time parsing.

> [!NOTE]
> **Disclaimer**: This workflow is vibecoded with AI assistance.

![Vikunja Alfred Workflow](icon.png)

---

## Features

- **Blazing Fast**: Instant local disk caching with background revalidation. Keystrokes filter in Alfred's native memory.
- **Global Hotkey**: Press `⌃R` (Control+R) anywhere in macOS to jump directly to your tasks.
- **Flexible Agenda Views**: Switch instantly between dynamic ranges (`v today`, `v week`, `v 7d`, `v 8d`, `v 2w`, `v 3m`, `v 1y`).
- **Global Search**: Type `v <search>` to search across all open tasks and labels across all projects.
- **Natural Language Rescheduling**: Press `⌥↩` (Option+Enter) on any task to reschedule with expressions like `in 9 hours`, `5pm`, `tomorrow 9am`, `friday 2pm`, `weekend`, or `+3d`.
- **Smart Fallback Quick-Create**: If a search yields no matches, press `↩` to immediately create the task in your default project.
- **Rich Task Inspection**: Press `⌘C` to copy detailed task information (due date, recurrence, priority, labels, project, URL) or `⌘L` to view in Large Type.
- **Zero Dependencies**: Pure Python 3 using standard library modules.

---

## Installation & Setup

1. **Download Workflow**:
   Download the latest `Vikunja.alfredworkflow` from the [Releases](https://github.com/kzssc/alfred-vikunja/releases) page and double-click to install in Alfred 5.

2. **Generate API Token in Vikunja**:
   - In Vikunja, navigate to **Settings > API Tokens**.
   - Create a token with read/write permissions for **Tasks**, **Projects**, and **User Settings**.

3. **Configure in Alfred**:
   - Open **Alfred Preferences > Workflows > Vikunja**.
   - In the **Workflow Configuration** panel, set:
     - `VIKUNJA_URL`: Your Vikunja instance URL (e.g. `https://vikunja.example.com` or `vikunja.example.com`).
     - `VIKUNJA_API_TOKEN`: The API token generated above.
     - `VIKUNJA_RANGE`: Default agenda view (`today`, `week`, `7d`, `8d`, `2w`, `3m`, `1y`, or `all`; default: `today`).
     - `VIKUNJA_DEFAULT_PROJECT`: *(Optional)* Default project title or ID for quick-add (`vadd`). If omitted, uses your Vikunja default project.
     - `VIKUNJA_CACHE_TTL`: Cache duration in seconds (default: `60`).

---

## Usage & Commands

### `v` — View, Search & Quick-Create Tasks

- **Browse Agenda**:
  - `v` — View tasks in your configured range (plus overdue).
  - `v today` — Tasks due today.
  - `v week` — Tasks due this week (through Sunday).
  - `v 8d` / `v 14d` — Tasks due in the next 8 or 14 days.
  - `v 2w` / `v 3w` — Tasks due in the next 2 or 3 weeks.
  - `v 3m` / `v 6m` — Tasks due in the next 3 or 6 months.
  - `v 1y` — Tasks due in the next year.

- **Search All Tasks**:
  - `v <search>` — Search all open tasks across all projects (e.g. `v dishwasher`, `v taxes`).
  - `v 2w <search>` — Search restricted to tasks due in the next 2 weeks.

- **Actions on Highlighted Task**:
  - `↩` (Enter): Open task in browser.
  - `⌘↩` (Cmd+Enter): Mark task as complete.
  - `⌥↩` (Option+Enter): Reschedule task.
  - `⌃↩` (Control+Enter): Snooze 1 day. `⇧↩` (Shift+Enter): Snooze 1 week. The new due date is that many days after the current due date, or after today if the task is overdue or has no due date. It keeps the task's time of day (18:00 when it had none).
  - `⌘C`: Copy full task details.
  - `⌘L`: View full task details in Large Type.

- **Fallback Quick-Create**:
  - If no tasks match your search query, press `↩` on the fallback item to create the task immediately.
  - Add `@ProjectName` (or `/ProjectName`) anywhere in the query to create the task in that project instead of your default (e.g. `v Buy milk @Groceries` or `v Buy milk /Groceries`). Matching is fuzzy: `/towatch` finds "To Watch" (see `vadd` below for the rules).
  - Add a trailing date/time expression to set the due date on creation (e.g. `v Buy milk tomorrow`, `v Call dentist next friday 2pm`). It's stripped from the title automatically.
  - Press `⌘↩` on the create item to create the task and open it in your browser, for tasks you want to flesh out.
  - Add notes after a standalone `--` (e.g. `v Call dentist friday -- ask about the invoice`). Everything after it becomes the task description. Tags, dates and repeats are read only from the part before `--`.

---

### `vadd <title>` — Quick Add Task

Creates a new task in your default project.

- Add `@ProjectName` (or `/ProjectName`) anywhere in the title to create the task in that project instead (e.g. `vadd Buy milk @Groceries` or `vadd Buy milk /Groceries`). The tag is stripped from the title and fuzzy-matched against your project titles, ignoring case, spaces and punctuation, so `/towatch` or `/to-watch` finds "To Watch". The best match wins in this order: exact, starts with, contains, close spelling. Tags cannot contain spaces. If two projects tie in the best tier, nothing is created and the error lists the candidates.
- Add notes after a standalone `--` to set the task description (e.g. `vadd Buy milk -- oat, not soy`). Tags, dates and repeats are read only from the part before `--`, so `@names` or dates in the notes stay as written.
- Add a trailing date/time expression to set a due date (e.g. `vadd Buy milk tomorrow`, `vadd Call dentist next friday 2pm`). It's stripped from the title and parsed with the same rules as `vdue` (see below).
- If Vikunja is unreachable, the task is saved locally and shown at the top of the `v` list as "Retry unsent task" (`↩` retries, `⌥` discards), so you don't have to retype it. Once Vikunja is reachable again, a macOS notification tells you how many unsent tasks are waiting (at most once every 30 minutes). Nothing is retried automatically, because a request that timed out may still have reached the server and would then create a duplicate.
- Add a trailing repeat to make the task recur: `every wednesday`, `every 3 days`, `every 2 weeks`, `every other week`, `every month`, `every year`, optionally with a time (`every monday 9am`, `every 3 days at 5pm`). `every <weekday>` is first due on the next such day; `every N <unit>` is first due N units from now; the time defaults to 18:00. An explicit date before it (`Pay rent 1 dec every month`) sets the first due date. By default Vikunja repeats from the due date, so a Wednesday task stays on Wednesdays; in the `v` dialogue, a second row ("repeat after completion") counts the next due date from when you complete the task instead (`vadd` always repeats from the due date). `every month` uses Vikunja's calendar-month repeat; `every N months` (N > 1) is N × 30 days.

---

### `vdue` — Reschedule Tasks (Standalone)

Searches across all open tasks to pick one and assign a new due date or time:
- Quick presets: `In 1 hour`, `In 3 hours`, `Tonight (20:00)`, `Tomorrow (18:00)`, `This weekend`, `Next week`, `Clear due date`.
- Relative times: `in 9 hours`, `9h`, `in 30 mins`, `in 2h 30m`, `+9h`.
- Specific times today: `5pm`, `17:30`, `noon`, `tonight`.
- Day + time combinations: `tomorrow 9am`, `tomorrow 15:30`, `friday 2pm`.
- Relative dates: `+3d`, `tomorrow`, `friday`, `weekend`.
- Explicit dates: `2026-09-01 15:30`, `2026-09-01`, `01/12` (day/month; falls back to month/day when invalid, e.g. `12/25`), `dec` / `december` (1st of the month).
- Clear date: `none`, `clear`, `no date`.

---

### `vfilter` — Browse Projects & Saved Filters

Browse tasks scoped to specific Projects or Saved Filters:
- Selecting a project/filter lists its open tasks.
- `↩` opens in browser; `⌘↩` marks as complete; `⌥↩` reschedules.

---

### `vproject` — Set Default Project

Lists active projects. Selecting one sets it as your Vikunja account's default project.

---

## Requirements

- macOS with Alfred 5 (Powerpack).
- Python 3 (`/usr/bin/env python3`).
- Zero external Python dependencies.

---

## Acknowledgements

Natural-language date parsing (`aug 21`, `on friday`, `next thursday at 15.30`, etc.) was refined against the input formats documented by [surrealroad/alfred-reminders](https://github.com/surrealroad/alfred-reminders), a fellow Alfred workflow for Apple Reminders.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

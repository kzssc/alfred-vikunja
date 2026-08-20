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
  - `⌘C`: Copy full task details.
  - `⌘L`: View full task details in Large Type.

- **Fallback Quick-Create**:
  - If no tasks match your search query, press `↩` on the fallback item to create the task immediately.
  - Add `@ProjectName` anywhere in the query to create the task in that project instead of your default (e.g. `v Buy milk @Groceries`).

---

### `vadd <title>` — Quick Add Task

Creates a new task in your default project.

- Add `@ProjectName` anywhere in the title to create the task in that project instead (e.g. `vadd Buy milk @Groceries`). The `@ProjectName` tag is stripped from the title and matched case-insensitively against your project titles.

---

### `vdue` — Reschedule Tasks (Standalone)

Searches across all open tasks to pick one and assign a new due date or time:
- Quick presets: `In 1 hour`, `In 3 hours`, `Tonight (20:00)`, `Tomorrow (18:00)`, `This weekend`, `Next week`, `Clear due date`.
- Relative times: `in 9 hours`, `9h`, `in 30 mins`, `in 2h 30m`, `+9h`.
- Specific times today: `5pm`, `17:30`, `noon`, `tonight`.
- Day + time combinations: `tomorrow 9am`, `tomorrow 15:30`, `friday 2pm`.
- Relative dates: `+3d`, `tomorrow`, `friday`, `weekend`.
- Explicit dates: `2026-09-01 15:30`, `2026-09-01`, `09/01`.
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

## License

MIT License. See [LICENSE](LICENSE) for details.

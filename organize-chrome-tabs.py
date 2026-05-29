#!/usr/bin/env python3
"""
organize-chrome-tabs  —  Tame your Chrome tab chaos.

Closes duplicates, sorts tabs into themed windows, clusters by domain,
and optionally creates native Chrome tab group labels.

USAGE
  organize-chrome-tabs [options]

OPTIONS
  --single-window      Merge everything into one window, cluster by domain
  --combine            Merge first, then redistribute into themed windows
  --cluster-only       Cluster by domain within current windows (no redistribution)
  --no-close-dupes     Skip duplicate removal
  --build-extension    Write a Chrome extension that creates native tab group labels
  --init               Interactively create a personal themes config
  --config PATH        Load themes from a JSON file (overrides built-in themes)
  --dry-run            Preview changes without applying them

INSTALL
  brew install chrome-cli
  chmod +x organize-chrome-tabs.py

SHARE
  Copy this file to ~/scripts/ on any Mac. No other dependencies.
"""

import subprocess, re, json, sys, argparse, time, os
from collections import defaultdict
from urllib.parse import urlparse

# ── Default themes ──────────────────────────────────────────────────────────
# Order matters: window 1 → theme[0], window 2 → theme[1], etc.
# Edit or override with --config to match your own projects.

DEFAULT_THEMES = [
    {
        "name": "Dev & Cloud",
        "patterns": [
            "github", "gitlab", "bitbucket",
            "localhost", "127.0.0.1", "0.0.0.0",
            "vercel", "netlify", "render", "railway", "fly.io", "heroku",
            "cloudflare", "supabase", "firebase", "aws", "azure",
        ],
    },
    {
        "name": "Productivity",
        "patterns": [
            "notion", "linear", "jira", "confluence", "asana", "trello",
            "figma", "airtable", "clickup", "monday.com",
            "slack", "discord", "loom",
        ],
    },
    {
        "name": "AI & Research",
        "patterns": [
            "chatgpt", "claude", "openai", "perplexity", "gemini",
            "youtube", "medium", "substack", "hackernews", "hn.algolia",
            "wikipedia", "reddit",
        ],
    },
    {
        "name": "Email & Comms",
        "patterns": [
            "gmail", "mail.google", "outlook", "calendar.google",
            "messages.google", "linkedin", "twitter", "x.com",
        ],
    },
    # Anything unmatched stays in its current window.
]

# ── Extension template ──────────────────────────────────────────────────────

_EXT_MANIFEST = """{
  "manifest_version": 3,
  "name": "Tab Group Organizer",
  "version": "1.0",
  "description": "Groups open tabs by domain. Load once, then remove.",
  "permissions": ["tabs", "tabGroups"],
  "host_permissions": ["<all_urls>"],
  "background": { "service_worker": "background.js" }
}
"""

_EXT_BACKGROUND = """
chrome.runtime.onInstalled.addListener(async () => {
  const wins = await chrome.windows.getAll({ populate: true });
  let groups = 0;
  for (const win of wins) {
    const byDomain = {};
    for (const tab of win.tabs) {
      if (!tab.url || tab.url.startsWith("chrome")) continue;
      let domain = "other";
      try { domain = new URL(tab.url).hostname.replace(/^www\\./, ""); } catch {}
      (byDomain[domain] = byDomain[domain] || []).push(tab.id);
    }
    for (const [domain, ids] of Object.entries(byDomain)) {
      if (ids.length < 2) continue;
      try {
        const gid = await chrome.tabs.group({ tabIds: ids, createProperties: { windowId: win.id } });
        await chrome.tabGroups.update(gid, { title: domain, collapsed: ids.length > 6 });
        groups++;
      } catch (e) { console.warn("Could not group", domain, e.message); }
    }
  }
  console.log(`Tab Group Organizer: created ${groups} group(s).`);
});
"""

# ── Chrome helpers ──────────────────────────────────────────────────────────

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()

def _apple(script):
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout.strip()

def ensure_chrome_cli():
    if subprocess.run(["which", "chrome-cli"], capture_output=True).returncode != 0:
        print("Installing chrome-cli…")
        subprocess.run(["brew", "install", "chrome-cli"], check=True)

def list_windows():
    """Returns [(window_id, title), …]"""
    out = _run(["chrome-cli", "list", "windows"])
    result = []
    for line in out.splitlines():
        m = re.match(r"\[(\d+)\]\s*(.*)", line)
        if m:
            result.append((m.group(1), m.group(2).strip()))
    return result

def list_tabs():
    """Returns [(window_id, tab_id, title), …].

    chrome-cli omits the window prefix when only one window is open,
    outputting [tabId] instead of [windowId:tabId].
    """
    out = _run(["chrome-cli", "list", "tabs"])
    windows = list_windows()
    sole_win = windows[0][0] if len(windows) == 1 else None
    tabs = []
    for line in out.splitlines():
        m = re.match(r"\[(\d+):(\d+)\]\s*(.*)", line)
        if m:
            tabs.append((m.group(1), m.group(2), m.group(3).strip()))
            continue
        if sole_win:
            m = re.match(r"\[(\d+)\]\s*(.*)", line)
            if m:
                tabs.append((sole_win, m.group(1), m.group(2).strip()))
    return tabs

def get_tab_url(tab_id):
    out = _run(["chrome-cli", "info", "-t", tab_id])
    for line in out.splitlines():
        if line.startswith("Url:"):
            return line[4:].strip()
    return ""

def close_tab(tab_id, dry_run=False):
    if dry_run:
        return
    subprocess.run(["chrome-cli", "close", "-t", tab_id], capture_output=True)

def move_tab(tab_id, window_id, dry_run=False):
    """Move a tab by ID to the end of a window. Uses AppleScript (chrome-cli move is unreliable)."""
    if dry_run:
        return
    _apple(f"""
    tell application "Google Chrome"
        set tgt to missing value
        repeat with w in windows
            if id of w = {window_id} then set tgt to w
        end repeat
        if tgt is missing value then return
        repeat with w in windows
            repeat with t in tabs of w
                if id of t = {tab_id} then
                    move t to end of tgt
                    return
                end if
            end repeat
        end repeat
    end tell
    """)

def open_new_window():
    """Open a new Chrome window and return its ID."""
    before = {w[0] for w in list_windows()}
    subprocess.run(["chrome-cli", "open", "about:blank", "-n"], capture_output=True)
    time.sleep(0.3)
    after = list_windows()
    new = [w[0] for w in after if w[0] not in before]
    return new[0] if new else None

def merge_all_windows(dry_run=False):
    """Collapse every Chrome window into window 1 via AppleScript."""
    if dry_run:
        return
    _apple("""
    tell application "Google Chrome"
        repeat while (count windows) > 1
            set src to window (count windows)
            repeat while (count tabs of src) > 0
                move tab 1 of src to end of window 1
            end repeat
        end repeat
    end tell
    """)

# ── Core logic ──────────────────────────────────────────────────────────────

def find_duplicates(tabs):
    """Return tab IDs to close, keeping the first of each title."""
    seen, dupes = {}, []
    for _, tab_id, title in tabs:
        key = title.lower().strip()
        if not key:
            continue
        if key in seen:
            dupes.append(tab_id)
        else:
            seen[key] = tab_id
    return dupes

def match_theme(title, url, themes):
    """Return theme index (first match) or -1."""
    hay = (title + " " + url).lower()
    for i, theme in enumerate(themes):
        if any(p.lower() in hay for p in theme["patterns"]):
            return i
    return -1

def ensure_enough_windows(themes, existing_windows, dry_run):
    """Return a list of window IDs, opening new windows as needed."""
    needed = len(themes)
    ids = [w[0] for w in existing_windows]
    for _ in range(needed - len(existing_windows)):
        if dry_run:
            ids.append("NEW")
        else:
            new_id = open_new_window()
            if new_id:
                ids.append(new_id)
    return ids[:needed]

def extract_domain(url):
    if not url or url.startswith("chrome") or url in ("about:blank", ""):
        return "chrome"
    try:
        return re.sub(r"^www\.", "", urlparse(url).hostname or "") or "other"
    except Exception:
        return "other"

def cluster_by_domain(window_id, tabs_in_window, dry_run=False):
    """Reorder tabs in a window so same-domain tabs are adjacent."""
    print(f"\n  Fetching URLs for {len(tabs_in_window)} tabs…")
    tab_info = []
    for _, tab_id, title in tabs_in_window:
        url = get_tab_url(tab_id) if not dry_run else f"https://example-{tab_id}.com/"
        tab_info.append((tab_id, title, extract_domain(url)))

    by_domain = defaultdict(list)
    for tab_id, title, domain in tab_info:
        by_domain[domain].append(tab_id)

    # Sort: multi-tab domains first (descending count), then alpha
    ordered = sorted(by_domain.keys(), key=lambda d: (-len(by_domain[d]), d))

    print("  Domain clusters:")
    for domain in ordered:
        count = len(by_domain[domain])
        marker = "▸ " if count > 1 else "  "
        print(f"    {marker}{domain} ({count})")

    if not dry_run:
        for domain in ordered:
            for tab_id in by_domain[domain]:
                move_tab(tab_id, window_id)

# ── --build-extension ────────────────────────────────────────────────────────

def build_extension():
    ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tab-groups-ext")
    os.makedirs(ext_dir, exist_ok=True)
    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        f.write(_EXT_MANIFEST)
    with open(os.path.join(ext_dir, "background.js"), "w") as f:
        f.write(_EXT_BACKGROUND)
    print(f"Extension written to: {ext_dir}")
    print()
    print("To create native Chrome tab groups:")
    print("  1. Open  chrome://extensions")
    print("  2. Enable  Developer mode  (top-right toggle)")
    print(f"  3. Click  Load unpacked  → select:  {ext_dir}")
    print("  4. Tab groups appear immediately")
    print("  5. Remove the extension when done (it's no longer needed)")

# ── --init ───────────────────────────────────────────────────────────────────

def run_init():
    print("Creating a personal themes config.\n")
    print("Each theme becomes a Chrome window. Tabs are matched by title/URL substring.")
    print("Press Enter to skip a theme.\n")

    themes = []
    default_names = ["Work", "Side Projects", "Research", "Personal"]
    for name in default_names:
        theme_name = input(f"Theme name [{name}]: ").strip() or name
        raw = input(f"  Patterns for '{theme_name}' (comma-separated keywords/domains): ").strip()
        if raw:
            patterns = [p.strip() for p in raw.split(",") if p.strip()]
            themes.append({"name": theme_name, "patterns": patterns})

    if not themes:
        print("No themes entered — keeping built-in defaults.")
        return

    out_path = os.path.expanduser("~/scripts/chrome-themes.json")
    with open(out_path, "w") as f:
        json.dump(themes, f, indent=2)
    print(f"\nSaved to {out_path}")
    print(f"Use it with:  organize-chrome-tabs.py --config {out_path}")

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="organize-chrome-tabs",
        description="Close duplicate tabs, sort into themed windows, cluster by domain.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--single-window",   action="store_true", help="Merge all windows into one, cluster by domain")
    parser.add_argument("--combine",         action="store_true", help="Merge all windows first, then redistribute into themed windows")
    parser.add_argument("--cluster-only",    action="store_true", help="Only cluster by domain within current windows")
    parser.add_argument("--no-close-dupes",  action="store_true", help="Skip duplicate tab removal")
    parser.add_argument("--build-extension", action="store_true", help="Write a Chrome extension for native tab group labels")
    parser.add_argument("--init",            action="store_true", help="Create a personal themes config interactively")
    parser.add_argument("--config",          metavar="PATH",      help="JSON themes file (overrides built-in themes)")
    parser.add_argument("--dry-run",         action="store_true", help="Preview changes without applying them")
    args = parser.parse_args()

    if args.build_extension:
        build_extension()
        return

    if args.init:
        run_init()
        return

    ensure_chrome_cli()

    themes = DEFAULT_THEMES
    if args.config:
        with open(os.path.expanduser(args.config)) as f:
            themes = json.load(f)
        print(f"Loaded {len(themes)} themes from {args.config}")

    print("Reading Chrome tabs…")
    tabs = list_tabs()
    windows = list_windows()
    print(f"  {len(tabs)} tabs across {len(windows)} window(s)")

    # ── 1. Close duplicates ─────────────────────────────────────────────────
    dupes_closed = 0
    if not args.no_close_dupes:
        dupes = find_duplicates(tabs)
        if dupes:
            print(f"\nClosing {len(dupes)} duplicate(s)…")
            for tab_id in dupes:
                title = next((t[2] for t in tabs if t[1] == tab_id), tab_id)
                print(f"  × {title[:80]}")
                close_tab(tab_id, args.dry_run)
            dupes_closed = len(dupes)
            if not args.dry_run:
                tabs = list_tabs()
                windows = list_windows()

    # ── 2a. --single-window: merge + cluster, done ──────────────────────────
    if args.single_window:
        if not args.dry_run:
            print(f"\nMerging {len(windows)} window(s) into one…")
            merge_all_windows()
            tabs = list_tabs()
            windows = list_windows()
            target = windows[0][0]
            print(f"  {len(tabs)} tabs in window {target}")
            print("\nClustering by domain…")
            cluster_by_domain(target, tabs)
        else:
            print(f"\n[dry-run] would merge {len(windows)} window(s) then cluster by domain")
            cluster_by_domain(windows[0][0], tabs, dry_run=True)
        _done(dupes_closed, 0, args.dry_run)
        return

    # ── 2b. --combine: merge first, then fall through to redistribution ──────
    if args.combine:
        if not args.dry_run:
            print(f"\nMerging {len(windows)} window(s)…")
            merge_all_windows()
            tabs = list_tabs()
            windows = list_windows()
            print(f"  {len(tabs)} tabs ready")
        else:
            print("\n[dry-run] would merge all windows before redistributing")

    # ── 3. Redistribute into themed windows ─────────────────────────────────
    moves = 0
    if not args.cluster_only:
        win_ids = ensure_enough_windows(themes, windows, args.dry_run)
        theme_win = {i: win_ids[i] for i in range(len(themes))}

        print("\nWindow → theme:")
        for i, theme in enumerate(themes):
            print(f"  {win_ids[i] if i < len(win_ids) else '?':>12}  {theme['name']}")

        print("\nOrganizing…")
        for win_id, tab_id, title in tabs:
            url = get_tab_url(tab_id) if not args.dry_run else ""
            idx = match_theme(title, url, themes)
            if idx == -1:
                continue
            target = theme_win.get(idx)
            if target and target != win_id and target != "NEW":
                print(f"  → [{themes[idx]['name']}] {title[:72]}")
                move_tab(tab_id, target, args.dry_run)
                moves += 1

        if not args.dry_run:
            tabs = list_tabs()
            windows = list_windows()

    # ── 4. Cluster by domain within each window ──────────────────────────────
    print("\n── Clustering by domain ──")
    by_win = defaultdict(list)
    for win_id, tab_id, title in tabs:
        by_win[win_id].append((win_id, tab_id, title))

    for win_id, win_tabs in by_win.items():
        label = next((w[1] for w in windows if w[0] == win_id), win_id)
        print(f"\nWindow {win_id}  {label}  ({len(win_tabs)} tabs)")
        cluster_by_domain(win_id, win_tabs, args.dry_run)

    # ── 5. Suggest native tab groups ─────────────────────────────────────────
    print("\n── Native tab group labels ──")
    print("  python3 organize-chrome-tabs.py --build-extension")
    print("  Then: chrome://extensions → Developer mode → Load unpacked → tab-groups-ext/")

    _done(dupes_closed, moves, args.dry_run)


def _done(dupes, moves, dry_run):
    parts = []
    if dupes:
        parts.append(f"{dupes} duplicate(s) closed")
    if moves:
        parts.append(f"{moves} tab(s) moved")
    print("\n" + (", ".join(parts) or "Nothing changed") + ("  (dry-run)" if dry_run else "") + ".")


if __name__ == "__main__":
    main()

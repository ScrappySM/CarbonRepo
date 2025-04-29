import os
import json
import re
import asyncio
import hashlib
import aiohttp
import aiofiles
import tempfile
import shutil
import subprocess
import time
from datetime import datetime
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt
from rich.panel import Panel
from rich import box
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, ListView, ListItem, Input, Button, Static, LoadingIndicator, Label
from textual.containers import Vertical, Horizontal, Container, ScrollableContainer
from textual import events
from textual.screen import Screen
from textual.notifications import Notification
from textual.binding import Binding

CONFIG_FILE = "config.json"

# Constants
GITHUB_API = "https://api.github.com"
HEADERS = lambda token: {
    "Authorization": f"token {token}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "carbonrepo-generator"
}

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
console = Console()

async def load_config():
    if os.path.exists(CONFIG_FILE):
        async with aiofiles.open(CONFIG_FILE, 'r') as f:
            content = await f.read()
            return json.loads(content)
    return []

async def save_config(config):
    async with aiofiles.open(CONFIG_FILE, 'w') as f:
        await f.write(json.dumps(config, indent=4))
    console.print(f"[green]Updated {CONFIG_FILE}.[/green]\n")

def parse_repo_input(text):
    if text.startswith("http"):
        match = re.match(r"https?://github\.com/([^/]+/[^/]+)", text)
        return match.group(1) if match else None
    elif re.match(r"^[\w.-]+/[\w.-]+$", text):
        return text
    return None

async def fetch_asset_hash(session, url):
    sha256 = hashlib.sha256()
    
    # Add cache control headers to get the most up-to-date version
    headers = HEADERS(GITHUB_TOKEN)
    headers['Cache-Control'] = 'no-cache, no-store'
    headers['Pragma'] = 'no-cache'
    
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Failed to download {url} status={resp.status}")
        
        async for chunk in resp.content.iter_chunked(1024):
            sha256.update(chunk)
    
    return sha256.hexdigest()

async def fetch_latest_release_assets(session, repo_name):
    repo_url = f"https://api.github.com/repos/{repo_name}"
    resp = await session.get(repo_url, headers=HEADERS(GITHUB_TOKEN))
    data = await resp.json()
    default_branch = data.get('default_branch', 'main')
    branch_url = f"{repo_url}/branches/{default_branch}"
    resp2 = await session.get(branch_url, headers=HEADERS(GITHUB_TOKEN))
    br = await resp2.json()
    sha = br['commit']['sha']
    date = br['commit']['commit']['author']['date']
    comment = f"Last commit on {default_branch}: {sha} @ {date}"
    rel_url = f"https://api.github.com/repos/{repo_name}/releases/latest"
    resp3 = await session.get(rel_url, headers=HEADERS(GITHUB_TOKEN))
    if resp3.status != 200:
        return {}, comment
    rel = await resp3.json()
    assets = rel.get('assets', [])
    hashes = {}
    for asset in assets:
        try:
            h = await fetch_asset_hash(session, asset['browser_download_url'])
        except Exception as e:
            console.print(f"[red]Error hashing {asset['name']}: {e}[/red]")
            h = None
        hashes[asset['name']] = h
    return hashes, comment

async def fetch_latest_commit_info(session, repo_name):
    repo_url = f"https://api.github.com/repos/{repo_name}"
    resp = await session.get(repo_url, headers=HEADERS(GITHUB_TOKEN))
    data = await resp.json()
    default_branch = data.get('default_branch', 'main')
    
    branch_url = f"{repo_url}/branches/{default_branch}"
    resp2 = await session.get(branch_url, headers=HEADERS(GITHUB_TOKEN))
    br = await resp2.json()
    
    sha = br.get('commit', {}).get('sha', '')
    date = br.get('commit', {}).get('commit', {}).get('author', {}).get('date', '')
    message = br.get('commit', {}).get('commit', {}).get('message', '')
    
    return {
        'sha': sha,
        'date': date,
        'message': message
    }

class RepoItem(ListItem):
    """Custom ListItem showing repo information with plain visible text indicators"""
    
    def __init__(self, repo_name, is_updated=False, is_outdated=False, is_loading=False):
        super().__init__()
        self.repo_name = repo_name
        self.is_updated = is_updated
        self.is_outdated = is_outdated
        self.is_loading = is_loading
        self.loading_indicator_id = f"load-{id(self)}"
        
    def compose(self) -> ComposeResult:
        status_text = ""
        
        # Determine repo display and status with color
        if self.is_loading:
            # Loading state
            display_text = f"{self.repo_name}"
            yield Static(display_text)
            yield LoadingIndicator(id=self.loading_indicator_id)
        else:
            # Normal state with colored status indicators
            if self.is_outdated:
                status = "[yellow](!)[/yellow]"  # Yellow warning for outdated
                yield Static(f"{self.repo_name.ljust(30)} {status}", markup=True)
            elif self.is_updated:
                status = "[green](✓)[/green]"  # Green check for updated
                yield Static(f"{self.repo_name.ljust(30)} {status}", markup=True)
            else:
                # Default state - no special indicator
                yield Static(f"{self.repo_name.ljust(30)}")

class ConfirmationDialog(Horizontal):
    """Dialog to confirm updates"""
    
    def __init__(self, repo_name, message):
        super().__init__(id="confirm_dialog")
        self.repo_name = repo_name
        self.message = message
    
    def compose(self) -> ComposeResult:
        yield Static(f"{self.message}", classes="confirm-message")
        yield Button("Yes", variant="success", name="confirm_yes")
        yield Button("No", variant="error", name="confirm_no")

class DiffScreen(Screen):
    """Screen to display git diff output with proper scrolling support and syntax highlighting"""
    
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back"),
    ]
    
    def __init__(self, diff_text, title):
        super().__init__()
        self.title = title
        self.diff_text = diff_text
        self.processed_text = self._process_diff_text(diff_text)
        
    def _process_diff_text(self, text):
        """Process the diff text to add syntax highlighting using Rich markup"""
        import re
        
        # Split the text into lines
        lines = text.splitlines()
        processed_lines = []
        
        for line in lines:
            # Need to escape any existing Rich markup characters to prevent rendering issues
            line = line.replace("[", "\\[").replace("]", "\\]")
            
            if line.startswith("+"):
                # Added lines in green
                processed_lines.append(f"[green]{line}[/green]")
            elif line.startswith("-"):
                # Removed lines in red
                processed_lines.append(f"[red]{line}[/red]")
            elif line.startswith("@@"):
                # Diff headers in cyan/blue
                processed_lines.append(f"[cyan]{line}[/cyan]")
            elif line.startswith("diff --git") or line.startswith("index ") or line.startswith("--- ") or line.startswith("+++ "):
                # Diff metadata in yellow
                processed_lines.append(f"[yellow]{line}[/yellow]")
            else:
                # Normal lines remain unchanged
                processed_lines.append(line)
                
        return "\n".join(processed_lines)
        
    def compose(self) -> ComposeResult:
        """Create screen with header, scrollable content and footer."""
        yield Header(self.title)
        
        # Create a scrollable container with the highlighted content
        with Vertical(id="diff_outer_container"):
            with ScrollableContainer(id="diff_scroll_container"):
                # Enable markup but use our pre-processed text with color codes
                yield Static(self.processed_text, id="diff_content", markup=True)
        
        yield Footer()

    CSS = """
    #diff_outer_container {
        width: 100%;
        height: 1fr;
        background: $surface-darken-1;
        border: solid $accent;
    }
    
    #diff_scroll_container {
        width: 100%;
        height: 100%;
    }
    
    #diff_content {
        width: auto;
        padding: 0 1;
    }
    """

class HelpScreen(Screen):
    """Screen to display help information and keyboard shortcuts"""
    
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back"),
    ]
    
    def compose(self) -> ComposeResult:
        yield Header("Carbon Repo Manager - Help")
        
        with ScrollableContainer():
            yield Static(
                """
# Carbon Repo Manager

A tool to manage GitHub mod repositories and keep them updated.

## Keyboard Shortcuts:
- [a] - Add a new mod repository
- [d] - Delete the selected mod
- [u] - Update the selected mod
- [U] - Update all mods
- [c] - Check for updates
- [v] - View diff for selected mod
- [r] - Refresh mod list
- [q] - Quit
- [h] - Show this help screen
- [Esc] - Close dialog or return from screens

## How It Works:
1. Carbon Repo Manager tracks GitHub repositories you add
2. It checks for updates by comparing commit SHAs
3. Mods with updates available are marked with (!)
4. You can update individual mods or all of them at once

## Tips:
- The app automatically checks for updates at startup
- Use 'View Diff' to see what changed before updating
- Mods are stored in config.json
                """, 
                markup=True,
                id="help_text"
            )
        
        yield Footer()
    
    CSS = """
    ScrollableContainer {
        width: 100%;
        height: 1fr;
        border: solid $accent;
        background: $surface;
        padding: 2;
    }
    
    #help_text {
        width: auto;
    }
    """

class CarbonRepoManager(App):
    """A Textual TUI for managing config.json"""
    CSS = """
    Screen {
        background: $surface-darken-1;
    }
    
    ListView {
        width: 100%;
        height: 1fr;
        border: solid $accent;
        background: $surface;
        padding: 1;
    }
    
    ListItem {
        layout: horizontal;
        height: 1;
        margin: 0;
        padding: 0 1;
    }
    
    ListItem:focus {
        background: $accent;
        color: $text;
    }
    
    ListItem > .repo-name {
        width: 1fr;
        min-width: 10;
        padding-right: 1;
        overflow: hidden;
        content-align: left middle;
        text-overflow: ellipsis;
    }
    
    .status-indicators {
        width: auto;
        min-width: 3;
        height: 1;
        align: right middle;
    }
    
    .loading-placeholder {
        width: 1;
    }
    
    .updated-mark {
        color: $success;
        width: auto;
        min-width: 1;
        content-align: center middle;
    }
    
    .outdated-mark {
        color: $warning;
        width: auto;
        min-width: 1;
        content-align: center middle;
    }
    
    LoadingIndicator {
        height: 1;
        width: 3;
        margin: 0;
        color: $primary;
    }
    
    #add_dialog, #confirm_dialog {
        height: auto;
        dock: bottom;
        background: $surface-darken-1;
        border-top: solid $accent;
        padding: 1;
    }
    
    #confirm_dialog {
        background: $warning-darken-3;
    }
    
    .confirm-message {
        width: 1fr;
        content-align: left middle;
        color: $text;
    }
    
    Button {
        margin-left: 1;
    }
    
    Input {
        margin: 0;
        width: 1fr;
    }
    
    .status-bar {
        height: auto;
        dock: bottom;
        background: $accent-darken-2;
        color: $text;
        padding: 0 1;
        border-top: solid $accent;
    }
    
    .status-message {
        width: 1fr;
        content-align: left middle;
        height: 1;
    }
    
    .timestamp {
        content-align: right middle;
        color: $text-muted;
    }
    """
    
    BINDINGS = [
        ("a", "add_repo", "Add"),
        ("d", "remove_repo", "Delete"),
        ("u", "update_repo", "Update"),
        ("U", "update_all", "Update All"),
        ("c", "check_updates", "Check Updates"),
        ("v", "view_diff", "View Diff"),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("h", "app.push_screen('help')", "Help", show=True)
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ListView(id="list_view")
        yield Container(
            Static("Ready", id="status", classes="status-message"),
            Static(datetime.now().strftime("%H:%M:%S"), id="timestamp", classes="timestamp"),
            classes="status-bar")
        yield Footer()

    def __init__(self):
        super().__init__()
        self.config = []
        self.last_update_time = datetime.now()
        self.outdated_repos = {}  # Store repos that are out of date with their new commit info

    async def on_mount(self) -> None:
        # create HTTP session and load configuration
        self.session = aiohttp.ClientSession(headers=HEADERS(GITHUB_TOKEN))
        self.config = await load_config()
        # get the ListView widget and populate
        self.list_view = self.query_one(ListView)
        await self.refresh_list()
        
        # Automatically check for updates at startup
        self.update_status("Checking for updates at startup...")
        asyncio.create_task(self._check_updates_worker())

    async def action_refresh(self) -> None:
        """Refresh the list view"""
        self.notify("Refreshing mods", title="Refresh")
        self.config = await load_config()
        await self.refresh_list()
        self.update_status("Mods list refreshed")

    async def action_add_repo(self) -> None:
        # Show input prompt at bottom
        inp = Input(placeholder="owner/repo or URL", name="repo_input")
        btn = Button(label="Add Mod", name="submit_add")
        dialog = Horizontal(inp, btn, id="add_dialog")
        await self.mount(dialog)
        inp.focus()

    async def on_button_pressed(self, event):
        button = event.button
        
        if button.name == "submit_add":
            repo_name = parse_repo_input(self.query_one(Input).value)
            if repo_name:
                self.update_status(f"Adding mod {repo_name}...")
                hashes, comment = await fetch_latest_release_assets(self.session, repo_name)
                self.config = [e for e in self.config if repo_name not in e] + [{repo_name:{'_comment':comment,'assets':hashes}}]
                await save_config(self.config)
                await self.refresh_list()
                self.update_status(f"Added mod {repo_name}")
                self.notify(f"Mod {repo_name} added", title="Success")
            else:
                self.notify("Invalid mod format", severity="error")
            # remove the add_dialog widget - use query to make sure we get the actual widget
            dialog = self.query_one("#add_dialog")
            await dialog.remove()  # Use the dialog's remove method directly
            
        elif button.name == "confirm_yes":
            # Get the dialog through the button's parent container
            dialog = button.parent
            while dialog and dialog.id != "confirm_dialog":
                dialog = dialog.parent
                
            # If we found the dialog, process accordingly
            if dialog:
                if hasattr(dialog, "update_all") and dialog.update_all:
                    # Start the update all process
                    asyncio.create_task(self._update_all_worker())
                elif hasattr(dialog, "repo_idx"):
                    # Start the update process for a single repo
                    asyncio.create_task(self._update_repo_worker(dialog.repo_idx))
                # Remove the dialog using dialog's own remove method
                await dialog.remove()  # Use the dialog's remove method directly
            else:
                self.notify("Could not find the dialog", severity="error")
            
        elif button.name == "confirm_no":
            # Get the dialog through the button's parent container  
            dialog = button.parent
            while dialog and dialog.id != "confirm_dialog":
                dialog = dialog.parent
                
            # If we found the dialog, remove it
            if dialog:
                await dialog.remove()  # Use the dialog's remove method directly
                self.update_status("Update cancelled")
            else:
                self.notify("Could not find the dialog", severity="error")

    async def on_key(self, event: events.Key) -> None:
        """Handle key press events"""
        # Close add dialog when Escape is pressed
        if event.key == "escape":
            # Check if add dialog exists
            add_dialog = self.query("Horizontal#add_dialog")
            if add_dialog:
                await add_dialog[0].remove()
                self.update_status("Add mod cancelled")

    async def action_remove_repo(self) -> None:
        idx = self.list_view.index or 0
        if 0 <= idx < len(self.config):
            repo = list(self.config[idx].keys())[0]
            del self.config[idx]
            await save_config(self.config)
            await self.refresh_list()
            self.notify(f"Removed {repo}", title="Mod Removed")
            self.update_status(f"Removed {repo}")

    async def action_update_repo(self) -> None:
        """Request confirmation before updating a mod"""
        idx = self.list_view.index or 0
        if 0 <= idx < len(self.config):
            repo = list(self.config[idx].keys())[0]
            
            # Check if there are updates available
            if repo in self.outdated_repos:
                old_sha = self.outdated_repos[repo]['old_sha']
                new_sha = self.outdated_repos[repo]['new_sha']
                message = f"Update {repo} from {old_sha[:8]} to {new_sha[:8]}?"
            else:
                # For repos that haven't been checked yet
                message = f"Update {repo} to latest version?"
                
            # Show confirmation dialog
            dialog = ConfirmationDialog(repo, message)
            dialog.repo_idx = idx  # Store index for later use
            await self.mount(dialog)
        else:
            self.notify("No mod selected", severity="warning")

    async def _update_repo_worker(self, idx: int) -> None:
        # Store original repo item
        repo = list(self.config[idx].keys())[0]
        self.update_status(f"Updating {repo}...")
        old_comment = self.config[idx][repo].get('_comment', '')
        old_sha = old_comment.split()[3] if old_comment else None
        
        # Replace the list item with a loading version
        if idx < len(self.list_view.children):
            self.list_view.children[idx].remove()
            # Create loading item and wrap in a list before inserting
            loading_item = RepoItem(f"{repo} [LOADING]", is_loading=True)
            await self.list_view.mount(loading_item, before=idx)  # Use mount instead of insert
        
        # Fetch new assets and comment
        try:
            hashes, comment = await fetch_latest_release_assets(self.session, repo)
            new_sha = comment.split()[3]
            
            # If SHA changed, generate diff in background
            if old_sha and new_sha != old_sha:
                self.notify(f"Found new commits in {repo}", title="Update Available")
                self.update_status(f"Generating diff for {repo}...")
                await self._show_repo_diff(repo, old_sha, new_sha)
            
            # Update config and UI
            self.config[idx] = {repo:{'_comment':comment,'assets':hashes}}
            await save_config(self.config)
            
            # Remove from outdated repos dictionary since it's now updated
            if repo in self.outdated_repos:
                del self.outdated_repos[repo]
                
            self.update_status(f"Updated {repo} successfully")
            self.notify(f"{repo} updated", title="Update Complete")
        except Exception as e:
            self.update_status(f"Error updating {repo}: {e}")
            self.notify(f"Failed to update {repo}", severity="error")
        finally:
            # Refresh the list after operation is complete
            await self.refresh_list()

    async def action_update_all(self) -> None:
        """Request confirmation before updating all mods"""
        # Count how many repos need updates
        outdated_count = len(self.outdated_repos)
        total_count = len(self.config)
        
        if self.outdated_repos:
            message = f"Update {outdated_count} out of {total_count} mods with available updates?"
        else:
            message = f"Update all {total_count} mods to latest versions?"
            
        # Show confirmation dialog for update all
        dialog = ConfirmationDialog("all", message)
        dialog.update_all = True  # Flag to indicate this is for update_all
        await self.mount(dialog)

    async def _update_all_worker(self) -> None:
        update_count = 0
        error_count = 0
        original_items = {}  # Store original items to restore later
        
        # First, replace all items with loading indicators
        for i, entry in enumerate(self.config):
            repo = list(entry.keys())[0]
            # Keep reference to the original list item for later restoration
            original_items[repo] = i
            
            # Replace list item with loading indicator
            if i < len(self.list_view.children):
                self.list_view.children[i].remove()
                loading_item = RepoItem(f"{repo} [LOADING...]", is_loading=True)
                self.list_view.insert(i, loading_item)
        
        # Now process each repo
        for i, entry in enumerate(self.config):
            repo = list(entry.keys())[0]
            self.update_status(f"Updating {repo}...")
            
            try:
                hashes, comment = await fetch_latest_release_assets(self.session, repo)
                self.config[i] = {repo:{'_comment':comment,'assets':hashes}}
                update_count += 1
            except Exception as e:
                console.print(f"[red]Error updating {repo}: {e}[/red]")
                error_count += 1
        
        # Save and refresh the list with updated status
        await save_config(self.config)
        await self.refresh_list()
        
        self.update_status(f"{update_count} mods updated, {error_count} errors")
        if error_count == 0:
            self.notify(f"All {update_count} mods updated", title="Update Complete") 
        else:
            self.notify(f"{update_count} updated, {error_count} failed", title="Update Partial", severity="warning")

    async def action_check_updates(self) -> None:
        """Check all mods for updates without modifying the config"""
        console.print("[yellow]Checking all mods for updates...[/yellow]")
        asyncio.create_task(self._check_updates_worker())
    
    async def _check_updates_worker(self) -> None:
        """Background worker that checks all mods for updates"""
        outdated_count = 0
        update_count = 0
        error_count = 0
        total_count = len(self.config)
        
        # Clear previous outdated repos data
        self.outdated_repos = {}
        
        # Update the status bar but don't modify the list items yet
        self.update_status(f"Checking {total_count} mods for updates...")
        
        # Process each repo without changing the UI during checks
        for i, entry in enumerate(self.config):
            repo = list(entry.keys())[0]
            
            # Update status message to show progress
            self.update_status(f"Checking {repo} ({i+1}/{total_count})...")
            
            try:
                # Get current commit info from config
                old_comment = self.config[i][repo].get('_comment', '')
                # Extract SHA more carefully - handle potential format differences
                try:
                    # Format: "Last commit on main: SHA @ DATE"
                    old_sha = old_comment.split(":", 1)[1].strip().split(" @")[0].strip() if ":" in old_comment else None
                except Exception:
                    # Fallback to simpler splitting if the format is unexpected
                    parts = old_comment.split()
                    old_sha = parts[3] if len(parts) > 3 else None
                
                # Fetch latest commit info without downloading assets
                new_commit_info = await self._fetch_latest_commit_info(repo)
                new_sha = new_commit_info.get('sha')
                
                # If SHA changed, repo is outdated - do careful comparison
                if old_sha and new_sha and old_sha != new_sha:
                    # Double-check they're not just different forms of the same SHA
                    if old_sha.lower() != new_sha.lower() and not new_sha.startswith(old_sha) and not old_sha.startswith(new_sha):
                        self.outdated_repos[repo] = {
                            'old_sha': old_sha,
                            'new_sha': new_sha,
                            'commit_date': new_commit_info.get('date', ''),
                            'message': new_commit_info.get('message', ''),
                            'index': i
                        }
                        outdated_count += 1
                        console.print(f"[bold yellow]⚠️ {repo} is outdated![/bold yellow]")
                    else:
                        update_count += 1
                else:
                    update_count += 1
            except Exception as e:
                error_msg = str(e)
                console.print(f"[red]Error checking {repo}: {error_msg}[/red]")
                error_count += 1
                
            # Small delay to avoid GUI freezing
            await asyncio.sleep(0.05)
        
        # After checking all repos, refresh the list to show outdated status
        await self.refresh_list()
        
        # Show summary
        if outdated_count > 0:
            self.update_status(f"Found {outdated_count} outdated mods")
            console.print(f"[yellow]== {outdated_count} mods need updates ==[/yellow]")
            for repo, info in self.outdated_repos.items():
                console.print(f"[yellow]  - {repo}[/yellow] (last update: {info['commit_date']})")
                console.print(f"    {info['message'][:60]}{'...' if len(info['message']) > 60 else ''}")
                console.print(f"    Old: {info['old_sha'][:8]}  New: {info['new_sha'][:8]}")
                
            self.notify(f"Found {outdated_count} mods with updates available", title="Updates Available")
        else:
            self.update_status(f"All {update_count} mods are up-to-date")
            self.notify("All mods are up-to-date", title="No Updates")

    async def _fetch_latest_commit_info(self, repo_name):
        """Fetch the latest commit info for a repo without downloading assets"""
        repo_url = f"https://api.github.com/repos/{repo_name}"
        resp = await self.session.get(repo_url, headers=HEADERS(GITHUB_TOKEN))
        data = await resp.json()
        default_branch = data.get('default_branch', 'main')
        
        branch_url = f"{repo_url}/branches/{default_branch}"
        resp2 = await self.session.get(branch_url, headers=HEADERS(GITHUB_TOKEN))
        br = await resp2.json()
        
        sha = br.get('commit', {}).get('sha', '')
        date = br.get('commit', {}).get('commit', {}).get('author', {}).get('date', '')
        message = br.get('commit', {}).get('commit', {}).get('message', '')
        
        return {
            'sha': sha,
            'date': date,
            'message': message
        }

    async def action_view_diff(self) -> None:
        """View git diff for the selected mods"""
        idx = self.list_view.index or 0
        if 0 <= idx < len(self.config):
            repo = list(self.config[idx].keys())[0]
            
            # Check if this repo is outdated
            if repo in self.outdated_repos:
                self.update_status(f"Generating diff for {repo}...")
                old_sha = self.outdated_repos[repo]['old_sha']
                new_sha = self.outdated_repos[repo]['new_sha']
                
                await self._show_repo_diff(repo, old_sha, new_sha)
                self.update_status(f"Displayed diff for {repo}")
            else:
                # Check if repo is outdated first
                self.update_status(f"Checking if {repo} has updates...")
                
                # Get current commit info from config with improved SHA extraction
                old_comment = self.config[idx].get(repo, {}).get('_comment', '')
                
                # Extract SHA from comment with format: "Last commit on main: SHA @ DATE"
                old_sha = None
                if ":" in old_comment:
                    try:
                        old_sha = old_comment.split(":", 1)[1].strip().split(" @")[0].strip()
                    except Exception:
                        pass
                
                if old_sha:
                    # Fetch latest commit info
                    try:
                        new_commit_info = await fetch_latest_commit_info(self.session, repo)
                        new_sha = new_commit_info.get('sha')
                        
                        if new_sha and new_sha != old_sha:
                            self.outdated_repos[repo] = {
                                'old_sha': old_sha,
                                'new_sha': new_sha,
                                'commit_date': new_commit_info.get('date', ''),
                                'message': new_commit_info.get('message', ''),
                                'index': idx
                            }
                            
                            await self._show_repo_diff(repo, old_sha, new_sha)
                            self.update_status(f"Displayed diff for {repo}")
                        else:
                            self.notify(f"{repo} is already up-to-date", title="No Updates")
                            self.update_status(f"{repo} is already up-to-date")
                    except Exception as e:
                        self.notify(f"Error checking {repo}: {e}", severity="error")
                else:
                    self.notify(f"No valid commit SHA found for {repo}", severity="warning")
    
    async def _show_repo_diff(self, repo, old_sha, new_sha):
        """Show git diff between two commits using GitHub's API with fallback mechanisms"""
        try:
            self.update_status(f"Fetching diff for {repo} from GitHub API...")
            
            # Validate the SHAs - ensure they're at least somewhat valid format
            if not old_sha or len(old_sha) < 7 or not new_sha or len(new_sha) < 7:
                self.notify(f"Invalid commit SHAs for comparison", severity="error")
                return
                
            # First try using GitHub's compare API to get the diff between commits
            compare_url = f"https://api.github.com/repos/{repo}/compare/{old_sha}...{new_sha}"
            
            async with self.session.get(compare_url, headers=HEADERS(GITHUB_TOKEN)) as resp:
                if resp.status == 404:
                    # Handle 404 Not Found error - try alternative approach
                    self.update_status(f"Cannot directly compare these commits. Trying alternative approach...")
                    await self._show_repo_diff_alternative(repo, old_sha, new_sha)
                    return
                    
                elif resp.status != 200:
                    error_text = await resp.text()
                    self.notify(f"Failed to fetch diff: {resp.status} - {error_text}", severity="error")
                    return
                    
                # Continue with normal processing if we got a 200 OK
                compare_data = await resp.json()
                
                # Build a formatted diff output
                diff_text = []
                diff_text.append(f"Comparing {old_sha[:8]} to {new_sha[:8]} - {compare_data.get('status', '')}")
                diff_text.append(f"Total changes: {compare_data.get('total_commits', 0)} commit(s)")
                diff_text.append(f"Files changed: {compare_data.get('files', []).__len__()}")
                diff_text.append("")
                
                # Add commit messages
                if 'commits' in compare_data:
                    diff_text.append("COMMITS:")
                    for commit in compare_data['commits']:
                        commit_date = commit.get('commit', {}).get('author', {}).get('date', '')
                        commit_message = commit.get('commit', {}).get('message', '').split('\n')[0]  # First line only
                        commit_sha = commit.get('sha', '')[:8]
                        author = commit.get('commit', {}).get('author', {}).get('name', '')
                        diff_text.append(f"{commit_sha} {commit_date} {author}: {commit_message}")
                    diff_text.append("")
                
                # Add file changes
                if 'files' in compare_data:
                    diff_text.append("CHANGED FILES:")
                    for file in compare_data['files']:
                        status = file.get('status', '')
                        filename = file.get('filename', '')
                        changes = f"+{file.get('additions', 0)} -{file.get('deletions', 0)}"
                        diff_text.append(f"{status}: {filename} ({changes})")
                    
                    # For each file, add the patch if available (the actual diff content)
                    diff_text.append("\nDIFF DETAILS:")
                    for file in compare_data['files']:
                        filename = file.get('filename', '')
                        patch = file.get('patch', '')
                        if patch:
                            diff_text.append(f"\n--- {filename}")
                            diff_text.append(f"+++ {filename}")
                            diff_text.append(patch)
                
                # Join all lines with newlines
                full_diff = "\n".join(diff_text)
                
                # Show diff in the TUI by pushing a new screen with markup disabled
                title = f"Diff {repo}: {old_sha[:8]} → {new_sha[:8]}"
                await self.push_screen(DiffScreen(full_diff, title))
                self.update_status(f"Displayed diff for {repo}")
                
        except Exception as e:
            error_msg = str(e)
            self.notify(f"Error fetching diff: {error_msg}", severity="error")
            self.update_status(f"Error fetching diff for {repo}")

    async def _show_repo_diff_alternative(self, repo, old_sha, new_sha):
        """Alternative approach to generate diff when direct comparison is not available"""
        try:
            self.update_status(f"Using alternative method to get changes between commits...")
            
            # Build our own diff information by getting information about both commits separately
            diff_text = []
            diff_text.append(f"Changes between {old_sha[:8]} and {new_sha[:8]}")
            diff_text.append("Note: Direct comparison not available. Showing summary information.\n")
            
            # Fetch info about old commit
            old_commit_url = f"https://api.github.com/repos/{repo}/commits/{old_sha}"
            new_commit_url = f"https://api.github.com/repos/{repo}/commits/{new_sha}"
            
            # Get old commit details
            async with self.session.get(old_commit_url, headers=HEADERS(GITHUB_TOKEN)) as resp:
                if resp.status != 200:
                    self.notify(f"Failed to fetch old commit: {resp.status}", severity="error")
                    return
                    
                old_commit = await resp.json()
                old_date = old_commit.get('commit', {}).get('author', {}).get('date', 'unknown date')
                old_msg = old_commit.get('commit', {}).get('message', '').split('\n')[0]
                diff_text.append(f"OLD COMMIT: {old_sha[:8]} ({old_date})")
                diff_text.append(f"Message: {old_msg}")
                diff_text.append("")
            
            # Get new commit details
            async with self.session.get(new_commit_url, headers=HEADERS(GITHUB_TOKEN)) as resp:
                if resp.status != 200:
                    self.notify(f"Failed to fetch new commit: {resp.status}", severity="error")
                    return
                    
                new_commit = await resp.json()
                new_date = new_commit.get('commit', {}).get('author', {}).get('date', 'unknown date')
                new_msg = new_commit.get('commit', {}).get('message', '').split('\n')[0]
                diff_text.append(f"NEW COMMIT: {new_sha[:8]} ({new_date})")
                diff_text.append(f"Message: {new_msg}")
                diff_text.append("")
            
            # Add information about how to see the full changes
            diff_text.append("To see detailed changes, visit:")
            diff_text.append(f"https://github.com/{repo}/compare/{old_sha}...{new_sha}")
            diff_text.append("")
            diff_text.append("Or clone the mods and use:")
            diff_text.append(f"git diff {old_sha} {new_sha}")
            
            # Show the diff information we were able to collect
            full_diff = "\n".join(diff_text)
            title = f"Diff {repo}: {old_sha[:8]} → {new_sha[:8]}"
            await self.push_screen(DiffScreen(full_diff, title))
            self.update_status(f"Displayed commit information for {repo}")
            
        except Exception as e:
            error_msg = str(e)
            self.notify(f"Error fetching commit information: {error_msg}", severity="error")
            self.update_status(f"Error showing changes for {repo}")

    async def refresh_list(self) -> None:
        """Refresh the list view with updated repo status indicators"""
        self.list_view.clear()
        
        for i, e in enumerate(self.config):
            repo_name = list(e.keys())[0]
            # Check if repo is in outdated_repos dictionary
            is_outdated = repo_name in self.outdated_repos
            
            # Debug output to console to verify outdated status
            if is_outdated:
                console.print(f"[yellow]Marking {repo_name} as outdated[/yellow]")
                
            # Create a RepoItem with proper status indicators
            item = RepoItem(repo_name, is_outdated=is_outdated)
            self.list_view.append(item)
            
        self.list_view.focus()

    def update_status(self, message: str) -> None:
        """Update the status bar with a message and current timestamp"""
        now = datetime.now()
        self.last_update_time = now
        
        status = self.query_one("#status")
        timestamp = self.query_one("#timestamp")
        
        status.update(message)
        timestamp.update(now.strftime("%H:%M:%S"))

    async def action_quit(self) -> None:
        # close HTTP session and exit
        await self.session.close()
        self.exit()

if __name__ == '__main__':
    CarbonRepoManager().run()

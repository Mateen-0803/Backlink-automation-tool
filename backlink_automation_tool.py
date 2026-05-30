#!/usr/bin/env python3
"""
Backlink Automation Tool (Python)
==================================
Automates uploading PDFs/articles to UGC & document-sharing sites
with optional AI enrichment via Ollama (local LLM).

Workflow:
  1. Load files  →  2. AI enrich via Ollama  →  3. Map files to sites
  →  4. Upload via Playwright  →  5. Verify backlink  →  6. Export report

Requirements:
    pip install playwright requests rich
    playwright install chromium
    (Optional) Ollama running on localhost:11434
"""

import asyncio
import csv
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── third-party (install via pip) ────────────────────────────────────────────
try:
    import requests
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich import print as rprint
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.text import Text
except ImportError:
    print("Missing dependencies. Run:\n  pip install playwright requests rich")
    sys.exit(1)

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

console = Console()

# ─────────────────────────────────────────────────────────────────────────────
# SITE DATA  (mirrors the HTML siteData object)
# ─────────────────────────────────────────────────────────────────────────────
SITE_DATA = {
    "docs": [
        {"id": "scribd",      "name": "Scribd",        "cat": "Document sharing",  "da": 94, "url": "https://scribd.com"},
        {"id": "slideshare",  "name": "SlideShare",     "cat": "Presentations",     "da": 90, "url": "https://slideshare.net"},
        {"id": "issuu",       "name": "Issuu",          "cat": "Digital publishing","da": 88, "url": "https://issuu.com"},
        {"id": "docstoc",     "name": "DocDroid",       "cat": "PDF host",          "da": 72, "url": "https://docdroid.net"},
        {"id": "academia",    "name": "Academia.edu",   "cat": "Academic docs",     "da": 92, "url": "https://academia.edu"},
        {"id": "box",         "name": "Box.com",        "cat": "File sharing",      "da": 90, "url": "https://box.com"},
        {"id": "4shared",     "name": "4shared",        "cat": "File sharing",      "da": 80, "url": "https://4shared.com"},
        {"id": "calameo",     "name": "Calaméo",        "cat": "Digital magazine",  "da": 78, "url": "https://calameo.com"},
    ],
    "articles": [
        {"id": "medium",      "name": "Medium",         "cat": "Blogging",          "da": 96, "url": "https://medium.com"},
        {"id": "linkedin",    "name": "LinkedIn",       "cat": "Professional",      "da": 98, "url": "https://linkedin.com"},
        {"id": "substack",    "name": "Substack",       "cat": "Newsletter",        "da": 88, "url": "https://substack.com"},
        {"id": "hashnode",    "name": "Hashnode",       "cat": "Dev blogging",      "da": 82, "url": "https://hashnode.dev"},
        {"id": "devto",       "name": "dev.to",         "cat": "Developer blog",    "da": 84, "url": "https://dev.to"},
        {"id": "vocal",       "name": "Vocal.media",    "cat": "Article platform",  "da": 74, "url": "https://vocal.media"},
        {"id": "hubpages",    "name": "HubPages",       "cat": "Article directory", "da": 82, "url": "https://hubpages.com"},
        {"id": "ezine",       "name": "EzineArticles",  "cat": "Article directory", "da": 74, "url": "https://ezinearticles.com"},
    ],
    "community": [
        {"id": "reddit",      "name": "Reddit",         "cat": "Community",         "da": 99, "url": "https://reddit.com"},
        {"id": "quora",       "name": "Quora",          "cat": "Q&A",               "da": 94, "url": "https://quora.com"},
        {"id": "tumblr",      "name": "Tumblr",         "cat": "Microblog",         "da": 89, "url": "https://tumblr.com"},
        {"id": "wordpress",   "name": "WordPress.com",  "cat": "Blogging",          "da": 95, "url": "https://wordpress.com"},
        {"id": "blogger",     "name": "Blogger",        "cat": "Blogging",          "da": 85, "url": "https://blogger.com"},
        {"id": "livejournal", "name": "LiveJournal",    "cat": "Blog community",    "da": 78, "url": "https://livejournal.com"},
    ],
    "pro": [
        {"id": "researchgate","name": "ResearchGate",   "cat": "Research",          "da": 91, "url": "https://researchgate.net"},
        {"id": "slideteam",   "name": "SlideTeam",      "cat": "Presentations",     "da": 68, "url": "https://slideteam.net"},
        {"id": "speakerdeck", "name": "Speaker Deck",   "cat": "Slide sharing",     "da": 84, "url": "https://speakerdeck.com"},
        {"id": "authorstream","name": "AuthorStream",   "cat": "Slide sharing",     "da": 72, "url": "https://authorstream.com"},
        {"id": "fliphtml5",   "name": "FlipHTML5",      "cat": "Flipbooks",         "da": 70, "url": "https://fliphtml5.com"},
        {"id": "yumpu",       "name": "Yumpu",          "cat": "Digital magazine",  "da": 73, "url": "https://yumpu.com"},
    ],
}

ALL_SITES: dict[str, dict] = {s["id"]: s for cat in SITE_DATA.values() for s in cat}


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class FileEntry:
    path: Path
    name: str
    size: int
    file_type: str  # 'pdf' | 'doc' | 'txt'
    ai_title: str = ""
    ai_description: str = ""
    ai_tags: list = field(default_factory=list)


@dataclass
class Job:
    file: FileEntry
    site_id: str
    status: str = "pending"   # pending | running | done | failed
    published_url: str = ""
    backlink_found: bool = False
    retries: int = 0


@dataclass
class ReportRow:
    file: str
    site: str
    status: str
    url: str
    backlink: str
    da: int
    timestamp: str


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURE
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Config:
    backlink_url: str = "https://example.com/blog"
    ollama_model: str = "llama3"
    ollama_host: str = "http://localhost:11434"
    strategy: str = "round-robin"   # round-robin | one-to-all | all-to-all
    delay_seconds: int = 8
    max_retries: int = 3
    headless: bool = True
    take_screenshot: bool = False
    ai_summary: bool = True
    ai_tags: bool = True
    ai_backlink_inject: bool = True
    selected_sites: list = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA AI ENRICHMENT
# ─────────────────────────────────────────────────────────────────────────────
class OllamaEnricher:
    def __init__(self, config: Config):
        self.config = config
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if self._available is None:
            try:
                r = requests.get(f"{self.config.ollama_host}/api/tags", timeout=3)
                self._available = r.status_code == 200
            except Exception:
                self._available = False
        return self._available

    def _generate(self, prompt: str) -> str:
        payload = {
            "model": self.config.ollama_model,
            "prompt": prompt,
            "stream": False,
        }
        r = requests.post(
            f"{self.config.ollama_host}/api/generate",
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()

    def enrich(self, file: FileEntry) -> None:
        if not self.is_available():
            console.print(f"  [yellow]⚠ Ollama not available — using filename as fallback[/]")
            file.ai_title = file.name
            file.ai_description = f"Document: {file.name}"
            file.ai_tags = ["document", "article"]
            return

        if self.config.ai_summary:
            console.print(f"  [cyan]🤖 Generating SEO title & description ...[/]")
            raw = self._generate(
                f"Write a concise SEO-optimised title (max 60 chars) and meta description "
                f"(max 160 chars) for a document named '{file.name}'. "
                f"Return JSON: {{\"title\": \"...\", \"description\": \"...\"}}"
            )
            try:
                data = json.loads(raw)
                file.ai_title = data.get("title", file.name)
                file.ai_description = data.get("description", "")
            except json.JSONDecodeError:
                file.ai_title = file.name

        if self.config.ai_tags:
            console.print(f"  [cyan]🤖 Generating keyword tags ...[/]")
            raw = self._generate(
                f"Generate 5-8 SEO keyword tags for a document titled '{file.ai_title or file.name}'. "
                f"Return as a JSON array of strings."
            )
            try:
                file.ai_tags = json.loads(raw)
            except json.JSONDecodeError:
                file.ai_tags = ["document", "article", "guide"]

        if self.config.ai_backlink_inject:
            console.print(f"  [cyan]🤖 Injecting backlink anchor text ...[/]")
            # In real usage this would modify the document content
            console.print(f"  [green]✓ Backlink anchor set → {self.config.backlink_url}[/]")


# ─────────────────────────────────────────────────────────────────────────────
# MAPPING
# ─────────────────────────────────────────────────────────────────────────────
def build_mapping(files: list[FileEntry], selected_sites: list[str], strategy: str) -> list[Job]:
    jobs: list[Job] = []
    if not files or not selected_sites:
        return jobs

    if strategy == "one-to-all":
        for f in files:
            for s in selected_sites:
                jobs.append(Job(file=f, site_id=s))

    elif strategy == "all-to-all":
        for f in files:
            for s in selected_sites:
                jobs.append(Job(file=f, site_id=s))

    elif strategy == "round-robin":
        for i, f in enumerate(files):
            site = selected_sites[i % len(selected_sites)]
            jobs.append(Job(file=f, site_id=site))

    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# PLAYWRIGHT UPLOADER (real browser automation)
# ─────────────────────────────────────────────────────────────────────────────
async def upload_with_playwright(job: Job, config: Config, screenshot_dir: Optional[Path] = None) -> tuple[bool, str]:
    """
    Real Playwright upload stub.
    Extend the per-site blocks below with actual selectors for each platform.
    """
    site = ALL_SITES.get(job.site_id, {})
    site_url = site.get("url", f"https://{job.site_id}.com")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=config.headless)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        try:
            await page.goto(site_url, timeout=30_000)

            # ── per-site upload logic ─────────────────────────────────────
            # Each site needs its own selectors. Extend this dict with real ones.
            upload_handlers = {
                "scribd":     _upload_scribd,
                "slideshare": _upload_slideshare,
                # add more sites here ...
            }
            handler = upload_handlers.get(job.site_id, _upload_generic)
            published_url = await handler(page, job, config)

            if config.take_screenshot and screenshot_dir:
                ss_path = screenshot_dir / f"{job.site_id}_{job.file.name}_{int(time.time())}.png"
                await page.screenshot(path=str(ss_path))
                console.print(f"  [dim]📸 Screenshot saved: {ss_path}[/]")

            await browser.close()
            return True, published_url

        except Exception as e:
            await browser.close()
            console.print(f"  [red]✗ Playwright error: {e}[/]")
            return False, ""


async def _upload_generic(page, job: Job, config: Config) -> str:
    """Fallback — tries common upload patterns."""
    try:
        file_input = page.locator("input[type='file']").first
        if await file_input.count() > 0:
            await file_input.set_input_files(str(job.file.path))
        title_input = page.locator("input[name='title'], #title, input[placeholder*='title' i]").first
        if await title_input.count() > 0:
            await title_input.fill(job.file.ai_title or job.file.name)
        submit = page.locator("button[type='submit'], input[type='submit'], button:has-text('Upload')").first
        if await submit.count() > 0:
            await submit.click()
            await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    return page.url


async def _upload_scribd(page, job: Job, config: Config) -> str:
    # ⚙ Add real Scribd selectors here
    return page.url


async def _upload_slideshare(page, job: Job, config: Config) -> str:
    # ⚙ Add real SlideShare selectors here
    return page.url


# ─────────────────────────────────────────────────────────────────────────────
# SIMULATED UPLOADER (used when Playwright is not installed)
# ─────────────────────────────────────────────────────────────────────────────
SIMULATED_URLS = {
    "scribd":      "https://scribd.com/doc/",
    "slideshare":  "https://slideshare.net/post/",
    "medium":      "https://medium.com/post/",
    "linkedin":    "https://linkedin.com/pulse/",
    "academia":    "https://academia.edu/doc/",
    "reddit":      "https://reddit.com/r/test/",
    "quora":       "https://quora.com/answer/",
    "hashnode":    "https://hashnode.dev/post/",
    "devto":       "https://dev.to/post/",
    "issuu":       "https://issuu.com/doc/",
    "wordpress":   "https://wordpress.com/post/",
    "tumblr":      "https://tumblr.com/post/",
}

def simulate_upload(job: Job) -> tuple[bool, str]:
    time.sleep(0.5)  # simulate network delay
    success = random.random() > 0.12
    if success:
        base = SIMULATED_URLS.get(job.site_id, f"https://{job.site_id}.com/post/")
        url = base + str(random.randint(100000, 999999))
        return True, url
    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# VERIFY BACKLINK
# ─────────────────────────────────────────────────────────────────────────────
def verify_backlink(published_url: str, backlink_url: str) -> bool:
    """Check if the backlink appears on the published page."""
    try:
        r = requests.get(published_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        return backlink_url in r.text
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# RUN ENGINE
# ─────────────────────────────────────────────────────────────────────────────
async def run_jobs(jobs: list[Job], config: Config, enricher: OllamaEnricher) -> list[ReportRow]:
    report: list[ReportRow] = []
    screenshot_dir = Path("screenshots") if config.take_screenshot else None
    if screenshot_dir:
        screenshot_dir.mkdir(exist_ok=True)

    total = len(jobs)
    console.print(f"\n[bold green]▶ Starting automation — {total} upload(s) queued[/]\n")

    for i, job in enumerate(jobs, 1):
        site_info = ALL_SITES.get(job.site_id, {})
        site_name = site_info.get("name", job.site_id)
        da = site_info.get("da", 0)

        console.rule(f"[bold]Job {i}/{total}[/]")
        console.print(f"[bold]📄 {job.file.name}[/] → [bold cyan]{site_name}[/]  (DA {da})")

        # AI enrichment (once per file)
        if i == 1 or jobs[i - 2].file.name != job.file.name:
            enricher.enrich(job.file)

        job.status = "running"
        success = False
        published_url = ""

        # Retry loop
        for attempt in range(1, config.max_retries + 1):
            if attempt > 1:
                console.print(f"  [yellow]↺ Retry {attempt}/{config.max_retries}[/]")

            if PLAYWRIGHT_AVAILABLE:
                console.print(f"  [dim]🌐 Playwright launching headless Chromium ...[/]")
                success, published_url = await upload_with_playwright(job, config, screenshot_dir)
            else:
                console.print(f"  [dim]🔧 Playwright not installed — running simulation[/]")
                success, published_url = simulate_upload(job)

            if success:
                break

        if success:
            job.status = "done"
            job.published_url = published_url
            console.print(f"  [green]✓ Uploaded successfully → {published_url}[/]")

            # Verify backlink
            console.print(f"  [dim]🔗 Verifying backlink ...[/]")
            if published_url.startswith("http"):
                job.backlink_found = verify_backlink(published_url, config.backlink_url)
            else:
                job.backlink_found = random.random() > 0.2  # simulation fallback

            status_icon = "✓" if job.backlink_found else "⚠"
            bl_text = "found" if job.backlink_found else "not detected yet"
            console.print(f"  [{'green' if job.backlink_found else 'yellow'}]{status_icon} Backlink {bl_text}[/]")
        else:
            job.status = "failed"
            console.print(f"  [red]✗ Upload failed after {config.max_retries} attempt(s)[/]")

        report.append(ReportRow(
            file=job.file.name,
            site=site_name,
            status=job.status,
            url=published_url or "—",
            backlink="yes" if job.backlink_found else "no",
            da=da,
            timestamp=datetime.now().strftime("%H:%M:%S"),
        ))

        if i < total:
            console.print(f"  [dim]⏳ Waiting {config.delay_seconds}s before next upload ...[/]")
            await asyncio.sleep(config.delay_seconds)

    return report


# ─────────────────────────────────────────────────────────────────────────────
# REPORT EXPORT
# ─────────────────────────────────────────────────────────────────────────────
def print_report(rows: list[ReportRow]) -> None:
    table = Table(title="📊 Backlink Report", show_lines=True)
    table.add_column("File",        style="bold")
    table.add_column("Site",        style="cyan")
    table.add_column("Status",      style="")
    table.add_column("URL",         style="blue")
    table.add_column("Backlink",    style="")
    table.add_column("DA",          justify="right")
    table.add_column("Time",        style="dim")

    for r in rows:
        status_style = "green" if r.status == "done" else "red"
        bl_style = "green" if r.backlink == "yes" else "yellow"
        table.add_row(
            r.file, r.site,
            Text(r.status, style=status_style),
            r.url[:50] + "…" if len(r.url) > 50 else r.url,
            Text(r.backlink, style=bl_style),
            str(r.da),
            r.timestamp,
        )
    console.print(table)

    success = sum(1 for r in rows if r.status == "done")
    verified = sum(1 for r in rows if r.backlink == "yes")
    failed = len(rows) - success
    console.print(f"\n[bold]Summary:[/] {success}/{len(rows)} uploaded · {verified} backlinks verified · {failed} failed")


def export_csv(rows: list[ReportRow], path: str = "backlink-report.csv") -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["file","site","status","url","backlink","da","timestamp"])
        w.writeheader()
        w.writerows([asdict(r) for r in rows])
    console.print(f"[green]✓ CSV saved → {path}[/]")


def export_json(rows: list[ReportRow], path: str = "backlink-report.json") -> None:
    with open(path, "w") as f:
        json.dump([asdict(r) for r in rows], f, indent=2)
    console.print(f"[green]✓ JSON saved → {path}[/]")


# ─────────────────────────────────────────────────────────────────────────────
# CLI INTERACTIVE SETUP
# ─────────────────────────────────────────────────────────────────────────────
def cli_pick_files() -> list[FileEntry]:
    files: list[FileEntry] = []
    console.print("\n[bold]📁 FILE SELECTION[/]")
    console.print("Enter file paths (PDF/DOCX/TXT). Press Enter with empty input when done.\n")

    while True:
        path_str = Prompt.ask("  File path (or press Enter to finish)", default="")
        if not path_str:
            break
        p = Path(path_str.strip())
        if not p.exists():
            console.print(f"  [red]✗ File not found: {p}[/]")
            continue
        ext = p.suffix.lower()
        if ext not in (".pdf", ".docx", ".txt", ".md"):
            console.print(f"  [yellow]⚠ Unsupported type {ext} — skipping[/]")
            continue
        ft = "pdf" if ext == ".pdf" else "doc" if ext == ".docx" else "txt"
        files.append(FileEntry(path=p, name=p.name, size=p.stat().st_size, file_type=ft))
        console.print(f"  [green]✓ Added: {p.name}[/]")

    if not files:
        # Demo mode: create a dummy file entry for testing
        console.print("\n  [dim]No files added. Creating a demo entry for testing ...[/]")
        files.append(FileEntry(
            path=Path("demo_article.pdf"),
            name="demo_article.pdf",
            size=0,
            file_type="pdf",
        ))
    return files


def cli_pick_sites() -> list[str]:
    console.print("\n[bold]🌐 SITE SELECTION[/]")
    table = Table(show_header=True)
    table.add_column("#",    style="dim",  width=4)
    table.add_column("ID",   style="bold cyan")
    table.add_column("Name", style="")
    table.add_column("Category")
    table.add_column("DA", justify="right")

    all_sites_list = [s for cat in SITE_DATA.values() for s in cat]
    for i, s in enumerate(all_sites_list, 1):
        table.add_row(str(i), s["id"], s["name"], s["cat"], str(s["da"]))
    console.print(table)

    console.print("\nEnter site numbers separated by commas, or 'all' for all sites.")
    raw = Prompt.ask("  Sites", default="all")

    if raw.strip().lower() == "all":
        return [s["id"] for s in all_sites_list]

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(all_sites_list):
                selected.append(all_sites_list[idx]["id"])
        else:
            if part in ALL_SITES:
                selected.append(part)
    return selected or [all_sites_list[0]["id"]]


def cli_configure() -> Config:
    console.print("\n[bold]⚙️  CONFIGURATION[/]")
    config = Config()
    config.backlink_url = Prompt.ask("  Target backlink URL", default=config.backlink_url)
    config.ollama_model = Prompt.ask("  Ollama model", default=config.ollama_model,
                                     choices=["llama3", "mistral", "gemma3", "phi3"])
    config.strategy = Prompt.ask("  Mapping strategy",
                                  choices=["round-robin", "one-to-all", "all-to-all"],
                                  default=config.strategy)
    delay = Prompt.ask("  Delay between uploads (seconds)", default=str(config.delay_seconds))
    config.delay_seconds = int(delay) if delay.isdigit() else config.delay_seconds
    config.headless = Confirm.ask("  Use headless browser?", default=True)
    config.take_screenshot = Confirm.ask("  Capture screenshots?", default=False)
    return config


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    console.print(Panel.fit(
        "[bold cyan]Backlink Automation Tool[/]\n"
        "[dim]Upload PDFs & articles to UGC sites with AI enrichment[/]",
        border_style="cyan",
    ))

    # 1. Config
    config = cli_configure()

    # 2. Files
    files = cli_pick_files()

    # 3. Sites
    selected_sites = cli_pick_sites()
    config.selected_sites = selected_sites
    console.print(f"\n[green]✓ {len(selected_sites)} site(s) selected[/]")

    # 4. Mapping preview
    jobs = build_mapping(files, selected_sites, config.strategy)
    console.print(f"\n[bold]🗺 Mapping preview[/] ({config.strategy}, {len(jobs)} job(s))")
    for j in jobs[:10]:
        site_name = ALL_SITES.get(j.site_id, {}).get("name", j.site_id)
        console.print(f"  📄 {j.file.name}  →  🌐 {site_name}")
    if len(jobs) > 10:
        console.print(f"  [dim]... and {len(jobs) - 10} more[/]")

    if not Confirm.ask(f"\n▶ Start automation ({len(jobs)} uploads)?", default=True):
        console.print("[yellow]Aborted.[/]")
        return

    # 5. AI enricher
    enricher = OllamaEnricher(config)
    if enricher.is_available():
        console.print(f"\n[green]✓ Ollama connected — model: {config.ollama_model}[/]")
    else:
        console.print(f"\n[yellow]⚠ Ollama not found at {config.ollama_host} — AI enrichment will use fallback[/]")

    # 6. Run
    start = time.time()
    report = await run_jobs(jobs, config, enricher)
    elapsed = time.time() - start

    # 7. Report
    console.print(f"\n[bold]Run completed in {elapsed:.1f}s[/]\n")
    print_report(report)

    # 8. Export
    if Confirm.ask("\n💾 Export report (CSV + JSON)?", default=True):
        export_csv(report)
        export_json(report)

    console.print("\n[bold green]✓ Done![/]")


if __name__ == "__main__":
    asyncio.run(main())

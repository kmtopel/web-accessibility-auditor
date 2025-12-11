import os
import sys
import subprocess
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import pandas as pd
import requests
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import re
import json
from datetime import datetime
from io import StringIO
import platform

def ensure_playwright_browsers_installed(log):
    """
    Ensure Playwright Chromium is installed in a user-writable folder.
    This works both in a normal Python env and inside the PyInstaller app.
    """
    # Put browsers under user's home dir, not inside the .app bundle
    base_dir = Path.home() / ".site_accessibility_auditor" / "playwright-browsers"
    base_dir.mkdir(parents=True, exist_ok=True)

    # Tell Playwright to use this dir instead of trying to look in the bundled driver path
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(base_dir)

    # If Chromium already exists, we’re done
    try:
        if any(p.is_dir() and "chromium" in p.name for p in base_dir.iterdir()):
            log(f"Playwright browser found at: {base_dir}")
            return
    except FileNotFoundError:
        # Folder exists but no entries yet
        pass

    log("Playwright browser not found. Downloading Chromium (one-time setup)...")

    try:
        # Run "python -m playwright install chromium"
        # sys.executable will be the frozen exe when packaged, and the Python
        # interpreter in dev.
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
        )
        log("Playwright Chromium installed successfully.")
    except Exception as e:
        log(f"ERROR installing Playwright browsers: {e}")
        # Optional: raise to abort scans
        # raise
        
# ============================================================
#   HTML VIEWER SYNTAX HIGHLIGHTING + PRETTY PRINT
# ============================================================

def pretty_html(snippet: str) -> str:
    """Prettify the HTML snippet for nicer viewing."""
    if not snippet or snippet.startswith("("):  # e.g. "(Element not found)"
        return snippet or ""
    try:
        soup = BeautifulSoup(snippet, "html.parser")
        el = soup.find()
        if el:
            return el.prettify()
        return soup.prettify()
    except Exception:
        return snippet


def highlight_html(widget, html):
    widget.delete("1.0", "end")
    widget.insert("1.0", html)

    for tag in widget.tag_names():
        widget.tag_delete(tag)

    widget.tag_config("tag", foreground="#0077cc")
    widget.tag_config("attr", foreground="#aa00aa")
    widget.tag_config("value", foreground="#dd5500")
    widget.tag_config("comment", foreground="#777")

    content = widget.get("1.0", "end")

    patterns = {
        "comment": r"<!--[\s\S]*?-->",
        "tag": r"</?[\w\-]+",
        "attr": r"\s[\w\-]+(?==)",
        "value": r'"[^"]*"'
    }

    for tag, pat in patterns.items():
        for m in re.finditer(pat, content):
            widget.tag_add(tag, f"1.0+{m.start()}c", f"1.0+{m.end()}c")


# ============================================================
#   ELEMENT HELPERS
# ============================================================

def extract_id_and_class(snippet):
    if not snippet:
        return "", ""
    try:
        s = BeautifulSoup(snippet, "html.parser")
        el = s.find()
        if el:
            return el.get("id", ""), " ".join(el.get("class", []))
    except Exception:
        pass
    return "", ""


def analyze_element(snippet):
    """
    Get a tag name + inner text to help identify the element in the dashboard.
    """
    tag_name = ""
    inner_text = ""
    if not snippet:
        return tag_name, inner_text

    try:
        s = BeautifulSoup(snippet, "html.parser")
        el = s.find()
        if el:
            tag_name = el.name or ""
            inner_text = el.get_text(separator=" ", strip=True)
    except Exception:
        pass
    return tag_name, inner_text


# ============================================================
#   ACCESSIBILITY (AXE-CORE) SCAN
# ============================================================

def run_axe_scan(url):
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="load")

            axe = requests.get(
                "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.7.0/axe.min.js"
            ).text
            page.add_script_tag(content=axe)

            raw = page.evaluate("""
                async () => await axe.run({
                    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa"] }
                })
            """)

            items = []
            for v in raw["violations"]:
                for n in v["nodes"]:
                    selector = n["target"][0] if n.get("target") else None

                    try:
                        snippet = (
                            page.eval_on_selector(selector, "el => el.outerHTML")
                            if selector else "(No selector)"
                        )
                    except Exception:
                        snippet = "(Element not found)"

                    el_id, el_cls = extract_id_and_class(snippet)
                    tag_name, inner_text = analyze_element(snippet)

                    items.append({
                        "url": url,
                        "priority": v.get("impact", ""),
                        "description": v.get("description", ""),
                        "element_html": snippet,
                        "element_id": el_id,
                        "element_classes": el_cls,
                        "rule_id": v["id"],
                        "tag": tag_name,
                        "inner_text": inner_text,
                    })

            browser.close()
            return items

    except Exception as e:
        return [{
            "url": url,
            "priority": "critical",
            "description": f"SCAN ERROR: {e}",
            "element_html": "",
            "element_id": "",
            "element_classes": "",
            "rule_id": "SCAN_ERROR",
            "tag": "",
            "inner_text": "",
        }]


# ============================================================
#   SITEMAP FETCH + RECURSIVE PARSER (pandas + BS4)
# ============================================================

URL_REGEX = re.compile(r"https?://[^\s,]+")


def fetch_sitemap_xml(url, log):
    """Fetch sitemap XML/HTML with browser-like headers to avoid 403."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/xml,application/xml,application/xhtml+xml,text/html;q=0.9",
        "Accept-Language": "en-US,en;q=0.8",
    }

    try:
        log(f"Fetching sitemap: {url}")
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            log(f"ERROR fetching sitemap {url}: {resp.status_code} {resp.reason}")
            return None
        return resp.text
    except Exception as e:
        log(f"ERROR: Exception fetching sitemap {url}: {e}")
        return None


def extract_urls_from_sitemap(root_url, log):
    """
    Recursively crawl a Yoast-style sitemap index:
    - If the root is a sitemap index, follow child .xml sitemaps.
    - If it's a URL set, collect page URLs.
    - Uses pandas.read_xml first, falls back to BeautifulSoup.
    """
    visited = set()
    page_urls = []

    def add_page_url(u):
        if u and u not in page_urls:
            page_urls.append(u)

    def _walk(url):
        if url in visited:
            log(f"Skipping already-visited sitemap: {url}")
            return
        visited.add(url)

        xml = fetch_sitemap_xml(url, log)
        if not xml:
            return

        log(f"Parsing sitemap content for {url}")

        # Detect HTML vs XML
        head = xml.lstrip()[:200].lower()
        is_html = head.startswith("<!doctype html") or "<html" in head

        # ---------- HTML fallback (Yoast human-readable HTML view) ----------
        if is_html:
            log(f"Sitemap {url} appears to be HTML; using BeautifulSoup HTML parser.")
            soup = BeautifulSoup(xml, "html.parser")
            table = soup.find("table", id="sitemap") or soup.find("table")
            if not table:
                log(f"WARNING: No <table> found in HTML sitemap {url}")
                return

            for row in table.find_all("tr"):
                a = row.find("a")
                if not a:
                    continue
                loc = (a.get("href") or a.text or "").strip()
                if not loc:
                    continue

                if loc.endswith(".xml"):
                    log(f"Found child sitemap in HTML index: {loc}")
                    _walk(loc)
                else:
                    log(f"Found page URL in HTML sitemap: {loc}")
                    add_page_url(loc)
            return

        # ---------- XML via pandas.read_xml ----------
        sitemap_df = None
        url_df = None

        try:
            sitemap_df = pd.read_xml(StringIO(xml), xpath="//sitemap")
            if sitemap_df is not None and sitemap_df.empty:
                sitemap_df = None
        except Exception as e:
            log(f"pandas.read_xml sitemap xpath failed for {url}: {e}")

        try:
            url_df = pd.read_xml(StringIO(xml), xpath="//url")
            if url_df is not None and url_df.empty:
                url_df = None
        except Exception as e:
            log(f"pandas.read_xml url xpath failed for {url}: {e}")

        # Case 1: sitemap index
        if sitemap_df is not None and "loc" in sitemap_df.columns:
            log(f"Detected sitemap index at {url} with {len(sitemap_df)} child sitemaps.")
            for loc in sitemap_df["loc"].dropna().astype(str):
                child = loc.strip()
                if not child:
                    continue
                log(f"Following child sitemap: {child}")
                _walk(child)
            return

        # Case 2: urlset
        if url_df is not None and "loc" in url_df.columns:
            log(f"Detected URL-set sitemap at {url} with {len(url_df)} URLs.")
            for loc in url_df["loc"].dropna().astype(str):
                add_page_url(loc.strip())
            return

        # ---------- Fallback XML parsing with BeautifulSoup ----------
        log(f"pandas.read_xml did not detect structure; falling back to BeautifulSoup XML parsing for {url}")
        soup = BeautifulSoup(xml, "xml")

        sitemapindex = soup.find("sitemapindex")
        if sitemapindex:
            childs = sitemapindex.find_all("sitemap")
            log(f"BS4 XML detected sitemapindex at {url} with {len(childs)} child sitemaps.")
            for sm in childs:
                loc_tag = sm.find("loc")
                if loc_tag and loc_tag.text:
                    child = loc_tag.text.strip()
                    if child.endswith(".xml"):
                        log(f"Following child sitemap (BS4): {child}")
                        _walk(child)
            return

        urlset = soup.find("urlset")
        if urlset:
            urls = urlset.find_all("url")
            log(f"BS4 XML detected urlset at {url} with {len(urls)} URLs.")
            for u in urls:
                loc_tag = u.find("loc")
                if loc_tag and loc_tag.text:
                    add_page_url(loc_tag.text.strip())
            return

        log(f"WARNING: Could not detect valid sitemap format for {url}")

    # Kick off recursion
    _walk(root_url)

    if not page_urls:
        log(f"No page URLs found from sitemap {root_url}")

    return page_urls


# ============================================================
#   URL DETECTION HELPERS FOR CSV
# ============================================================

def extract_urls_from_df(df):
    urls = []

    first_col = df.columns[0]
    urls.extend(df[first_col].dropna().astype(str).tolist())

    cleaned = []
    for u in urls:
        found = URL_REGEX.findall(u)
        cleaned.extend(found)

    return list(dict.fromkeys(cleaned))


# ============================================================
#   MAIN GUI APPLICATION
# ============================================================

class AccessibilityApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Site Accessibility Auditor")
        # Slightly taller default window
        self.root.geometry("1500x1000")

        self.raw_results = []    # page-level node results
        self.results = []        # aggregated component+problem rows
        self.loaded_csv_urls = []

        self.is_scanning = False
        self.cancel_requested = False

        # ----------------------------------------------------
        # Input Mode Dropdown
        # ----------------------------------------------------
        mode_frame = tk.Frame(root)
        mode_frame.pack(pady=5, anchor="w", padx=10)

        tk.Label(mode_frame, text="Input Mode:").pack(side="left", padx=5)

        self.mode_var = tk.StringVar(value="Manual")
        self.mode_select = ttk.Combobox(
            mode_frame,
            textvariable=self.mode_var,
            state="readonly",
            values=["Manual", "Sitemap", "CSV File"]
        )
        self.mode_select.pack(side="left")
        self.mode_select.bind("<<ComboboxSelected>>", self.update_input_mode)

        # ----------------------------------------------------
        # Input Container (URLs + sitemap + CSV)
        # ----------------------------------------------------
        self.input_container = tk.Frame(root)
        self.input_container.pack(fill="x", padx=10, pady=5)

        # Manual textarea (starts enabled for Manual mode)
        self.url_text = tk.Text(self.input_container, height=8, bd=2, relief="solid")
        self.url_text.pack(fill="x")

        # Sitemap frame (URL + Load button)
        self.sitemap_frame = tk.Frame(self.input_container)

        tk.Label(self.sitemap_frame, text="Sitemap URL:").pack(side="left")
        self.sitemap_entry = tk.Entry(self.sitemap_frame, width=60)
        self.sitemap_entry.pack(side="left", padx=5)

        self.load_sitemap_btn = tk.Button(
            self.sitemap_frame,
            text="Load Sitemap",
            command=self.load_sitemap
        )
        self.load_sitemap_btn.pack(side="left", padx=5)

        # CSV input frame
        self.csv_frame = tk.Frame(self.input_container)

        self.csv_label = tk.Label(self.csv_frame, text="No CSV loaded.")
        self.csv_label.pack(anchor="w")

        self.csv_button = tk.Button(
            self.csv_frame, text="Choose CSV File", command=self.load_csv_file
        )
        self.csv_button.pack(anchor="w", pady=5)

        self.csv_help = tk.Label(
            self.csv_frame,
            text="Upload a CSV formatted as a one column list of URLs with no header.",
            justify="left",
            fg="#555",
        )
        self.csv_help.pack(anchor="w", pady=5)

        # ----------------------------------------------------
        # Start / Cancel + Status + Progress
        # ----------------------------------------------------
        start_frame = tk.Frame(root)
        start_frame.pack(pady=10)

        self.start_btn = tk.Button(
            start_frame, text="Run Accessibility Scan", command=self.start_scan
        )
        self.start_btn.pack(side="left", padx=5)

        self.cancel_btn = tk.Button(
            start_frame, text="Cancel Scan", command=self.cancel_scan
        )
        # hidden by default
        self.cancel_btn.pack(side="left", padx=5)
        self.cancel_btn.pack_forget()

        self.status_label = tk.Label(root, text="", font=("Arial", 12))
        self.status_label.pack()

        self.progress = ttk.Progressbar(root, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=5)

        # ----------------------------------------------------
        # Dual Dashboard: Component View (top) + Raw View (bottom)
        # ----------------------------------------------------
        dashboards_frame = tk.Frame(root)
        dashboards_frame.pack(fill="both", expand=True, padx=10, pady=(5, 0))

        # Top: Component / Aggregated view
        comp_frame = tk.Frame(dashboards_frame)
        comp_frame.pack(fill="both", expand=True)

        tk.Label(
            comp_frame,
            text="Component View (Aggregated by Element / Rule)",
            font=("Arial", 11, "bold")
        ).pack(anchor="w", pady=(0, 3))

        comp_cols = (
            "Error",
            "Priority",
            "Tag",
            "Element ID",
            "Classes",
            "Inner Text",
            "URL Count",
        )

        comp_table_container = tk.Frame(comp_frame)
        comp_table_container.pack(fill="both", expand=True)

        self.comp_table = ttk.Treeview(
            comp_table_container, columns=comp_cols, show="headings"
        )

        # Sorting: bind column headers to sort handler
        for col in comp_cols:
            self.comp_table.heading(
                col,
                text=col,
                command=lambda c=col: self.sort_treeview(self.comp_table, c, False)
            )

        self.comp_table.column("Error", width=260, anchor="w")
        self.comp_table.column("Priority", width=80, anchor="w")
        self.comp_table.column("Tag", width=70, anchor="w")
        self.comp_table.column("Element ID", width=150, anchor="w")
        self.comp_table.column("Classes", width=220, anchor="w")
        self.comp_table.column("Inner Text", width=400, anchor="w")
        self.comp_table.column("URL Count", width=80, anchor="center")

        comp_scroll_y = tk.Scrollbar(
            comp_table_container, orient="vertical", command=self.comp_table.yview
        )
        self.comp_table.configure(yscrollcommand=comp_scroll_y.set)

        self.comp_table.pack(side="left", fill="both", expand=True)
        comp_scroll_y.pack(side="right", fill="y")

        self.comp_table.bind("<<TreeviewSelect>>", self.on_comp_select)

        # View Details button directly under component view
        self.view_btn = tk.Button(
            comp_frame,
            text="View Details",
            command=self.view_details,
            state="disabled",
        )
        self.view_btn.pack(anchor="e", pady=(4, 8))

        # Bottom: Raw violations view
        raw_frame = tk.Frame(dashboards_frame)
        raw_frame.pack(fill="both", expand=True)

        tk.Label(
            raw_frame,
            text="All Errors by URL",
            font=("Arial", 11, "bold")
        ).pack(anchor="w", pady=(0, 3))

        raw_cols = (
            "URL",
            "Priority",
            "Error",
            "Rule ID",
            "Tag",
            "Element ID",
            "Classes",
            "Inner Text",
        )

        raw_table_container = tk.Frame(raw_frame)
        raw_table_container.pack(fill="both", expand=True)

        self.raw_table = ttk.Treeview(
            raw_table_container, columns=raw_cols, show="headings"
        )

        for col in raw_cols:
            self.raw_table.heading(
                col,
                text=col,
                command=lambda c=col: self.sort_treeview(self.raw_table, c, False)
            )

        self.raw_table.column("URL", width=260, anchor="w")
        self.raw_table.column("Priority", width=80, anchor="w")
        self.raw_table.column("Error", width=260, anchor="w")
        self.raw_table.column("Rule ID", width=120, anchor="w")
        self.raw_table.column("Tag", width=70, anchor="w")
        self.raw_table.column("Element ID", width=150, anchor="w")
        self.raw_table.column("Classes", width=220, anchor="w")
        self.raw_table.column("Inner Text", width=350, anchor="w")

        raw_scroll_y = tk.Scrollbar(
            raw_table_container, orient="vertical", command=self.raw_table.yview
        )
        self.raw_table.configure(yscrollcommand=raw_scroll_y.set)

        self.raw_table.pack(side="left", fill="both", expand=True)
        raw_scroll_y.pack(side="right", fill="y")

        # ----------------------------------------------------
        # Save / Load / Export Buttons (bottom controls)
        # ----------------------------------------------------
        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=10)

        self.save_btn = tk.Button(btn_frame, text="Save Scan", command=self.save_scan)
        self.save_btn.pack(side="left", padx=5)

        self.load_btn = tk.Button(btn_frame, text="Load Scan", command=self.load_scan)
        self.load_btn.pack(side="left", padx=5)

        self.export_btn = tk.Button(
            btn_frame, text="Export to Excel", command=self.export_excel
        )
        self.export_btn.pack(side="left", padx=5)

        # ----------------------------------------------------
        # Activity Log at Bottom (copyable)
        # ----------------------------------------------------
        log_frame = tk.Frame(root)
        log_frame.pack(fill="both", expand=False, padx=10, pady=(5, 10))

        tk.Label(log_frame, text="Activity Log").pack(anchor="w")

        log_text_frame = tk.Frame(log_frame)
        log_text_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_text_frame,
            height=8,
            wrap="none",
            bd=2,
            relief="solid",
        )
        log_scroll = tk.Scrollbar(
            log_text_frame, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self.log("Application started.")

    # ============================================================
    # Activity Log Helper
    # ============================================================

    def log(self, message: str):
        """Append a timestamped message to the activity log."""
        timestamp = datetime.now().strftime("[%H:%M:%S] ")
        self.log_text.insert("end", timestamp + message + "\n")
        self.log_text.see("end")

    # ============================================================
    # Generic Treeview Sorting
    # ============================================================

    def sort_treeview(self, tree, col, reverse):
        """Sort a Treeview by a given column."""
        # Pull data out of the tree
        data = []
        for item in tree.get_children(""):
            value = tree.set(item, col)
            data.append((value, item))

        # Try numeric sort, fall back to string
        def try_float(s):
            try:
                return float(s)
            except (ValueError, TypeError):
                return None

        # Detect if column is mostly numeric
        numeric_sample = [try_float(v) for v, _ in data[:20]]
        numeric_sample = [x for x in numeric_sample if x is not None]

        if numeric_sample:
            # Numeric sort
            data.sort(
                key=lambda t: (try_float(t[0]) if try_float(t[0]) is not None else float("-inf")),
                reverse=reverse,
            )
        else:
            # String sort (case-insensitive)
            data.sort(key=lambda t: (t[0] or "").lower(), reverse=reverse)

        # Reorder items in tree
        for idx, (_, item) in enumerate(data):
            tree.move(item, "", idx)

        # Toggle sort order on next click
        tree.heading(col, command=lambda: self.sort_treeview(tree, col, not reverse))

    # ============================================================
    # Control Enabling / Disabling
    # ============================================================

    def set_scanning_state(self, scanning: bool):
        self.is_scanning = scanning
        state_btn = "disabled" if scanning else "normal"
        state_combo = "disabled" if scanning else "readonly"

        self.start_btn.config(state=state_btn)
        self.mode_select.config(state=state_combo)
        self.load_sitemap_btn.config(state=state_btn)
        self.csv_button.config(state=state_btn)
        self.save_btn.config(state=state_btn)
        self.load_btn.config(state=state_btn)
        self.export_btn.config(state=state_btn)

        if scanning:
            self.view_btn.config(state="disabled")
            self.cancel_requested = False
            self.cancel_btn.config(state="normal")
            self.cancel_btn.pack(side="left", padx=5)
        else:
            self.cancel_btn.pack_forget()
            # Re-evaluate whether View Details should be active
            self.on_comp_select()

    def cancel_scan(self):
        if not self.is_scanning:
            return
        self.log("Cancel requested by user.")
        self.cancel_requested = True
        self.cancel_btn.config(state="disabled")

    # ============================================================
    # Input Mode Switching
    # ============================================================

    def update_input_mode(self, event=None):
        mode = self.mode_var.get()
        self.log(f"Input mode changed to: {mode}")

        # Hide existing input widgets
        for widget in self.input_container.winfo_children():
            widget.pack_forget()

        # Switch behavior based on mode, including textarea enabled/disabled
        if mode == "Manual":
            # Re-enable editing in manual mode
            self.url_text.config(state="normal")
            self.url_text.pack(fill="x")

        elif mode == "Sitemap":
            self.sitemap_frame.pack(fill="x", pady=(0, 5))
            # Show textarea but make it read-only (display only)
            self.url_text.config(state="disabled")
            self.url_text.pack(fill="x")

        elif mode == "CSV File":
            self.csv_frame.pack(fill="x", pady=(0, 5))
            # Show textarea but make it read-only (display only)
            self.url_text.config(state="disabled")
            self.url_text.pack(fill="x")

    # ============================================================
    # CSV Loader
    # ============================================================

    def load_csv_file(self):
        self.log("CSV file selection opened.")
        file = filedialog.askopenfilename(filetypes=[("CSV Files", "*.csv")])
        if not file:
            self.log("CSV selection canceled.")
            return

        self.log(f"CSV file chosen: {file}")
        try:
            df = pd.read_csv(file, header=None, dtype=str)
        except Exception as e:
            self.log(f"ERROR reading CSV: {e}")
            messagebox.showerror("Error", f"Could not read CSV:\n{e}")
            return

        urls = extract_urls_from_df(df)

        if not urls:
            self.log("No URLs detected in CSV.")
            messagebox.showerror("Error", "No URLs detected in CSV.")
            return

        self.loaded_csv_urls = urls
        self.csv_label.config(text=f"Loaded {len(urls)} URLs from CSV.")
        self.log(f"Loaded {len(urls)} URLs from CSV.")

        # Populate textarea even though it may be read-only in this mode
        self.url_text.config(state="normal")
        self.url_text.delete("1.0", "end")
        self.url_text.insert("1.0", "\n".join(urls))
        # If current mode is CSV, keep it read-only to the user
        if self.mode_var.get() == "CSV File":
            self.url_text.config(state="disabled")

    # ============================================================
    # Load Sitemap → Populate URL textarea
    # ============================================================

    def load_sitemap(self):
        root_sitemap = self.sitemap_entry.get().strip()
        if not root_sitemap:
            messagebox.showerror("Error", "Enter a sitemap URL first.")
            return

        self.log(f"Load Sitemap button clicked. Root sitemap: {root_sitemap}")

        urls = extract_urls_from_sitemap(root_sitemap, self.log)

        if not urls:
            messagebox.showwarning(
                "No URLs",
                f"No page URLs found from sitemap {root_sitemap}",
            )
            self.log(f"No page URLs found from sitemap {root_sitemap}")
            return

        self.log(f"Sitemap load complete. Found {len(urls)} page URLs.")
        # Populate textarea even though it may be read-only in this mode
        self.url_text.config(state="normal")
        self.url_text.delete("1.0", "end")
        self.url_text.insert("1.0", "\n".join(urls))
        # If current mode is Sitemap, keep it read-only to the user
        if self.mode_var.get() == "Sitemap":
            self.url_text.config(state="disabled")

    # ============================================================
    # Start Scan
    # ============================================================

    def start_scan(self):
        if self.is_scanning:
            return

        self.log("Start scan requested.")
        ensure_playwright_browsers_installed(self.log)
        self.raw_results.clear()
        self.results.clear()
        self.comp_table.delete(*self.comp_table.get_children())
        self.raw_table.delete(*self.raw_table.get_children())
        self.view_btn.config(state="disabled")

        mode = self.mode_var.get()

        if mode == "Manual":
            urls = [
                u.strip()
                for u in self.url_text.get("1.0", "end").split("\n")
                if u.strip()
            ]
            self.log(f"Manual mode: {len(urls)} URLs entered.")

        elif mode == "Sitemap":
            urls = [
                u.strip()
                for u in self.url_text.get("1.0", "end").split("\n")
                if u.strip()
            ]
            if not urls:
                messagebox.showerror(
                    "Error",
                    "No URLs loaded from sitemap. Click 'Load Sitemap' first.",
                )
                self.log("Sitemap mode: no URLs in textarea; user must load sitemap.")
                return
            self.log(f"Sitemap mode: using {len(urls)} loaded URLs.")

        elif mode == "CSV File":
            urls = self.loaded_csv_urls
            self.log(f"CSV mode: using {len(urls)} URLs from CSV.")

        else:
            urls = []

        if not urls:
            messagebox.showerror("Error", "No URLs found.")
            self.log("Scan aborted: no URLs to scan.")
            return

        self.progress["maximum"] = len(urls)
        self.progress["value"] = 0
        self.status_label.config(text="Starting scan...")
        self.log("Accessibility scan started.")

        self.set_scanning_state(True)

        threading.Thread(
            target=self.run_scan_thread, args=(urls,), daemon=True
        ).start()

    # ============================================================
    # Aggregation: component + problem → URLs
    # ============================================================

    def build_aggregated_results(self):
        self.log("Aggregating results by component + problem.")

        severity_rank = {"critical": 3, "serious": 2, "moderate": 1, "minor": 0, "": -1}
        agg = {}

        for v in self.raw_results:
            key = (
                v.get("rule_id", ""),
                v.get("tag", ""),
                v.get("element_id", ""),
                v.get("element_classes", ""),
                (v.get("inner_text", "") or "")[:120],
            )
            url = v.get("url", "")

            if key not in agg:
                agg[key] = {
                    "priority": v.get("priority", ""),
                    "description": v.get("description", ""),
                    "element_id": v.get("element_id", ""),
                    "element_classes": v.get("element_classes", ""),
                    "rule_id": v.get("rule_id", ""),
                    "tag": v.get("tag", ""),
                    "inner_text": v.get("inner_text", ""),
                    "urls": set(),
                    "element_html": v.get("element_html", ""),
                }
            entry = agg[key]
            entry["urls"].add(url)

            # worst (highest severity) priority wins
            old_p = entry.get("priority", "")
            new_p = v.get("priority", "")
            if severity_rank.get(new_p, -1) > severity_rank.get(old_p, -1):
                entry["priority"] = new_p

        self.results = []
        self.comp_table.delete(*self.comp_table.get_children())

        for entry in agg.values():
            urls_sorted = sorted(entry["urls"])
            url_count = len(urls_sorted)

            row = {
                "error_id": entry["rule_id"],
                "priority": entry["priority"],
                "description": entry["description"],
                "element_id": entry["element_id"],
                "element_classes": entry["element_classes"],
                "tag": entry["tag"],
                "inner_text": entry["inner_text"],
                "urls": urls_sorted,
                "url_count": url_count,
                "element_html": entry["element_html"],
            }
            self.results.append(row)

        # optional: sort by severity then error id
        self.results.sort(
            key=lambda r: (
                {"critical": 3, "serious": 2, "moderate": 1, "minor": 0}.get(r["priority"], -1) * -1,
                r["error_id"],
            )
        )

        for r in self.results:
            self.comp_table.insert(
                "",
                "end",
                values=(
                    r["description"],
                    r["priority"],
                    r["tag"],
                    r["element_id"],
                    r["element_classes"],
                    r["inner_text"],
                    r["url_count"],
                ),
            )

        self.log(f"Aggregation complete. {len(self.results)} rows in summary table.")

    def populate_raw_table(self):
        """Fill the raw violations table from self.raw_results."""
        self.raw_table.delete(*self.raw_table.get_children())
        for v in self.raw_results:
            self.raw_table.insert(
                "",
                "end",
                values=(
                    v.get("url", ""),
                    v.get("priority", ""),
                    v.get("description", ""),
                    v.get("rule_id", ""),
                    v.get("tag", ""),
                    v.get("element_id", ""),
                    v.get("element_classes", ""),
                    v.get("inner_text", ""),
                ),
            )

    # ============================================================
    # Scan Thread
    # ============================================================

    def run_scan_thread(self, urls):
        for i, url in enumerate(urls, start=1):
            if self.cancel_requested:
                break

            self.status_label.config(text=f"Scanning {url}")
            self.log(f"Scanning URL {i}/{len(urls)}: {url}")

            violations = run_axe_scan(url)

            for v in violations:
                self.raw_results.append(v)

            self.progress["value"] = i

        cancelled = self.cancel_requested
        if cancelled:
            self.log("Accessibility scan cancelled by user.")
            self.status_label.config(text="Scan cancelled.")
        else:
            self.log("Accessibility scan complete.")
            self.status_label.config(text="Scan complete.")

        # Build aggregated component+problem rows & repopulate tables
        self.build_aggregated_results()
        self.populate_raw_table()
        self.set_scanning_state(False)

    # ============================================================
    # Selection handler for Component table
    # ============================================================

    def on_comp_select(self, event=None):
        """Enable/disable View Details based on selection."""
        if self.is_scanning:
            self.view_btn.config(state="disabled")
            return
        if self.comp_table.selection():
            self.view_btn.config(state="normal")
        else:
            self.view_btn.config(state="disabled")

    # ============================================================
    # View Details (URLs + HTML) for Component view
    # ============================================================

    def view_details(self):
        selection = self.comp_table.selection()
        if not selection:
            messagebox.showerror("Error", "No row selected.")
            return

        idx = self.comp_table.index(selection[0])
        if idx < 0 or idx >= len(self.results):
            messagebox.showerror("Error", "Invalid selection.")
            return

        row = self.results[idx]
        self.log(f"Opening details for row {idx} ({row['error_id']}).")

        win = tk.Toplevel(self.root)
        win.title("Details – Component Issue")
        win.geometry("1000x750")

        # Top: summary
        top_frame = tk.Frame(win)
        top_frame.pack(fill="x", padx=10, pady=10)

        lbl_err = tk.Label(
            top_frame,
            text=f"Error ID: {row['error_id']}  |  Priority: {row['priority']}",
            font=("Arial", 11, "bold"),
        )
        lbl_err.pack(anchor="w")

        lbl_desc = tk.Label(
            top_frame,
            text=row["description"],
            wraplength=960,
            justify="left",
        )
        lbl_desc.pack(anchor="w", pady=(5, 0))

        meta = (
            f"Tag: {row['tag'] or '-'}   "
            f"ID: {row['element_id'] or '-'}   "
            f"Classes: {row['element_classes'] or '-'}"
        )
        lbl_meta = tk.Label(top_frame, text=meta, wraplength=960, justify="left", fg="#555")
        lbl_meta.pack(anchor="w", pady=(5, 10))

        if row.get("inner_text"):
            lbl_text = tk.Label(
                top_frame,
                text=f"Inner Text: {row['inner_text']}",
                wraplength=960,
                justify="left",
                fg="#333",
            )
            lbl_text.pack(anchor="w", pady=(0, 10))

        # URLs (copy-able)
        urls_frame = tk.LabelFrame(win, text=f"Affected URLs ({row['url_count']})")
        urls_frame.pack(fill="both", expand=False, padx=10, pady=5)

        urls_text = tk.Text(urls_frame, height=8, wrap="none")
        urls_text.pack(side="left", fill="both", expand=True)

        urls_scroll_y = tk.Scrollbar(urls_frame, orient="vertical", command=urls_text.yview)
        urls_scroll_y.pack(side="right", fill="y")
        urls_text.config(yscrollcommand=urls_scroll_y.set)

        urls_scroll_x = tk.Scrollbar(urls_frame, orient="horizontal", command=urls_text.xview)
        urls_scroll_x.pack(side="bottom", fill="x")
        urls_text.config(xscrollcommand=urls_scroll_x.set)

        urls_text.insert("1.0", "\n".join(row["urls"]))

        def copy_all_urls():
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(row["urls"]))
            self.log("All affected URLs copied to clipboard.")

        copy_btn = tk.Button(urls_frame, text="Copy All URLs", command=copy_all_urls)
        copy_btn.pack(anchor="e", padx=5, pady=5)

        # HTML
        html_frame = tk.LabelFrame(win, text="Element HTML")
        html_frame.pack(fill="both", expand=True, padx=10, pady=10)

        html_text = tk.Text(html_frame, wrap="none", font=("Courier", 10))
        html_text.pack(side="left", fill="both", expand=True)

        scroll_y = tk.Scrollbar(html_frame, orient="vertical", command=html_text.yview)
        scroll_y.pack(side="right", fill="y")
        html_text.config(yscrollcommand=scroll_y.set)

        scroll_x = tk.Scrollbar(html_frame, orient="horizontal", command=html_text.xview)
        scroll_x.pack(side="bottom", fill="x")
        html_text.config(xscrollcommand=scroll_x.set)

        pretty = pretty_html(row["element_html"])
        highlight_html(html_text, pretty)

    # ============================================================
    # Save / Load
    # ============================================================

    def save_scan(self):
        if not self.results and not self.raw_results:
            messagebox.showerror("Error", "No scan results to save.")
            self.log("Save scan requested, but no results.")
            return

        all_urls = set()
        for r in self.results:
            all_urls.update(r.get("urls", []))
        for v in self.raw_results:
            if v.get("url"):
                all_urls.add(v["url"])

        data = {
            "metadata": {
                "scan_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "urls": sorted(all_urls),
            },
            "results": self.results,
            "raw_results": self.raw_results,
        }

        file = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON files", "*.json")])
        if not file:
            self.log("Save scan canceled by user.")
            return

        with open(file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        messagebox.showinfo("Saved", f"Scan saved:\n{file}")
        self.log(f"Scan saved to {file}")

    def load_scan(self):
        self.log("Load scan requested.")
        file = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if not file:
            self.log("Load scan canceled by user.")
            return

        with open(file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.results = data.get("results", [])
        self.raw_results = data.get("raw_results", [])
        self.comp_table.delete(*self.comp_table.get_children())
        self.raw_table.delete(*self.raw_table.get_children())
        self.view_btn.config(state="disabled")

        for r in self.results:
            url_count = r.get("url_count")
            if url_count is None:
                url_count = len(r.get("urls", []))
                r["url_count"] = url_count

            self.comp_table.insert(
                "",
                "end",
                values=(
                    r.get("description", ""),
                    r.get("priority", ""),
                    r.get("tag", ""),
                    r.get("element_id", ""),
                    r.get("element_classes", ""),
                    r.get("inner_text", ""),
                    url_count,
                ),
            )

        self.populate_raw_table()

        scan_date = data.get("metadata", {}).get("scan_date", "Unknown")
        messagebox.showinfo("Loaded", f"Loaded scan from {scan_date}")
        self.log(f"Scan loaded from {file} (original date: {scan_date})")

    # ============================================================
    # Export Excel (2 sheets)
    # ============================================================

    def export_excel(self):
        if not self.results and not self.raw_results:
            messagebox.showerror("Error", "No results to export.")
            self.log("Export to Excel requested, but no results.")
            return

        # Sheet 1: component view
        comp_rows = []
        for r in self.results:
            comp_rows.append({
                "Error ID": r.get("error_id", ""),
                "Priority": r.get("priority", ""),
                "Description": r.get("description", ""),
                "Tag": r.get("tag", ""),
                "Element ID": r.get("element_id", ""),
                "Element Classes": r.get("element_classes", ""),
                "Inner Text": r.get("inner_text", ""),
                "URL Count": r.get("url_count", 0),
                "URLs (newline-separated)": "\n".join(r.get("urls", [])),
                "Element HTML": pretty_html(r.get("element_html", "")),
            })
        df_comp = pd.DataFrame(comp_rows)

        # Sheet 2: raw per-violation-per-URL view
        raw_rows = []
        for v in self.raw_results:
            raw_rows.append({
                "URL": v.get("url", ""),
                "Priority": v.get("priority", ""),
                "Description": v.get("description", ""),
                "Rule ID": v.get("rule_id", ""),
                "Tag": v.get("tag", ""),
                "Element ID": v.get("element_id", ""),
                "Element Classes": v.get("element_classes", ""),
                "Inner Text": v.get("inner_text", ""),
                "Element HTML": pretty_html(v.get("element_html", "")),
            })
        df_raw = pd.DataFrame(raw_rows)

        file = filedialog.asksaveasfilename(
            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")]
        )

        if not file:
            self.log("Export to Excel canceled by user.")
            return

        with pd.ExcelWriter(file) as writer:
            df_comp.to_excel(writer, index=False, sheet_name="Violations by Component")
            df_raw.to_excel(writer, index=False, sheet_name="All Violations (Raw Data)")

        messagebox.showinfo("Saved", f"Excel report saved:\n{file}")
        self.log(f"Excel report (2 sheets) saved to {file}")


# ============================================================
#   RUN APP
# ============================================================

if __name__ == "__main__":
    root = tk.Tk()

    # ---- macOS fullscreen / zoom crash workaround ----
    if platform.system() == "Darwin":
        try:
            # Make sure Tk isn't in fullscreen
            root.attributes("-fullscreen", False)
        except Exception:
            pass

        try:
            # Use a safer macOS window style:
            #   - document-style window
            #   - close + minimize buttons
            #   - no zoom button (so no problematic fullscreen/zoom)
            root.tk.call(
                "tk::unsupported::MacWindowStyle",
                "style",
                root._w,
                "document",
                "closeBox miniaturizeBox"
            )
        except tk.TclError:
            # If the style call fails, just ignore; app still runs.
            pass

    app = AccessibilityApp(root)
    root.mainloop()
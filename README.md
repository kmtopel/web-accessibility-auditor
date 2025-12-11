# Web Accessibility Auditor

This document explains how to install and run the **Web Accessibility Auditor** locally.

## 1. Prerequisites

- **Python 3.9+**
- Bash / Terminal (macOS, Linux) or PowerShell (Windows)
- Internet connection (required for Playwright browser download)

## 2. Installation

1. Clone or download the repository

    Navigate to the directory where you want the project, then:

    ```bash
    git clone <repo-url>
    cd web-accessibility-auditor
    ```

2. Create a Python virtual environment

    ```bash
    python3 -m venv env
    ```

3. Activate the virtual environment

    <em>macOS / Linux</em>:
    ```bash
    source env/bin/activate
    ```

    <em>Windows (PowerShell)</em>:
    ```powershell
    env\Scripts\activate
    ```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Install Playwright Chromium

```bash
python -m playwright install chromium
```

### 6. Run the program

```bash
python main.py
```

## 3. Using the Tool

### Manual Mode
Paste URLs line‑by‑line.

### Sitemap Mode
Enter a sitemap URL and click **Load Sitemap** (Currently, this is only setup for use with WordPress Yoast SEO sitemap_index.xml and child pages of sitemap_index.xml pages).

### CSV File Mode
Upload a CSV containing one column of URLs (no header).

### Running a Scan
Click **Run Accessibility Scan**.

### Viewing Details
Select a row → **View Details**.

### Save / Load
Save results to `.json` or reload previous scans.

### Export
Generate an Excel `.xlsx` report.
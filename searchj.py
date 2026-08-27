import concurrent.futures
import csv
import json
import os
import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram_alert(matches):
    """Sends matched jobs directly to your Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not matches:
        return

    message = f"🏎️ <b>{len(matches)} New Motorsport Job(s) Found!</b>\n\n"
    for m in matches:
        sponsor_tag = "✅ <b>Eligible Sponsorship: YES</b>" if m.get("is_sponsor") else "❌ Sponsorship: No"
        message += (
            f"📌 <b><a href='{m['url']}'>{m['title']}</a></b>\n"
            f"🏢 Company: <code>{m['company']}</code>\n"
            f"🎯 Keywords: <code>{', '.join(m['keywords'])}</code>\n"
            f"🛂 {sponsor_tag}\n\n"
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("Telegram alert sent successfully!")
        else:
            print(f"Failed to send Telegram alert: {resp.text}")
    except Exception as e:
        print(f"Error sending Telegram alert: {e}")


BASE_URL = "https://www.motorsportjobs.com"
SEARCH_URL = f"{BASE_URL}/en/jobs"

# Target keywords (case-insensitive regex patterns)
KEYWORDS = [
    r"\bpython\b",
    r"\bsoftware\b",
    r"\bdeveloper\b",
    r"\btelemetry\b",
    r"\bc#\b",
    r"\bsql\b",
]

# Blacklisted location terms/patterns
EXCLUDED_LOCATIONS = [
    r"\bUSA\b",
    r"\bUnited States\b",
    r"\bU\.S\.A\b",
    r"\bLebanon\b",
    r"\bBeirut\b",
    # Common 2-letter US state postal abbreviations
    r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b",
]

SPONSOR_CSV = "UK.csv"
DB_FILE = "seen_jobs.json"
OUTPUT_FILE = "matched_jobs.md"
MAX_PAGES = 200
WORKERS = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def normalize_company_name(name: str) -> str:
    """Standardizes company names for cleaner fuzzy/set matching."""
    if not name:
        return ""
    name = name.lower()
    # Remove common corporate suffixes & punctuation
    name = re.sub(r"\b(ltd|limited|llc|inc|incorporated|gmbh|sa|plc|corp|motorsport|racing)\b", "", name)
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def load_sponsors(csv_path=SPONSOR_CSV):
    """Loads and normalizes sponsor names from sp.csv."""
    sponsors = set()
    if not os.path.exists(csv_path):
        print(f"Warning: {csv_path} not found. Running without sponsorship checks.")
        return sponsors

    try:
        with open(csv_path, mode="r", encoding="utf-8-sig", errors="ignore") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            
            # If the CSV has a header like 'Organisation Name', detect column index
            company_col_idx = 0
            if header:
                for idx, col in enumerate(header):
                    if any(key in col.lower() for key in ["organisation", "organization", "company", "sponsor", "name"]):
                        company_col_idx = idx
                        break
                # Check first row if header wasn't standard
                if company_col_idx == 0 and not any(key in header[0].lower() for key in ["organisation", "company"]):
                    sponsors.add(normalize_company_name(header[0]))

            for row in reader:
                if row and len(row) > company_col_idx:
                    raw_name = row[company_col_idx]
                    cleaned = normalize_company_name(raw_name)
                    if cleaned:
                        sponsors.add(cleaned)

        print(f"Loaded {len(sponsors)} sponsor companies from {csv_path}")
    except Exception as e:
        print(f"Error reading {csv_path}: {e}")
    return sponsors


def check_sponsorship_match(company_name: str, sponsors: set) -> bool:
    """Checks if the extracted company matches any sponsor record."""
    if not company_name or not sponsors:
        return False

    cleaned_company = normalize_company_name(company_name)
    if not cleaned_company:
        return False

    # Exact normalized match
    if cleaned_company in sponsors:
        return True

    # Substring / contained-in matching (e.g. 'Red Bull Racing' in 'Red Bull Technology Limited')
    for sponsor in sponsors:
        if len(cleaned_company) >= 4 and len(sponsor) >= 4:
            if cleaned_company in sponsor or sponsor in cleaned_company:
                return True
    return False


def load_seen_jobs():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_jobs(seen_ids):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen_ids), f, indent=2)


def is_excluded_location(text):
    if not text:
        return False
    for pattern in EXCLUDED_LOCATIONS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def check_keywords(text):
    matched = []
    for pattern in KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE):
            matched.append(pattern.replace(r"\b", ""))
    return matched


def fetch_page_jobs(session, page_num):
    page_url = f"{SEARCH_URL}?page={page_num}" if page_num > 1 else SEARCH_URL
    try:
        resp = session.get(page_url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        job_links = soup.find_all("a", href=re.compile(r"/en/job/"))

        jobs = {}
        for link in job_links:
            href = link.get("href")
            full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
            job_id = full_url.rstrip("/").split("/")[-1]
            title = link.get_text(strip=True)

            if job_id not in jobs or len(title) > len(jobs[job_id]["title"]):
                jobs[job_id] = {"id": job_id, "title": title, "url": full_url}

        return list(jobs.values())
    except Exception as e:
        print(f"Error fetching page {page_num}: {e}")
        return []


def process_single_job(session, job, sponsors):
    title = job["title"] or "Job Posting"
    full_url = job["url"]

    try:
        resp = session.get(full_url, headers=HEADERS, timeout=8)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        full_page_text = soup.get_text(" ", strip=True)

        # 1. Location extraction & exclusion check
        location_tag = soup.find(string=re.compile(r"Country|Location", re.I))
        location_context = location_tag.parent.get_text() if location_tag and location_tag.parent else ""

        if is_excluded_location(location_context) or is_excluded_location(title):
            return None

        # 2. Precise Company / Recruiter extraction
        company_tag = (
            soup.find("span", class_="recruiter-company-profile-job-organization")
            or soup.find("a", href=re.compile(r"/en/company/|/company/|/employer/"))
            or soup.find("div", class_=re.compile(r"recruiter|employer|company", re.I))
        )
        company_name = company_tag.get_text(strip=True) if company_tag else ""

        # 3. Description extraction
        desc_div = soup.find("div", class_=re.compile(r"description|content|body", re.I))
        desc_text = desc_div.get_text(" ", strip=True) if desc_div else full_page_text

        # Secondary location check on description header
        if is_excluded_location(desc_text[:300]):
            return None

        # 4. Keyword check
        title_matches = check_keywords(title)
        desc_matches = check_keywords(desc_text)
        all_matches = list(set(title_matches + desc_matches))

        if all_matches:
            # 5. Sponsorship verification against sp.csv
            is_sponsor = check_sponsorship_match(company_name, sponsors)

            return {
                "title": title,
                "company": company_name or "Unknown Company",
                "url": full_url,
                "keywords": all_matches,
                "is_sponsor": is_sponsor,
                "found_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }

    except Exception:
        pass

    return None


def run_tracker(max_pages=MAX_PAGES):
    sponsors = load_sponsors(SPONSOR_CSV)
    seen_jobs = load_seen_jobs()
    new_seen_jobs = set(seen_jobs)
    all_unseen_jobs = []

    print(f"1. Fetching {max_pages} search pages concurrently...")
    with requests.Session() as session:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_pages, 8)) as executor:
            page_futures = [executor.submit(fetch_page_jobs, session, p) for p in range(1, max_pages + 1)]
            for future in concurrent.futures.as_completed(page_futures):
                jobs = future.result()
                for job in jobs:
                    if job["id"] not in seen_jobs and job["id"] not in new_seen_jobs:
                        all_unseen_jobs.append(job)
                        new_seen_jobs.add(job["id"])

        print(f"2. Found {len(all_unseen_jobs)} new unseen jobs. Processing across {WORKERS} threads...")

        matches = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
            job_futures = [executor.submit(process_single_job, session, job, sponsors) for job in all_unseen_jobs]
            for future in concurrent.futures.as_completed(job_futures):
                result = future.result()
                if result:
                    matches.append(result)
                    sponsor_tag = " [ELIGIBLE SPONSORSHIP]" if result["is_sponsor"] else ""
                    print(f"  [MATCH]{sponsor_tag} {result['title']} ({result['company']}) -> {result['keywords']}")

    # Write output to Markdown
    if matches:
        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            for match in matches:
                sponsor_label = " | **Eligible Sponsorship: YES**" if match["is_sponsor"] else " | Eligible Sponsorship: No"
                f.write(
                    f"- [{match['title']}]({match['url']}) | Company: `{match['company']}` | Keywords: `{', '.join(match['keywords'])}`{sponsor_label} | Found: {match['found_at']}\n"
                )
        print(f"\nSaved {len(matches)} new matching jobs to {OUTPUT_FILE}")
    else:
        print("\nNo matching jobs found in this batch.")

    save_seen_jobs(new_seen_jobs)


if __name__ == "__main__":
    run_tracker()

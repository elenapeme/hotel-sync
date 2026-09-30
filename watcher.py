"""Listing watcher.

Downloads the full hotel list, validates that real data came back, compares it with the
previous run and sends an email when there are new (or newly available) hotels or when
something goes wrong.
"""
import html
import json
import os
import re
import smtplib
import sys
import time
import traceback
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

# Full list (sold out included). Everything is in the initial HTML: the "lazy loading" on
# the page only affects the thumbnails, there is no pagination or infinite scroll.
# The URL comes from the environment so the (public) repo doesn't reveal the target site.
HOTELS_URL = os.environ.get("TARGET_URL", "")
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
ERROR_REPEAT_SECONDS = 6 * 3600
MIN_EXPECTED_HOTELS = int(os.environ.get("MIN_EXPECTED_HOTELS", "1"))
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

BLOCK_SPLIT_RE = re.compile(r'<div class="ticket-(?P<pid>\d+) product ')
TITLE_RE = re.compile(r'<a href="(?P<link>[^"]+)" class="product_title">(?P<title>.*?)</a>', re.S)
PRICE_RE = re.compile(r'<span class="money">(?P<price>.*?)</span>', re.S)


class ScrapeError(Exception):
    pass


def download_page(target_url: str) -> str:
    request_obj = urllib.request.Request(
        target_url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en"}
    )
    with urllib.request.urlopen(request_obj, timeout=30) as response_obj:
        return response_obj.read().decode("utf-8", errors="replace")


def extract_hotels(page_html: str) -> dict:
    if 'class="list_products"' not in page_html:
        raise ScrapeError("The product list container was not found (layout changed or blocked).")

    list_start = page_html.index('class="list_products"')
    chunks = BLOCK_SPLIT_RE.split(page_html[list_start:])
    # split() with a capture group yields: [prefix, id1, body1, id2, body2, ...]
    found_hotels = {}
    for pid, body_text in zip(chunks[1::2], chunks[2::2]):
        title_match = TITLE_RE.search(body_text)
        price_match = PRICE_RE.search(body_text)
        if not title_match or not price_match:
            raise ScrapeError(f"Product {pid} has no title/price: page structure changed.")
        found_hotels[pid] = {
            "title": html.unescape(title_match["title"]).strip(),
            "price": html.unescape(price_match["price"]).strip(),
            "url": title_match["link"],
            "available": "product_sold_out" not in body_text,
        }

    if len(found_hotels) < MIN_EXPECTED_HOTELS:
        raise ScrapeError(f"No data received: parsed {len(found_hotels)} hotels (expected at least {MIN_EXPECTED_HOTELS}).")
    return found_hotels


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state_map: dict) -> None:
    STATE_FILE.write_text(json.dumps(state_map, indent=2, ensure_ascii=False), encoding="utf-8")


def post_request(target_url: str, body_bytes: bytes, header_map: dict) -> None:
    request_obj = urllib.request.Request(target_url, data=body_bytes, headers=header_map, method="POST")
    urllib.request.urlopen(request_obj, timeout=30).read()


def send_email(subject_text: str, body_text: str) -> bool:
    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        return False
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    recipient = os.environ.get("MAIL_TO") or smtp_user

    mail_obj = EmailMessage()
    mail_obj["Subject"] = subject_text
    mail_obj["From"] = os.environ.get("MAIL_FROM") or smtp_user
    mail_obj["To"] = recipient
    mail_obj.set_content(body_text)

    connection_cls = smtplib.SMTP_SSL if smtp_port == 465 else smtplib.SMTP
    with connection_cls(smtp_host, smtp_port, timeout=30) as smtp_conn:
        if smtp_port != 465:
            smtp_conn.ehlo()
            if smtp_conn.has_extn("starttls"):
                smtp_conn.starttls()
                smtp_conn.ehlo()
        if smtp_user and smtp_pass:
            smtp_conn.login(smtp_user, smtp_pass)
        smtp_conn.send_message(mail_obj)
    return True


def send_push(subject_text: str, body_text: str) -> None:
    ntfy_topic = os.environ.get("NTFY_TOPIC")
    if ntfy_topic:
        post_request(
            f"https://ntfy.sh/{ntfy_topic}",
            body_text.encode("utf-8"),
            {"Title": subject_text.encode("ascii", "ignore").decode(), "Priority": "high", "Click": HOTELS_URL},
        )
    tg_token = os.environ.get("TELEGRAM_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        post_request(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            urllib.parse.urlencode({"chat_id": tg_chat, "text": f"{subject_text}\n\n{body_text}"}).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )


def notify(subject_text: str, body_text: str) -> None:
    print(subject_text)  # body stays out of the logs: Actions logs are public on public repos
    email_sent = send_email(subject_text, body_text)
    send_push(subject_text, body_text)
    if not email_sent:
        print("(SMTP_HOST not configured: email skipped)")


def describe(hotel_info: dict) -> str:
    status_text = "AVAILABLE" if hotel_info["available"] else "sold out"
    return f"- [{status_text}] {hotel_info['title']} ({hotel_info['price']})\n  {hotel_info['url']}"


def find_changes(current_hotels: dict, previous_hotels: dict) -> list:
    change_lines = []
    for pid, hotel_info in current_hotels.items():
        old_info = previous_hotels.get(pid)
        if old_info is None:
            change_lines.append("NEW " + describe(hotel_info))
        elif hotel_info["available"] and not old_info.get("available", True):
            change_lines.append("AVAILABLE AGAIN " + describe(hotel_info))
        elif hotel_info["price"] != old_info.get("price", hotel_info["price"]):
            change_lines.append(f"PRICE CHANGED {old_info['price']} -> {hotel_info['price']} " + describe(hotel_info))
    return change_lines


def run_check() -> None:
    if not HOTELS_URL:
        raise ScrapeError("TARGET_URL is not set.")
    state_map = load_state()
    # Validation happens first: an empty/broken response never reaches the comparison.
    current_hotels = extract_hotels(download_page(HOTELS_URL))
    available_total = sum(1 for info in current_hotels.values() if info["available"])
    print(f"Hotels listed: {len(current_hotels)} ({available_total} available)")

    if "hotels" not in state_map:
        print("First run: saving baseline, no notification.")
    else:
        change_lines = find_changes(current_hotels, state_map["hotels"])
        if change_lines:
            notify(
                f"Hotel watcher: {len(change_lines)} change(s)",
                "\n".join(change_lines) + f"\n\n{HOTELS_URL}?show=ao",
            )
        else:
            print("No changes.")

    # Saved only after notify() succeeded, so a failed email is retried on the next run.
    # Only what the comparison needs: titles/URLs would make the public state.json identifiable.
    save_state({"hotels": {pid: {"price": info["price"], "available": info["available"]}
                           for pid, info in current_hotels.items()}})


def report_error(error_text: str) -> None:
    state_map = {}
    try:
        state_map = load_state()
    except Exception:
        pass
    last_error = state_map.get("error") or {}
    is_repeat = (
        last_error.get("message") == error_text.splitlines()[-1]
        and time.time() - last_error.get("at", 0) < ERROR_REPEAT_SECONDS
    )
    if is_repeat:
        print("Same error already reported recently: no new email.")
        return
    try:
        notify("Hotel watcher: ERROR", error_text)
    except Exception as notify_failure:
        print(f"Could not send the error notification: {notify_failure}")
        return
    state_map["error"] = {"message": error_text.splitlines()[-1], "at": time.time()}
    save_state(state_map)


def main() -> int:
    try:
        run_check()
        return 0
    except Exception:
        error_text = traceback.format_exc()
        print(error_text, file=sys.stderr)
        report_error(error_text)
        return 1


if __name__ == "__main__":
    sys.exit(main())

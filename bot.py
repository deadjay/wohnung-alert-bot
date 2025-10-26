import json
import os
import re
import requests
import html as html_lib

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from datetime import datetime

# ============================================================================
# CONFIGURATION
# ============================================================================

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("telegram_bot_token")
SUBSCRIBERS_FILE = "subscribers.json"
LISTINGS_UPDATE_INTERVAL = 300  # 5 mins
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0"
}
ALLOWED_DISTRICTS = [
    "Kreuzberg", "Friedrichshain", "Pankow", "Neukölln", "Mitte", "Tempelhof", "Schöneberg"
]


# ============================================================================
# DATA PERSISTENCE
# ============================================================================

def load_seen_ids():
    """Load the set of already-seen listing IDs from disk."""
    if os.path.exists("seen.json"):
        with open("seen.json", "r") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(ids):
    """Save the set of seen listing IDs to disk."""
    with open("seen.json", "w") as f:
        json.dump(list(ids), f)


def load_subscribers():
    """Load the set of subscriber chat IDs from disk."""
    try:
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, "r") as f:
                return set(json.load(f))
    except Exception as e:
        print(f"Error loading subscribers: {e}")
    return set()


def save_subscribers(subscribers):
    """Save the set of subscriber chat IDs to disk."""
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(subscribers), f)


# ============================================================================
# WEB SCRAPING - HELPER FUNCTIONS
# ============================================================================

def extract_listing_metadata(container):
    """Extract objectId and deeplink from wire:snapshot JSON data."""
    snapshot_attr = container.get("wire:snapshot", "")

    if not snapshot_attr:
        return None, None

    try:
        snapshot_json = html_lib.unescape(snapshot_attr)
        snapshot_data = json.loads(snapshot_json)

        item_data = snapshot_data.get("data", {}).get("item", [])
        if item_data and len(item_data) > 0:
            objekt_id = item_data[0].get("objectId", "")
            deeplink = item_data[0].get("deeplink", "")
            return objekt_id, deeplink
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  ! Could not parse snapshot data: {e}")

    return None, None


def parse_listing_details(detail_span):
    """Parse room count, size, price, and address from listing span."""
    if not detail_span:
        return None

    text = detail_span.get_text(separator=" ", strip=True)

    # Extract rooms
    zimmer_match = re.search(r'(\d+[,.]?\d*)\s*Zimmer', text)
    zimmer = zimmer_match.group(1).replace(",", ".") if zimmer_match else "?"

    # Extract square meters
    qm_match = re.search(r'(\d+[,.]?\d*)\s*m²', text)
    qm = qm_match.group(1).replace(",", ".") if qm_match else ""

    # Extract rent
    price_match = re.search(r'(\d+[,.]?\d*)\s*€', text)
    kaltmiete = price_match.group(1) if price_match else ""

    # Extract address (after the pipe |)
    addr_match = re.search(r'\|\s*(.+)$', text)
    adresse = addr_match.group(1).strip() if addr_match else ""

    return {
        "zimmer": zimmer,
        "qm": qm,
        "kaltmiete": kaltmiete,
        "adresse": adresse,
        "text": text
    }


def normalize_price(kaltmiete):
    """Convert price string to float, handling German number format."""
    try:
        normalized = kaltmiete.replace(".", "").replace(",", ".")
        return float(normalized)
    except ValueError:
        return None


def generate_fallback_id(adresse, qm, kaltmiete):
    """Generate a hash-based ID when objectId is not available."""
    normalized_addr = adresse.replace(" ", "").lower()
    normalized_qm = qm.replace(",", ".")
    normalized_price = kaltmiete.replace(".", "").replace(",", ".")
    return str(hash(f"{normalized_addr}_{normalized_qm}_{normalized_price}"))[-8:]


def should_include_listing(details, rent):
    """Check if listing passes all filters (price, district)."""
    if not details or not details["kaltmiete"]:
        return False

    if rent is None or rent > 1000:
        return False

    # Check district filter
    text = details["text"]
    if not any(district.lower() in text.lower() for district in ALLOWED_DISTRICTS):
        return False

    return True


# ============================================================================
# WEB SCRAPING - MAIN FUNCTION
# ============================================================================

def fetch_offers():
    """
    Fetch apartment listings by scraping the HTML page directly.
    Returns a list of offer dictionaries.
    """
    url = "https://www.inberlinwohnen.de/wohnungsfinder/"

    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"[{datetime.now()}] HTTP error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[{datetime.now()}] Request failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find all listing containers
    listing_containers = soup.find_all("div", attrs={"wire:id": True, "id": re.compile(r"apartment-\d+")})
    print(f"[{datetime.now()}] Found {len(listing_containers)} listing containers")

    offers = []

    for container in listing_containers:
        try:
            # Extract metadata
            objekt_id, deeplink = extract_listing_metadata(container)

            # Parse listing details
            detail_span = container.find("span", class_="block")
            details = parse_listing_details(detail_span)

            if not details:
                continue

            # Normalize and validate price
            rent = normalize_price(details["kaltmiete"])

            # Apply filters
            if not should_include_listing(details, rent):
                continue

            # Generate fallback ID if needed
            if not objekt_id:
                objekt_id = generate_fallback_id(
                    details["adresse"],
                    details["qm"],
                    details["kaltmiete"]
                )

            offers.append({
                "objektID": objekt_id,
                "adresse": details["adresse"] or "Adresse nicht verfügbar",
                "zimmer": details["zimmer"],
                "qm": details["qm"],
                "kaltmiete": details["kaltmiete"],
                "deeplink": deeplink
            })

        except Exception as e:
            print(f"[{datetime.now()}] Error parsing listing: {e}")
            continue

    print(f"[{datetime.now()}] Parsed {len(offers)} valid offers")
    return offers


# ============================================================================
# TELEGRAM BOT - MESSAGE SENDING
# ============================================================================

async def send_telegram_message(text, chat_id, context):
    """Send a message to a specific chat via Telegram."""
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        print(f"Sent message to chat_id={chat_id}")
    except Exception as e:
        print(f"Failed to send message to {chat_id}: {e}")


# ============================================================================
# TELEGRAM BOT - SCHEDULED TASKS
# ============================================================================

async def check_new_listings(context: ContextTypes.DEFAULT_TYPE):
    """
    Check for new apartment listings and notify subscribers.
    This runs periodically as a scheduled job.
    """
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking new listings...")

    seen_ids = load_seen_ids()
    offers = fetch_offers()
    new_offers = []

    print(f"Total listings in response: {len(offers)}")

    for offer in offers:
        offer_id = offer.get("objektID")
        if offer_id and offer_id not in seen_ids:
            new_offers.append(offer)
            seen_ids.add(offer_id)

    print(f"New listings: {len(new_offers)}")

    save_seen_ids(seen_ids)

    subscribers = load_subscribers()
    if not subscribers:
        print("No subscribers to send messages to.")
        return

    for offer in new_offers:
        # Use deeplink if available, otherwise fall back to wohnungsfinder
        link = offer.get('deeplink') or f"https://www.inberlinwohnen.de/wohnungsfinder/?oID={offer.get('objektID')}"

        message = (
            f"🏠 <b>{offer.get('adresse')}</b>\n"
            f"{offer.get('zimmer')} Zimmer | {offer.get('qm')} m² | {offer.get('kaltmiete')} €\n"
            f"<a href='{link}'>🔗 Zum Angebot</a>"
        )
        print(f"New offer:\n{message}\n")

        for chat_id in subscribers:
            await send_telegram_message(message, chat_id, context)


# ============================================================================
# TELEGRAM BOT - COMMAND HANDLERS
# ============================================================================

async def start_command(update, context):
    """Handle /start command - subscribe user to notifications."""
    chat_id = update.effective_chat.id
    subscribers = load_subscribers()
    if chat_id not in subscribers:
        subscribers.add(chat_id)
        save_subscribers(subscribers)
        print(f"✅ User {chat_id} subscribed")
        await update.message.reply_text("✅ You are now subscribed to apartment alerts!")
    else:
        print(f"👀 User {chat_id} already subscribed")
        await update.message.reply_text("👀 You're already subscribed.")


async def stop_command(update, context):
    """Handle /stop command - unsubscribe user from notifications."""
    chat_id = update.effective_chat.id
    subscribers = load_subscribers()
    if chat_id in subscribers:
        subscribers.remove(chat_id)
        save_subscribers(subscribers)
        print(f"❌ User {chat_id} unsubscribed.")
        await update.message.reply_text("❌ You have unsubscribed from apartment alerts.")
    else:
        print(f"👀 User {chat_id} tried to unsubscribe but was not subscribed.")
        await update.message.reply_text("👀 You were not subscribed.")


# ============================================================================
# MAIN APPLICATION
# ============================================================================

def main():
    """Initialize and run the Telegram bot."""
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stop", stop_command))

    # Schedule the periodic listing check
    job_queue = application.job_queue
    job_queue.run_repeating(check_new_listings, interval=LISTINGS_UPDATE_INTERVAL, first=10)

    print(f"[{datetime.now()}] Bot started successfully!")
    application.run_polling()


if __name__ == "__main__":
    main()
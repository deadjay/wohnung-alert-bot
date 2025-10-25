import json
import os
import re
import requests

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from datetime import datetime

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("telegram_bot_token")
SUBSCRIBERS_FILE = "subscribers.json"
LISTINGS_UPDATE_INTERVAL = 600 # 10 mins
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


def fetch_offers():
    """
    Fetch apartment listings by scraping the HTML page directly.
    The old API endpoint no longer exists after website redesign in 2024.
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

    offers = []

    # Find all listing containers with wire:id attribute (each represents one listing)
    listing_containers = soup.find_all("div", attrs={"wire:id": True, "id": re.compile(r"apartment-\d+")})

    print(f"[{datetime.now()}] Found {len(listing_containers)} listing containers")

    for container in listing_containers:
        try:
            # Extract the wire:snapshot JSON data which contains objectId and deeplink
            snapshot_attr = container.get("wire:snapshot", "")
            objekt_id = ""
            deeplink = ""

            if snapshot_attr:
                try:
                    import json
                    # The snapshot is HTML-escaped, so we need to unescape it first
                    import html
                    snapshot_json = html.unescape(snapshot_attr)
                    snapshot_data = json.loads(snapshot_json)

                    # Navigate through the nested structure
                    item_data = snapshot_data.get("data", {}).get("item", [])
                    if item_data and len(item_data) > 0:
                        objekt_id = item_data[0].get("objectId", "")
                        deeplink = item_data[0].get("deeplink", "")
                        print(f"  -> Extracted: ID={objekt_id}, Link={deeplink[:50] if deeplink else 'None'}...")
                except (json.JSONDecodeError, KeyError, IndexError) as e:
                    print(f"  ! Could not parse snapshot data: {e}")

            # Find the span with listing details
            detail_span = container.find("span", class_="block")
            if not detail_span:
                continue

            text = detail_span.get_text(separator=" ", strip=True)

            # Parse the format: "X,X Zimmer, X,XX m², X,XX € | Address"
            # Extract rooms
            zimmer_match = re.search(r'(\d+[,.]?\d*)\s*Zimmer', text)
            zimmer = zimmer_match.group(1).replace(",", ".") if zimmer_match else "?"

            # Extract square meters
            qm_match = re.search(r'(\d+[,.]?\d*)\s*m²', text)
            qm = qm_match.group(1).replace(",", ".") if qm_match else ""

            # Extract rent (Kaltmiete)
            price_match = re.search(r'(\d+[,.]?\d*)\s*€', text)
            kaltmiete = price_match.group(1) if price_match else ""

            if not kaltmiete:
                continue

            # Extract address (everything after the pipe |)
            addr_match = re.search(r'\|\s*(.+)$', text)
            adresse = addr_match.group(1).strip() if addr_match else ""

            # Normalize price for filtering
            normalized_price = kaltmiete.replace(".", "").replace(",", ".")

            try:
                rent = float(normalized_price)
            except ValueError:
                continue

            # Filter: max rent 1000€
            if rent > 1000:
                continue

            # Filter by district - check in full text
            if not any(district.lower() in text.lower() for district in ALLOWED_DISTRICTS):
                continue

            # If no objectId found, create one from hash
            if not objekt_id:
                # Normalize data for consistent hashing
                normalized_addr = adresse.replace(" ", "").lower()
                normalized_qm = qm.replace(",", ".")
                normalized_price_clean = normalized_price
                objekt_id = str(hash(f"{normalized_addr}_{normalized_qm}_{normalized_price_clean}"))[-8:]

            offers.append({
                "objektID": objekt_id,
                "adresse": adresse or "Adresse nicht verfügbar",
                "zimmer": zimmer,
                "qm": qm,
                "kaltmiete": kaltmiete,
                "deeplink": deeplink  # Add the actual deeplink
            })

        except Exception as e:
            print(f"[{datetime.now()}] Error parsing listing: {e}")
            continue

    print(f"[{datetime.now()}] Parsed {len(offers)} valid offers")
    return offers


def load_seen_ids():
    if os.path.exists("seen.json"):
        with open("seen.json", "r") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(ids):
    with open("seen.json", "w") as f:
        json.dump(list(ids), f)


def load_subscribers():
    try:
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, "r") as f:
                return set(json.load(f))
    except Exception as e:
        print(f"Error loading subscribers: {e}")
    return set()


def save_subscribers(subscribers):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(subscribers), f)


async def send_telegram_message(text, chat_id, context):
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        print(f"Sent message to chat_id={chat_id}")
    except Exception as e:
        print(f"Failed to send message to {chat_id}: {e}")


async def check_new_listings(context: ContextTypes.DEFAULT_TYPE):
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
        print(f"New offer:\n{message}")

        for chat_id in subscribers:
            await send_telegram_message(message, chat_id, context)


async def start_command(update, context):
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


def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stop", stop_command))

    # Schedule the check_new_listings job
    job_queue = application.job_queue
    job_queue.run_repeating(check_new_listings, interval=LISTINGS_UPDATE_INTERVAL, first=10)

    print(f"[{datetime.now()}] Bot started successfully!")
    application.run_polling()

if __name__ == "__main__":
    main()

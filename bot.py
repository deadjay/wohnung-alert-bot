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

    # Find all listing containers - they appear to be in article tags or similar containers
    # We need to find the pattern in the HTML
    listing_containers = soup.find_all("article") or soup.find_all("div",
                                                                   class_=re.compile(r"wohnung|offer|listing|apartment",
                                                                                     re.I))

    # If we can't find article tags, try to find divs with listing information
    if not listing_containers:
        # Look for elements that contain typical listing info patterns
        all_divs = soup.find_all("div")
        listing_containers = [div for div in all_divs if
                              div.find(string=re.compile(r"Zimmer|m²|€", re.I))]

    print(f"[{datetime.now()}] Found {len(listing_containers)} potential listing containers")

    for container in listing_containers:
        try:
            # Extract text content
            text = container.get_text(separator=" ", strip=True)

            # Skip if doesn't look like a listing
            if "Zimmer" not in text or "€" not in text:
                continue

            # Extract address - look for common patterns
            adresse = ""

            # Try to find address in various common HTML structures
            addr_elem = (container.find("div", class_=re.compile(r"address|adresse", re.I)) or
                         container.find("span", class_=re.compile(r"address|adresse", re.I)) or
                         container.find(string=re.compile(r"Adresse:", re.I)))

            if addr_elem:
                if isinstance(addr_elem, str):
                    adresse = addr_elem.split(":")[-1].strip()
                else:
                    adresse = addr_elem.get_text(strip=True)
                    # Clean up "Adresse:" prefix if present
                    if adresse.startswith("Adresse:"):
                        adresse = adresse.replace("Adresse:", "").strip()

            # If no address element found, try to extract from full text
            if not adresse:
                # Look for "Adresse: XXX" pattern first - capture everything until we hit "Zimmer" or line break
                addr_match = re.search(r'Adresse:\s*(.+?)(?=\s+Zimmer|\s+Wohnfläche|\n|$)', text, re.I)
                if addr_match:
                    adresse = addr_match.group(1).strip()
                else:
                    # Look for pattern after pipe: "Address, PLZ District"
                    addr_match = re.search(r'\|\s*(.+?)\s+(\d{5})\s*,?\s*([^\n]+?)(?=\s+Wohnung|\s+Alle Details|$)',
                                           text)
                    if addr_match:
                        # Combine all parts: street, postal code, district
                        adresse = f"{addr_match.group(1).strip()}, {addr_match.group(2)}, {addr_match.group(3).strip()}"

            # Extract number of rooms
            zimmer_match = re.search(r'(\d+[,.]?\d*)\s*(?:Zimmer|Zi\.)', text, re.I)
            zimmer = zimmer_match.group(1).replace(",", ".") if zimmer_match else "?"

            # Extract square meters
            qm_match = re.search(r"(\d+[\.,]?\d*)\s*m²", text, re.I)
            qm = qm_match.group(1).replace(",", ".") if qm_match else ""

            # Extract rent - look for "Kaltmiete" specifically
            kaltmiete = ""
            kalt_match = re.search(r"Kaltmiete[:\s]*(\d+[\.,]?\d*)\s*€", text, re.I)
            if kalt_match:
                kaltmiete = kalt_match.group(1)
            else:
                # Fallback to any price if Kaltmiete not found
                price_match = re.search(r"(\d+[\.,]?\d*)\s*€", text)
                if price_match:
                    kaltmiete = price_match.group(1)

            if not kaltmiete:
                continue

            # Normalize price (remove thousand separators, convert comma to dot)
            normalized_price = kaltmiete.replace(".", "").replace(",", ".")

            try:
                rent = float(normalized_price)
            except ValueError:
                continue

            # Filter: max rent 1000€
            if rent > 1000:
                print(f"  X Filtered (rent>1000): {adresse}, {rent}€")
                continue

            # Filter by district
            if not any(district.lower() in text.lower() for district in ALLOWED_DISTRICTS):
                # If no address or not in allowed districts, skip
                if not adresse or not any(district.lower() in text.lower() for district in ALLOWED_DISTRICTS):
                    continue

            # Try to extract object ID from links
            objekt_id = ""
            links = container.find_all("a", href=True)
            for link in links:
                href = link.get("href", "")
                # Look for ID patterns like oID=12345 or /wohnung/12345
                id_match = re.search(r"(?:oID=|/wohnung/)(\d+)", href)
                if id_match:
                    objekt_id = id_match.group(1)
                    break

            # If no ID found, create one from the listing content hash
            if not objekt_id:
                # Use a hash of the key details as ID
                objekt_id = str(hash(f"{adresse}_{zimmer}_{qm}_{kaltmiete}"))[-8:]

            offers.append({
                "objektID": objekt_id,
                "adresse": adresse or "Adresse nicht verfügbar",
                "zimmer": zimmer,
                "qm": qm,
                "kaltmiete": kaltmiete
            })

            print(
                f"  -> Parsed: ID={objekt_id}, Addr={adresse[:30] if adresse else 'None'}, Rooms={zimmer}, SqM={qm}, Rent={kaltmiete}")

        except Exception as e:
            print(f"[{datetime.now()}] Error parsing listing: {e}")
            continue

    print(f"[{datetime.now()}] Parsed {len(offers)} valid offers")

    # Deduplicate offers by objektID
    seen_ids_in_batch = set()
    unique_offers = []
    for offer in offers:
        if offer['objektID'] not in seen_ids_in_batch:
            seen_ids_in_batch.add(offer['objektID'])
            unique_offers.append(offer)

    print(f"[{datetime.now()}] After deduplication: {len(unique_offers)} unique offers")
    return unique_offers  # Change this from 'return offers'


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
        message = (
            f"🏠 <b>{offer.get('adresse')}</b>\n"
            f"{offer.get('zimmer')} Zimmer | {offer.get('qm')} m² | {offer.get('kaltmiete')} €\n"
            f"<a href='https://inberlinwohnen.de/wohnungsfinder/?oID={offer.get('objektID')}'>🔗 Zum Angebot</a>"
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

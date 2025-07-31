import requests
import telegram
import re
import os
import json
import asyncio
import threading
from telegram.ext import ApplicationBuilder, CommandHandler
from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("telegram_bot_token")
SUBSCRIBERS_FILE = "subscribers.json"

def fetch_offers():
    url = "https://inberlinwohnen.de/wp-content/themes/ibw/skript/wohnungsfinder.php"
    payload = {
        "q": "wf-save-srch",
        "save": "false"
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://inberlinwohnen.de/wohnungsfinder/",
        "Origin": "https://inberlinwohnen.de",
    }

    resp = requests.post(url, data=payload, headers=headers, timeout=10)
    resp.raise_for_status()

    data = resp.json()
    html = data.get("searchresults", "")
    soup = BeautifulSoup(html, "html.parser")

    print(f"HTML length: {len(html)}")
    # print(html[:1000])  # first 1000 characters

    offers = []
    flats = soup.find_all("li", class_="tb-merkflat")
    print(f"Found listing blocks: {len(flats)}")

    for flat in flats:
        div_id = flat.get("id", "")
        match = re.search(r"flat_(\d+)", div_id)
        objekt_id = match.group(1) if match else ""

        # основной текст с данными
        span = flat.find("span", class_="_tb_left")
        if not span:
            continue

        text = span.get_text(separator=" ", strip=True)
        # Пример: "1 Zimmer, 40,06 ma, 290,40 € | Riemannstr. 22, Kreuzberg"

        zimmer_match = re.search(r"(\d+[\.,]?\d*)\s*Zimmer", text)
        qm_match = re.search(r"(\d+[\.,]?\d*)\s*m²", text, re.IGNORECASE)
        price_match = re.search(r"(\d+[\.,]?\d*)\s*€", text)

        zimmer = zimmer_match.group(1) if zimmer_match else ""
        qm = qm_match.group(1) if qm_match else ""
        kaltmiete = price_match.group(1) if price_match else ""

        adresse = text.split("|")[-1].strip() if "|" in text else ""

        # Filter: Only listings with rent <= 1000 and in selected districts
        try:
            rent = float(kaltmiete.replace(",", "."))
        except ValueError:
            continue  # Skip if price is invalid

        allowed_districts = [
            "Kreuzberg", "Friedrichshain", "Pankow", "Neukölln", "Mitte", "Tempelhof", "Schöneberg"
        ]

        if rent > 1000:
            continue

        if not any(district.lower() in adresse.lower() for district in allowed_districts):
           continue

        offers.append({
            "objektID": objekt_id,
            "adresse": adresse,
            "zimmer": zimmer,
            "qm": qm,
            "kaltmiete": kaltmiete
        })

    return offers

def find_kaltmiete(trs, start_index):
    for j in range(start_index, min(start_index + 10, len(trs))):
        th = trs[j].find("th")
        if th:
            header_text = th.text.strip()
            print(f"Checking row {j} with header: '{header_text}'")
            if "kaltmiete" in header_text.lower():
                td = trs[j].find("td")
                if td:
                    return td.text.strip()
    return ""

def load_seen_ids():
    if os.path.exists("seen.json"):
        with open("seen.json", "r") as f:
            return set(json.load(f))
    return set()

def save_seen_ids(ids):
    with open("seen.json", "w") as f:
        json.dump(list(ids), f)

async def send_telegram_message(text, chat_id):
    try:
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception as e:
        print(f"Failed to send message to {chat_id}: {e}")

def load_subscribers():
    if os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_subscribers(subscribers):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(subscribers), f)

async def start_command(update, context):
    chat_id = update.effective_chat.id
    subscribers = load_subscribers()
    if chat_id not in subscribers:
        subscribers.add(chat_id)
        save_subscribers(subscribers)
        await update.message.reply_text("✅ You are now subscribed to apartment alerts!")
    else:
        await update.message.reply_text("👀 You're already subscribed.")

def run_bot_listener():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    loop.run_until_complete(app.initialize())
    loop.run_until_complete(app.start())
    loop.run_until_complete(app.updater.start_polling())
    loop.run_forever()


def main():
    # запустить слушателя в фоне
    threading.Thread(target=run_bot_listener, daemon=True).start()

    seen_ids = load_seen_ids()
    offers = fetch_offers()
    new_offers = []

    print(f"Total listings in response: {len(offers)}")
    print(f"New listings: {len(new_offers)}")
    print("Example listing:", offers[0] if offers else "empty list")

    for offer in offers:
        offer_id = offer.get("objektID")
        if offer_id and offer_id not in seen_ids:
            new_offers.append(offer)
            seen_ids.add(offer_id)

    save_seen_ids(seen_ids)

    for offer in new_offers:
        message = (
            f"🏠 <b>{offer.get('adresse')}</b>\n"
            f"{offer.get('zimmer')} Zimmer | {offer.get('qm')} m² | {offer.get('kaltmiete')} €\n"
            f"<a href='https://inberlinwohnen.de/wohnungsfinder/?oID={offer.get('objektID')}'>🔗 Zum Angebot</a>"
        )

        print(message)
        subscribers = load_subscribers()
        for chat_id in subscribers:
            asyncio.run(send_telegram_message(message, chat_id))

if __name__ == "__main__":
    main()


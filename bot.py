import requests
from bs4 import BeautifulSoup
import re
import os
import json

def fetch_offers():
    url = "https://inberlinwohnen.de/wp-content/themes/ibw/skript/wohnungsfinder.php"
    payload = {
        "q": "wf-save-srch",
        "save": "false"
        # Добавляй фильтры сюда, если нужно
    }
    resp = requests.post(url, data=payload)
    data = resp.json()
    html = data.get("searchresults", "")
    soup = BeautifulSoup(html, "html.parser")

    print(f"HTML length: {len(html)}")
    print(html[:1000])  # first 1000 characters


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

def main():
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
        print(f"🏠 {offer.get('adresse')} | {offer.get('zimmer')} Zimmer | {offer.get('qm')} m² | {offer.get('kaltmiete')} €")
        print(f"https://inberlinwohnen.de/wohnungsfinder/?oID={offer.get('objektID')}")
        print("---")

if __name__ == "__main__":
    main()
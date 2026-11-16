#!/usr/bin/env python3
"""
Controleert periodiek een aanbodlijst en stuurt een pushmelding bij nieuwe
items die aan de filters in config.json voldoen.

De bron wordt ingesteld via de omgevingsvariabele SOURCE_URL (of
"source_url" in config.json), de meldingen via NTFY_TOPIC.

Gebruik:
    python check.py                 # één keer controleren
    python check.py --list          # toon alle huidige items
    python check.py --test-notify   # stuur een testmelding
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

DEFAULTS = {
    "city": "",
    "max_price": 0,
    "include_service_costs": False,
    "stages": ["available"],
    "types": [],
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    for key in ("source_url", "ntfy_topic"):
        if os.environ.get(key.upper()):
            cfg[key] = os.environ[key.upper()]
    if not cfg.get("source_url"):
        sys.exit("SOURCE_URL ontbreekt (secret of config.json).")
    cfg["source_url"] = cfg["source_url"].rstrip("/")
    return cfg


def key_of(item_id: str) -> str:
    # Alleen een hash opslaan, zodat er geen adressen in de openbare map staan
    return hashlib.sha256(item_id.encode()).hexdigest()[:16]


def load_state() -> list:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def fetch(cfg: dict) -> list[dict]:
    items, page = [], 1
    while True:
        params = [("perPage", 100), ("page", page), ("sort", "stage"),
                  ("filter[stage]", "available"), ("filter[stage]", "option"),
                  ("filter[import_type]", "RentResident")]
        r = requests.get(f"{cfg['source_url']}/api/ms/listing/properties",
                         params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        body = r.json()
        items.extend(simplify(i, cfg) for i in body.get("data", []))
        if page >= (body.get("meta") or {}).get("last_page", 1):
            return items
        page += 1
        time.sleep(1)


def simplify(i: dict, cfg: dict) -> dict:
    addr, char, hand = i.get("address") or {}, i.get("characteristic") or {}, i.get("handover") or {}
    return {
        "id": str(i.get("id")),
        "title": i.get("title") or "",
        "city": (addr.get("location") or "").strip(),
        "price": float(hand.get("price") or 0),
        "service": float(hand.get("service_costs") or 0),
        "area": char.get("living_area"),
        "rooms": (char.get("layout") or {}).get("number_of_rooms"),
        "type": char.get("type") or "",
        "stage": i.get("stage") or "",
        "available": i.get("available_at_label") or "",
        "url": f"{cfg['source_url']}/huur/woningen/{i.get('slug', '')}",
    }


def matches(x: dict, cfg: dict) -> bool:
    price = x["price"] + (x["service"] if cfg.get("include_service_costs") else 0)
    return (x["city"].lower() == cfg["city"].lower()
            and 0 < price <= cfg["max_price"]
            and (not cfg.get("stages") or x["stage"] in cfg["stages"])
            and (not cfg.get("types") or x["type"].lower() in [t.lower() for t in cfg["types"]]))


def describe(x: dict) -> str:
    bits = [f"€{x['price']:.0f} p/m" + (f" (+€{x['service']:.0f} service)" if x["service"] else "")]
    if x["area"]:
        bits.append(f"{x['area']} m²")
    if x["rooms"]:
        bits.append(f"{x['rooms']} kamers")
    if x["available"]:
        bits.append(x["available"])
    return f"{x['title']}, {x['city']} – " + " · ".join(bits)


def notify(cfg: dict, found: list[dict]) -> None:
    """Stuurt de details alleen naar ntfy; de openbare log toont alleen aantallen."""
    if not cfg.get("ntfy_topic"):
        print("Geen NTFY_TOPIC ingesteld; melding niet verstuurd.")
        return
    title = f"🏠 {len(found)} nieuwe woning(en) in {cfg['city']}"
    body = "\n\n".join(f"{describe(x)}\n{x['url']}" for x in found)
    try:
        requests.post(f"https://ntfy.sh/{cfg['ntfy_topic']}", data=body.encode("utf-8"),
                      headers={"Title": title.encode("utf-8"), "Tags": "house",
                               "Priority": "high", "Click": found[0]["url"]},
                      timeout=15).raise_for_status()
        print(f"Melding verstuurd ({len(found)}).")
    except Exception as e:
        print(f"Melding mislukt: {type(e).__name__}")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_once(cfg: dict) -> None:
    try:
        items = fetch(cfg)
    except Exception as e:
        print(f"[{now()}] Ophalen mislukt: {type(e).__name__}")
        return
    hits = [x for x in items if matches(x, cfg)]
    state = load_state()
    new = [x for x in hits if key_of(x["id"]) not in state]
    print(f"[{now()}] {len(items)} items, {len(hits)} passend, {len(new)} nieuw.")
    if new:
        state.extend(key_of(x["id"]) for x in new)
        STATE_FILE.write_text(json.dumps(state, indent=0), encoding="utf-8")
        notify(cfg, new)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="toon alle huidige items")
    ap.add_argument("--test-notify", action="store_true", help="stuur een testmelding")
    args = ap.parse_args()
    cfg = load_config()
    if args.test_notify:
        notify(cfg, [{"title": "Testwoning 1", "city": cfg["city"], "price": 1234, "service": 0,
                      "area": 50, "rooms": 2, "available": "Per direct beschikbaar",
                      "url": cfg["source_url"]}])
    elif args.list:
        for x in sorted(fetch(cfg), key=lambda x: (x["city"], x["price"])):
            print(("✅ " if matches(x, cfg) else "   ") + describe(x))
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()

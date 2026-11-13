"""
Verkkokauppa.com Outlet watcher.

Uses the same data feed the Outlet page itself loads, and sends a Telegram
message when a matching item appears.

Settings (environment variables, set in GitHub - see SETUP.md):
  PRODUCT           what to watch. Best: the normal product link, e.g.
                    https://www.verkkokauppa.com/fi/product/757282/DJI-RC-Pro-kauko-ohjain
                    (or just the product number 757282, or search words)
  ALLOWED_GRADES    optional, e.g. "A,B"  (empty = any condition grade)
  TELEGRAM_TOKEN    bot token from @BotFather                        (secret)
  TELEGRAM_CHAT_ID  your chat id (the first test run tells you this) (secret)
  TEST_MODE         "true" = always report what was found, don't save state
"""

import json
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request

API = "https://web-api.service.verkkokauppa.com"
SITE = "https://www.verkkokauppa.com"
STATE_FILE = pathlib.Path("state.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": SITE,
    "Referer": SITE + "/fi/outlet/yksittaiskappaleet",
}

PRODUCT = os.environ.get("PRODUCT", "").strip()
GRADES = {g.strip().upper() for g in os.environ.get("ALLOWED_GRADES", "").split(",") if g.strip()}
TEST = os.environ.get("TEST_MODE", "").lower() == "true"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


# ---------------------------------------------------------------- HTTP helpers
def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def tg(method, payload=None):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def send(text):
    print("TELEGRAM >", text.replace("\n", " | "))
    tg("sendMessage", {"chat_id": CHAT_ID, "text": text})


def find_chat_id():
    for u in reversed(tg("getUpdates").get("result", [])):
        msg = u.get("message") or u.get("edited_message")
        if msg:
            return str(msg["chat"]["id"])
    return ""


# ---------------------------------------------------------------- Outlet data
def outlet_search(query=None, size=100):
    params = {"private": "true", "pageNo": 0, "pageSize": size,
              "sort": "releaseDate:desc", "lang": "fi", "isCustomerReturn": "true"}
    if query:
        params["query"] = query
    data = get_json(f"{API}/search?{urllib.parse.urlencode(params)}")
    if "products" not in data:
        raise RuntimeError(f"Unexpected answer from the Outlet feed: {str(data)[:200]}")
    return data["products"]


def to_item(p):
    info = p.get("customerReturnsInfo") or {}
    name = info.get("product_name") or p.get("name")
    if isinstance(name, dict):
        name = name.get("fi") or next(iter(name.values()), "")
    normal = (p.get("price") or {}).get("current")
    return {
        "id": str(info.get("id", "")),
        "pid": str(p.get("pid", "")),
        "name": name or "(no name)",
        "grade": (info.get("condition") or "?").upper(),
        "price": info.get("price_with_tax"),
        "normal": normal,
        "note": (info.get("product_extra_info") or "").strip(),
        "url": f"{SITE}/fi/outlet/yksittaiskappaleet/{info.get('id')}",
    }


def parse_product(value):
    """Return (pid, words) from a product link, a product number or search words."""
    m = re.search(r"/product/(\d+)", value) or re.fullmatch(r"(\d+)", value)
    if m:
        return m.group(1), None
    return None, value.lower().split()


def product_name(pid):
    try:
        name = get_json(f"{API}/product/{pid}").get("name")
        return name.get("fi") if isinstance(name, dict) else name
    except Exception as e:
        print("Could not look up product name:", e)
        return None


def short_query(name):
    """The store's search treats '-word' as 'exclude' and long queries get fuzzy,
    so search with the core of the name: 'DJI RC Pro -kauko-ohjain' -> 'DJI RC Pro'."""
    if not name:
        return None
    core = re.split(r"\s-|,|\(", name)[0]
    words = [w for w in re.sub(r"[^\w\s+.\-]", " ", core).split() if not w.startswith("-")]
    return " ".join(words[:3]) or None


def eur(x):
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",") if isinstance(x, (int, float)) else "?"


def fmt(i):
    lines = [f"🔔 Outlet: {i['name']}",
             f"Hinta: {eur(i['price'])} (uutena {eur(i['normal'])})",
             f"Kuntoluokka: {i['grade']}"]
    if i["note"]:
        lines.append(f"Lisätiedot: {i['note']}")
    lines.append(i["url"])
    return "\n".join(lines)


def find_matches():
    pid, words = parse_product(PRODUCT)
    if pid:
        name = product_name(pid)
        print(f"Watching product {pid}: {name}")
        query = short_query(name)
        is_match = lambda i: i["pid"] == pid
    else:
        print(f"Watching for words: {words}")
        query = " ".join(words)
        is_match = lambda i: all(w in i["name"].lower() for w in words)

    # 1) the 100 newest Outlet listings  2) a search for the product itself
    items = {}
    for p in outlet_search():
        i = to_item(p); items[i["id"]] = i
    if query:
        for p in outlet_search(query):
            i = to_item(p); items[i["id"]] = i
    print(f"Checked {len(items)} Outlet listings")

    hits = [i for i in items.values() if is_match(i)]
    if GRADES:
        hits = [i for i in hits if i["grade"] in GRADES or i["grade"] == "?"]
    return hits


# ---------------------------------------------------------------- State
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": [], "failing": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ---------------------------------------------------------------- Main
def main():
    global CHAT_ID
    if not TOKEN:
        sys.exit("TELEGRAM_TOKEN secret is missing.")
    if not CHAT_ID:
        CHAT_ID = find_chat_id()
        if not CHAT_ID:
            sys.exit("No TELEGRAM_CHAT_ID and no recent message to the bot. "
                     "Send your bot any message in Telegram, then run again.")
        send(f"👋 Bot connected. Your chat id is {CHAT_ID}\n"
             "Save it in GitHub as the secret TELEGRAM_CHAT_ID.")
    if not PRODUCT:
        sys.exit("PRODUCT is empty.")

    state = load_state()
    try:
        hits = find_matches()
    except Exception as e:
        print("ERROR:", repr(e))
        if TEST or not state.get("failing"):
            send(f"⚠️ Outlet watcher couldn't check the site:\n{e}")
        if not TEST:
            state["failing"] = True
            save_state(state)
        sys.exit(1)

    print(f"{len(hits)} match(es)")
    if TEST:
        if hits:
            send(f"✅ Test OK – {len(hits)} matching Outlet item(s):")
            for i in hits[:5]:
                send(fmt(i))
        else:
            send(f"✅ Test OK – the Outlet feed works, but nothing matches \"{PRODUCT}\" right now.")
        return

    if state.get("failing"):
        send("✅ Outlet watcher is working again.")
        state["failing"] = False
    seen = set(state["seen"])
    for i in hits:
        if i["id"] not in seen:
            send(fmt(i))
            seen.add(i["id"])
    state["seen"] = sorted(seen)
    save_state(state)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Nutrislice menus -> Gemini report -> Discord. Standard library only."""
from datetime import date, datetime
import html
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

API = "https://wisc-housingdining.api.nutrislice.com"
WEB = "https://wisc-housingdining.nutrislice.com"
OUT = Path("cloud-data")


def fetch(url, data=None, headers=None, retries=5):
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": "NutrisliceDaily/2.0", **(headers or {})}
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            if error.code == 429 or error.code in (500, 502, 503, 504):
                if attempt + 1 < retries:
                    time.sleep(min(60, 5 * (2 ** attempt)))
                    continue
            try:
                message = json.loads(body).get("error", {}).get("message", "")
            except ValueError:
                message = ""
            message = re.sub(r"AIza[0-9A-Za-z_-]+", "[REDACTED]", str(message))[:500]
            raise RuntimeError(f"HTTP {error.code}: {message or 'request rejected'}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt + 1 < retries:
                time.sleep(min(60, 5 * (2 ** attempt)))
                continue
            raise RuntimeError("Network request failed") from None


def get_json(url):
    return json.loads(fetch(url))


def clean(value):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())


def locations():
    url = API + "/menu/api/schools/?format=json"
    result = []
    while url:
        data = get_json(url)
        if isinstance(data, list):
            result.extend(data)
            break
        result.extend(data.get("results", []))
        url = urljoin(API, data["next"]) if data.get("next") else None
    if not result:
        raise RuntimeError("Nutrislice returned no dining halls")
    return sorted(result, key=lambda item: item.get("name", ""))


def menu_api_url(hall, meal, day):
    template = (meal.get("urls") or {}).get("full_menu_by_date_api_url_template")
    if not template:
        template = f"/menu/api/weeks/school/{hall['id']}/menu-type/{meal['id']}/{{year}}/{{month}}/{{day}}"
    return urljoin(API, template.format(year=day.year, month=day.month, day=day.day))


def collect(day, folder):
    sections = [f"<h1>UW–Madison dining menus for {day.isoformat()}</h1>"]
    food_count = 0
    halls = locations()
    for hall in halls:
        hall_name = clean(hall.get("name"))
        hall_slug = hall.get("slug")
        sections.append(f"<section><h2>{html.escape(hall_name)}</h2>")
        meals = hall.get("active_menu_types") or []
        if not meals:
            sections.append("<p>No active menus published.</p>")
        for meal in meals:
            meal_name = clean(meal.get("name"))
            page_url = f"{WEB}/menu/{hall_slug}/{meal.get('slug')}/{day.year}/{day.month}/{day.day}"
            source_name = f"{hall_slug}__{meal.get('slug')}.html"
            try:
                (folder / "source" / source_name).write_bytes(fetch(page_url))
            except RuntimeError:
                pass
            payload = get_json(menu_api_url(hall, meal, day))
            matching = [entry for entry in payload.get("days", []) if entry.get("date") == day.isoformat()]
            if len(matching) != 1:
                raise RuntimeError(f"Nutrislice omitted {hall_name} {meal_name} for {day}")
            selected = matching[0]
            info = selected.get("menu_info") or {}
            rows = selected.get("menu_items") or []
            sections.append(f"<h3>{html.escape(meal_name)}</h3><ul>")
            found = 0
            for row in rows:
                food = row.get("food")
                if not isinstance(food, dict) or not clean(food.get("name")):
                    continue
                nutrition = food.get("rounded_nutrition_info") or {}
                serving = food.get("serving_size_info") or {}
                station = clean((info.get(str(row.get("menu_id")), {}).get("section_options") or {}).get("display_name")) or "General"
                protein = nutrition.get("g_protein")
                protein_text = f"{protein}g" if isinstance(protein, (int, float)) else "unknown"
                serving_text = clean(f"{serving.get('serving_size_amount', '')} {serving.get('serving_size_unit', '')}") or "unknown"
                sections.append(
                    "<li>" + html.escape(clean(food["name"])) +
                    " | station: " + html.escape(station) +
                    " | protein: " + html.escape(protein_text) +
                    " | serving: " + html.escape(serving_text) + "</li>"
                )
                found += 1
                food_count += 1
            if not found:
                sections.append("<li>No food published for this meal.</li>")
            sections.append("</ul>")
        sections.append("</section>")
    combined = "<!doctype html><html><body>\n" + "\n".join(sections) + "\n</body></html>\n"
    (folder / "menus.html").write_text(combined, encoding="utf-8")
    print(f"Collected {food_count} foods from {len(halls)} dining halls")
    return combined


def ask_gemini(day, menu_html, folder):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY secret")
    prompt = Path("prompt.txt").read_text(encoding="utf-8")
    body = {
        "systemInstruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": [{"text": f"Date: {day}\nBEGIN MENU HTML\n{menu_html}\nEND MENU HTML"}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 32768},
    }
    response = json.loads(fetch(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    ))
    try:
        candidate = response["candidates"][0]
        if candidate.get("finishReason") != "STOP":
            raise RuntimeError(f"Gemini stopped with {candidate.get('finishReason', 'unknown reason')}")
        text = "".join(part.get("text", "") for part in candidate["content"]["parts"] if not part.get("thought"))
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Gemini returned no usable text") from None
    if not text.strip():
        raise RuntimeError("Gemini returned empty text")
    (folder / "gemini-output.txt").write_text(text, encoding="utf-8")
    return text


def split_discord(text, limit=1900):
    chunks = []
    while text:
        units = 0
        cut = 0
        for index, char in enumerate(text):
            size = 2 if ord(char) > 0xFFFF else 1
            if units + size > limit:
                break
            units += size
            cut = index + 1
        if cut == len(text):
            chunks.append(text)
            break
        newline = text.rfind("\n", 0, cut)
        if newline > 0:
            cut = newline + 1
        chunks.append(text[:cut])
        text = text[cut:]
    return chunks


def send_discord(text):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    parsed = urlparse(webhook)
    if parsed.scheme != "https" or parsed.netloc != "discord.com" or "/api/webhooks/" not in parsed.path:
        raise RuntimeError("Missing or invalid DISCORD_WEBHOOK_URL secret")
    role = os.environ.get("DISCORD_ROLE_ID", "").strip()
    chunks = split_discord(text)
    for index, chunk in enumerate(chunks):
        content = ((f"<@&{role}>\n" if role and index == 0 else "") + chunk)
        allowed = {"parse": [], "roles": [role] if role and index == 0 else []}
        fetch(webhook + ("&" if "?" in webhook else "?") + "wait=true",
              data=json.dumps({"content": content, "allowed_mentions": allowed}).encode(),
              headers={"Content-Type": "application/json"}, retries=3)
        time.sleep(0.7)
    print(f"Sent Gemini output to Discord in {len(chunks)} message(s)")


def main():
    day = datetime.now(ZoneInfo("America/Chicago")).date()
    if os.environ.get("MENU_DATE"):
        day = date.fromisoformat(os.environ["MENU_DATE"])
    folder = OUT / day.isoformat()
    (folder / "source").mkdir(parents=True, exist_ok=True)
    menu_html = collect(day, folder)
    output = ask_gemini(day, menu_html, folder)
    send_discord(output)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}")
        raise SystemExit(1)

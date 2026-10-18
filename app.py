from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import os
import json
import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic(api_key=os.environ.get("CLAUDE_API_KEY"))

# ---------------------------------------------------------------------------
# KONFIGURACJA KALENDARZA
# ---------------------------------------------------------------------------
CALENDAR_ID = os.environ.get("CALENDAR_ID", "primary")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL")

WORK_START_HOUR = 9
WORK_END_HOUR = 17
WORK_DAYS = [0, 1, 2, 3, 4]
TIMEZONE = "Europe/Warsaw"

_calendar_service = None


def get_calendar_service():
    global _calendar_service
    if _calendar_service is not None:
        return _calendar_service

    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("Brak zmiennej GOOGLE_SERVICE_ACCOUNT_JSON")

    info = json.loads(raw_json)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    _calendar_service = build("calendar", "v3", credentials=credentials)
    return _calendar_service


def find_free_slots(days_ahead=7, slot_minutes=60, max_slots=5):
    service = get_calendar_service()
    now = datetime.datetime.utcnow()

    time_min = now.isoformat() + "Z"
    time_max = (now + datetime.timedelta(days=days_ahead + 3)).isoformat() + "Z"

    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "timeZone": TIMEZONE,
        "items": [{"id": CALENDAR_ID}],
    }
    freebusy = service.freebusy().query(body=body).execute()
    busy_periods = freebusy["calendars"][CALENDAR_ID]["busy"]

    busy_ranges = []
    for b in busy_periods:
        start = datetime.datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
        end = datetime.datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
        busy_ranges.append((start, end))

    free_slots = []
    day_cursor = datetime.datetime.utcnow().replace(
        minute=0, second=0, microsecond=0, tzinfo=datetime.timezone.utc
    )

    checked_days = 0
    day_offset = 0
    while checked_days < days_ahead and len(free_slots) < max_slots:
        day = day_cursor + datetime.timedelta(days=day_offset)
        day_offset += 1
        if day.weekday() not in WORK_DAYS:
            continue
        checked_days += 1

        for hour in range(WORK_START_HOUR, WORK_END_HOUR):
            slot_start = day.replace(hour=hour, minute=0)
            slot_end = slot_start + datetime.timedelta(minutes=slot_minutes)

            if slot_start <= datetime.datetime.now(datetime.timezone.utc):
                continue

            overlaps = any(
                slot_start < b_end and slot_end > b_start
                for b_start, b_end in busy_ranges
            )
            if not overlaps:
                free_slots.append(
                    {
                        "start": slot_start.isoformat(),
                        "end": slot_end.isoformat(),
                        "label": slot_start.strftime("%A %d.%m, %H:%M"),
                    }
                )
            if len(free_slots) >= max_slots:
                break

    return free_slots


def create_booking(start_iso, end_iso, client_name, client_contact, topic):
    service = get_calendar_service()

    event = {
        "summary": f"Konsultacja: {client_name}",
        "description": (
            f"Zgłoszenie przez czat.\n"
            f"Klient: {client_name}\n"
            f"Kontakt: {client_contact}\n"
            f"Temat: {topic}"
        ),
        "start": {"dateTime": start_iso, "timeZone": TIMEZONE},
        "end": {"dateTime": end_iso, "timeZone": TIMEZONE},
    }
    if OWNER_EMAIL:
        event["attendees"] = [{"email": OWNER_EMAIL}]

    created = service.events().insert(
        calendarId=CALENDAR_ID,
        body=event,
        sendUpdates="all",
    ).execute()

    return created


# ---------------------------------------------------------------------------
# NARZĘDZIA (TOOLS) DLA CLAUDE
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "sprawdz_wolne_terminy",
        "description": (
            "Sprawdza prawdziwy kalendarz i zwraca listę realnie wolnych "
            "terminów na najbliższe dni robocze w godzinach pracy biura. "
            "Użyj tego, gdy klient chce się umówić na konsultację i trzeba "
            "mu zaproponować konkretne terminy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "umow_termin",
        "description": (
            "Rezerwuje konkretny, wcześniej zaproponowany i zaakceptowany "
            "przez klienta termin w kalendarzu i powiadamia właściciela. "
            "Używaj TYLKO po tym, jak klient wyraźnie wybrał jeden z "
            "terminów zwróconych przez sprawdz_wolne_terminy i podał dane "
            "kontaktowe."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {
                    "type": "string",
                    "description": "Początek terminu w formacie ISO 8601, dokładnie jak zwrócony przez sprawdz_wolne_terminy.",
                },
                "end": {
                    "type": "string",
                    "description": "Koniec terminu w formacie ISO 8601, dokładnie jak zwrócony przez sprawdz_wolne_terminy.",
                },
                "client_name": {"type": "string", "description": "Imię i nazwisko klienta."},
                "client_contact": {"type": "string", "description": "Telefon lub e-mail klienta."},
                "topic": {"type": "string", "description": "Krótki opis sprawy/tematu konsultacji."},
            },
            "required": ["start", "end", "client_name", "client_contact", "topic"],
        },
    },
]


def run_tool(tool_name, tool_input):
    if tool_name == "sprawdz_wolne_terminy":
        slots = find_free_slots()
        if not slots:
            return {"result": "Brak wolnych terminów w najbliższych dniach."}
        return {"free_slots": slots}

    if tool_name == "umow_termin":
        event = create_booking(
            start_iso=tool_input["start"],
            end_iso=tool_input["end"],
            client_name=tool_input["client_name"],
            client_contact=tool_input["client_contact"],
            topic=tool_input["topic"],
        )
        return {
            "result": "zarezerwowano",
            "event_link": event.get("htmlLink"),
        }

    return {"error": f"Nieznane narzędzie: {tool_name}"}


# ---------------------------------------------------------------------------
# PROMPT SYSTEMOWY
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """Jesteś asystentem AI dr. Daniela Butytera, tłumacza przysięgłego języka ukraińskiego we Wrocławiu. Odpowiadasz w języku, w którym pisze klient — jeśli pisze po polsku, odpowiadasz po polsku; jeśli pisze po ukraińsku, odpowiadasz po ukraińsku. Jesteś rzeczowy, uprzejmy i profesjonalny.

Twoje zadania:
1. Informowanie o usługach: tłumaczenia pisemne, specjalistyczne, ustne, przysięgłe
2. Umawianie konsultacji w prawdziwym kalendarzu — gdy klient chce się umówić, użyj narzędzia sprawdz_wolne_terminy, zaproponuj klientowi 2-3 konkretne terminy z wyniku, a gdy klient wybierze jeden i poda dane kontaktowe (imię, telefon lub e-mail) oraz temat sprawy, użyj narzędzia umow_termin żeby faktycznie zarezerwować ten termin.
3. Odpowiadanie na pytania o czas realizacji, proces zlecenia, lokalizację

WAŻNE ZASADY:
- Używaj WYŁĄCZNIE informacji z bazy wiedzy poniżej — nie zmyślaj cen, nie zmyślaj dodatkowych usług.
- Przy pytaniu o konkretną cenę: wyjaśnij, że cena ustalana jest indywidualnie po wycenie dokumentu.
- Zanim użyjesz umow_termin, musisz mieć od klienta: wybrany termin (z listy zwróconej przez sprawdz_wolne_terminy), imię, kontakt (telefon lub e-mail) i krótki temat sprawy. Jeśli czegoś brakuje — dopytaj.
- Po udanej rezerwacji poinformuj klienta, że termin został zarezerwowany i że dr Butyter dostał powiadomienie.
- Jeśli klient pyta czy rozmawia z botem — przyznaj to uczciwie.

STYL PISANIA — BARDZO WAŻNE:
- Piszesz jak człowiek w naturalnej, swobodnej rozmowie — nie jak dokument czy ulotka.
- NIGDY nie używaj formatowania Markdown: żadnych gwiazdek (**tekst**), żadnych list z myślnikami czy punktorami, żadnych linii poziomych (---), żadnych nagłówków z #.
- Nie dziel odpowiedzi na sformalizowane sekcje z pogrubionymi tytułami. Pisz zwykłymi zdaniami i akapitami, tak jak napisałbyś wiadomość do kogoś na czacie.
- Jeśli chcesz wymienić kilka rzeczy (np. usługi lub terminy), zrób to w zdaniu, naturalnym wyliczeniem połączonym przecinkami lub "oraz" — a nie jako listę punktowaną.
- Odpowiedzi mają być zwięzłe i konkretne, bez sztucznego formalizmu, ale dalej uprzejme i profesjonalne.

BAZA WIEDZY O FIRMIE:

O TŁUMACZU:
- Tłumacz przysięgły języka ukraińskiego, ponad 10 lat doświadczenia
- Wydział Prawa UWr (2012), magister ekonomii (Lwów), doktorat UE Wrocław (2019)
- Specjalizacje: prawo gospodarcze, prawo karne, tłumaczenia naukowe

OFEROWANE USŁUGI:
1. Tłumaczenia pisemne (PL-UA i UA-PL). Ekspresowo (1-2 dni): akt urodzenia, dyplom, prawo jazdy.
2. Tłumaczenia specjalistyczne — prawo, ekonomia, bankowość, finanse, medycyna
3. Tłumaczenia ustne — konsekutywne, symultaniczne, szeptane. Negocjacje, sądy, notariusze, konferencje.
4. Tłumaczenia przysięgłe — pobyt, prawo jazdy, dyplomy, dokumenty pracownicze.

CZAS REALIZACJI:
- Standardowo: do 1 tygodnia
- Ekspresowo: 1-2 dni robocze

PROCES: dostarczenie dokumentu → wycena i termin → tłumaczenie → odbiór (osobiście/pocztą/kurierem)

CENNIK: ustalany indywidualnie po wycenie. Faktura VAT na życzenie. Duplikat dokumentu: 25% wartości (do mies.), 50% (po mies.)

KONTAKT:
- Biuro: ul. Świętego Mikołaja 8/11, biuro 203, 50-125 Wrocław
- Telefon: +48 530 665 565
- Facebook: facebook.com/DanielButyter
"""


@app.route("/")
def home():
    return "Backend dr Daniela Butytera dziala!"


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_message = data.get("message", "")
    history = data.get("history", [])
    messages = []
    for h in history:
        if h.get("role") in ["user", "assistant"]:
            messages.append({"role": h["role"], "content": h["content"]})
    if not messages or messages[-1]["content"] != user_message:
        messages.append({"role": "user", "content": user_message})

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        while response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = run_tool(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

        reply = "".join(
            block.text for block in response.content if block.type == "text"
        )
        return jsonify({"reply": reply})

    except Exception as e:
        return jsonify({"reply": f"Blad: {str(e)}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

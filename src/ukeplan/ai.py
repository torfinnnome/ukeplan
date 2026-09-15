import json
import re
import datetime
from typing import List, Optional
import httpx
import logging
from pydantic import BaseModel, Field
from ukeplan.config import settings
from ukeplan.models import ItemType

logger = logging.getLogger("ukeplan.ai")


class ExtractedItem(BaseModel):
    title: str = Field(description="Kort beskrivelse av aktiviteten, utstyret eller oppgaven (på norsk)")
    description: Optional[str] = Field(default=None, description="Ytterligere detaljer, notater eller instruksjoner (på norsk)")
    item_type: ItemType = Field(default=ItemType.EVENT, description="'event' for planlagte/hendelser med tid, 'gear' for ting som skal huskes/tas med (gymtøy, svømmetøy, uteskole, etc.), 'task' for lekser/frister, 'note' for informasjon, 'middag' for hva som er til middag (person = hvem som lager det)")
    person: Optional[str] = Field(default=None, description="Navnet på barnet eller familiemedlemmet dette gjelder (f.eks. barnets navn eller 'Alle')")
    date: Optional[str] = Field(default=None, description="ISO-dato YYYY-MM-DD KUN hvis dokumentet eksplisitt oppgir en kalenderdato (f.eks. '14. september'), ellers null")
    day_of_week: Optional[int] = Field(default=None, description="1-7 (1=mandag ... 7=søndag) hvis elementet gjelder en bestemt ugedag (f.eks. 'onsdag'), ellers null")
    start_time: Optional[str] = Field(default=None, description="Tid i HH:MM-format hvis aktuelt")
    end_time: Optional[str] = Field(default=None, description="Tid i HH:MM-format hvis aktuelt")
    location: Optional[str] = Field(default=None, description="Sted/plass hvis oppgitt")


class ExtractedWeekPlan(BaseModel):
    week_number: Optional[int] = Field(default=None, description="Ukenummer hvis nevnt (f.eks. uke 37)")
    year: Optional[int] = Field(default=None, description="År hvis kjent")
    summary: Optional[str] = Field(default=None, description="Kort 1-setningssammendrag av planen (på norsk)")
    items: List[ExtractedItem] = Field(default_factory=list, description="Liste over ekstraherte planposter, utstyr og oppgaver")


def infer_person_from_items(items: List["ExtractedItem"]) -> Optional[str]:
    """If all extracted items belong to a single person, that person owns the document."""
    people = {it.person for it in items if it.person and it.person.strip().lower() != "alle"}
    if len(people) == 1:
        return people.pop()
    return None


def resolve_item_date(item: ExtractedItem, plan: ExtractedWeekPlan) -> datetime.date:
    """
    Resolves the actual calendar date for a plan item deterministically (no LLM date math).

    Priority:
    1. Explicit ISO date reported by the LLM (document literally states a date).
    2. Document week (week_number/year) + item weekday (day_of_week 1-7).
    3. Document week -> Monday of that week.
    4. Today (last resort).
    """
    today = datetime.date.today()
    year = plan.year or today.year

    # 1. Explicit date from the document
    if item.date:
        try:
            return datetime.date.fromisoformat(item.date)
        except ValueError:
            logger.warning(f"Ugyldig ISO-dato '{item.date}' for '{item.title}' - bruker uke/ugedag i stedet")

    # 2. Week + weekday
    if plan.week_number and item.day_of_week:
        try:
            dow = min(max(int(item.day_of_week), 1), 7)
            return datetime.date.fromisocalendar(year, plan.week_number, dow)
        except (ValueError, TypeError) as e:
            logger.warning(f"Kunne ikke løse dato for uke {plan.week_number}/{year} dag {item.day_of_week}: {e}")

    # 3. Week only -> Monday
    if plan.week_number:
        try:
            monday = datetime.date.fromisocalendar(year, plan.week_number, 1)
            logger.info(f"Ukedag ikke angitt for '{item.title}' - plasseres på mandag {monday.isoformat()} (uke {plan.week_number})")
            return monday
        except ValueError as e:
            logger.warning(f"Ugyldig ISO-uke {plan.week_number}/{year}: {e}")

    # 4. Last resort
    logger.warning(f"Verken dato, ukedag eller ukenummer for '{item.title}' - bruker i dag ({today.isoformat()})")
    return today
def build_system_prompt(family_members: Optional[List[dict]] = None) -> str:
    now = datetime.date.today()

    member_entries = []
    if family_members:
        for m in family_members:
            name = m.get("name") if isinstance(m, dict) else getattr(m, "name", str(m))
            school_class = m.get("school_class") if isinstance(m, dict) else getattr(m, "school_class", None)
            if school_class:
                member_entries.append(f"{name} ({school_class})")
            else:
                member_entries.append(f"{name}")
    else:
        member_entries = [
            "Ann (1A)",
            "Berit (6B)",
            "Matthew (10C)",
            "Mor",
            "Far",
        ]

    members_inline = ", ".join(member_entries)
    example_person = family_members[0].get("name") if (family_members and isinstance(family_members[0], dict) and family_members[0].get("name")) else "Ann"
    example_person2 = family_members[1].get("name") if (family_members and len(family_members) > 1 and isinstance(family_members[1], dict) and family_members[1].get("name")) else "Berit"

    return f"""Du er en nøyaktig, strukturert assistent for en familiers ukeplan. De registrerte familiemedlemmene og barna er: {members_inline}

Din oppgave: analyse ukeplaner fra skolen, aktivitetsplaner, idrettsplaner og dagsplaner fra tekst, PDF eller bilder. Resultatene dine brukes til å lage en kalender som foreldre kan bruke til å holde orden på oppgaver for uken.

Dine instrukser:

Identifiser ukenummer og år for planen (f.eks. "Uke 36"). Hvis året er utelatt, bruk det nåværende år ({now.year}).
Knytt dokumentet til det riktige barnet basert på klasse/trinn (f.eks. 1B, 1. trinn, 6B, 6. trinn, 10A, 10. trinn), lærernavn, barnets navn eller fagnivå.
Ekstraher alle konkrete ting og kategoriser dem strengt som:
"gear": Ting barnet MÅ huske å ta med (f.eks. gymtøy, skosett, svømmetøy, utstyr til uteskole (sekk/mat/sitteunderlag), innesko, skift, lesebøker, regntøy).
"event": Tidfestede hendelser, treninger, kamper, turer, foreldremøter, tannlege, etterårs-klubber.
"task": Lektier, lesing eller oppgaver med frist (f.eks. les side 20-25, regneark).
"note": Informasjon som planleggingsdager (skolen stengt), tema-uker, eller viktige beskjeder.
"middag": Hva som er til middag (f.eks. lasagne, biff, boller). Sett "person" til familiemedlemmet som har ansvaret for å lage det, hvis det står noe om hvem som lager.
Tildel hvert element til det aktuelle barnet eller familiemedlemmet (f.eks. '{example_person}', '{example_person2}').

Ofte ligger det en timeplan i dokumentet, i tabellform. Noen ganger er overskriftene i tabellen vanskelige å tyde, men det er over hvit skrift på svart bakgrunn. VIKTIG: Prøv hardt å plassere hendelser på riktig dag.

VIKTIG: IGNORER læringsmål, sosialt mål og andre mål for uka eller perioden.

VIKTIG: IGNORER lekser.

VIKTIG: Aldri legg til to identiske hendelser, på samme dag.

DATO-REGLER (veldig viktig - følg nøyaktig!):
Beregn ALDRI kalenderdatoer selv. Du gjetter aldri datoer. Systemet fyller inn riktig dato ut fra ukenummer og ukedag.
"date": KUN hvis dokumentet eksplisitt inneholder en kalenderdato (f.eks. "14. september" eller "9/14"). Da konverter til ISO-format YYYY-MM-DD. Ellers MÅ "date" være null.
"day_of_week": 1-7 (1=mandag, 2=tirsdag, 3=onsdag, 4=torsdag, 5=fredag, 6=lørdag, 7=søndag) hvis elementet gjelder en bestemt ukedag (f.eks. "onsdag", "tirsdag og torsdag" -> lag da ÉN oppføring per dag med riktig day_of_week).
Hvis elementet gjelder hele uken (f.eks. påminnelse om innesko eller skift), sett både "date" og "day_of_week" til null.
Merk: Et element med en eksplisitt dato kan ligge i ANNEN uke enn dokumentets uke (f.eks. foreldremøte neste uke nevnt i ukeplanen). Behold alltid den eksplisitte datoen.
Svar alltid på norsk. Bruk gjerne dokumentets egne ord for titler og beskrivelser.

Du MÅ svare KUN med et gyldig JSON-objekt som samsvarer med dette skemaet:
{{{{
  "week_number": 36,
  "year": {now.year},
  "summary": "Ukeplan for 1B, uke 36",
  "items": [
    {{{{
      "title": "Gymtøy og skosett",
      "description": "Husk gymsaker til 3. time",
      "item_type": "gear",
      "person": "{example_person2}",
      "date": null,
      "day_of_week": 3,
      "start_time": null,
      "end_time": null,
      "location": "Idrettshall"
    }}}},
    {{{{
      "title": "Foreldremøte",
      "description": "Samling i auditoriet, deretter klassevis",
      "item_type": "event",
      "person": "{example_person}",
      "date": "{now.year}-09-08",
      "day_of_week": null,
      "start_time": "17:00",
      "end_time": null,
      "location": "Auditorium"
    }}}}
  ]
}}}}
Svar KUN med rått JSON uten Markdown-formattering eller ekstra tekst.
"""
def clean_json_response(raw: str) -> dict:
    """Strip markdown code blocks or stray formatting and parse JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    # Find outer JSON brackets if model emitted extra commentary
    match = re.search(r"(\{.*\})", raw, re.DOTALL)
    if match:
        raw = match.group(1)
    return json.loads(raw)


async def _llm_chat(
    client: httpx.AsyncClient,
    system_text: str,
    user_text: str,
) -> str:
    """
    One chat turn against the configured AI engine (OpenAI-compatible server or Ollama).
    Returns the raw model content.
    """
    logger.info(f"Sending extraction request to AI engine: {settings.ai_engine}")

    if settings.ai_engine == "ollama":
        ollama_url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        logger.info(f"Ollama URL: {ollama_url} | Model: {settings.ollama_model}")
        payload = {
            "model": settings.ollama_model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            "format": "json",
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_ctx": 4096,
            },
        }
        resp = await client.post(ollama_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "{}")

    base_url = settings.ai_base_url.rstrip("/")
    url = f"{base_url}/chat/completions"
    logger.info(f"OpenAI-compatible URL: {url} | Model: {settings.ai_model}")
    headers = {}
    if settings.ai_api_key and settings.ai_api_key != "not-needed":
        headers["Authorization"] = f"Bearer {settings.ai_api_key}"
    payload = {
        "model": settings.ai_model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    resp = await client.post(url, headers=headers, json=payload)
    logger.info(f"AI response status: {resp.status_code}")
    if resp.status_code != 200:
        logger.error(f"AI request failed with status {resp.status_code}: {resp.text}")
        raise ValueError(f"AI API error ({resp.status_code}): {resp.text}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _finalize_plan(raw_content: str) -> ExtractedWeekPlan:
    """Parse and log the raw model JSON as an ExtractedWeekPlan."""
    logger.info(f"Raw AI JSON response:\n{raw_content}")
    parsed_data = clean_json_response(raw_content)
    plan = ExtractedWeekPlan.model_validate(parsed_data)
    logger.info(f"Successfully parsed plan: week={plan.week_number}, year={plan.year}, summary='{plan.summary}', items_count={len(plan.items)}")
    for idx, it in enumerate(plan.items):
        logger.info(f"  Item [{idx+1}]: {it.title} | Type: {it.item_type} | Person: {it.person} | Date: {it.date} | Time: {it.start_time}-{it.end_time}")
    return plan


async def parse_plan_text_with_llm(
    text: str,
    target_person: Optional[str] = None,
    family_members: Optional[List[dict]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> ExtractedWeekPlan:
    """
    Sends extracted plan text to LLM (OpenAI-compatible server or Ollama) to parse into structured ExtractedWeekPlan.
    """
    system_text = build_system_prompt(family_members=family_members)
    user_prompt = f"Her er innholdet fra ukeplan-dokumentet:\n---\n{text}\n---\n"
    if target_person:
        user_prompt += f"Merk: Brukeren har spesifisert at denne planen tilhører: {target_person}\n"
    user_prompt += "Ekstraher alle hendelser, nødvendig utstyr ('gear') og lekser/oppgaver ('task') til det påkrevde JSON-formatet."
    logger.info(f"Target document text length: {len(text)} chars | Target person: {target_person}")

    should_close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=120.0)
        should_close_client = True

    try:
        raw_content = await _llm_chat(client, system_text, user_prompt)
        return _finalize_plan(raw_content)

    except Exception as e:
        logger.exception(f"Error communicating with AI or parsing response: {e}")
        raise

    finally:
        if should_close_client:
            await client.aclose()




# ---------------------------------------------------------------------------
# Natural-language commands (f.eks. "Sigrid har sjakktrening hver onsdag 18:00-19:50")
# ---------------------------------------------------------------------------


class CommandItem(BaseModel):
    title: str = Field(description="Kort tittel på norsk (f.eks. 'Sjakktrening')")
    description: Optional[str] = Field(default=None, description="Ytterligere detaljer på norsk")
    item_type: ItemType = Field(default=ItemType.EVENT, description="'event' for hendelser/treninger, 'task' for lekser/oppgaver, 'gear' for ting som skal tas med, 'note' for informasjon, 'middag' for hva som er til middag (person = hvem som lager det)")
    person: Optional[str] = Field(default=None, description="Navnet på familiemedlemmet dette gjelder, hvis nevnt")
    date: Optional[str] = Field(default=None, description="ISO-dato YYYY-MM-DD KUN hvis kommandoen eksplisitt oppgir en kalenderdato (f.eks. '12. september'), ellers null")
    day_of_week: Optional[int] = Field(default=None, description="1-7 (1=mandag ... 7=søndag) hvis en ukedag nevnes (f.eks. 'onsdag'), ellers null")
    start_time: Optional[str] = Field(default=None, description="Starttid HH:MM hvis oppgitt")
    end_time: Optional[str] = Field(default=None, description="Sluttid HH:MM hvis oppgitt")
    location: Optional[str] = Field(default=None, description="Sted hvis oppgitt")
    recurring_weekly: bool = Field(default=False, description="true hvis aktiviteten gjentas hver uke ('hver onsdag', 'all fredag'), false ellers")


class CommandResult(BaseModel):
    summary: str = Field(description="Kort 1-setningssammendrag på norsk av hva som ble lagt til (eller hvorfor ingenting ble lagt til)")
    items: List[CommandItem] = Field(default_factory=list)


def next_weekday_date(day_of_week: int, on_or_after: Optional[datetime.date] = None) -> datetime.date:
    """Next date >= on_or_after (default: today) that matches day_of_week (1=Monday..7=Sunday)."""
    base = on_or_after or datetime.date.today()
    target = min(max(int(day_of_week), 1), 7)
    offset = (target - 1) - base.weekday()  # both 0=Monday based
    return base + datetime.timedelta(days=offset % 7)


def build_command_prompt(family_members: Optional[List[dict]] = None) -> str:
    now = datetime.date.today()
    member_entries = []
    if family_members:
        for m in family_members:
            name = m.get("name") if isinstance(m, dict) else getattr(m, "name", str(m))
            school_class = m.get("school_class") if isinstance(m, dict) else getattr(m, "school_class", None)
            member_entries.append(f"{name} ({school_class})" if school_class else name)
    else:
        member_entries = ["Ann (1A)", "Berit (6B)", "Matthew (10C)", "Mor", "Far"]
    members_inline = ", ".join(member_entries)
    weekday_names = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]

    return f"""Du er en assistent for familiens ukeplan. Registrerte familiemedlemmer: {members_inline}
I dag er {now.isoformat()} ({weekday_names[now.isoweekday() - 1]}), uke {now.isocalendar()[1]}, {now.year}.

Oppgave: tolk korte familiekommandoer (norsk eller engelsk) og konverter dem til strukturerte kalenderposter.

Eksempler:
- "Sigrid har sjakktrening hver onsdag 18:00-19:50" -> étt event: title="Sjakktrening", person="Sigrid", day_of_week=3, start_time="18:00", end_time="19:50", recurring_weekly=true, date=null.
- "Fotballtrening fredag 17:30" (uten "hver") -> ett engangsevent på den kommende fredagen: day_of_week=5, date=null, recurring_weekly=false.
- "Berit har tannlege 12. september kl 09:30" -> date="{now.year}-09-12" (nåværende år med mindre annet år nevnes), recurring_weekly=false.
- "Middag i morgen er lasagne" (eller "Far lager lasagne i kveld") -> ett middag-item: item_type="middag", title="Lasagne", person="Far" hvis en som lager det nevnes, recurring_weekly=false (dato: i kveld -> date=null, i morgen -> day_of_week for morgendagen).

Regler:
- "person": match til en av de registrerte familiemedlemmene hvis et navn nevnes; ellers null.
- "recurring_weekly": true KUN hvis kommandoen sier "hver"/"all"/"every" + ukedag. Da MÅ "day_of_week" settes.
- "date": KUN hvis en eksplisitt kalenderdato er nevnt (f.eks. "12. september"), som ISO YYYY-MM-DD. Anta nåværende år ({now.year}) med mindre annet år er nevnt. Ellers null.
- "day_of_week": 1-7 (1=mandag, 2=tirsdag, 3=onsdag, 4=torsdag, 5=fredag, 6=lørdag, 7=søndag) hvis en ukedag nevnes, ellers null.
- "i morgen"/"tomorrow": beregn den konkrete ukedagen ut fra i dag ({now.isoweekday()} = {weekday_names[now.isoweekday() - 1]}) og sett "day_of_week" til morgendagens ukedag (eller ISO-dato hvis du er sikker på den).

- Tider i HH:MM-format. "18:00-19:50" gir start_time="18:00", end_time="19:50".
- Hvis kommandoen IKKE handler om å planlegge noe (spørsmål, smalltalk, uforståelig), returner items=[] og en kort norsk setning i "summary" som forklarer dette.
- "summary": én kort setning på norsk som beskriver hva som blir lagt til (eller hvorfor ingenting legges til).

Du MÅ svare KUN med et gyldig JSON-objekt i dette formatet:
{{{{
  "summary": "La til sjakktrening for Sigrid hver onsdag 18:00-19:50.",
  "items": [
    {{{{
      "title": "Sjakktrening",
      "description": null,
      "item_type": "event",
      "person": "Sigrid",
      "date": null,
      "day_of_week": 3,
      "start_time": "18:00",
      "end_time": "19:50",
      "location": null,
      "recurring_weekly": true
    }}}}
  ]
}}}}
Svar KUN med rått JSON uten Markdown-formattering eller ekstra tekst.
"""


async def parse_command_with_llm(
    text: str,
    family_members: Optional[List[dict]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> CommandResult:
    """Parse a free-text family command into structured calendar items via the LLM."""
    system_text = build_command_prompt(family_members=family_members)
    user_prompt = f"Kommando:\n---\n{text}\n---\n"
    logger.info(f"Command parse request: '{text}'")

    should_close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=120.0)
        should_close_client = True

    try:
        raw_content = await _llm_chat(client, system_text, user_prompt)
        logger.info(f"Raw command JSON response:\n{raw_content}")
        parsed_data = clean_json_response(raw_content)
        result = CommandResult.model_validate(parsed_data)
        for idx, it in enumerate(result.items):
            logger.info(
                f"  Command item [{idx+1}]: {it.title} | Type: {it.item_type} | Person: {it.person} | "
                f"Date: {it.date} | DOW: {it.day_of_week} | Time: {it.start_time}-{it.end_time} | Weekly: {it.recurring_weekly}"
            )
        return result
    except Exception as e:
        logger.exception(f"Error parsing command '{text}': {e}")
        raise
    finally:
        if should_close_client:
            await client.aclose()

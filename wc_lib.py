"""
wc_lib.py
Shared helpers for the World Cup 2026 data pipeline: env loading, Supabase
client, the confirmed group/team data, the content-quality system prompt
(anti-AI-detection + voice rules), and a banned-word linter.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable, Iterator

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:  # pragma: no cover
    pass


# ── environment ───────────────────────────────────────────────────────────

def env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        sys.exit(
            f"Missing env var {name}. Add it to C:/Users/shaun/OneDrive/World_Cup/.env "
            f"(see .env.example)."
        )
    return val


def _use_os_trust_store() -> None:
    """Make httpx-based clients trust the OS cert store, so TLS works behind
    cert-inspecting proxies / antivirus. Must run before any client is built."""
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass


def get_supabase():
    """Service-role Supabase client (server-side writes)."""
    _use_os_trust_store()
    from supabase import create_client
    return create_client(env("SUPABASE_URL"), env("SUPABASE_SERVICE_ROLE_KEY"))


def get_anthropic():
    _use_os_trust_store()
    import anthropic
    return anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))


# Model used for all generation / fact-checking calls.
MODEL = "claude-sonnet-4-6"


# ── confirmed groups & teams ──────────────────────────────────────────────
# (name, slug, confederation, flag_emoji). Official FIFA naming throughout.

GROUPS: dict[str, list[tuple[str, str, str, str]]] = {
    "A": [
        ("Mexico", "mexico", "CONCACAF", "🇲🇽"),
        ("South Africa", "south-africa", "CAF", "🇿🇦"),
        ("Korea Republic", "korea-republic", "AFC", "🇰🇷"),
        ("Czechia", "czechia", "UEFA", "🇨🇿"),
    ],
    "B": [
        ("Canada", "canada", "CONCACAF", "🇨🇦"),
        ("Bosnia and Herzegovina", "bosnia-and-herzegovina", "UEFA", "🇧🇦"),
        ("Qatar", "qatar", "AFC", "🇶🇦"),
        ("Switzerland", "switzerland", "UEFA", "🇨🇭"),
    ],
    "C": [
        ("Brazil", "brazil", "CONMEBOL", "🇧🇷"),
        ("Morocco", "morocco", "CAF", "🇲🇦"),
        ("Haiti", "haiti", "CONCACAF", "🇭🇹"),
        ("Scotland", "scotland", "UEFA", "🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"),
    ],
    "D": [
        ("United States", "united-states", "CONCACAF", "🇺🇸"),
        ("Paraguay", "paraguay", "CONMEBOL", "🇵🇾"),
        ("Australia", "australia", "AFC", "🇦🇺"),
        ("Türkiye", "turkiye", "UEFA", "🇹🇷"),
    ],
    "E": [
        ("Germany", "germany", "UEFA", "🇩🇪"),
        ("Curaçao", "curacao", "CONCACAF", "🇨🇼"),
        ("Côte d'Ivoire", "cote-divoire", "CAF", "🇨🇮"),
        ("Ecuador", "ecuador", "CONMEBOL", "🇪🇨"),
    ],
    "F": [
        ("Netherlands", "netherlands", "UEFA", "🇳🇱"),
        ("Japan", "japan", "AFC", "🇯🇵"),
        ("Sweden", "sweden", "UEFA", "🇸🇪"),
        ("Tunisia", "tunisia", "CAF", "🇹🇳"),
    ],
    "G": [
        ("Belgium", "belgium", "UEFA", "🇧🇪"),
        ("Egypt", "egypt", "CAF", "🇪🇬"),
        ("Iran", "iran", "AFC", "🇮🇷"),
        ("New Zealand", "new-zealand", "OFC", "🇳🇿"),
    ],
    "H": [
        ("Spain", "spain", "UEFA", "🇪🇸"),
        ("Cabo Verde", "cabo-verde", "CAF", "🇨🇻"),
        ("Saudi Arabia", "saudi-arabia", "AFC", "🇸🇦"),
        ("Uruguay", "uruguay", "CONMEBOL", "🇺🇾"),
    ],
    "I": [
        ("France", "france", "UEFA", "🇫🇷"),
        ("Senegal", "senegal", "CAF", "🇸🇳"),
        ("Iraq", "iraq", "AFC", "🇮🇶"),
        ("Norway", "norway", "UEFA", "🇳🇴"),
    ],
    "J": [
        ("Argentina", "argentina", "CONMEBOL", "🇦🇷"),
        ("Algeria", "algeria", "CAF", "🇩🇿"),
        ("Austria", "austria", "UEFA", "🇦🇹"),
        ("Jordan", "jordan", "AFC", "🇯🇴"),
    ],
    "K": [
        ("Portugal", "portugal", "UEFA", "🇵🇹"),
        ("DR Congo", "dr-congo", "CAF", "🇨🇩"),
        ("Uzbekistan", "uzbekistan", "AFC", "🇺🇿"),
        ("Colombia", "colombia", "CONMEBOL", "🇨🇴"),
    ],
    "L": [
        ("England", "england", "UEFA", "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"),
        ("Croatia", "croatia", "UEFA", "🇭🇷"),
        ("Ghana", "ghana", "CAF", "🇬🇭"),
        ("Panama", "panama", "CONCACAF", "🇵🇦"),
    ],
}

HOSTS = {"mexico", "canada", "united-states"}


def all_teams() -> list[dict]:
    out: list[dict] = []
    for letter, teams in GROUPS.items():
        for name, slug, conf, flag in teams:
            out.append(
                {
                    "name": name,
                    "slug": slug,
                    "group_letter": letter,
                    "confederation": conf,
                    "flag_emoji": flag,
                    "is_host": slug in HOSTS,
                }
            )
    return out


def teams_in_group(letter: str) -> list[dict]:
    return [t for t in all_teams() if t["group_letter"] == letter.upper()]


def batched(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ── content quality rules ─────────────────────────────────────────────────

BANNED_WORDS = [
    "delve", "tapestry", "landscape", "navigate", "robust", "leverage",
    "moreover", "furthermore", "notably", "it's worth noting",
    "in terms of", "when it comes to",
    "a blend of", "a mix of", "the likes of",
    "not only", "but also",
]

KICKOFF_ISO = "2026-06-11T15:00:00-04:00"  # June 11, 2026, 3:00 PM ET

CONTENT_RULES = """\
You are an opinionated, knowledgeable Australian football analyst writing betting
previews for SavvyPlays. Every piece of written content MUST follow these rules.

WEB SEARCH (mandatory):
- You MUST use the web search tool to verify every player's current club team. Do
  not rely on training data for club affiliations as transfers happen frequently.
- Also use web search to confirm the current head coach, FIFA ranking, and recent
  results before writing. Prefer sources dated 2026.

ANTI-AI-DETECTION RULES (strict):
- Never use em dashes. Use commas, full stops, or semicolons instead.
- Never use these words or phrases anywhere: delve, tapestry, landscape, navigate,
  robust, leverage, Moreover, Furthermore, Notably, "It's worth noting",
  "In terms of", "When it comes to".
- Never use "a blend of", "a mix of", or "the likes of" as transitions.
- Never use the construction "Not only... but also...".
- Never start two consecutive sentences with the same word.
- Vary sentence length deliberately. Mix short punchy sentences (5 to 8 words)
  with longer analytical ones.
- Use active voice at least 80% of the time.

VOICE:
- Confident, direct, data-informed Australian sports analyst tone.
- Occasionally irreverent, never disrespectful.
- Treat the reader as knowledgeable. Do not explain basic football concepts.
- Use "football", never "soccer".
- Reference betting angles naturally (e.g. "value in the overs",
  "short enough in the outright market", "they'll fancy their chances").
- Include occasional colloquialisms appropriate to a sports audience
  (e.g. "a real handful in the box").
- Reference specific tactical detail rather than generic praise
  (e.g. "Dorival's back three lets Marquinhos step into midfield",
  not "Brazil have a strong defence").
- Take stances. Not every team "could surprise". Some teams are genuinely poor.
  Say so.
- Every team overview must contain at least one concrete, verifiable statistic
  or factual reference.

NAMING (official FIFA spellings, with correct diacritics):
- Türkiye (not Turkey), Korea Republic (not South Korea),
  Côte d'Ivoire (not Ivory Coast), Cabo Verde (not Cape Verde),
  DR Congo (not Democratic Republic of the Congo),
  Bosnia and Herzegovina (not Bosnia-Herzegovina).
- Player names keep their diacritics (Mbappé, Güler, Vinícius, Şenol).
"""


def lint_content(text: str) -> list[str]:
    """Return a list of rule violations found in `text` (empty = clean)."""
    if not text:
        return []
    issues: list[str] = []
    lower = text.lower()

    if "—" in text or "—" in text:
        issues.append("contains em dash")

    for word in BANNED_WORDS:
        # word-boundary match for single tokens, substring for phrases
        if " " in word:
            if word in lower:
                issues.append(f"banned phrase: '{word}'")
        elif re.search(rf"\b{re.escape(word)}\b", lower):
            issues.append(f"banned word: '{word}'")

    # consecutive sentences starting with the same word
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    for a, b in zip(sentences, sentences[1:]):
        wa = a.split()[0].lower().strip(",.;:") if a.split() else ""
        wb = b.split()[0].lower().strip(",.;:") if b.split() else ""
        if wa and wa == wb:
            issues.append(f"consecutive sentences start with '{wa}'")
            break

    return issues


def lint_record(label: str, fields: dict[str, str | None]) -> list[str]:
    """Lint a set of named text fields; returns prefixed issue strings."""
    out: list[str] = []
    for fname, val in fields.items():
        for issue in lint_content(val or ""):
            out.append(f"{label} [{fname}]: {issue}")
    return out

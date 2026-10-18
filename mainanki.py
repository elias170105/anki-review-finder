#!/usr/bin/env python3
"""Anki Review-Finder CLI (TypeSafe Jev & AnkiConnect).

Identifiziert gezielt revisionsbedürftige Karten über deterministische
System-1-Entscheidungen, ohne den Karteninhalt zu verändern.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import math
import os
import random
import re
import sys
import getpass
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import aiohttp

# NEU: Import des ausgelagerten Report-Generators
from report_generator import AnkiReportGenerator

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

VERSION = "v0.2.0-beta"
PRICE_PER_M_INPUT_TOKENS = 0.042 
TAG_PREFIX = "jev"  # KORREKTUR: Fehlende Konstante ergänzt

FRONT_CANDIDATES = ["front", "frage", "vorderseite", "text", "aufgabe", "question"]
BACK_CANDIDATES = ["back", "antwort", "rückseite", "rueckseite", "lösung", "loesung", "extra", "answer"]


@dataclass
class RunMetrics:
    total_notes_found: int = 0
    cards_evaluated: int = 0
    cards_skipped: int = 0
    retries_429: int = 0
    measured_input_tokens: int = 0
    estimated_input_tokens: int = 0
    has_unmetered_calls: bool = False
    total_clarity_score: float = 0.0  # NEU: Für die Berechnung der Durchschnittsklarheit
    latencies: list[float] = field(default_factory=list)
    flag_stats: dict[str, int] = field(default_factory=dict)
    prio_stats: dict[str, int] = field(
        default_factory=lambda: {"high": 0, "medium": 0, "low": 0, "clean": 0}
    )

    def add_flag(self, flag: str) -> None:
        self.flag_stats[flag] = self.flag_stats.get(flag, 0) + 1

    def add_prio(self, prio: str) -> None:
        self.prio_stats[prio] = self.prio_stats.get(prio, 0) + 1


def clean_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    return " ".join(text.split()).strip()


def estimate_tokens_fallback(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def extract_card_fields(
    note_fields: dict[str, dict[str, Any]],
    custom_front: str | None = None,
    custom_back: str | None = None,
) -> tuple[str, str, str | None]:
    field_keys = list(note_fields.keys())
    if len(field_keys) < 2:
        return "", "", "Weniger als zwei Felder vorhanden."

    if custom_front and custom_back:
        if custom_front in note_fields and custom_back in note_fields:
            return (
                clean_html(note_fields[custom_front]["value"]),
                clean_html(note_fields[custom_back]["value"]),
                None,
            )
        return ("", "", f"Vorgegebene Felder ('{custom_front}', '{custom_back}') nicht in Notiz gefunden.")

    lower_map = {k.lower(): k for k in field_keys}
    matched_front = next((lower_map[c] for c in FRONT_CANDIDATES if c in lower_map), None)
    matched_back = next((lower_map[c] for c in BACK_CANDIDATES if c in lower_map and lower_map[c] != matched_front), None)

    if matched_front and matched_back:
        return (clean_html(note_fields[matched_front]["value"]), clean_html(note_fields[matched_back]["value"]), None)

    f_val = clean_html(note_fields[field_keys[0]]["value"])
    b_val = clean_html(note_fields[field_keys[1]]["value"])
    return f_val, b_val, f"Heuristik griff nicht. Nutze Felder '{field_keys[0]}' und '{field_keys[1]}'."


async def anki_call(session: Any, anki_url: str, action: str, **params: Any) -> Any:
    def _sync_request() -> Any:
        payload = json.dumps({"action": action, "version": 6, "params": params}).encode("utf-8")
        req = urllib.request.Request(anki_url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("error"):
                raise RuntimeError(f"AnkiConnect Fehler: {data['error']}")
            return data["result"]
    return await asyncio.to_thread(_sync_request)


def build_jev_payload(front: str, back: str) -> dict[str, Any]:
    return {
        "model": "jev-latest",
        "state": f"F: {front}\nA: {back}",
        "questions": {
            "aggregation_trap": {"type": "noul", "instructions": "The question is an open-ended aggregation demand..."},
            "scope_bloat": {"type": "noul", "instructions": "Answer contains excessive trivia..."},
            "scope_underkill": {"type": "noul", "instructions": "Answer is an incomplete single fragment..."},
            "is_binary": {"type": "noul", "instructions": "Question can be answered with a simple binary choice..."},
            "leak": {"type": "noul", "instructions": "Front inadvertently reveals or strongly hints at answer..."},
            "orphan": {"type": "noul", "instructions": "Card lacks essential domain context..."},
            "clarity": {
                "type": "score",
                "instructions": "Rate the clarity, precision, and unambiguity of the question and answer.",
                "criteria": ["1 - Very vague", "2 - Flawed", "3 - Acceptable", "4 - Clear", "5 - Highly precise"]
            },
            "knowledge_type": {
                "type": "choice",
                "instructions": "Classify the flashcard...",
                "criteria": {"FACT_DEFINITION": "...", "NUMERIC": "...", "PROCEDURE": "...", "REGULATION": "...", "ANATOMY": "...", "OTHER": "..."}
            },
        },
    }


def evaluate_decisions(result: dict[str, Any]) -> tuple[list[str], str, str | None]:
    flags: list[str] = []
    high_sev_count = 0
    med_sev_count = 0
    has_observation = False

    def process_noul(metric_key: str, flag_name: str, is_high_severity: bool) -> None:
        nonlocal high_sev_count, med_sev_count, has_observation
        val = float(result.get(metric_key, {}).get("noul", 0.0))
        if val >= 0.80:
            flags.append(f"{TAG_PREFIX}::flag::{flag_name}")
            if is_high_severity:
                high_sev_count += 1
            else:
                med_sev_count += 1
        elif 0.60 <= val < 0.80:
            has_observation = True

    process_noul("scope_underkill", "scope_underkill", True)
    process_noul("leak", "leak", True)
    process_noul("orphan", "kontextlos", True)
    process_noul("aggregation_trap", "sammelkarte", False)
    process_noul("scope_bloat", "scope_bloat", False)
    process_noul("is_binary", "binaerfrage", False)

    raw_score = float(result.get("clarity", {}).get("score", 4.0))
    clarity_score = raw_score + 1.0
    if clarity_score <= 3.0:
        flags.append(f"{TAG_PREFIX}::flag::unklar")
        med_sev_count += 1
    elif clarity_score < 4.0:
        has_observation = True

    k_type = result.get("knowledge_type", {}).get("choice")
    type_tag = f"{TAG_PREFIX}::typ::{k_type.lower()}" if k_type and k_type != "OTHER" else None

    if high_sev_count >= 1 or med_sev_count >= 2:
        priority = "high"
    elif med_sev_count == 1:
        priority = "medium"
    elif has_observation:
        priority = "low"
    else:
        priority = "clean"

    return flags, priority, type_tag


async def evaluate_card_task(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    api_url: str,
    api_key: str,
    note: dict[str, Any],
    card_info: dict[str, Any],  # NEU: Nimmt die AnkiConnect cardInfo entgegen
    metrics: RunMetrics,
    custom_front: str | None,
    custom_back: str | None,
) -> tuple[int, list[str]] | None:
    note_id = note["noteId"]
    note_type = note.get("modelName", "Unbekannt")
    front, back, warn = extract_card_fields(note.get("fields", {}), custom_front, custom_back)

    if not front or not back:
        metrics.cards_skipped += 1
        return None

    payload = build_jev_payload(front, back)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    max_retries = 4
    attempt = 0

    async with sem:
        while attempt <= max_retries:
            t_start = time.perf_counter()
            try:
                async with session.post(api_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    latency = time.perf_counter() - t_start

                    if resp.status == 429:
                        metrics.retries_429 += 1
                        attempt += 1
                        if attempt > max_retries:
                            return None
                        retry_after = resp.headers.get("Retry-After")
                        delay = float(retry_after) + random.uniform(0.1, 0.4) if retry_after else (2**attempt) + random.uniform(0.1, 1.0)
                        await asyncio.sleep(delay)
                        continue

                    if resp.status != 200:
                        return None

                    data = await resp.json()
                    metrics.latencies.append(latency)

                    usage = data.get("usage", {})
                    input_toks = usage.get("input_tokens") or usage.get("prompt_tokens")
                    if input_toks:
                        metrics.measured_input_tokens += int(input_toks)
                    else:
                        metrics.has_unmetered_calls = True
                        metrics.estimated_input_tokens += estimate_tokens_fallback(json.dumps(payload))

                    flags, priority, type_tag = evaluate_decisions(data.get("answers", data))
                    tags_to_apply: list[str] = []

                    if priority != "clean":
                        tags_to_apply.extend(flags)
                        tags_to_apply.append(f"{TAG_PREFIX}::prio::{priority}")
                        for f in flags:
                            metrics.add_flag(f.replace(f"{TAG_PREFIX}::flag::", ""))

                    if type_tag:
                        tags_to_apply.append(type_tag)

                    metrics.add_prio(priority)
                    metrics.cards_evaluated += 1

                    answers = data.get("answers", data)
                    metrics_data = {k: float(answers.get(k, {}).get("noul", 0.0)) for k in ["aggregation_trap", "scope_bloat", "scope_underkill", "is_binary", "leak", "orphan"]}
                    clarity_val = float(answers.get("clarity", {}).get("score", 4.0)) + 1.0
                    metrics_data["clarity"] = clarity_val
                    metrics.total_clarity_score += clarity_val

                    # NEU: Aufbau exakt nach den Anforderungen des Report-Generators
                    return {
                        "note_id": note_id,
                        "note_type": note_type,
                        "front": front,
                        "back": back,
                        "priority": priority,
                        "flags": flags,
                        "tags": tags_to_apply,
                        "metrics": metrics_data,
                        "anki_info": {
                            "deckName": card_info.get("deckName", "-"),
                            "factor": card_info.get("factor", 0),
                            "interval": card_info.get("interval", 0)
                        }
                    }

            except (aiohttp.ClientError, asyncio.TimeoutError):
                attempt += 1
                if attempt > max_retries:
                    return None
                await asyncio.sleep((2**attempt) + random.uniform(0.1, 0.5))
    return None


def print_summary_report(metrics: RunMetrics, duration: float, dry_run: bool) -> None:
    latencies = sorted(metrics.latencies)
    n = len(latencies)
    p50 = latencies[int(n * 0.50)] * 1000 if n else 0.0
    p95 = latencies[int(n * 0.95)] * 1000 if n else 0.0
    throughput = metrics.cards_evaluated / duration if duration > 0 else 0.0
    total_tokens = metrics.measured_input_tokens + metrics.estimated_input_tokens
    cost_usd = (total_tokens / 1_000_000) * PRICE_PER_M_INPUT_TOKENS

    print("\n" + "=" * 70)
    print(f"            ANKI REVIEW-FINDER REPORT ({VERSION})")
    print("=" * 70)
    print(f"Erfolgreich evaluiert:       {metrics.cards_evaluated}")
    print(f"Berechnete API-Kosten:       {cost_usd:.4f} $")
    print("=" * 70 + "\n")


async def run_linter(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY")
    
    # Check if API key is missing and prompt the user automatically
    if not api_key:
        print("\n[Info] Es wurde keine .env Datei mit einem API-Key gefunden.")
        
        # Use getpass to securely hide the user input in the terminal
        user_input = getpass.getpass("Bitte füge deinen TypeSafe API-Key hier ein (Eingabe bleibt unsichtbar) und drücke Enter: ").strip()
        
        if user_input:
            # Save the newly entered key to a local .env file
            with open(".env", "w", encoding="utf-8") as f:
                f.write(f"JEV_API_KEY={user_input}\n")
            
            print("[Erfolg] Key wurde in .env gespeichert!\n")
            api_key = user_input
        else:
            print("[Fehler] Kein Key eingegeben. Abbruch.")
            sys.exit(1)

    t_start = time.perf_counter()
    metrics = RunMetrics()
    sem = asyncio.Semaphore(args.concurrency)
    connector = aiohttp.TCPConnector(limit=args.concurrency + 5)

    async with aiohttp.ClientSession(connector=connector) as session:
        query = f'deck:"{args.deck}" -tag:{TAG_PREFIX}*'
        try:
            print(f"Suche Karten im Deck '{args.deck}'...")
            note_ids = await anki_call(session, args.anki_url, "findNotes", query=query)
        except Exception as e:
            print(f"[Fehler] AnkiConnect antwortet nicht! Ist Anki offen? ({e})")
            sys.exit(1)

        metrics.total_notes_found = len(note_ids)
        print(f"Gefundene Notizen ohne '{TAG_PREFIX}'-Tag: {len(note_ids)}")
        
        if not note_ids:
            print("[Info] Abbruch: Keine Karten zur Prüfung gefunden.")
            return

        # Rest des Codes bleibt gleich...
        print(f"Lade Daten für {len(note_ids)} Notizen...")
        notes_data = []
        for i in range(0, len(note_ids), 100):
            notes_data.extend(await anki_call(session, args.anki_url, "notesInfo", notes=note_ids[i : i + 100]))

        all_card_ids = []
        for note in notes_data:
            all_card_ids.extend(note.get("cards", []))

        cards_data = []
        for i in range(0, len(all_card_ids), 500):
            cards_data.extend(await anki_call(session, args.anki_url, "cardsInfo", cards=all_card_ids[i: i+500]))

        note_to_card = {}
        for c in cards_data:
            nid = c.get("note")
            if nid and nid not in note_to_card:
                note_to_card[nid] = c

        print("Starte JEV-Analyse...")
        tasks = [
            evaluate_card_task(
                session, sem, args.api_url, api_key, note, note_to_card.get(note["noteId"], {}), metrics, args.front_field, args.back_field
            )
            for note in notes_data
        ]

        results = await asyncio.gather(*tasks)

        tag_batches: dict[str, list[int]] = {}
        eval_records = []

        for item in results:
            if not item:
                continue
            eval_records.append(item)
            for t in item["tags"]:
                tag_batches.setdefault(t, []).append(item["note_id"])

        if not args.dry_run and tag_batches:
            print("Schreibe Tags zurück nach Anki...")
            for tag_name, nids in tag_batches.items():
                await anki_call(session, args.anki_url, "addTags", notes=nids, tags=tag_name)

    duration = time.perf_counter() - t_start

    avg_clarity = metrics.total_clarity_score / metrics.cards_evaluated if metrics.cards_evaluated > 0 else 0.0
    report_stats = {
        "total_scanned": metrics.cards_evaluated,
        "avg_clarity": avg_clarity,
        "deck_name": args.deck  # <-- NEU: Übergibt den Decknamen an den Generator
    }
    
    print("Erstelle HTML Report...")
    generator = AnkiReportGenerator(template_path="report_template.html", lang="de")
    generator.generate(eval_records, report_stats, filename="review_report.html")

    if args.report or args.dry_run:
        print_summary_report(metrics, duration, args.dry_run)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deck", type=str, required=True)
    parser.add_argument("--concurrency", type=int, default=15)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--front-field", type=str, default=None)
    parser.add_argument("--back-field", type=str, default=None)
    parser.add_argument("--api-key", type=str, default=None)
    parser.add_argument("--api-url", type=str, default="https://api.typesafe.ai/v1/systemone")
    parser.add_argument("--anki-url", type=str, default="http://127.0.0.1:8765")
    args = parser.parse_args()

    asyncio.run(run_linter(args))

if __name__ == "__main__":
    main()
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
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

VERSION = "v1"
TAG_PREFIX = f"linter::{VERSION}"
PRICE_PER_M_INPUT_TOKENS = 0.042  # 0,042 $ pro 1.000.000 Input-Tokens

# Bekannte Standard-Feldnamen für die automatische Erkennung
FRONT_CANDIDATES = ["front", "frage", "vorderseite", "text", "aufgabe", "question"]
BACK_CANDIDATES = [
    "back",
    "antwort",
    "rückseite",
    "rueckseite",
    "lösung",
    "loesung",
    "extra",
    "answer",
]


@dataclass
class RunMetrics:
    total_notes_found: int = 0
    cards_evaluated: int = 0
    cards_skipped: int = 0
    retries_429: int = 0
    measured_input_tokens: int = 0
    estimated_input_tokens: int = 0
    has_unmetered_calls: bool = False
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
    """Entfernt HTML-Tags, normalisiert Sonderzeichen und bereinigt Whitespaces."""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    return " ".join(text.split()).strip()


def estimate_tokens_fallback(text: str) -> int:
    """Konservative Token-Schätzung bei fehlendem API-Usage-Header (~4 Zeichen/Token)."""
    return max(1, math.ceil(len(text) / 4))


def extract_card_fields(
    note_fields: dict[str, dict[str, Any]],
    custom_front: str | None = None,
    custom_back: str | None = None,
) -> tuple[str, str, str | None]:
    """Extrahiert Vorder- und Rückseite anhand expliziter Namen, Heuristik oder Reihenfolge.

    Gibt (front_text, back_text, warning_message) zurück.
    """
    field_keys = list(note_fields.keys())
    if len(field_keys) < 2:
        return "", "", "Weniger als zwei Felder vorhanden."

    # 1. Explizit über CLI vorgegebene Feldnamen
    if custom_front and custom_back:
        if custom_front in note_fields and custom_back in note_fields:
            return (
                clean_html(note_fields[custom_front]["value"]),
                clean_html(note_fields[custom_back]["value"]),
                None,
            )
        return (
            "",
            "",
            f"Vorgegebene Felder ('{custom_front}', '{custom_back}') nicht in Notiz gefunden.",
        )

    # 2. Heuristische Erkennung anhand geläufiger Feldnamen (case-insensitive)
    lower_map = {k.lower(): k for k in field_keys}
    matched_front = next(
        (lower_map[c] for c in FRONT_CANDIDATES if c in lower_map), None
    )
    matched_back = next(
        (
            lower_map[c]
            for c in BACK_CANDIDATES
            if c in lower_map and lower_map[c] != matched_front
        ),
        None,
    )

    if matched_front and matched_back:
        return (
            clean_html(note_fields[matched_front]["value"]),
            clean_html(note_fields[matched_back]["value"]),
            None,
        )

    # 3. Fallback: Erstes und zweites Feld der Notiz
    f_val = clean_html(note_fields[field_keys[0]]["value"])
    b_val = clean_html(note_fields[field_keys[1]]["value"])
    warning = (
        f"Heuristik griff nicht. Nutze Felder '{field_keys[0]}' und '{field_keys[1]}'."
    )
    return f_val, b_val, warning

import urllib.request


async def anki_call(
    session: Any, anki_url: str, action: str, **params: Any
) -> Any:
    """Führt eine zuverlässige Anfrage an AnkiConnect via urllib in einem Thread aus."""

    def _sync_request() -> Any:
        payload = json.dumps(
            {"action": action, "version": 6, "params": params}
        ).encode("utf-8")
        req = urllib.request.Request(
            anki_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("error"):
                raise RuntimeError(f"AnkiConnect Fehler: {data['error']}")
            return data["result"]

    return await asyncio.to_thread(_sync_request)


def build_jev_payload(front: str, back: str) -> dict[str, Any]:
    """Erzeugt das exakt an die TypeSafe API angepasste Payload-Schema."""
    return {
        "model": "jev-latest",
        "state": f"F: {front}\nA: {back}",
        "questions": {
            "aggregation_trap": {
                "type": "noul",
                "instructions": "The question is an open-ended aggregation demand (e.g. broad duties, unstructured symptoms, multiple disparate items) rather than testing a single bounded mental chunk.",
            },
            "scope_bloat": {
                "type": "noul",
                "instructions": "Answer contains excessive trivia, secondary textbook context, or unnecessary filler beyond the question.",
            },
            "scope_underkill": {
                "type": "noul",
                "instructions": "Answer is an incomplete single fragment for a question that inherently asked for a broader scope. Does not apply to truncated lists, examples, or intentional enumerations.",
            },
            "is_binary": {
                "type": "noul",
                "instructions": "Question can be answered with a simple binary choice (yes/no, true/false).",
            },
            "leak": {
                "type": "noul",
                "instructions": "Front inadvertently reveals or strongly hints at answer via identical word roots or obvious grammar markers.",
            },
            "orphan": {
                "type": "noul",
                "instructions": "Card lacks essential domain context, system anchors, or relies on isolated pronouns making standalone recall impossible.",
            },
            "clarity": {
                "type": "score",
                "instructions": "Rate the clarity, precision, and unambiguity of the question and answer.",
                "criteria": [
                    "1 - Very vague, confusing, or ambiguous",
                    "2 - Flawed clarity or slightly confusing",
                    "3 - Acceptable and reasonably clear",
                    "4 - Clear, concise, and understandable",
                    "5 - Highly precise, sharp, and unambiguous",
                ],
            },
            "knowledge_type": {
                "type": "choice",
                "instructions": "Classify the flashcard into the best matching knowledge type category.",
                "criteria": {
                    "FACT_DEFINITION": "Direct fact, term, definition, or vocabulary.",
                    "NUMERIC": "Numbers, limits, dimensions, or time values.",
                    "PROCEDURE": "Operational workflows, sequence of actions, or tactics.",
                    "REGULATION": "Service regulations, legal norms, or official standards.",
                    "ANATOMY": "Medical, physiological, or anatomical terms.",
                    "OTHER": "General or uncategorized knowledge.",
                },
            },
        },
    }

def evaluate_decisions(
    result: dict[str, Any],
) -> tuple[list[str], str, str | None]:
    """Wertet Wahrscheinlichkeiten nach der 3-Stufen- und Severity-Matrix aus.

    Gibt (flags, priority, knowledge_type_tag) zurück.
    """
    flags: list[str] = []
    high_sev_count = 0
    med_sev_count = 0
    has_observation = False

    def process_noul(
        metric_key: str, flag_name: str, is_high_severity: bool
    ) -> None:
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

    # High Severity (Zerstören Lerneffekt aktiv)
    process_noul("scope_underkill", "scope_underkill", is_high_severity=True)
    process_noul("leak", "leak", is_high_severity=True)
    process_noul("orphan", "kontextlos", is_high_severity=True)

    # Medium Severity (Effizienzbremsen / falsches Format)
    process_noul(
        "aggregation_trap", "sammelkarte", is_high_severity=False
    )
    process_noul("scope_bloat", "scope_bloat", is_high_severity=False)
    process_noul("is_binary", "binaerfrage", is_high_severity=False)

# Klarheits-Score (1-5)
    raw_score = float(result.get("clarity", {}).get("score", 4.0))
    clarity_score = raw_score + 1.0
    if clarity_score <= 3.0:
        flags.append(f"{TAG_PREFIX}::flag::unklar")
        med_sev_count += 1
    elif clarity_score < 4.0:
        has_observation = True

    # Wissenstyp-Klassifikation
    k_type = result.get("knowledge_type", {}).get("choice")
    type_tag = (
        f"{TAG_PREFIX}::typ::{k_type.lower()}"
        if k_type and k_type != "OTHER"
        else None
    )

    # Prioritätsgewichtung
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
    metrics: RunMetrics,
    custom_front: str | None,
    custom_back: str | None,
) -> tuple[int, list[str]] | None:
    """Verarbeitet eine einzelne Karteikarte mit Semaphore und 429-Backoff."""
    note_id = note["noteId"]
    front, back, warn = extract_card_fields(
        note.get("fields", {}), custom_front, custom_back
    )

    if not front or not back:
        metrics.cards_skipped += 1
        return None

    payload = build_jev_payload(front, back)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    max_retries = 4
    attempt = 0

    async with sem:
        while attempt <= max_retries:
            t_start = time.perf_counter()
            try:
                async with session.post(
                    api_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    latency = time.perf_counter() - t_start

                    if resp.status == 429:
                        metrics.retries_429 += 1
                        attempt += 1
                        if attempt > max_retries:
                            print(
                                f"[Fehler] Notiz {note_id}: Max Retries nach HTTP 429 erreicht."
                            )
                            return None

                        # Retry-After Header respektieren oder exponentiellen Backoff nutzen
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            try:
                                delay = float(retry_after) + random.uniform(
                                    0.1, 0.4
                                )
                            except ValueError:
                                delay = 2.0
                        else:
                            delay = (2**attempt) + random.uniform(0.1, 1.0)

                        await asyncio.sleep(delay)
                        continue

                    if resp.status != 200:
                        err_text = await resp.text()
                        print(
                            f"[Fehler] Notiz {note_id} (Status {resp.status}): {err_text}"
                        )
                        return None

                    data = await resp.json()
                    metrics.latencies.append(latency)

                    # Token-Erfassung: Bevorzuge API-Header, sonst deterministischer Schätzer
                    usage = data.get("usage", {})
                    input_toks = usage.get("input_tokens") or usage.get(
                        "prompt_tokens"
                    )

                    if input_toks:
                        metrics.measured_input_tokens += int(input_toks)
                    else:
                        metrics.has_unmetered_calls = True
                        metrics.estimated_input_tokens += estimate_tokens_fallback(
                            json.dumps(payload)
                        )

                    flags, priority, type_tag = evaluate_decision_logic(data)
                    tags_to_apply: list[str] = []

                    if priority != "clean":
                        tags_to_apply.extend(flags)
                        tags_to_apply.append(f"{TAG_PREFIX}::prio::{priority}")
                        for f in flags:
                            metrics.add_flag(
                                f.replace(f"{TAG_PREFIX}::flag::", "")
                            )

                    if type_tag:
                        tags_to_apply.append(type_tag)

                    metrics.add_prio(priority)
                    metrics.cards_evaluated += 1
                    # Messwerte für den Report aufbereiten
                    answers = data.get("answers", data)
                    metrics_data = {}
                    for k in [
                        "aggregation_trap",
                        "scope_bloat",
                        "scope_underkill",
                        "is_binary",
                        "leak",
                        "orphan",
                    ]:
                        metrics_data[k] = float(
                            answers.get(k, {}).get("noul", 0.0)
                        )

                    metrics_data["clarity"] = (
                        float(answers.get("clarity", {}).get("score", 4.0)) + 1.0
                    )

                    card_record = {
                        "note_id": note_id,
                        "front": front,
                        "back": back,
                        "priority": priority,
                        "flags": flags,
                        "tags": tags_to_apply,
                        "metrics": metrics_data,
                    }

                    return card_record

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                attempt += 1
                if attempt > max_retries:
                    print(
                        f"[Netzwerkfehler] Notiz {note_id} nach {max_retries} Retries: {exc}"
                    )
                    return None
                await asyncio.sleep((2**attempt) + random.uniform(0.1, 0.5))

    return None


def evaluate_decision_logic(
    data: dict[str, Any],
) -> tuple[list[str], str, str | None]:
    # Extrahiere die Antworten aus dem API-Key "answers"
    answers = data.get("answers", data)
    return evaluate_decisions(answers)

def generate_html_report(
    eval_records: list[dict[str, Any]], filename: str = "review_report.html"
) -> None:
    """Erzeugt einen lokalen, filterbaren HTML-Bericht ohne zusätzliche API-Kosten."""
    # Nur Karten aufnehmen, die tatsächlich Überarbeitungsbedarf haben
    flagged_cards = [r for r in eval_records if r["priority"] != "clean"]

    # Nach Priorität sortieren: high zuerst, dann medium, dann low
    prio_order = {"high": 0, "medium": 1, "low": 2}
    flagged_cards.sort(key=lambda x: prio_order.get(x["priority"], 99))

    rows_html = []
    for c in flagged_cards:
        prio_color = {
            "high": "#e63946",
            "medium": "#f4a261",
            "low": "#e9c46a",
        }.get(c["priority"], "#6c757d")

        # Details/Gründe aufbereiten
        reasons = []
        for metric, val in c["metrics"].items():
            if metric == "clarity":
                if val <= 3.0:
                    reasons.append(
                        f"<b>Klarheit mangelhaft:</b> {val:.1f} / 5.0"
                    )
            elif val >= 0.60:
                pct = int(val * 100)
                reasons.append(f"<b>{metric}:</b> {pct} % Wahrscheinlichkeit")

        reasons_list = (
            "".join(f"<li>{r}</li>" for r in reasons)
            if reasons
            else "Keine Einzelmetrik über Schwelle"
        )

        rows_html.append(
            f"""
        <tr>
            <td><span class="badge" style="background-color: {prio_color}">{c['priority'].upper()}</span></td>
            <td><code>{c['note_id']}</code></td>
            <td class="text-col">{html.escape(c['front'])}</td>
            <td class="text-col">{html.escape(c['back'])}</td>
            <td><ul class="reason-list">{reasons_list}</ul></td>
        </tr>
        """
        )

    table_body = (
        "\n".join(rows_html)
        if rows_html
        else "<tr><td colspan='5'>Keine auffälligen Karten gefunden.</td></tr>"
    )

    html_content = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>Anki Review Report</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }}
        h1 {{ color: #f8fafc; margin-bottom: 0.5rem; }}
        p {{ color: #94a3b8; margin-bottom: 1.5rem; }}
        table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; }}
        th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; vertical-align: top; }}
        th {{ background: #0f172a; color: #cbd5e1; font-weight: 600; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em; }}
        tr:hover {{ background: #243247; }}
        .badge {{ padding: 4px 8px; border-radius: 4px; font-weight: 700; font-size: 0.75rem; color: #fff; text-transform: uppercase; }}
        .text-col {{ max-width: 380px; word-wrap: break-word; line-height: 1.4; }}
        .reason-list {{ margin: 0; padding-left: 1.2rem; font-size: 0.85rem; color: #cbd5e1; }}
        code {{ background: #0f172a; padding: 2px 4px; border-radius: 4px; font-size: 0.8rem; color: #38bdf8; }}
    </style>
</head>
<body>
    <h1>Anki Review-Finder Report</h1>
    <p>Gefundene revisionsbedürftige Karten: <strong>{len(flagged_cards)}</strong></p>
    <table>
        <thead>
            <tr>
                <th style="width: 90px;">Priorität</th>
                <th style="width: 140px;">Notiz-ID</th>
                <th>Vorderseite</th>
                <th>Rückseite</th>
                <th style="width: 320px;">Diagnose / Messwerte</th>
            </tr>
        </thead>
        <tbody>
            {table_body}
        </tbody>
    </table>
</body>
</html>"""

    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"\n[HTML-Report] Detaillierte Diagnose gespeichert in: {filename}")



def print_summary_report(
    metrics: RunMetrics, duration: float, dry_run: bool
) -> None:
    """Gibt den Abschlussbericht mit Latenzen, Tokenkosten und Flags aus."""
    latencies = sorted(metrics.latencies)
    n = len(latencies)
    p50 = latencies[int(n * 0.50)] * 1000 if n else 0.0
    p95 = latencies[int(n * 0.95)] * 1000 if n else 0.0
    throughput = metrics.cards_evaluated / duration if duration > 0 else 0.0

    total_tokens = (
        metrics.measured_input_tokens + metrics.estimated_input_tokens
    )
    tokens_per_card = (
        total_tokens / metrics.cards_evaluated
        if metrics.cards_evaluated
        else 0.0
    )
    cost_usd = (total_tokens / 1_000_000) * PRICE_PER_M_INPUT_TOKENS
    cost_eur = cost_usd * 0.92

    token_label = (
        "Mischung (Empirisch + Schätzung)"
        if metrics.has_unmetered_calls and metrics.measured_input_tokens > 0
        else (
            "Schätzung (Fallback)"
            if metrics.has_unmetered_calls
            else "Vollständig gemessen"
        )
    )

    print("\n" + "=" * 70)
    print(f"            ANKI REVIEW-FINDER REPORT ({VERSION})")
    if dry_run:
        print("            [DRY-RUN: Keine Änderungen in Anki geschrieben]")
    print("=" * 70)
    print(f"Gefundene Notizen im Deck:   {metrics.total_notes_found}")
    print(f"Erfolgreich evaluiert:       {metrics.cards_evaluated}")
    print(f"Übersprungen (leer/inkomp.): {metrics.cards_skipped}")
    print(f"HTTP 429 Retries:            {metrics.retries_429}")
    print("-" * 70)
    print("DURCHSATZ & LATENZEN")
    print(f"Gesamtlaufzeit:              {duration:.2f} Sekunden")
    print(f"Durchsatz:                   {throughput:.1f} Karten/s")
    print(f"Latenz p50 / p95:            {p50:.1f} ms / {p95:.1f} ms")
    print("-" * 70)
    print(f"TOKEN- & KOSTENBILANZ ({token_label})")
    print(f"Input-Tokens Gesamt:         {total_tokens:,}")
    print(f"Ø Tokens pro Karte:          {tokens_per_card:.1f}")
    print(f"Berechnete API-Kosten:       {cost_usd:.4f} $ (~{cost_eur:.4f} €)")
    print("-" * 70)
    print("AUFFÄLLIGKEITEN (FLAGS)")
    if metrics.flag_stats:
        for flag, cnt in sorted(
            metrics.flag_stats.items(), key=lambda x: x[1], reverse=True
        ):
            pct = (
                (cnt / metrics.cards_evaluated) * 100
                if metrics.cards_evaluated
                else 0.0
            )
            print(f"  {flag:<20} {cnt:>5}  ({pct:>4.1f} %)")
    else:
        print("  Keine auffälligen Karten festgestellt.")
    print("-" * 70)
    print("REVIEW-PRIORITÄTEN")
    for prio in ["high", "medium", "low", "clean"]:
        cnt = metrics.prio_stats.get(prio, 0)
        pct = (
            (cnt / metrics.cards_evaluated) * 100
            if metrics.cards_evaluated
            else 0.0
        )
        tag_desc = (
            f"-> tag:{TAG_PREFIX}::prio::{prio}"
            if prio != "clean"
            else "(Kein Review nötig)"
        )
        print(f"  {prio.upper():<8} {cnt:>5} ({pct:>5.1f} %)  {tag_desc}")
    print("=" * 70 + "\n")


async def run_linter(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        print(
            "Fehler: Kein API-Schlüssel gefunden. Setze JEV_API_KEY oder nutze --api-key.",
            file=sys.stderr,
        )
        sys.exit(1)

    t_start = time.perf_counter()
    metrics = RunMetrics()
    sem = asyncio.Semaphore(args.concurrency)
    connector = aiohttp.TCPConnector(limit=args.concurrency + 5)

    async with aiohttp.ClientSession(connector=connector) as session:
        # 1. Notizen aus Anki abrufen
        print(f"Verbinde mit AnkiConnect ({args.anki_url})...")
        # Ankis Suchsyntax für Tags mit Unterordnern/Prefixes: "tag:prefix*"
        query = f'deck:"{args.deck}" -tag:{TAG_PREFIX}*'
        try:
            note_ids = await anki_call(
                session, args.anki_url, "findNotes", query=query
            )
        except Exception as err:
            print(f"Verbindung zu AnkiConnect fehlgeschlagen: {err}", file=sys.stderr)
            sys.exit(1)

        metrics.total_notes_found = len(note_ids)
        if not note_ids:
            print(
                f"Keine ungeprüften Notizen im Deck '{args.deck}' gefunden (alle besitzen bereits '{TAG_PREFIX}*')."
            )
            return

        print(
            f"Lade Daten für {len(note_ids)} ungeprüfte Notizen (Deck: '{args.deck}')..."
        )
        notes_data: list[dict[str, Any]] = []
        batch_size = 100
        for i in range(0, len(note_ids), batch_size):
            chunk = note_ids[i : i + batch_size]
            chunk_data = await anki_call(
                session, args.anki_url, "notesInfo", notes=chunk
            )
            notes_data.extend(chunk_data)

        # 2. Asynchrone Abarbeitung starten
        print(
            f"Starte Review-Prüfung via Jev (Concurrency: {args.concurrency})..."
        )
        tasks = [
            evaluate_card_task(
                session,
                sem,
                args.api_url,
                api_key,
                note,
                metrics,
                args.front_field,
                args.back_field,
            )
            for note in notes_data
        ]

        results = await asyncio.gather(*tasks)

        # 3. Tags nach Anki schreiben & Records sammeln
        tag_batches: dict[str, list[int]] = {}
        eval_records: list[dict[str, Any]] = []

        for item in results:
            if not item:
                continue
            eval_records.append(item)
            nid = item["note_id"]
            for t in item["tags"]:
                tag_batches.setdefault(t, []).append(nid)

        if not args.dry_run and tag_batches:
            print(
                f"Synchronisiere {len(tag_batches)} Tag-Kategorien nach Anki..."
            )
            for tag_name, nids in tag_batches.items():
                await anki_call(
                    session,
                    args.anki_url,
                    "addTags",
                    notes=nids,
                    tags=tag_name,
                )
            print("Tags erfolgreich in Anki angelegt.")

    duration = time.perf_counter() - t_start

    # HTML-Report erzeugen
    generate_html_report(eval_records)

    if args.report or args.dry_run:
        print_summary_report(metrics, duration, args.dry_run)

    if args.report or args.dry_run:
        print_summary_report(metrics, duration, args.dry_run)
    else:
        print(
            f"\nPrüfung beendet: {metrics.cards_evaluated} Karten in {duration:.2f} s verarbeitet."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Anki Review-Finder: Filtert revisionsbedürftige Karten deterministisch via TypeSafe Jev heraus.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--deck",
        type=str,
        required=True,
        help="Name des zu filternden Anki-Decks.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=15,
        help="Parallele HTTP-Anfragen (Empfohlen: 10–25).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Prüft die Karten und erzeugt den Report, ohne Tags in Anki zu schreiben.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Gibt nach Durchlauf die Benchmark- und Qualitätsstatistiken aus.",
    )
    parser.add_argument(
        "--front-field",
        type=str,
        default=None,
        help="Optional: Expliziter Name des Vorderseiten-Feldes.",
    )
    parser.add_argument(
        "--back-field",
        type=str,
        default=None,
        help="Optional: Expliziter Name des Rückseiten-Feldes.",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="TypeSafe/Jev API-Schlüssel (alternativ via ENV: JEV_API_KEY).",
    )
    parser.add_argument(
    "--api-url",
    type=str,
    default="https://api.typesafe.ai/v1/systemone",
    help="API-Endpunkt von TypeSafe Jev.",
    )
    parser.add_argument(
        "--anki-url",
        type=str,
        default="http://127.0.0.1:8765",
        help="Lokale AnkiConnect-URL.",
    )

    args = parser.parse_args()

    if args.concurrency < 1 or args.concurrency > 50:
        parser.error("--concurrency muss zwischen 1 und 50 liegen.")

    try:
        asyncio.run(run_linter(args))
    except KeyboardInterrupt:
        print("\nAbbruch durch Nutzer.")
        sys.exit(130)


if __name__ == "__main__":
    main()
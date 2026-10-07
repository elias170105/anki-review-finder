# Anki Review-Finder (Jev Linter)

# Anki Review-Finder (Jev Linter)

> 🧪 **Project Status: Alpha / Experimental**  
> This tool is in active early development. Heuristics, prompt formats, and tag naming conventions are subject to change. Feedback, edge-case reports, and GitHub Issues are welcome.

A CLI tool for automated quality auditing of Anki flashcards via AnkiConnect and the TypeSafe Jev API. It deterministically detects cards with structural defects (e.g., scope underkill, answer leaks, missing context/orphans, collection traps, binary questions) and writes corresponding diagnostic tags directly to Anki while generating an interactive HTML audit report.

> ⚠️ **Important: Back Up Your Collection Before Use**
> While this tool **never modifies the text content of your cards** and only writes tags via AnkiConnect, always create a full backup of your Anki collection before running any automated script.
> Go to **File -> Export -> Anki Collection Package (`.colpkg`)** (ensure *Include media* is checked).
> It is strongly recommended to test your deck with the `--dry-run` flag first.

---

## AI Disclosure & Validation

This project was developed with the assistance of artificial intelligence and refined through structured empirical validation. To ensure high precision and minimal false positives, the detection thresholds and prompts were benchmarked against a custom 100-card test suite covering diverse domains (medicine, emergency services, IT/systems administration, and general science) across all targeted error classes.

---

## Features

* **Deterministic Evaluation:** Fast System-1 classification of flashcard anti-patterns.
* **Asynchronous Throughput:** High-concurrency processing with semaphores and automatic HTTP 429 exponential backoff.
* **Tag Synchronization:** Categorizes cards into review priorities (`high`, `medium`, `low`) and specific defect flags directly in Anki.
* **Standalone HTML Report:** Automatically generates a comprehensive HTML summary (`review_report.html`) detailing latencies, token consumption, and card metrics.
* **Card Content Protection:** Read-only analysis of card fields; no changes are made to your question or answer text.

---

## Prerequisites

1. **Python 3.9+**
2. **Anki Desktop** running locally.
3. **AnkiConnect Add-on:** Installed in Anki (Add-on Code: `2055492159`).
4. **TypeSafe Jev API Key:** Required for LLM-based evaluation.

---

## Installation & Setup

1. Clone the repository:

```bash
git clone https://github.com/elias170105/anki-review-finder.git
cd anki-review-finder

```

2. Install dependencies:

```bash
pip install -r requirements.txt

```

3. Create a `.env` file in the project root directory:

```env
JEV_API_KEY=your_api_key_here

```

---

## Usage

Ensure Anki is open and AnkiConnect is active before starting the script.

### 1. Test Run (Recommended)

Analyze a deck and generate the HTML report without modifying any tags in Anki:

```bash
python mainanki.py --deck "MyDeck" --dry-run --report

```

### 2. Live Run

Evaluate cards, generate the HTML report, and write diagnostic tags back to Anki:

```bash
python mainanki.py --deck "MyDeck" --report

```

---

## CLI Parameters

| Flag | Type / Default | Description |
| --- | --- | --- |
| `--deck` | `str` *(Required)* | Name of the target Anki deck to evaluate. |
| `--concurrency` | `int` (Default: `15`) | Number of concurrent API requests (allowed range: 1–50). |
| `--dry-run` | Flag (Default: `False`) | Evaluates cards and generates the HTML report without writing tags back to Anki. |
| `--report` | Flag (Default: `False`) | Prints execution metrics (throughput, latencies, tokens, estimated cost) to the console. |
| `--front-field` | `str` (Default: `None`) | Explicit field name for the front side (overrides heuristic autodetection). |
| `--back-field` | `str` (Default: `None`) | Explicit field name for the back side (overrides heuristic autodetection). |
| `--api-key` | `str` (Default: `None`) | TypeSafe/Jev API key (alternatively set via `JEV_API_KEY` or `TYPESAFE_API_KEY` in `.env`). |
| `--api-url` | `str` (Default: `[https://api.typesafe.ai/v1/systemone](https://api.typesafe.ai/v1/systemone)`) | API endpoint for TypeSafe Jev System-1 inference. |
| `--anki-url` | `str` (Default: `[http://127.0.0.1:8765](http://127.0.0.1:8765)`) | URL for local AnkiConnect HTTP JSON-RPC service. |

---

## Diagnostic Flags & Priority Model

Cards are categorized into three severity levels based on their impact on long-term retention:

### High Priority (`prio::high`)

* `flag::scope_underkill`: Incomplete single fragment for a question demanding broader context.
* `flag::leak`: The front side inadvertently reveals the answer through shared word roots or grammar clues.
* `flag::kontextlos`: Card lacks system anchors, domain context, or relies on ambiguous pronouns.

### Medium Priority (`prio::medium`)

* `flag::sammelkarte`: Unbounded lists, excessive enumerations, or open-ended aggregation tasks.
* `flag::scope_bloat`: Unnecessary trivia, excessive textbook filler, or secondary context.
* `flag::binaerfrage`: Simple binary choices (yes/no, true/false) susceptible to guessing.
* `flag::unklar`: Ambiguous, vague, or confusing wording (clarity rating $\le 3.0$).

### Low Priority / Clean

* `prio::low`: Minor stylistic observations with no critical flaws.
* Unflagged: Clean, single-concept retrieval units suitable for spaced repetition.

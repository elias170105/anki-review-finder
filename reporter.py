import html
import os
from typing import Any

def generate_html_report(eval_records: list[dict[str, Any]], filename: str = "review_report.html") -> None:
    """Erzeugt einen lokalen, filterbaren HTML-Bericht aus der Template-Datei."""
    flagged_cards = [r for r in eval_records if r["priority"] != "clean"]

    prio_order = {"high": 0, "medium": 1, "low": 2}
    flagged_cards.sort(key=lambda x: prio_order.get(x["priority"], 99))

    rows_html = []
    for c in flagged_cards:
        prio_color = {
            "high": "#e63946",
            "medium": "#f4a261",
            "low": "#e9c46a",
        }.get(c["priority"], "#6c757d")

        reasons = []
        for metric, val in c["metrics"].items():
            if metric == "clarity":
                if val <= 3.0:
                    reasons.append(f"<b>Klarheit mangelhaft:</b> {val:.1f} / 5.0")
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

    # Template im selben Verzeichnis laden
    template_path = os.path.join(os.path.dirname(__file__), "report_template.html")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template_content = f.read()
    except FileNotFoundError:
        template_content = "<html><body><h1>Fehler: report_template.html fehlt!</h1>{table_body}</body></html>"

    # Platzhalter ersetzen
    html_content = template_content.replace("{total_flagged}", str(len(flagged_cards)))
    html_content = html_content.replace("{table_body}", table_body)

    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"\n[HTML-Report] Detaillierte Diagnose gespeichert in: {filename}")
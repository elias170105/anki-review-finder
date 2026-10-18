import html
import os
from typing import Any, Dict, List

class AnkiReportGenerator:
    def __init__(self, template_path: str = "report_template.html", lang: str = "de"):
        self.template_path = template_path
        self.lang = lang
        self.i18n = {
            "de": {"no_cards": "Keine auffälligen Karten gefunden.", "error": "Template fehlt!"},
            "en": {"no_cards": "No flagged cards found.", "error": "Template missing!"}
        }

    def generate(self, eval_records: List[Dict[str, Any]], stats: Dict[str, Any], filename: str = "review_report.html") -> None:
        """Erzeugt den filterbaren, interaktiven HTML-Bericht."""
        
        flagged_cards = [r for r in eval_records if r.get("priority") != "clean"]
        
        prio_order = {"high": 0, "medium": 1, "low": 2}
        flagged_cards.sort(key=lambda x: prio_order.get(x.get("priority", "low"), 99))

        rows_html = []
        for c in flagged_cards:
            prio = c.get("priority", "low")
            note_id = c.get("note_id", "N/A")
            note_type = html.escape(c.get("note_type", "Unbekannt"))
            
            anki_info = c.get("anki_info", {})
            deck = html.escape(anki_info.get("deckName", "-"))
            
            ease_raw = anki_info.get("factor")
            ease = f"{ease_raw / 10}%" if ease_raw else "-"

            reasons = []
            for metric, val in c.get("metrics", {}).items():
                if metric == "clarity":
                    if val <= 3.0:
                        # Die Tags data-i18n-metric dienen der dynamischen JS-Übersetzung im Browser
                        reasons.append(f"<b data-i18n-metric='clarity'>Klarheit</b>: {val:.1f} / 5.0")
                elif val >= 0.60:
                    reasons.append(f"<b data-i18n-metric='{metric}'>{metric}</b>: {int(val * 100)}%")

            reasons_list = "".join(f"<li>{r}</li>" for r in reasons) if reasons else "<span data-i18n='no_metric'>Keine spezifische Metrik</span>"

            rows_html.append(f"""
            <tr data-prio="{prio}" data-type="{note_type}">
                <td><input type="checkbox" class="card-check" data-id="{note_id}"></td>
                <td><span class="badge badge-{prio}">{prio.upper()}</span></td>
                <td><code>{note_id}</code></td>
                <td>{note_type}<br><small class="text-muted">{deck}</small></td>
                <td class="text-col">{html.escape(c.get('front', ''))}</td>
                <td class="text-col">{html.escape(c.get('back', ''))}</td>
                <td>
                    <ul class="reason-list">{reasons_list}</ul>
                    <small class="text-muted">Ease: {ease}</small>
                </td>
            </tr>
            """)

        table_body = "\n".join(rows_html) if rows_html else f"<tr><td colspan='7'>{self.i18n[self.lang]['no_cards']}</td></tr>"

        template_full_path = os.path.join(os.path.dirname(__file__), self.template_path)
        
        try:
            with open(template_full_path, "r", encoding="utf-8") as f:
                template_content = f.read()
        except FileNotFoundError:
            print(f"[Fehler] Die Template-Datei '{template_full_path}' fehlt! Der Report konnte nicht erstellt werden.")
            return


        html_content = template_content.replace("{lang}", self.lang)
        html_content = html_content.replace("{deck_name}", html.escape(stats.get("deck_name", "Unbekannt"))) # <-- NEU
        html_content = html_content.replace("{total_flagged}", str(len(flagged_cards)))
        html_content = html_content.replace("{total_scanned}", str(stats.get("total_scanned", 0)))
        html_content = html_content.replace("{avg_clarity}", f"{stats.get('avg_clarity', 0):.2f}")
        html_content = html_content.replace("{table_body}", table_body)

        with open(filename, "w", encoding="utf-8") as f:
            f.write(html_content)

        print(f"[HTML-Report] Diagnose Report erfolgreich gespeichert in: {filename}")
"""
Hero response formatter — Orchestrator v1.

Output:
  - Confidence badge + issue summary
  - Cost breakdown table
  - TOP sources comparison table (up to 5 priced results)
  - Best buy link highlighted
  - Excel report path mention
  - YouTube tutorial link
"""

from __future__ import annotations

from typing import Any


def format_diagnostic_markdown(
    vision: dict[str, Any],
    parts: dict[str, Any],
    tutorial: dict[str, Any],
) -> str:
    """
    Build the final user-facing Markdown.

    vision  : part_name, part_number, estimated_labor_cost, confidence, issue_summary
    parts   : price_usd, purchase_url, stock_status, source_site, all_sources, excel_path
    tutorial: video_url, video_title, duration_seconds
    """
    part_name    = vision.get("part_name") or "Unknown part"
    part_number  = vision.get("part_number") or "N/A"
    labor        = float(vision.get("estimated_labor_cost") or 0.0)
    confidence   = vision.get("confidence")
    issue_summary = (vision.get("issue_summary") or "").strip()

    price        = float(parts.get("price_usd") or 0.0)
    purchase_url = parts.get("purchase_url") or "#"
    stock_status = parts.get("stock_status") or "check vendor"
    source_site  = parts.get("source_site") or "vendor"
    all_sources  = parts.get("all_sources") or []
    excel_path   = parts.get("excel_path") or ""

    video_url   = tutorial.get("video_url") or "#"
    video_title = tutorial.get("video_title") or "Tutorial"
    dur_s       = int(tutorial.get("duration_seconds") or 0)

    savings      = labor - price
    savings_sign = "+" if savings >= 0 else ""

    # ── Confidence badge ────────────────────────────────────────────────────
    conf_badge = ""
    if isinstance(confidence, (int, float)):
        pct = int(float(confidence) * 100)
        if pct >= 80:
            badge = f"🟢 {pct}% confident"
        elif pct >= 55:
            badge = f"🟡 {pct}% confident"
        else:
            badge = f"🔴 {pct}% confident (verify part number)"
        conf_badge = f"\n*Confidence: {badge}*\n"

    summary_block = f"\n> {issue_summary}\n" if issue_summary else ""

    # ── Duration display ────────────────────────────────────────────────────
    if dur_s:
        minutes, secs = divmod(dur_s, 60)
        dur_label   = f"{minutes}m {secs}s" if minutes else f"{secs}s"
        dur_display = f" · {dur_label}"
    else:
        dur_display = ""

    # ── Best deal highlight ─────────────────────────────────────────────────
    if price > 0:
        best_deal_line = (
            f"🏆 **Best price: ${price:,.2f}** at **{source_site}** "
            f"— **[→ Buy Now]({purchase_url})**"
        )
    else:
        best_deal_line = f"🏆 **Best source: {source_site}** — **[→ Check Price]({purchase_url})**"

    # ── Sources comparison table ────────────────────────────────────────────
    sources_table = _build_sources_table(all_sources, purchase_url, source_site, price)

    # ── Excel mention ───────────────────────────────────────────────────────
    excel_note = ""
    if excel_path:
        import os
        fname = os.path.basename(excel_path)
        excel_note = (
            f"\n📊 **Full price comparison spreadsheet:** "
            f"[{fname}](reports/{fname}) *(saved locally in `reports/`)*\n"
        )

    return f"""\
### 🔍 Diagnostic Complete

**Identified Part:** {part_name}
**Part Number:** `{part_number}`
{summary_block}{conf_badge}
---

### 💰 Cost Breakdown

|| |
| :--- | ---: |
| Standard repairman estimate | **${labor:,.2f}** |
| Best DIY part cost ({source_site}) | **${price:,.2f}** |
| **Total savings** | **{savings_sign}${abs(savings):,.2f}** |

{best_deal_line}

---

### 🛒 Buy the Part — All Sources Found
{sources_table}{excel_note}
---

### 🎬 How to Fix It Yourself

**[{video_title}]({video_url})**{dur_display}

---

*Always unplug the appliance or disconnect the battery before starting repairs. \
This is an AI-generated estimate, not a guaranteed quote.*

*You've got this! 🛠️ Let me know if you need help with anything else.*\
"""


def _build_sources_table(
    all_sources: list,
    best_url: str,
    best_site: str,
    best_price: float,
) -> str:
    """Build a Markdown table of all sources (up to 10), best price first."""

    def _as_dict(s) -> dict:
        if isinstance(s, dict):
            return s
        return {
            "source_site":  getattr(s, "source_site", ""),
            "price_usd":    getattr(s, "price_usd", 0.0),
            "purchase_url": getattr(s, "purchase_url", ""),
            "stock_status": getattr(s, "stock_status", ""),
        }

    rows = [_as_dict(s) for s in all_sources]

    if not rows:
        rows = [{
            "source_site":  best_site,
            "price_usd":    best_price,
            "purchase_url": best_url,
            "stock_status": "Check Vendor",
        }]

    # Sort: priced in-stock first, then cheapest, unpriced last
    rows.sort(key=lambda r: (r["price_usd"] <= 0, r["price_usd"]))
    rows = rows[:10]

    lines = [
        "",
        "| # | Store | Price | Stock | Link |",
        "| :- | :---- | ----: | :---- | :--- |",
    ]
    for i, r in enumerate(rows, 1):
        p     = r.get("price_usd") or 0.0
        url   = r.get("purchase_url") or "#"
        site  = r.get("source_site") or "vendor"
        stock = r.get("stock_status") or "Check Vendor"

        if i == 1 and p > 0:
            price_str = f"**${p:,.2f}**"
            star = " ⭐ BEST"
        elif p > 0:
            price_str = f"${p:,.2f}"
            star = ""
        else:
            price_str = "—"
            star = ""

        lines.append(f"| {i}{star} | {site} | {price_str} | {stock} | [→ Buy]({url}) |")

    lines.append("")
    return "\n".join(lines)

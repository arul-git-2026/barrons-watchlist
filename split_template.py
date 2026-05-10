"""
split_template.py  —  Splits widget_v2.html into Jinja2 feature files.

Run once from the streetwise directory:
    python split_template.py

Produces:
    templates/widget_v2.html          (shell — global CSS + include tags + all JS)
    templates/features/chart.html
    templates/features/episode_brief.html
    templates/features/table_view.html
    templates/features/add_ticker.html
    templates/features/csv_import.html
    templates/features/watchlist.html
    templates/features/crisis_monitor.html
    templates/features/heatmap.html   (empty stub)
"""

import os, pathlib

SRC  = pathlib.Path("widget_v2.html")
TMPL = pathlib.Path("templates")
FEAT = TMPL / "features"

FEAT.mkdir(parents=True, exist_ok=True)

raw   = SRC.read_text(encoding="utf-8")
lines = raw.splitlines(keepends=True)   # 0-based, lines[0] = line 1

def get(start, end):
    """Return lines[start-1 .. end-1]  (1-based, inclusive)."""
    return "".join(lines[start-1:end])


# ── CSS slice helpers ────────────────────────────────────────────────────────

css_core         = get(26,  200)    # :root, body, sidebar, sort-bar, table, workspace, card
css_detail       = get(381, 465)    # detail panel, ctx-menu
css_rest         = get(562, 985)    # zoom, research, fields, ticker-editor, summary, cases,
                                    # toast, loader, empty, sidebar ticker-line, RC card,
                                    # ETF, RPO, SEC, TOS, Finnhub, scrollbar, mobile

css_chart        = get(201, 315)
css_ep_brief     = get(317, 327)
css_table_view   = get(329, 379)
css_add_ticker   = get(466, 522)
css_csv_import   = get(524, 560)
css_crisis       = get(987, 1011)

# ── HTML slice helpers ───────────────────────────────────────────────────────

html_tabbar      = get(1016, 1022)
html_filterbar   = get(1024, 1057)
html_table_view  = get(1059, 1083)   # includes closing comment line
html_shared      = get(1085, 1101)   # loader, toast, chart-ctx-menu
html_add_ticker  = get(1103, 1175)
html_csv_import  = get(1177, 1217)
html_watchlist   = get(1219, 1252)   # wl-modal + ctx-menu
html_mobile      = get(1254, 1257)
html_sidebar     = get(1261, 1354)
html_ep_brief    = get(1359, 1393)
html_chart       = get(1396, 1434)
html_detail      = get(1437, 1449)
html_crisis      = get(1454, 1494)

# ── JavaScript (entire <script> block content) ──────────────────────────────

js_block         = get(1498, 5438)   # JS content only — </script> excluded (line 5439)

# ── Head fixed lines (1-8) ──────────────────────────────────────────────────

head_fixed       = get(1, 9)         # DOCTYPE … <style>  (line 9 = <style>)

# ════════════════════════════════════════════════════════════════════════════
# Write feature files
# ════════════════════════════════════════════════════════════════════════════

def write_feature(name, content):
    p = FEAT / name
    p.write_text(content, encoding="utf-8")
    print(f"  wrote {p}  ({len(content.splitlines())} lines)")

# ── features/chart.html ──────────────────────────────────────────────────────
write_feature("chart.html",
f"""<style>
{css_chart.rstrip()}
</style>

{html_chart.rstrip()}
""")

# ── features/episode_brief.html ─────────────────────────────────────────────
write_feature("episode_brief.html",
f"""<style>
{css_ep_brief.rstrip()}
</style>

{html_ep_brief.rstrip()}
""")

# ── features/table_view.html ─────────────────────────────────────────────────
write_feature("table_view.html",
f"""<style>
{css_table_view.rstrip()}
</style>

{html_table_view.rstrip()}
""")

# ── features/add_ticker.html ─────────────────────────────────────────────────
write_feature("add_ticker.html",
f"""<style>
{css_add_ticker.rstrip()}
</style>

{html_add_ticker.rstrip()}
""")

# ── features/csv_import.html ─────────────────────────────────────────────────
write_feature("csv_import.html",
f"""<style>
{css_csv_import.rstrip()}
</style>

{html_csv_import.rstrip()}
""")

# ── features/watchlist.html (no separate CSS — uses core) ───────────────────
write_feature("watchlist.html",
f"""{html_watchlist.rstrip()}
""")

# ── features/crisis_monitor.html ─────────────────────────────────────────────
write_feature("crisis_monitor.html",
f"""<style>
{css_crisis.rstrip()}
</style>

{html_crisis.rstrip()}
""")

# ── features/heatmap.html (stub) ─────────────────────────────────────────────
write_feature("heatmap.html",
"""<!-- ═══ FEATURE: HEATMAP — STUB ══════════════════════════════════
 *  HTML, CSS and JS for the heatmap view will be added here.
 *  JS should be loaded via a separate <script> block below.
 * ═══════════════════════════════════════════════════════════════ -->
""")


# ════════════════════════════════════════════════════════════════════════════
# Write shell template:  templates/widget_v2.html
# ════════════════════════════════════════════════════════════════════════════

shell = f"""{head_fixed.rstrip()}
{{% raw %}}
/*
 * ══════════════════════════════════════════════════════════════════
 * FEATURE INDEX — templates/widget_v2.html  (Jinja2 shell)
 * ══════════════════════════════════════════════════════════════════
 * Feature CSS + HTML live in templates/features/<feature>.html
 * All JS remains in the <script> block at the bottom of this file.
 * ══════════════════════════════════════════════════════════════════
 */

/* ══ CORE ══════════════════════════════════════════════════════════ */
{css_core.rstrip()}

/* ══ DETAIL PANEL / CTX-MENU ══════════════════════════════════════ */
{css_detail.rstrip()}

/* ══ REMAINING SHARED CSS (zoom, research, fields, mobile …) ══════ */
{css_rest.rstrip()}
{{% endraw %}}
</style>
</head>
<body>

{html_tabbar.rstrip()}

{html_filterbar.rstrip()}

{{% include 'features/table_view.html' %}}

{html_shared.rstrip()}

{{% include 'features/add_ticker.html' %}}

{{% include 'features/csv_import.html' %}}

{{% include 'features/watchlist.html' %}}

{html_mobile.rstrip()}

<div class="layout" id="main-layout">

  <!-- ── Sidebar ──────────────────────────────────────────────── -->
  {html_sidebar.rstrip()}

  <!-- ── Workspace ──────────────────────────────────────────────── -->
  <div class="workspace" id="workspace">

    {{% include 'features/episode_brief.html' %}}

    {{% include 'features/chart.html' %}}

    {html_detail.rstrip()}

  </div>
</div><!-- /#main-layout -->

{{% include 'features/crisis_monitor.html' %}}

{{% include 'features/heatmap.html' %}}

<script>
{{% raw %}}
{js_block.rstrip()}
{{% endraw %}}
</script>

</body>
</html>
"""

out = TMPL / "widget_v2.html"
out.write_text(shell, encoding="utf-8")
print(f"\n  wrote {out}  ({len(shell.splitlines())} lines)")

print("\nDone. Now update server.py:  send_file -> render_template")

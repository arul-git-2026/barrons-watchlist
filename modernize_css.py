import os
import re

css_path = os.path.join("widget_v3", "styles.css")
with open(css_path, "r", encoding="utf-8") as f:
    css = f.read()

# 1. Fix HTML comments inside CSS
css = re.sub(r'<!--(.*?)-->', r'/*\1*/', css)

# 2. Modernize :root Palette (Dark Mode Default)
new_root = """
:root {
  /* Ultra-Premium Dark Mode Palette */
  --primary: #0f172a;        /* Deep background */
  --accent: #3b82f6;         /* Vibrant blue */
  --accent-light: rgba(59, 130, 246, 0.15);
  --bg: #0f172a;             /* Main background */
  --up: #10b981;             --up-bg: rgba(16, 185, 129, 0.1);
  --down: #ef4444;           --down-bg: rgba(239, 68, 68, 0.1);
  --neutral: #94a3b8;
  --border: rgba(255, 255, 255, 0.1); 
  --card: #1e293b;           /* Surface color */
  --text: #f8fafc;           /* Primary text */
  --muted: #94a3b8;          /* Secondary text */
  --font: 'Inter', system-ui, sans-serif;
  --radius: 12px;
}
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Outfit:wght@500;700&family=JetBrains+Mono:wght@400;700&display=swap');
"""
# Replace the original :root and @import
css = re.sub(r':root\s*\{.*?\}.*?@import.*?;', new_root, css, flags=re.DOTALL)

# 3. Modernize Typography
css = css.replace("'Google Sans'", "'Inter'")
css = css.replace("'Roboto Mono'", "'JetBrains Mono'")

# 4. Inject glassmorphism and modern UI touches
# Sidebar Header
css = css.replace('.sidebar-header{', '.sidebar-header{\n  background: rgba(30, 41, 59, 0.8);\n  backdrop-filter: blur(12px);')
# Sidebar layout
css = css.replace('.sidebar{', '.sidebar{\n  background: var(--primary);')
# Top filter bar
css = css.replace('.top-filter-bar{', '.top-filter-bar{\n  background: rgba(30, 41, 59, 0.6);\n  backdrop-filter: blur(10px);\n  border: 1px solid rgba(255,255,255,0.05);')
# Modals / Panel
css = css.replace('.add-ticker-modal{', '.add-ticker-modal{\n  background: rgba(30, 41, 59, 0.85);\n  backdrop-filter: blur(16px);\n  border: 1px solid rgba(255,255,255,0.1);\n  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);')
css = css.replace('.card{', '.card{\n  background: var(--card);\n  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);')
# Context menu
css = css.replace('#ctx-menu{', '#ctx-menu{\n  background: rgba(30, 41, 59, 0.9);\n  backdrop-filter: blur(12px);\n  border: 1px solid rgba(255,255,255,0.1);')
# Ticker active row glow
css = css.replace('.row.active td:first-child{box-shadow:inset 3px 0 0 var(--accent)}', '.row.active td:first-child{box-shadow:inset 4px 0 0 var(--accent)}')

# Change table header styles for modern look
css = css.replace('thead th{', 'thead th{\n  background: rgba(15, 23, 42, 0.9);\n  backdrop-filter: blur(8px);')

# Convert generic background colors to var(--card) or transparent
css = css.replace('background:#fff;', 'background:var(--card);')
css = css.replace('background:#f8f9fa;', 'background:var(--bg);')
css = css.replace('background:#202124;', 'background:#0f172a;')

# Make source badges modern
css = css.replace('background:#dbeafe;color:#1e40af', 'background:rgba(59,130,246,0.15);color:#60a5fa')
css = css.replace('background:#ede9fe;color:#5b21b6', 'background:rgba(139,92,246,0.15);color:#a78bfa')
css = css.replace('background:#f1f5f9;color:#475569', 'background:rgba(148,163,184,0.15);color:#cbd5e1')
css = css.replace('background:#e0f2fe;color:#0369a1', 'background:rgba(14,165,233,0.15);color:#38bdf8')
css = css.replace('background:#fef9c3;color:#92400e', 'background:rgba(234,179,8,0.15);color:#fde047')
css = css.replace('background:#dcfce7;color:#15803d', 'background:rgba(34,197,94,0.15);color:#4ade80')

with open(css_path, "w", encoding="utf-8") as f:
    f.write(css)

print("CSS Modernized Successfully!")

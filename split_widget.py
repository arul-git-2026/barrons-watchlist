import os
import re

source_file = "widget_v2.html"
output_dir = "widget_v3"

if not os.path.exists(output_dir):
    os.makedirs(output_dir)

with open(source_file, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Extract CSS
style_match = re.search(r'<style>(.*?)</style>', content, re.DOTALL)
css_content = style_match.group(1).strip() if style_match else ""

# 2. Extract Main JS (Last script tag)
scripts = list(re.finditer(r'<script(?:[^>]*)>(.*?)</script>', content, re.DOTALL))
if scripts:
    main_script_match = scripts[-1] # Assuming the last script tag holds the main app logic
    js_content = main_script_match.group(1).strip()
    
    # Remove to rebuild HTML
    html_content = content[:main_script_match.start()] + '<script src="app.js"></script>' + content[main_script_match.end():]
else:
    js_content = ""
    html_content = content

# Replace style block
if style_match:
    html_content = html_content[:style_match.start()] + '<link rel="stylesheet" href="styles.css">\n' + html_content[style_match.end():]

# 3. Write files
with open(os.path.join(output_dir, "styles.css"), "w", encoding="utf-8") as f:
    f.write(css_content)

with open(os.path.join(output_dir, "app.js"), "w", encoding="utf-8") as f:
    f.write(js_content)

with open(os.path.join(output_dir, "index.html"), "w", encoding="utf-8") as f:
    f.write(html_content)

print(f"Successfully split {source_file} into {output_dir}/")

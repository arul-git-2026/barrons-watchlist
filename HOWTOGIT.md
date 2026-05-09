Here's the workflow for this project:

## Deploy a change

**On your PC (in the streetwise folder):**
```bash
# Stage the changed files
git add server.py widget_v2.html

# Commit
git commit -m "your message here"

# Push to GitHub
git push
```

**Then SSH into the server:**
```bash
ssh -i C:\Users\vasanthaganesh.arulm\Downloads\claude\streetwise\ssh-key-2026-03-27.key opc@132.145.243.138
```

**On the server:**
```bash
cd /opt/streetwise
git pull
sudo systemctl restart streetwise
```

---

## Check what's changed before committing
```bash
git status          # which files are modified
git diff            # see the actual changes
git diff server.py  # changes in a specific file
```

---

## See recent commits
```bash
git log --oneline -10
```

---

## Undo uncommitted changes to a file
```bash
git checkout -- server.py   # discard local edits (cannot be undone)
```

---

**Key facts for this project:**
- Active branch: `feature/research`
- Remote: GitHub (`arul-git-2026/barrons-watchlist`)
- Files that matter: `server.py`, `widget_v2.html`
- Never commit: `.env`, `*.db`, `ssh-key-*.key`
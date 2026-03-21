#!/bin/bash
# Run this once to push all backend files to GitHub
# Usage: open Terminal, then run:
#   cd /path/to/CollectLocal-Backend && bash push_to_github.sh

git init
git add scraper.py requirements.txt setup_supabase.sql .env.example .gitignore
mkdir -p .github/workflows
git add .github/workflows/scrape.yml
git commit -m "Initial backend scraper pipeline"
git branch -M main
git remote add origin https://github.com/khantinuity7/collectlocal-backend.git
git push -u origin main

echo ""
echo "✅ Done! Files pushed to GitHub."
echo "Next: go to Settings → Secrets and variables → Actions to add your secrets."

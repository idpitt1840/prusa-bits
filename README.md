Prusa materials export/import for Prusa Connect Local.  This was generated with Google's Antigravity against the Prusa SDK Here: https://github.com/prusa3d/prusa-connect/tree/main/sdk

Usage Examples
1. Export over HTTP (Bearer Token or Username/Password)
   
# Using a Bearer token (created in UI or POST /api/v1/auth/credentials)
./prusa_materials_sync.py export \
  --url http://192.168.1.100:8080 \
  --token "pcl_..." \
  -o materials-backup.json

# Or using a local username/password
./prusa_materials_sync.py export \
  --url http://192.168.1.100:8080 \
  --username admin --password "..." \
  -o materials-backup.json

2. Export with Exact SQLite consumed_weight_g (Standalone or Hybrid)

# Offline directly from SQLite (safe while server runs)
./prusa_materials_sync.py export \
  --sqlite-db /path/to/database.sqlite \
  -o materials-backup.json

# Hybrid: live HTTP API + SQLite `consumed_weight_g` & all users' custom materials
./prusa_materials_sync.py export \
  --url http://192.168.1.100:8080 --token "pcl_..." \
  --sqlite-db /path/to/database.sqlite \
  -o materials-backup.json

3. Inspect Per-Material Consumption Summary

./prusa_materials_sync.py summary materials-backup.json

4. Import into Another Instance

# Preview first without writing anything
./prusa_materials_sync.py import \
  --url http://192.168.1.200:8080 --token "pcl_..." \
  -i materials-backup.json --dry-run

# Run HTTP import (add --ensure-catalogue to download OpenPrintTag DB on a fresh server)
./prusa_materials_sync.py import \
  --url http://192.168.1.200:8080 --token "pcl_..." \
  --ensure-catalogue \
  -i materials-backup.json

# Or lossless offline SQLite import (preserves exact consumed_weight_g & NFC UUIDs; stop server first)
./prusa_materials_sync.py import \
  --sqlite-db /path/to/target/database.sqlite \
  -i materials-backup.json

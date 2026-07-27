#!/usr/bin/env bash
# Schedule the morning brief at 7:00 AM weekdays via launchd (macOS-native cron).
# Output lands in ~/Desktop/morning-brief.txt. Uninstall: launchctl unload + rm the plist.
set -euo pipefail
PLATFORM_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.cos.morning-brief.plist"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.cos.morning-brief</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>cd "$PLATFORM_DIR" &amp;&amp; ./scripts/brief.sh > "$HOME/Desktop/morning-brief.txt" 2>&amp;1</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  </array>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Scheduled: morning brief weekdays at 7:00 AM -> ~/Desktop/morning-brief.txt"
echo "(Requires Docker Desktop to be running. Test now with: launchctl start com.cos.morning-brief)"

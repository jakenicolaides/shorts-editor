#!/bin/sh
# Builds ~/Applications/Shorts Editor.app: double-click to start the server in
# Terminal (so it keeps Terminal's Dropbox access and you can see the log) and
# open the page. If the server is already up it just opens the page.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/Shorts Editor.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Shorts Editor</string>
  <key>CFBundleDisplayName</key><string>Shorts Editor</string>
  <key>CFBundleIdentifier</key><string>games.twixtle.shorts-editor</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/launch" <<LAUNCH
#!/bin/sh
ROOT="$ROOT"
URL="http://localhost:8790"
if curl -s -m 1 "\$URL/jobs" >/dev/null 2>&1; then
  open "\$URL"; exit 0
fi
osascript <<'AS'
tell application "Terminal"
  activate
  do script "cd \"$ROOT\" && exec .venv/bin/python run.py"
end tell
AS
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  sleep 1
  if curl -s -m 1 "\$URL/jobs" >/dev/null 2>&1; then open "\$URL"; exit 0; fi
done
osascript -e 'display alert "Shorts Editor" message "The server did not start. Check the Terminal window for the error."'
LAUNCH
chmod +x "$APP/Contents/MacOS/launch"

# icon: a plain rounded square with a play mark, drawn with sips-free tools (Python + no deps)
"$ROOT/.venv/bin/python" - "$APP/Contents/Resources" <<'PY'
import sys, struct, zlib
out = sys.argv[1]
def png(size):
    rows = []
    c = size / 2; r = size * 0.42
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx, dy = x - c, y - c
            # rounded square background
            rad = size * 0.22
            ax, ay = abs(dx) - (r - rad), abs(dy) - (r - rad)
            d = ((max(ax, 0) ** 2 + max(ay, 0) ** 2) ** 0.5) + min(max(ax, ay), 0)
            inside = d <= rad
            # play triangle
            tx = dx + size * 0.02; ty = dy
            tri = inside and (tx > -size * 0.16) and (tx < size * 0.2) and (abs(ty) < (size * 0.2 - tx) * 0.9)
            if tri:
                row += bytes((255, 255, 255, 255))
            elif inside:
                row += bytes((34, 170, 119, 255))
            else:
                row += bytes((0, 0, 0, 0))
        rows.append(bytes(row))
    raw = b"".join(rows)
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
import os
iconset = os.path.join(out, "icon.iconset"); os.makedirs(iconset, exist_ok=True)
for s in (16, 32, 64, 128, 256, 512):
    open(os.path.join(iconset, f"icon_{s}x{s}.png"), "wb").write(png(s))
    open(os.path.join(iconset, f"icon_{s//2}x{s//2}@2x.png"), "wb").write(png(s))
PY
iconutil -c icns "$APP/Contents/Resources/icon.iconset" -o "$APP/Contents/Resources/icon.icns" && rm -rf "$APP/Contents/Resources/icon.iconset"
touch "$APP"
echo "built: $APP"

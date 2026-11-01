#!/bin/sh
# setkv.sh FILE KEY VALUE: set KEY=VALUE in FILE (a name under MEDIAHUB_HOME, default ~/.mediahub).
#
# Replaces the first `KEY=` line, else the first commented `# KEY=` line, else appends `KEY=VALUE`.
# Drops any further `KEY=` duplicates. Writes a temporary file and moves it over the original
# (keeping its permissions), then checks that exactly one `KEY=` line exists with the new value.
# POSIX sh and POSIX awk only (works on BusyBox). Values may hold / & \ and spaces, not a newline.
# On success prints `OK setkv <KEY>` (the key name, never the value) and exits 0; on any failure a `FAIL setkv: ...` line on stderr and a non-zero exit.
#
#   compose/scripts/setkv.sh stack.env TZ 'Europe/London'
#   compose/scripts/setkv.sh secrets.env NOTIFIER telegram
if [ "$#" -ne 3 ]; then
  echo "usage: setkv.sh <file> <KEY> <value>" >&2; exit 2
fi
f="${MEDIAHUB_HOME:-$HOME/.mediahub}/$1"; tmp="$f.tmp.$$"
[ -f "$f" ] || { echo "FAIL setkv: no such file $f" >&2; exit 1; }
cp -p "$f" "$tmp" && KEY="$2" VAL="$3" awk '
  { line[NR] = $0 }
  END {
    k = ENVIRON["KEY"]; v = ENVIRON["VAL"]; hit = 0
    for (i = 1; i <= NR; i++) if (index(line[i], k "=") == 1) { hit = i; break }
    for (i = 1; i <= NR && !hit; i++) {
      s = line[i]
      if (substr(s, 1, 1) != "#") continue
      s = substr(s, 2)
      while (substr(s, 1, 1) == " " || substr(s, 1, 1) == "\t") s = substr(s, 2)
      if (index(s, k "=") == 1) hit = i
    }
    for (i = 1; i <= NR; i++) {
      if (i == hit) print k "=" v
      else if (index(line[i], k "=") != 1) print line[i]
    }
    if (!hit) print k "=" v
  }' "$f" > "$tmp" && mv "$tmp" "$f" || { rm -f "$tmp"; echo "FAIL setkv: could not write $f" >&2; exit 1; }
if [ "$(grep -c "^$2=" "$f")" != 1 ] || ! grep -qxF "$2=$3" "$f"; then
  echo "FAIL setkv: $2 in $1 is not set exactly once to the new value" >&2; exit 1
fi
echo "OK setkv $2"

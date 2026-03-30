#!/bin/bash

set -euo pipefail

MINUTES=20
TOP=10
MIN_RX=$((1024 * 1024))

usage() {
  cat <<'EOF'
Usage: background-download-report.sh [--minutes N] [--top N] [--min-rx BYTES]

Summarize active network-heavy background downloads on macOS, with extra
context for Apple system update traffic handled by nsurlsessiond/mobileassetd.

Examples:
  ./scripts/background-download-report.sh
  ./scripts/background-download-report.sh --minutes 60 --top 15
  sudo ./scripts/background-download-report.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minutes)
      MINUTES="$2"
      shift 2
      ;;
    --top)
      TOP="$2"
      shift 2
      ;;
    --min-rx)
      MIN_RX="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

run_priv() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

trim() {
  awk '{$1=$1; print}'
}

human_bytes() {
  awk -v bytes="$1" '
    function human(x) {
      split("B KB MB GB TB PB", units, " ")
      i = 1
      while (x >= 1024 && i < 6) {
        x /= 1024
        i++
      }
      if (i == 1) {
        return sprintf("%d %s", x, units[i])
      }
      return sprintf("%.2f %s", x, units[i])
    }
    BEGIN { print human(bytes + 0) }
  '
}

plist_value() {
  local plist="$1"
  local key="$2"

  /usr/bin/plutil -extract "$key" raw -o - "$plist" 2>/dev/null || true
}

TMPDIR="$(mktemp -d /tmp/background-download-report.XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

TCP_FILE="$TMPDIR/tcp.txt"
UDP_FILE="$TMPDIR/udp.txt"
CONN_FILE="$TMPDIR/connections.tsv"
PROC_FILE="$TMPDIR/processes.tsv"
STAGED_FILE="$TMPDIR/staged.tsv"

run_priv netstat -anvp tcp > "$TCP_FILE" 2>/dev/null || true
run_priv netstat -anvp udp > "$UDP_FILE" 2>/dev/null || true

awk '
  function local_remote(addr) {
    return addr ~ /^127\./ || addr ~ /^::1\./ || addr ~ /^10\./ || addr ~ /^192\.168\./ || addr ~ /^172\.(1[6-9]|2[0-9]|3[0-1])\./ || addr ~ /^fe80:/ || addr ~ /^fd/ || addr ~ /^fc/
  }
  $1 ~ /^tcp/ && $6 == "ESTABLISHED" {
    pid = $11
    rx = $7 + 0
    tx = $8 + 0
    local = $4
    remote = $5
    if (pid ~ /^[0-9]+$/ && rx > 0 && remote !~ /^\*\.\*/ && !local_remote(remote)) {
      print rx "\t" tx "\t" pid "\t" $1 "\t" $6 "\t" local "\t" remote
    }
  }
' "$TCP_FILE" > "$CONN_FILE"

awk '
  function local_remote(addr) {
    return addr ~ /^127\./ || addr ~ /^::1\./ || addr ~ /^10\./ || addr ~ /^192\.168\./ || addr ~ /^172\.(1[6-9]|2[0-9]|3[0-1])\./ || addr ~ /^fe80:/ || addr ~ /^fd/ || addr ~ /^fc/
  }
  $1 ~ /^udp/ {
    pid = $10
    rx = $6 + 0
    tx = $7 + 0
    local = $4
    remote = $5
    if (pid ~ /^[0-9]+$/ && rx > 0 && remote !~ /^\*\.\*/ && !local_remote(remote)) {
      print rx "\t" tx "\t" pid "\t" $1 "\t" "-" "\t" local "\t" remote
    }
  }
' "$UDP_FILE" >> "$CONN_FILE"

awk -F'\t' '
  {
    rx[$3] += $1
    tx[$3] += $2
    if (remotes[$3] == "") {
      remotes[$3] = $7
      remote_count[$3] = 1
      seen[$3, $7] = 1
    } else if (!(($3 SUBSEP $7) in seen) && remote_count[$3] < 3) {
      remotes[$3] = remotes[$3] ", " $7
      remote_count[$3]++
      seen[$3, $7] = 1
    }
  }
  END {
    for (pid in rx) {
      print rx[pid] "\t" tx[pid] "\t" pid "\t" remotes[pid]
    }
  }
' "$CONN_FILE" > "$PROC_FILE"

echo "Background Download Report"
echo "Generated: $(date)"
echo

echo "Top active receivers by process"
printf "%-10s %-10s %-7s %-24s %s\n" "RX" "TX" "PID" "PROCESS" "REMOTE"

found_connection=0
while IFS=$'\t' read -r rx tx pid remote; do
  [[ -z "$pid" ]] && continue
  if (( rx < MIN_RX )); then
    continue
  fi

  proc_name="$(ps -p "$pid" -o comm= 2>/dev/null | trim | sed 's#.*/##')"
  [[ -z "$proc_name" ]] && proc_name="<exited>"
  printf "%-10s %-10s %-7s %-24s %s\n" \
    "$(human_bytes "$rx")" \
    "$(human_bytes "$tx")" \
    "$pid" \
    "$proc_name" \
    "$remote"
  found_connection=1
done < <(sort -nr -k1,1 "$PROC_FILE" | head -n "$TOP")

if [[ "$found_connection" -eq 0 ]]; then
  echo "No active remote connections above $(human_bytes "$MIN_RX") found."
fi

echo
echo "Recent nsurlsessiond bundle IDs (last ${MINUTES}m)"

ns_bundles="$(
  log show --style compact --last "${MINUTES}m" \
    --predicate 'process == "nsurlsessiond" AND eventMessage CONTAINS[c] "bundle id:"' 2>/dev/null \
    | perl -ne 'print "$1\n" if /bundle id:\s*([^,]+)/i' \
    | grep -v '^<private>$' \
    | sort \
    | uniq -c \
    | sort -nr || true
)"

if [[ -n "$ns_bundles" ]]; then
  while IFS= read -r line; do
    count="$(echo "$line" | awk '{print $1}')"
    bundle="$(echo "$line" | cut -d' ' -f2- | trim)"
    echo "- $bundle ($count)"
  done <<< "$ns_bundles"
else
  echo "- No bundle IDs exposed in recent nsurlsessiond logs."
fi

echo
echo "Software Update status"
current_product="$(sw_vers -productName 2>/dev/null || true)"
current_version="$(sw_vers -productVersion 2>/dev/null || true)"
current_build="$(sw_vers -buildVersion 2>/dev/null || true)"
echo "- Current OS: ${current_product:-macOS} ${current_version:-unknown} (${current_build:-unknown})"

desc_line="$(
  log show --style compact --last "${MINUTES}m" \
    --predicate 'process == "softwareupdated" AND eventMessage CONTAINS[c] "ChosenDescriptor:"' 2>/dev/null \
    | tail -n 1 || true
)"

if [[ -n "$desc_line" ]]; then
  chosen_update="$(echo "$desc_line" | sed -nE 's/.* SU:(.*) SFR:.*/\1/p')"
  if [[ -n "$chosen_update" ]]; then
    echo "- Chosen update: $chosen_update"
  fi
fi

progress_bytes="$(
  log show --style compact --last "${MINUTES}m" \
    --predicate 'process == "softwareupdated" AND eventMessage CONTAINS[c] "totalWrittenBytes:"' 2>/dev/null \
    | tail -n 1 \
    | sed -nE 's/.*totalWrittenBytes:([0-9]+) totalExpectedBytes:([0-9]+).*/\1 \2/p' || true
)"

if [[ -n "$progress_bytes" ]]; then
  written="$(echo "$progress_bytes" | awk '{print $1}')"
  expected="$(echo "$progress_bytes" | awk '{print $2}')"
  portion="$(awk -v a="$written" -v b="$expected" 'BEGIN { if (b > 0) printf "%.1f", (a / b) * 100; else print "0.0" }')"
  echo "- Download progress: $(human_bytes "$written") / $(human_bytes "$expected") (${portion}%)"
else
  echo "- No recent softwareupdated download progress found."
fi

auto_check="$(run_priv softwareupdate --schedule 2>/dev/null || true)"
if [[ -n "$auto_check" ]]; then
  echo "- $auto_check"
fi

echo
echo "Staged mobile assets"

find /System/Library/AssetsV2/downloadDir -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null \
  | while IFS= read -r dir; do
      [[ -z "$dir" ]] && continue
      size_kb="$(du -sk "$dir" 2>/dev/null | awk '{print $1}')"
      size_bytes=$((size_kb * 1024))
      name="$(basename "$dir")"
      extra=""

      if [[ -f "$dir/AssetData/boot/SystemVersion.plist" ]]; then
        target_version="$(plist_value "$dir/AssetData/boot/SystemVersion.plist" ProductVersion)"
        target_build="$(plist_value "$dir/AssetData/boot/SystemVersion.plist" ProductBuildVersion)"
        if [[ -n "$target_version" || -n "$target_build" ]]; then
          extra=" -> macOS ${target_version:-unknown} (${target_build:-unknown})"
        fi
      fi

      printf "%s\t%s\t%s\n" "$size_bytes" "$name" "$extra"
    done | sort -nr -k1,1 > "$STAGED_FILE"

if [[ -s "$STAGED_FILE" ]]; then
  while IFS=$'\t' read -r size_bytes name extra; do
    echo "- $name ($(human_bytes "$size_bytes"))$extra"
  done < "$STAGED_FILE"
else
  echo "- No staged assets found in /System/Library/AssetsV2/downloadDir"
fi

echo
echo "Recent mobileassetd clients (last ${MINUTES}m)"

mobile_clients="$(
  log show --style compact --last "${MINUTES}m" \
    --predicate 'process == "mobileassetd" AND eventMessage CONTAINS[c] "assetType:" AND eventMessage CONTAINS[c] "client:"' 2>/dev/null \
    | perl -ne 'print "$1\t$2\n" if /assetType:\s*([^ ]+)\s+client:\s*([^,]+)/' \
    | awk -F'\t' '{ count[$1 FS $2]++ } END { for (key in count) { split(key, parts, FS); print count[key] FS parts[1] FS parts[2] } }' \
    | sort -nr || true
)"

if [[ -n "$mobile_clients" ]]; then
  printed_mobile_client=0
  while IFS=$'\t' read -r count asset_type client; do
    if [[ -n "$asset_type" && -n "$client" ]]; then
      echo "- $client -> $asset_type ($count)"
      printed_mobile_client=1
    fi
  done <<< "$mobile_clients"

  if [[ "$printed_mobile_client" -eq 0 ]]; then
    echo "- No recent mobileassetd client activity found."
  fi
else
  echo "- No recent mobileassetd client activity found."
fi

echo
echo "Tip: run this with sudo for the most complete socket list:"
echo "  sudo ./macos-background-download-report/run.sh"


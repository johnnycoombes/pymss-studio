#!/usr/bin/env bash
set -euo pipefail

DESTINATION="${1:-src-tauri/resources/bin}"
mkdir -p "$DESTINATION/licenses" "$DESTINATION/lib"
for tool in ffmpeg ffprobe aria2c; do
  test -x "/usr/bin/$tool"
  cp "/usr/bin/$tool" "$DESTINATION/$tool"
  # ldd lists the transitive dependencies as well. Keep only glibc and its
  # loader on the host; other SONAMEs must travel with these Ubuntu binaries.
  while IFS= read -r library; do
    name="$(basename "$library")"
    case "$name" in
      ld-linux-*.so.*|libc.so.*|libm.so.*|libpthread.so.*|libdl.so.*|librt.so.*|libresolv.so.*|libutil.so.*) continue ;;
    esac
    cp -L "$library" "$DESTINATION/lib/$name"
    owner="$(dpkg-query -S "$library" 2>/dev/null | head -n 1 | sed 's/: \/.*//' || true)"
    owner="${owner%%:*}"
    if [[ -n "$owner" && -f "/usr/share/doc/$owner/copyright" ]]; then
      cp "/usr/share/doc/$owner/copyright" "$DESTINATION/licenses/${owner//:/-}-copyright"
    fi
  done < <(ldd "/usr/bin/$tool" | awk '$2 == "=>" && $3 ~ /^\// {print $3}')
  patchelf --set-rpath '$ORIGIN/lib' "$DESTINATION/$tool"
done
for library in "$DESTINATION"/lib/*; do
  patchelf --set-rpath '$ORIGIN' "$library"
done
for package in ffmpeg aria2; do
  cp "/usr/share/doc/$package/copyright" "$DESTINATION/licenses/$package-copyright"
done
cat > "$DESTINATION/THIRD_PARTY_TOOLS.txt" <<'TXT'
FFmpeg and ffprobe: https://ffmpeg.org/
Source packages: https://archive.ubuntu.com/ubuntu/pool/universe/f/ffmpeg/
aria2: https://aria2.github.io/
Source packages: https://archive.ubuntu.com/ubuntu/pool/universe/a/aria2/
These Ubuntu binaries are redistributed with their copyright and license notices
in the licenses directory. Shared libraries other than glibc are bundled in lib.
TXT
dpkg-query -W -f='${Package} ${Version}\n' ffmpeg aria2 >> "$DESTINATION/THIRD_PARTY_TOOLS.txt"

#!/bin/sh
# Link aibus into ~/.local/bin. Run it again after moving the clone.
set -eu
dir=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.local/bin"
ln -sfn "$dir/aibus" "$HOME/.local/bin/aibus"
echo "linked $HOME/.local/bin/aibus -> $dir/aibus"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "note: $HOME/.local/bin is not on PATH" ;;
esac

#!/bin/bash
# Bleed Tool — launcher macOS / Linux

cd "$(dirname "$0")"

echo ""
echo "  Bleed Tool"
echo "  =========="
echo ""

# Znajdz Python 3
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" --version 2>&1 | grep -oP '\d+\.\d+')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

# Fallback: sciezki Homebrew / pyenv
if [ -z "$PYTHON" ]; then
    for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$HOME/.pyenv/shims/python3"; do
        if [ -x "$p" ]; then
            PYTHON="$p"
            break
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    echo "  BLAD: Nie znaleziono Python 3.10+"
    echo "  Zainstaluj: https://www.python.org/downloads/"
    read -p "  Nacisnij Enter..."
    exit 1
fi

echo "  Python: $($PYTHON --version) @ $PYTHON"

# Sprawdz zaleznosci
$PYTHON -c "import customtkinter, fitz, numpy, PIL" 2>/dev/null
if [ $? -ne 0 ]; then
    echo ""
    echo "  Instaluje brakujace zaleznosci..."
    $PYTHON -m pip install --user -r requirements.txt
    echo ""
fi

# Uruchom GUI
echo "  Uruchamiam..."
echo ""
$PYTHON bleed_app.py

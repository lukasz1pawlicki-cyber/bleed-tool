#!/bin/bash
# Bleed Tool — launcher macOS / Linux

cd "$(dirname "$0")"

echo ""
echo "  Bleed Tool"
echo "  =========="
echo ""

# Znajdz Python 3.10+
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" --version 2>&1 | sed -n 's/Python \([0-9]*\.[0-9]*\).*/\1/p')
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
    echo ""
    read -p "  Nacisnij Enter..."
    exit 1
fi

echo "  Python: $($PYTHON --version) @ $PYTHON"

# Sprawdz zaleznosci (cairosvg opcjonalny — wymaga natywnego libcairo,
# program dziala bez niego; SVG input wtedy niedostepny)
$PYTHON -c "import PyQt6, fitz, numpy, PIL" 2>/dev/null
if [ $? -ne 0 ]; then
    echo ""
    echo "  Instaluje brakujace zaleznosci..."
    $PYTHON -m pip install --user -r requirements.txt
    echo ""
    # Sprawdz ponownie
    $PYTHON -c "import PyQt6, fitz, numpy, PIL" 2>/dev/null
    if [ $? -ne 0 ]; then
        echo "  BLAD: Instalacja bibliotek nie powiodla sie!"
        echo "  Sprobuj recznie:  $PYTHON -m pip install -r requirements.txt"
        echo ""
        read -p "  Nacisnij Enter..."
        exit 1
    fi
    echo "  Biblioteki zainstalowane pomyslnie."
    echo ""
fi

# Uruchom GUI w tle i zamknij terminal
echo "  Uruchamiam..."
echo ""
$PYTHON bleed_app.py &
sleep 1
osascript -e 'tell application "Terminal" to close front window' 2>/dev/null &
exit 0

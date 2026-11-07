"""Terminal-native artwork derived from the user's supplied GitHub reference."""
from pathlib import Path

OWNER = "YAN SUZANO"
TAGLINE = "IDEIAS QUE VIRAM SOFTWARE"
PORTRAIT = (Path(__file__).with_name("assets") / "portrait.txt").read_text().splitlines()
FONT = {
    "D": ["█▀▄", "█ █", "▀▀ "], "E": ["█▀▀", "█▀▀", "▀▀▀"],
    "V": ["█ █", "█ █", " ▀ "], "S": ["█▀▀", "▀▀█", "▀▀▀"],
    "W": ["█ █ █", "█ █ █", " ▀ ▀ "], "I": ["█", "█", "▀"],
    "T": ["▀█▀", " █ ", " ▀ "], "C": ["█▀▀", "█  ", "▀▀▀"],
    "H": ["█ █", "█▀█", "▀ ▀"],
}
WORDMARK = [" ".join(FONT[c][row] for c in "DEVSWITCH") for row in range(3)]

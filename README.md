# fix-english

Tools for a non-native speaker writing English to AI coding agents:

- `fix-english`: reads a draft on stdin and prints it with the grammar and spelling fixed and any Chinese translated. Use it as an editor filter.
- `fix-english-popup`: a compact dialog in the style of a Windows password prompt (the screen dims behind it), opened with a hotkey, for writing a message with help. It shows fixed and improved versions, a Chinese back-translation to check the meaning, learning notes, and answers to follow-up questions. Then it pastes the chosen version where the cursor was.

## Setup

```sh
ln -s ~/Work/fix-english/fix-english ~/.local/bin/fix-english
ln -s ~/Work/fix-english/fix-english-popup ~/.local/bin/fix-english-popup
mkdir -p ~/.config/fix-english && cp config.example.toml ~/.config/fix-english/config.toml
```

The default provider is `claude`: it runs `claude -p --model haiku` on your Claude subscription. The config also lists a local llama.cpp server and several free API providers.

The popup needs GTK4 and libadwaita (PyGObject), `wl-copy`, `wtype` and Hyprland. Its UI follows the GNOME HIG: a header bar with Cancel and Insert, and boxed lists for versions, meaning, notes and questions. Add this to `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + ALT + E", "Fix English", os.getenv("HOME") .. "/.local/bin/fix-english-popup")
```

Add this to `~/.config/hypr/hyprland.lua`:

```lua
o.window("^uno\\.guan810\\.FixEnglish$", {
  float = true, center = true, size = { 760, 640 }, pin = true,
  dim_around = true, rounding = 12, border_size = 0, tag = "-default-opacity",
})
```

## CLI

```sh
echo "why the test is fail" | fix-english             # -> Why is the test failing?
fix-english --json < draft.txt                        # fixed, improved, 含义 check, notes
fix-english --json --stream < draft.txt               # one JSON line per finished section
fix-english --json < d.txt | fix-english --ask "why not ensure?"
fix-english --bench | --list | --models -p NAME
```

Every change is logged to `~/.local/share/fix-english/log.jsonl`.

## Popup keys

| Key | Action |
|---|---|
| Ctrl+Enter | analyze the draft |
| Alt+1 / 2 / 3 | pick fixed / improved / my draft |
| Ctrl+Shift+Enter | paste the picked version into the previous window |
| Enter in the Ask box | ask a follow-up question |
| Esc | close without pasting |

The text goes back through the clipboard and a paste shortcut, not typed keystrokes. The paste shortcut is Ctrl+Shift+V in terminals and Ctrl+V elsewhere. Typed keystrokes would go through fcitx5, and a newline would send a chat message. The pasted text stays on the clipboard afterwards.

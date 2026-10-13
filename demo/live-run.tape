# Records a real, unedited live run: Gemma 4 (agent) and d3-flash (judge), both local.
# Render from the repository root:  vhs demo/live-run.tape

Output demo/flight-recorder-live.mp4

Require ollama

Set Shell zsh
Set FontFamily "Menlo"
Set FontSize 15
Set Width 1600
Set Height 960
Set Padding 28
Set LineHeight 1.25
Set TypingSpeed 28ms
Set WaitPattern /❯$/
Set WaitTimeout 1800s
Set Theme { "name": "flight-recorder", "background": "#0F1B2D", "foreground": "#E4EAF1", "cursor": "#FF6A30", "selection": "#263448", "black": "#0C131D", "red": "#F2776C", "green": "#55C891", "yellow": "#E5AA44", "blue": "#7AA7E0", "magenta": "#C792EA", "cyan": "#7CC4E8", "white": "#E4EAF1", "brightBlack": "#93A1B4", "brightRed": "#FF8E5E", "brightGreen": "#7FE0AE", "brightYellow": "#F5C76A", "brightBlue": "#9DC1F0", "brightMagenta": "#DDB3F5", "brightCyan": "#A6DCF2", "brightWhite": "#FFFFFF" }

Hide
Type "setopt interactivecomments && cd poc && export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH PY=$HOME/Models/d3-flash/.venv/bin/python PROMPT='%F{208}❯%f ' RPROMPT='' && clear" Enter
Sleep 1s
Show

Type "# Agent: Gemma 4 12B via Ollama.  Judge: d3-flash, local decision model.  Nothing leaves this machine."
Enter
Sleep 1.5s
Type "# Every tool call goes through the gateway: rules → judge → route → (human) → execute. All of it recorded."
Enter
Sleep 2s
Type "$PY run_demo.py --agent live --narrate --out runs/demo -s refund-overreach -s injected-ticket -s goodwill-credit -s make-ci-green -s secret-exfil 2>/dev/null"
Sleep 1s
Enter

Sleep 2s
Wait
Sleep 4s

Type "clear" Enter
Type "# Now try to rewrite history: edit a verdict, delete an entry, or edit and re-hash without the key."
Enter
Sleep 1s
Type "$PY verify.py runs/demo/flight-recorder.jsonl --tamper-demo"
Sleep 0.5s
Enter
Sleep 6s

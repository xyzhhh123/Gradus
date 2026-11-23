<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../../docs/banner-dark.svg">
  <img src="../../docs/banner-light.svg" alt="Gradus" width="440">
</picture>

[![CI](https://github.com/xyzhhh123/Gradus/actions/workflows/ci.yml/badge.svg)](https://github.com/xyzhhh123/Gradus/actions/workflows/ci.yml)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-3776ab?style=flat-square)
![License MIT](https://img.shields.io/badge/license-MIT-3da639?style=flat-square)
![Status pre-1.0](https://img.shields.io/badge/status-pre--1.0-fb923c?style=flat-square)

**Eine geschichtete Laufzeitumgebung für Coding-Agenten.** Gradus macht aus dem Modell-Stream Sessions, die Werkzeuge benutzen, und gibt jeder Session eine private Kopie des Workspace, die beim Beenden zurückgeführt wird.

[English](en.md) · [中文](../../README.md) · [日本語](ja.md) · [한국어](ko.md) · [Español](es.md) · [Français](fr.md) · [Deutsch](de.md)

<br>

<img src="../../docs/web-ui.gif" alt="Die Gradus-Weboberfläche: eine Session streamt eine Antwort und ruft ein bash-Werkzeug auf" width="820">

<sub>Ein Turn: gestreamte Antwort → <code>bash</code>-Aufruf → sein Ergebnis.</sub>

</div>

Benötigt Python 3.13 und ein Modell (drei Zeilen in `.env`). Alle Schritte unter [Schnellstart](#-schnellstart).

```bash
git clone https://github.com/xyzhhh123/Gradus.git
cd Gradus && uv sync
uv run gradus
```

## 🎯 Warum Gradus

Drei Aussagen, die es vom üblichen Agent-Harness unterscheiden.

1. 🧠 **Das Transkript gehört der Session, nicht dem Anbieter.** Der Verlauf liegt in einer anbieterneutralen Darstellung und wird pro Anfrage übersetzt, sodass eine Session mitten im Gespräch zwischen Endpunkten im Anthropic-, OpenAI- und Gemini-Stil wechseln kann. Anbieterspezifische Felder — etwa die `signature` eines Reasoning-Blocks — überstehen den Wechsel nicht, alles andere schon.
2. 📦 **Der Agent bearbeitet deinen Workspace nie direkt.** Jede Session arbeitet auf einer Kopie — ein `git worktree`, wenn der Workspace ein Repository ist, sonst ein gefilterter Baum — und Änderungen werden erst beim Schließen zusammengeführt. Ein abgeschossener Prozess kostet dich nichts.
3. 🧰 **Das ist eine Laufzeitumgebung, keine Anwendung.** Vier Pakete, Importe nur in eine Richtung. `ai` weiß nichts von den Schichten darüber, du kannst also nur die Protokollschicht mitnehmen; `agent` liefert eine Schleife und eine Fassade, ohne angehängte CLI.

## ✨ Funktionen

- 🔌 **Vier Modellprotokolle, eine interne Form** — Anthropic Messages, OpenAI Chat Completions, OpenAI Responses und Gemini werden auf ein Nachrichtenmodell plus 12 Streaming-Events reduziert. Gemini folgt dem dokumentierten Format und ist weiterhin **im Test** (kein echter Endpunkt verifiziert).
- 🔁 **Eine Werkzeugschleife mit echter Kontrolle** — eine asynchrone Schleife, 9 Hooks, 8 Ereignisse auf Ausführungsebene, parallele Werkzeuge, `steer` / `follow_up`, Abbruch und Wiederholungen.
- 🛡️ **Ausführung in der Sandbox** — `restricted` unter Windows ohne Adminrechte, dazu `appcontainer` (veraltet), `docker` und `local`. Siehe [Die Sandbox](#-die-sandbox).
- 👥 **Mehr als ein Agent** — Multi-Agent-Teams, Hintergrundaufgaben, geplante Aufgaben und Transkript-Recall kommen als Werkzeuge in der Schicht `coding` mit.
- 🧩 **MCP und Erweiterungen** — projektbezogene MCP-Server mit ausdrücklichem Vertrauen und OAuth, dazu Python-Werkzeuge, Slash-Befehle und `SKILL.md`-Skills aus `.gradus/`.
- 🐍 **Codemode** — das Modell schreibt Python, das `tools.*` aufruft; nur `text()` / `return` landen im Kontext.
- 💾 **Sessions und Kontext** — JSONL-Verlauf, Fortsetzen, Token-Budget, automatische Kompaktierung, Modellwechsel mitten in der Session.

## 🚀 Schnellstart

### 💻 Die CLI starten

```bash
uv sync
```

Oder installiere sie als Werkzeug, wenn du keinen Repo-Checkout behalten willst:

```bash
uv tool install git+https://github.com/xyzhhh123/Gradus
```

Die Beispiele unten laufen dann als `gradus` statt `uv run gradus`.

Schreibe einen Verbindungsblock in `<workspace>/.env`:

```ini
GRADUS_SERVICE=kimi-compat
GRADUS_MODEL=kimi-k2.7-code
GRADUS_API_KEY=sk-...
```

```bash
uv run gradus                      # workspace = aktuelles Verzeichnis
uv run gradus --workspace ../app   # oder woandershin zeigen lassen
uv run gradus --sandbox docker     # auto -> restricted -> appcontainer -> docker -> local
```

Ein Schlüssel allein identifiziert keinen Anbieter, deshalb werden Dienst, Modell und Schlüssel zusammen konfiguriert. CLI-Flags überschreiben `.env`, das wiederum `~/.gradus/config.json` überschreibt.

Im REPL: `/reload` baut Werkzeuge und Erweiterungen neu, `/command` und `/skill:name` entfalten gespeicherte Prompts, `exit` / `Ctrl-D` beendet.

<div align="center">

  <img src="../../docs/cli-demo.gif" alt="Eine echte Gradus-CLI-Session im Windows Terminal: eine Frage, zwei Werkzeugaufrufe, dann die Antwort" width="900">

  <sub>Aufgenommen im Windows Terminal: ein Turn, zwei Werkzeugaufrufe, dann die Antwort.</sub>

</div>

### 🌐 Die Weboberfläche starten

Zwei Prozesse: der Python-Host und das Browser-Bundle. Zuerst den Host starten.

```bash
uv sync --extra web
uv run gradus-serve --port 8765 --token dev
```

Er gibt die zu öffnende Adresse aus, inklusive Token:

```
Gradus web · kimi-k2.7-code · H:\your\workspace
open: http://127.0.0.1:8765/?token=dev
```

Das Token ist nicht optional: `/ws` schließt mit 4401 ohne das richtige. Lässt man `--token` weg, erzeugt der Host ein zufälliges und gibt diese URL aus.

Dann eines von beidem:

```bash
cd web && npm install && npm run build   # Host liefert web/dist — die ausgegebene Adresse öffnen
cd web && npm run dev                    # Vite auf :5173, leitet /ws an 127.0.0.1:8765 weiter
```

Mit dem Dev-Server `http://localhost:5173/?token=dev` öffnen: das Frontend liest `token` aus der Seiten-URL, es muss also zu `--token` passen. Mit `&mock=1` lädt die Oberfläche gegen eine geskriptete Verbindung, ganz ohne Host.

Nach Änderungen unter `gradus/` den Host neu starten; das Frontend lädt beim Speichern selbst neu.

### 🧪 Die Tests ausführen

```bash
uv run pytest -q
```

1002 Tests laufen offline durch, in etwa 95 Sekunden. Weitere 15 sind Live-Prüfungen, die standardmäßig übersprungen werden: Sandbox-Verträge brauchen `GRADUS_SANDBOX_LIVE=1`, Container-Tests `GRADUS_DOCKER_LIVE=1`.

## 🔒 Die Sandbox

| Backend | Wann es genutzt wird | Grenze |
| --- | --- | --- |
| 🪟 `restricted` | Windows; `auto` wählt es zuerst | Eingeschränktes Token (synthetische SID + Anmelde-SID + `Everyone`): Lesen folgt deinem Konto, Schreiben landet nur in der Kopie und im Scratch. **Keine Netzwerkisolation.** Kein Admin, keine Zusatzinstallation. |
| 🪟 `appcontainer` | Windows; nur wenn `restricted` nicht verfügbar ist | AppContainer-Token, kein Netzwerk, ein Job Object beendet Kindprozesse. **Veraltet**: bleibt funktionsfähig, keine weitere Arbeit. |
| 🐳 `docker` | Docker-Engine vorhanden | Container mit `--network none`, Speicher- / CPU- / PID-Limits. **Im Test**: als plattformübergreifender Rückfall behalten, noch nicht gegen eine echte Engine geprüft. |
| 🏠 `local` | Rückfall | Keine; Befehle laufen mit deinen Benutzerrechten |

Eine Sandbox-Session bearbeitet deinen Workspace nie direkt. Sie bekommt eine private Kopie — `git worktree`, wenn der Workspace ein Git-Repository ist, sonst eine gefilterte Baumkopie — und sowohl Dateiwerkzeuge als auch Befehle arbeiten auf dieser Kopie. Beim Schließen werden hinzugefügte und geänderte Dateien zurückgeschrieben, Löschungen angewendet, und eine fehlgeschlagene Zusammenführung behält die Kopie und gibt ihren Pfad aus. Kopien, die ein abgeschossener Prozess hinterlässt, werden beim nächsten Start entfernt.

Drei Grenzen, die man klar benennen sollte:

- ⚠️ **`restricted` blockiert das Netzwerk nicht.** Die Schreibgrenze ist echt, die Netzgrenze nicht. Nimm `docker`, wenn ein Befehl offline laufen muss.
- 🔒 **Die Befehlsfreigabe bleibt aktiv.** Die Kopie schützt den Host vor versehentlichem Schaden; sie macht beliebige Befehle nicht sicher.
- 🚫 **MCP-Serverprozesse liegen nicht in der Sandbox.** Sie laufen mit deinen Rechten auf dem Host. Das Standard-`cwd` eines stdio-Servers zeigt auf die Kopie, damit relative Pfade passen, aber HTTP-Server und absolute Pfade erreichen weiterhin den Host.

## 🧱 Architektur

```
gradus/host      web layer      Host-Prozess, Wire-Protokoll, Uhr
gradus/coding    product layer  Konfiguration, Sessions, Werkzeuge, Rechte, Sandbox, MCP, CLI
gradus/agent     control layer  Schleife, Hooks, parallele Werkzeuge, Agent-Fassade
gradus/ai        protocol layer Anbieter, Streaming-Events, Transport
```

Abhängigkeiten zeigen nur nach unten. `ai` importiert weder `agent` noch `coding`; `agent` importiert nie `coding`; `coding` importiert nie `host`.

### 📐 Entwurfsprinzipien

1. ⬇️ **Abhängigkeiten zeigen nur nach unten.** Höhere Schichten dürfen niedrigere aufrufen; niedrigere sehen höhere nie.
2. 📡 **Ereignisse sind die Schnittstelle.** Die 12 `ai`-Events werden von `agent` unverändert weitergereicht, das nur Ereignisse auf Ausführungsebene ergänzt.
3. ⚡ **Durchgehend asynchron.** `Provider.stream()`, `run_loop()` und `AgentTool.execute()` sind asynchron; die Schleife blockiert nie auf I/O.
4. 🔧 **Werkzeuge gehören zur oberen Schicht.** `agent` definiert das `AgentTool`-Protokoll; Datei-, Such- und Shell-Implementierungen leben in `coding`.

## 🐍 Python-API

Führe eine komplette Schleife offline aus — ohne Netzwerk und ohne API-Schlüssel:

```python
import asyncio

from gradus.agent.base import LoopConfig
from gradus.agent.loop import run_loop
from gradus.agent.types import AgentDone
from gradus.ai.base import ModelSpec
from gradus.ai.providers.fake import FakeProvider
from gradus.ai.types import UserMessage

spec = ModelSpec(
    provider="fake",
    model="fake",
    base_url="",
    context_window=8192,
    max_output_tokens=1024,
)
provider = FakeProvider(
    spec,
    scripts=[{
        "name": "answer",
        "steps": [
            {"kind": "text", "chunks": ["Hello, ", "I am Gradus"]},
            {"kind": "finish", "reason": "stop"},
        ],
    }],
)

async def main() -> None:
    async for event in run_loop(provider, [UserMessage("hi")], LoopConfig(system="")):
        if isinstance(event, AgentDone):
            print(event.new_messages[-1].content[0].text)

asyncio.run(main())
```

Ein echtes Modell nutzt du über die zustandsbehaftete `Agent`-Fassade:

```python
from gradus.agent.agent import Agent
from gradus.agent.base import LoopConfig
from gradus.ai.base import ModelSpec

spec = ModelSpec(
    provider="anthropic",
    model="claude-sonnet-4-5",
    base_url="https://api.anthropic.com",
    context_window=200_000,
    max_output_tokens=8192,
)

agent = await Agent.connect(
    spec,
    LoopConfig(system="You are a helpful assistant"),
    api_key="sk-...",
)
try:
    async for event in agent.prompt("hi"):
        ...
finally:
    await agent.aclose()
```

`Agent.connect()` erzeugt einen `httpx.AsyncClient` und nutzt ihn über mehrere Läufe weiter; `aclose()` schließt ihn. Übergib denselben `client` an mehrere `connect()`-Aufrufe, um den Pool zu teilen.

## 📖 Referenz

### 🚩 CLI-Flags

| Flag | Zweck |
| --- | --- |
| `--workspace` | Wurzel für Werkzeuge und Befehle (Standard: aktuelles Verzeichnis) |
| `--sandbox` | `auto` / `local` / `docker` / `appcontainer` / `restricted` |
| `--service` / `--provider` / `--model` / `--base-url` | Überschreiben die Verbindung |
| `--resume` / `--session-dir` | Setzt eine JSONL-Session fort oder wählt deren Ablage |
| `--mcp` / `--config` | Explizite Pfade für MCP und Konfigurationsdatei |
| `--untrust [path]` | Entzieht der MCP-Konfiguration eines Projekts das Vertrauen und beendet |
| `--reset-sandbox` | Räumt verwaiste Kopien, AppContainer-Freigaben und das Profil auf |
| `--log` | Hängt Werkzeug- und Fehlerereignisse an ein JSONL an |

### 🧩 Kandidatenmodelle

Der Modellwähler der Weboberfläche, das Einstellungsfenster und der Themenwechsel sind in **[docs/web-ui.md](../../docs/web-ui.md)** dokumentiert.

### 📁 Aufbau

```
Gradus/
├── gradus/
│   ├── ai/          # Anbieter, internes Protokoll, Streaming-Events, Transport
│   ├── agent/       # Schleife, Hooks, Werkzeug-Runtime, zustandsbehafteter Agent
│   ├── coding/      # Konfiguration, Sessions, Werkzeuge, Rechte, Sandbox, MCP, CLI
│   └── host/        # Web-Host-Prozess, Wire-Protokoll, Uhr
├── bench/           # Benchmark-Harness
├── web/             # Browser-Bundle
├── pyproject.toml
└── uv.lock
```

Python 3.13 · Laufzeitabhängigkeiten: `httpx`, `httpx2`, `jsonschema`, `mcp` · verwaltet mit `uv`.

## 📚 Dokumentation

| Schicht | README | Design | Plan |
| --- | --- | --- | --- |
| `ai` | [README](../../gradus/ai/README.md) | [Design](../../gradus/ai/docs/design.md) | [Plan](../../gradus/ai/ai_plan.md) |
| `agent` | [README](../../gradus/agent/README.md) | [Design](../../gradus/agent/docs/design.md) | [Plan](../../gradus/agent/agent_plan.md) |
| `coding` | [README](../../gradus/coding/README.md) | [Design](../../gradus/coding/docs/design.md) | [Plan](../../gradus/coding/coding_plan.md) |

Weiterführendes in `gradus/coding/docs/`: [Sandbox](../../gradus/coding/docs/sandbox.md) · [Kontext-Design](../../gradus/coding/docs/context_design.md) · [Multi-Agent](../../gradus/coding/docs/multi_agent.md) · [Hintergrundaufgaben](../../gradus/coding/docs/background_tasks.md) · [Geplante Aufgaben](../../gradus/coding/docs/cron_tasks.md) · [Recall](../../gradus/coding/docs/recall.md).

## 🙏 Danksagung

Schichtung, Ereignismodell und Werkzeugvertrag von Gradus sind von diesen Open-Source-Projekten inspiriert:

- **Pi** (`@earendil-works/pi-agent-core`): Referenz für die Agent-Schleife, das Hook-System, Steering / Follow-up und das Werkzeugprotokoll.
- **OpenCode**: Referenz für API- und Werkzeugdesign.

## 📄 Lizenz

[MIT](../../LICENSE)

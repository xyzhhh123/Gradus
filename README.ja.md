<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/banner-dark.svg">
  <img src="docs/banner-light.svg" alt="Gradus" width="440">
</picture>

![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-3776ab?style=flat-square)
![License MIT](https://img.shields.io/badge/license-MIT-3da639?style=flat-square)
![Status pre-1.0](https://img.shields.io/badge/status-pre--1.0-fb923c?style=flat-square)

**階層化されたコーディングエージェントランタイム。** Gradus はモデルのストリームをツールを使うセッションに変え、各セッションにワークスペースのプライベートコピーを与え、セッション終了時にホストへマージして戻します。

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

<br>

<img src="docs/web-ui.gif" alt="Gradus の Web UI：応答をストリーミングし、bash ツールを呼び出している様子" width="820">

<sub>1 ターン：ストリーミング応答 → <code>bash</code> ツール呼び出し → その結果。</sub>

</div>

```bash
git clone https://github.com/xyzhhh123/Gradus.git
cd Gradus && uv sync
uv run gradus
```

最後の行にはモデルが必要です — 3 行の `.env` をひとつ用意してください。[クイックスタート](#-クイックスタート)を参照。

## 🎯 Gradus とは

一般的なエージェントハーネスと異なる、3 つの主張があります。

1. 🧠 **トランスクリプトはセッションが持ち、プロバイダーは持たない。** 履歴はプロバイダー中立の表現で保存され、リクエストごとに翻訳されるため、ひとつのセッションが会話の途中で Anthropic・OpenAI・Gemini 形式のエンドポイントへ移動できます。thinking ブロックの `signature` のようなベンダー固有フィールドは引き継がれませんが、それ以外はすべて引き継がれます。
2. 📦 **エージェントはワークスペースを直接編集しない。** すべてのセッションはコピー上で動作し（ワークスペースがリポジトリなら `git worktree`、そうでなければフィルタ済みのツリーコピー）、変更は終了時にのみマージされます。プロセスが強制終了されても損害はありません。
3. 🧰 **これはランタイムであり、アプリではない。** 4 つのパッケージ、import は一方向。`ai` は上位層を一切知らないため、プロトコル層だけを取り出すこともできます。`agent` はループとファサードを提供し、CLI は付いてきません。

## ✨ 機能

- 🔌 **4 つのモデルプロトコル、1 つの内部表現** — Anthropic Messages、OpenAI Chat Completions、OpenAI Responses、Gemini を、1 つのメッセージモデルと 12 個のストリーミングイベントに還元します。Gemini は公式フォーマットに沿って実装していますが、**テスト中**です（実際のエンドポイントでは未検証）。
- 🔁 **制御可能なツールループ** — 単一の非同期ループ、9 個のフック、8 個の実行レベルイベント、並列ツール実行、`steer` / `follow_up`、キャンセル、リトライ。
- 🛡️ **サンドボックス実行** — Windows で管理者権限不要の `restricted`、ほかに `appcontainer`（非推奨）、`docker`、`local`。[サンドボックス](#-サンドボックス)を参照。
- 👥 **エージェントは 1 つだけではない** — マルチエージェントチーム、バックグラウンドタスク、スケジュール実行、トランスクリプトの recall が、`coding` 層のツールとして同梱されています。
- 🧩 **MCP と拡張** — プロジェクト単位の MCP サーバー（明示的な信頼 + OAuth）、さらに `.gradus/` から読み込む Python ツール、スラッシュコマンド、`SKILL.md` スキル。
- 🐍 **Codemode** — モデルが `tools.*` を呼ぶ Python を書き、`text()` / `return` の結果だけがコンテキストに入ります。
- 💾 **セッションとコンテキスト** — JSONL 履歴、再開、トークン予算、自動コンパクション、セッション途中でのモデル切り替え。

## 🚀 クイックスタート

### 💻 CLI を実行する

```bash
uv sync
```

`<workspace>/.env` に接続設定をひとまとまり書きます：

```ini
GRADUS_SERVICE=kimi-compat
GRADUS_MODEL=kimi-k2.7-code
GRADUS_API_KEY=sk-...
```

```bash
uv run gradus                      # workspace はカレントディレクトリ
uv run gradus --workspace ../app   # 別の場所を指定することもできます
uv run gradus --sandbox docker     # auto -> restricted -> appcontainer -> docker -> local
```

キーだけではプロバイダーを特定できないため、service・model・key はまとめて設定します。優先順位は CLI フラグ、次に `.env`、次に `~/.gradus/config.json` です。

REPL 内では、`/reload` がツールと拡張を再構築し、`/command` と `/skill:name` が保存済みプロンプトを展開し、`exit` または `Ctrl-D` で抜けます。

### 🌐 Web UI を実行する

プロセスは 2 つです。Python のホストとブラウザ用バンドルで、ホストを先に起動します。

```bash
uv sync --extra web
uv run gradus-serve --port 8765 --token dev
```

トークン込みのアドレスが表示されます：

```
Gradus web · kimi-k2.7-code · H:\your\workspace
open: http://127.0.0.1:8765/?token=dev
```

トークンは任意ではありません。正しいトークンがないと `/ws` は 4401 で切断されます。`--token` を省略するとホストがランダムに生成し、その URL を表示します。

次にどちらかを選びます：

```bash
cd web && npm install && npm run build   # ホストが web/dist を配信 — 表示されたアドレスを開く
cd web && npm run dev                    # Vite は :5173、/ws を 127.0.0.1:8765 へプロキシ
```

開発サーバーを使う場合、開くのは `http://localhost:5173/?token=dev` です。フロントエンドはページ URL から `token` を読むため、`--token` と一致している必要があります。`&mock=1` を付けると、ホストなしでスクリプト化された接続に対して UI を読み込めます。

`gradus/` 以下を編集したらホストを再起動してください。フロントエンドは保存時に自動でリロードされます。

### 🧪 テストを実行する

```bash
uv run pytest -q
```

既定ではすべてオフラインで実行されます。11 個のサンドボックス実機契約テストは、`GRADUS_SANDBOX_LIVE=1` のときだけ実行されます。

## 🔒 サンドボックス

| バックエンド | 使われるとき | 境界 |
| --- | --- | --- |
| 🪟 `restricted` | Windows。`auto` が最初に選ぶ | 制限付きトークン（合成 SID + ログオン SID + `Everyone`）。読み取りはアカウントに従い、書き込みはコピーとスクラッチにのみ落ちます。**ネットワーク分離はありません。** 管理者不要、追加インストールも不要。 |
| 🪟 `appcontainer` | Windows。`restricted` が使えないときのみ | AppContainer トークン、ネットワークなし、Job Object が子プロセスを終了させます。**非推奨**：動作は維持しますが、それ以上の作業は行いません。 |
| 🐳 `docker` | Docker エンジンがある場合 | `--network none`、メモリ / CPU / PID 制限付きのコンテナ。**テスト中**：クロスプラットフォームのフォールバックとして維持していますが、実際のエンジンでは未検証。 |
| 🏠 `local` | フォールバック | なし。コマンドは自分のユーザー権限で実行されます |

サンドボックスのセッションはワークスペースを直接編集しません。プライベートなコピー（ワークスペースが git リポジトリなら `git worktree`、そうでなければフィルタ済みのツリーコピー）を受け取り、ファイルツールもコマンドもそのコピー上で動作します。終了時に、追加・変更されたファイルは書き戻され、削除は適用されます。マージに失敗した場合はコピーを保持してパスを表示します。強制終了されたプロセスが残したコピーは、次回起動時に削除されます。

率直に書いておくべき境界が 3 つあります：

- ⚠️ **`restricted` はネットワークを遮断しません。** 書き込みの境界は本物ですが、ネットワークはそうではありません。コマンドを必ずオフラインにしたい場合は `docker` を使ってください。
- 🔒 **コマンド承認は有効なままです。** コピーはホストを誤操作から守りますが、任意のコマンドを安全にするわけではありません。
- 🚫 **MCP サーバーのプロセスはサンドボックス内にありません。** それらはあなたの権限でホスト上で動作します。stdio サーバーの既定の `cwd` は相対パスが合うようコピーを指しますが、HTTP サーバーと絶対パスはホストに届きます。

## 🧱 アーキテクチャ

```
gradus/host      web layer      ホストプロセス、wire プロトコル、クロック
gradus/coding    product layer  設定、セッション、ツール、権限、サンドボックス、MCP、CLI
gradus/agent     control layer  ループ、フック、並列ツール、Agent ファサード
gradus/ai        protocol layer プロバイダー、ストリーミングイベント、トランスポート
```

依存は下向きのみです。`ai` は `agent` も `coding` も import せず、`agent` は `coding` を import せず、`coding` は `host` を import しません。

### 📐 設計原則

1. ⬇️ **依存は下向きのみ。** 上位層は下位層を呼べますが、下位層は上位層を決して見ません。
2. 📡 **イベントがインターフェース。** 12 個の `ai` イベントは `agent` によってそのまま転送され、`agent` が追加するのは実行レベルイベントだけです。
3. ⚡ **全体が非同期。** `Provider.stream()`、`run_loop()`、`AgentTool.execute()` はすべて async で、ループが I/O でブロックすることはありません。
4. 🔧 **ツールは上位層に属する。** `agent` が `AgentTool` プロトコルを定義し、ファイル・検索・シェルの実装は `coding` にあります。

## 🐍 Python API

ネットワークも API キーもなしで、ループを丸ごと実行できます：

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

実際のモデルは、状態を持つ `Agent` ファサード経由で使います：

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

`Agent.connect()` は `httpx.AsyncClient` を 1 つ作成し、複数回の実行で再利用します。`aclose()` がそれを閉じます。同じ `client` を複数の `connect()` に渡せば、プールを共有できます。

## 📖 リファレンス

### 🚩 CLI フラグ

| フラグ | 用途 |
| --- | --- |
| `--workspace` | ツールとコマンドのルート（既定：カレントディレクトリ） |
| `--sandbox` | `auto` / `local` / `docker` / `appcontainer` / `restricted` |
| `--service` / `--provider` / `--model` / `--base-url` | 接続設定を上書き |
| `--resume` / `--session-dir` | JSONL セッションを再開、または新規の保存先を指定 |
| `--mcp` / `--config` | MCP と設定ファイルのパスを明示 |
| `--untrust [path]` | プロジェクトの MCP 設定の信頼を取り消して終了 |
| `--reset-sandbox` | 孤立したコピー、AppContainer の付与、プロファイルを消去 |
| `--log` | ツールとエラーのイベントを JSONL に追記 |

### 🧩 候補モデル

`~/.gradus/config.json`（または `--config` が指すファイル）に候補リストを持たせられ、Web のモデルピッカーがそれを直接読みます：

```json
{
  "models": [
    { "name": "Kimi · Chat", "service": "kimi-compat", "model": "kimi-k2.7-code" },
    { "name": "Kimi · Messages", "service": "kimi-anthropic", "model": "kimi-k2.7-code" },
    { "name": "Kimi · Responses", "service": "kimi-responses", "model": "kimi-k2.7-code" }
  ]
}
```

各エントリはトップレベルと同じ接続フィールド（`service` / `provider` / `model` / `base_url` / `api_key` / `context_window` / `max_output_tokens`）を受け付けます。`name` は UI に表示される名前で、`api_key` を省略すると現在のキーを再利用します。このリストはファイルからのみ読まれます — 環境変数は今のモデルだけを表します。

JSON を手で編集する必要はありません。Web ヘッダーの歯車アイコンが設定パネルを開き、候補モデルの追加・編集・削除ができます（キーを返すことはありません）。最後に選んだモデルはトップレベルの `"active"` フィールドに書き込まれ、次回起動時に復元されます。パネルの「Appearance」では light / dark / システム追従も切り替えられます（設定ファイルではなくブラウザの localStorage に保存されます）。

切り替えても新しいセッションは始まりません。履歴はプロバイダー中立の表現で、リクエストごとに有効なプロバイダーが翻訳するため、ひとつのセッションが `openai_compat`・`anthropic`・`openai_responses` の間を移動できます。thinking ブロックの `signature` のようなベンダー固有フィールドは変換されず、破棄されるかそのまま通過する可能性があります。

### 📁 ディレクトリ構成

```
Gradus/
├── gradus/
│   ├── ai/          # プロバイダー、内部プロトコル、ストリーミングイベント、トランスポート
│   ├── agent/       # ループ、フック、ツールランタイム、状態を持つ Agent
│   ├── coding/      # 設定、セッション、ツール、権限、サンドボックス、MCP、CLI
│   └── host/        # Web ホストのプロセス、wire プロトコル、クロック
├── bench/           # ベンチマークハーネス
├── web/             # ブラウザ用バンドル
├── pyproject.toml
└── uv.lock
```

Python 3.13 · 実行時依存：`httpx`、`httpx2`、`jsonschema`、`mcp` · `uv` で管理。

## 📚 ドキュメント

| 層 | README | 設計 | 計画 |
| --- | --- | --- | --- |
| `ai` | [README](gradus/ai/README.md) | [設計](gradus/ai/docs/design.md) | [計画](gradus/ai/ai_plan.md) |
| `agent` | [README](gradus/agent/README.md) | [設計](gradus/agent/docs/design.md) | [計画](gradus/agent/agent_plan.md) |
| `coding` | [README](gradus/coding/README.md) | [設計](gradus/coding/docs/design.md) | [計画](gradus/coding/coding_plan.md) |

`gradus/coding/docs/` の関連資料：[サンドボックス](gradus/coding/docs/sandbox.md) · [コンテキスト設計](gradus/coding/docs/context_design.md) · [マルチエージェント](gradus/coding/docs/multi_agent.md) · [バックグラウンドタスク](gradus/coding/docs/background_tasks.md) · [スケジュール実行](gradus/coding/docs/cron_tasks.md) · [recall](gradus/coding/docs/recall.md)。

## 🙏 謝辞

Gradus の階層構造、イベントモデル、ツール契約は、以下のオープンソースプロジェクトを参考にしています：

- **Pi**（`@earendil-works/pi-agent-core`）：エージェントループ、フック機構、steering / follow-up、ツールプロトコルの参照元。
- **OpenCode**：API とツール設計の参照元。

## 📄 ライセンス

[MIT](LICENSE)

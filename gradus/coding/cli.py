"""命令行入口。

负责：解析参数、读配置、建会话、把用户输入转成 `Session.prompt()`，消费并渲染事件。

不负责：装配细节（`session.py`）、工具实现（`tools/`）、权限判断（`permissions.py`）。

第一版按交互式 CLI 起步；换成服务端形态时替换本模块，其余模块不变。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import TextIO

from ..ai.envfile import load_env_file
from .config import (
    ENV_BASE_URL,
    ENV_MODEL,
    ENV_PROVIDER,
    ENV_SANDBOX,
    ENV_SERVICE,
    ConfigError,
    default_config_path,
    load,
)
from . import appcontainer, sandbox, workspace
from .mcp import McpConfigError, forget_trust
from .render import render, render_log
from .sandbox import BACKENDS, SandboxError
from .session import Session, SessionError, close_pool

EXIT_COMMANDS = {"exit", "quit"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gradus", description="Gradus coding CLI")
    parser.add_argument("--provider", help=f"覆盖 {ENV_PROVIDER}")
    parser.add_argument(
        "--service",
        help=f"内置服务商名，自动带出 {ENV_PROVIDER} 与 {ENV_BASE_URL}",
    )
    parser.add_argument("--model", help=f"覆盖 {ENV_MODEL}")
    parser.add_argument("--base-url", dest="base_url", help=f"覆盖 {ENV_BASE_URL}")
    parser.add_argument(
        "--sandbox",
        choices=("auto", *BACKENDS),
        help=(
            "命令与 codemode 的执行环境（缺省 auto："
            f"appcontainer -> docker -> local；覆盖 {ENV_SANDBOX}）"
        ),
    )
    parser.add_argument("--workspace", help="工具与命令的根目录（缺省为当前目录）")
    parser.add_argument("--log", help="把工具与错误事件追加写入这个 JSONL 文件")
    parser.add_argument(
        "--session-dir",
        help="会话文件目录（缺省为 <workspace>/.gradus/sessions）",
    )
    parser.add_argument("--resume", help="恢复会话文件；传目录时取最新的 .jsonl")
    parser.add_argument(
        "--mcp",
        help="MCP server 配置文件（缺省为 <workspace>/.gradus/mcp.json）",
    )
    parser.add_argument(
        "--config",
        help="JSON 配置文件（缺省为 ~/.gradus/config.json）",
    )
    parser.add_argument(
        "--untrust",
        nargs="?",
        const="",
        metavar="路径",
        help="撤销某个项目的 MCP 信任后退出；省略路径时用当前 workspace",
    )
    parser.add_argument(
        "--reset-sandbox",
        action="store_true",
        help="清掉强杀残留、AppContainer 授权与 profile 后退出",
    )
    return parser.parse_args(argv)


def overrides_from_args(args: argparse.Namespace) -> dict[str, str]:
    """把命令行参数翻成 `config.load(overrides=...)` 的键。"""
    pairs = (
        (ENV_SERVICE, args.service),
        (ENV_PROVIDER, args.provider),
        (ENV_MODEL, args.model),
        (ENV_BASE_URL, args.base_url),
        (ENV_SANDBOX, args.sandbox),
    )
    return {key: value for key, value in pairs if value}


async def confirm_tool(question: str) -> bool:
    """询问是否放行；只有 `y` / `yes` 算通过。"""
    try:
        answer = await asyncio.to_thread(input, f"{question} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}


async def run_once(
    session: Session,
    text: str,
    log_file: TextIO | None = None,
) -> None:
    """跑一轮，把事件流写到终端，需要留痕的事件写进日志。"""
    async for event in session.prompt(text):
        chunk = render(event)
        if chunk:
            print(chunk, end="", flush=True)

        line = render_log(event)
        if line and log_file is not None:
            log_file.write(line + "\n")
            log_file.flush()
    print()


async def reload_session(session: Session) -> None:
    """`/reload`：重读扩展并重建 Agent，历史与 MCP 连接保留。"""
    try:
        warnings = await session.reload()
    except (SessionError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return

    print(
        f"reloaded: {len(session.commands)} commands, "
        f"{len(session.skills)} skills, {len(session.tools)} tools"
    )
    for warning in warnings:
        print(f"extension warning: {warning}", file=sys.stderr)


async def interactive(session: Session, log_file: TextIO | None = None) -> None:
    """读一行、跑一轮，直到 `exit` / `quit` / EOF。"""
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not text:
            continue
        if text.lower() in EXIT_COMMANDS:
            return
        if text == "/reload":
            await reload_session(session)
            continue

        try:
            await run_once(session, text, log_file)
        except SessionError as exc:
            print(f"error: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            await session.abort()
            print("\n[aborted]")


def untrust(target: str, base: Path) -> int:
    """`--untrust`：撤销 MCP 信任后退出，不读模型配置。"""
    path = Path(target).expanduser() if target else base
    try:
        removed = forget_trust(path)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    state = "untrusted" if removed else "not trusted"
    print(f"{state}: {path.resolve()}")
    return 0


async def reset_sandbox() -> int:
    """`--reset-sandbox`：清掉沙箱持久状态后退出，不读模型配置。"""
    removed = await workspace.cleanup_orphans()
    for path in removed:
        print(f"removed orphan copy: {path}")
    for name in await sandbox.cleanup_orphans():
        print(f"removed orphan container: {name}")
    try:
        notes = await appcontainer.reset_state()
    except SandboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(note)
    if not removed and not notes:
        print("sandbox state is already clean")
    return 0


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = (
        Path(args.workspace).expanduser().resolve()
        if args.workspace is not None
        else Path.cwd()
    )
    if args.reset_sandbox:
        return await reset_sandbox()
    if args.untrust is not None:
        return untrust(args.untrust, base)
    load_env_file(base / ".env")

    try:
        settings = load(overrides=overrides_from_args(args), config=args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.workspace is not None and not Path(args.workspace).is_dir():
        print(f"workspace not found: {args.workspace}", file=sys.stderr)
        return 2

    session_dir = args.session_dir
    if session_dir is None and args.resume is None:
        session_dir = base / ".gradus" / "sessions"

    try:
        session = await Session.connect(
            settings,
            workspace=args.workspace,
            confirm=confirm_tool,
            session_dir=session_dir,
            resume=args.resume,
            mcp_config=args.mcp,
        )
    except (SessionError, McpConfigError) as exc:
        print(f"session error: {exc}", file=sys.stderr)
        return 2

    log_file = open(args.log, "a", encoding="utf-8") if args.log else None

    print(f"Gradus · {settings.spec.model} · {session.workspace}")
    config_path = (
        Path(args.config).expanduser() if args.config is not None else default_config_path()
    )
    if config_path.exists():
        print(f"config: {config_path}")
    if session.session_path is not None:
        print(f"session: {session.session_path}")
    backend = session.sandbox_backend
    if backend == "docker":
        state = "network on" if settings.sandbox_network else "no network"
        print(f"sandbox: docker · {settings.sandbox_image} · {state}")
    elif backend == "appcontainer":
        print("sandbox: appcontainer · no network")
    elif backend == "none":
        print(f"sandbox: {settings.sandbox} unavailable")
    elif settings.sandbox != "local":
        print(f"sandbox: local ({settings.sandbox} found no backend)")
    if session.sandbox_orphans:
        print(f"sandbox: removed {len(session.sandbox_orphans)} orphan leftover(s)")
    if session.sandbox_warning:
        print(f"sandbox warning: {session.sandbox_warning}", file=sys.stderr)
    if session.mcp_servers:
        print(f"mcp: {', '.join(session.mcp_servers)}")
    for warning in session.mcp_warnings:
        print(f"mcp warning: {warning}", file=sys.stderr)
    for warning in session.extension_warnings:
        print(f"extension warning: {warning}", file=sys.stderr)
    if session.commands:
        names = ", ".join(f"/{item.name}" for item in session.commands)
        print(f"commands: {names}")
    if session.context_files:
        print(f"context: {', '.join(str(path) for path in session.context_files)}")
    print("输入 exit 或 Ctrl-D 退出；运行中 Ctrl-C 中止。")
    try:
        await interactive(session, log_file)
    finally:
        if log_file is not None:
            log_file.close()
        await session.aclose()
        _print_workspace_sync(session)
        await close_pool()
    return 0


def _print_workspace_sync(session: Session) -> None:
    """沙箱副本合回宿主的结果：只在有改动或失败时输出。"""
    report = session.workspace_sync
    if report is None:
        return
    if report.error is not None:
        print(f"workspace: merge failed: {report.error}", file=sys.stderr)
        if report.kept is not None:
            print(f"workspace: changes kept at {report.kept}", file=sys.stderr)
        return
    if not report.changed:
        return
    print(f"workspace: merged {len(report.applied)} file(s) into {session.workspace}")
    if report.deleted:
        print(f"workspace: removed {len(report.deleted)} file(s)")


def cli() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    cli()

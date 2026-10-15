"""Noninteractive study workflows and structured output."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import yaml

from . import __version__
from .core.interchange import backup, export_library, import_library
from .core.planning import plan, stats, today_plan
from .core.scheduler import StudyError
from .core.store import Store


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise StudyError(message)


def parser():
    p = Parser(prog="ssp")
    p.add_argument("--config")
    p.add_argument("--data-dir")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    s = p.add_subparsers(dest="command", required=True)
    sub = s.add_parser("exam").add_subparsers(dest="action", required=True)
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--date", required=True)
    add.add_argument("--id")
    sub.add_parser("list")
    sub.add_parser("use").add_argument("id")
    sub = s.add_parser("subject").add_subparsers(dest="action", required=True)
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--weight", type=float, default=1)
    add.add_argument("--exam")
    sub.add_parser("list").add_argument("--exam")
    sub = s.add_parser("availability")
    sub.add_argument("--key")
    sub.add_argument("--minutes", type=int)
    sub = s.add_parser("import")
    sub.add_argument("file", type=Path)
    sub.add_argument("--subject")
    sub.add_argument("--exam")
    sub.add_argument("--at")
    sub.add_argument("--library", action="store_true")
    for command in ["plan", "today", "stats", "forecast", "review"]:
        sub = s.add_parser(command)
        sub.add_argument("--exam")
        sub.add_argument("--at")
        if command == "plan":
            sub.add_argument("--rebuild", action="store_true")
        if command in {"stats", "forecast"}:
            sub.add_argument("--days", type=int, default=30)
        if command == "review":
            sub.add_argument("--limit", type=int, default=20)
            sub.add_argument("--subject")
            sub.add_argument(
                "--mode", choices=["due", "new", "mistakes", "topic", "all"], default="due"
            )
            sub.add_argument("--tag")
    sub = s.add_parser("answer")
    sub.add_argument("item_id")
    sub.add_argument("--answer")
    sub.add_argument("--rating", choices=["again", "hard", "good", "easy"])
    sub.add_argument("--elapsed-s", type=float, default=0)
    sub.add_argument("--request-id")
    sub.add_argument("--expected-revision", type=int)
    sub.add_argument("--at")
    sub.add_argument("--session")
    for command in ["mock", "micro"]:
        sub = s.add_parser(command)
        sub.add_argument("--subject")
        sub.add_argument("--exam")
        sub.add_argument("--n", type=int, default=15)
        sub.add_argument("--minutes", type=int, default=30 if command == "mock" else 5)
        sub.add_argument("--at")
    sub = s.add_parser("finish")
    sub.add_argument("session_id")
    sub.add_argument("--at")
    sub = s.add_parser("export")
    sub.add_argument("--format", choices=["json", "csv"], default="json")
    sub.add_argument("--out", type=Path, required=True)
    sub.add_argument("--yes", action="store_true")
    s.add_parser("backup").add_argument("--out", type=Path)
    sub = s.add_parser("serve")
    sub.add_argument("--host", default="127.0.0.1")
    sub.add_argument("--port", type=int, default=8776)
    s.add_parser("mcp")
    return p


def get_store(args):
    path = args.config or os.environ.get("SSP_CONFIG")
    config = {}
    if path:
        file = Path(path).resolve()
        config = yaml.safe_load(file.read_text()) or {}
        if not isinstance(config, dict):
            raise StudyError("Config must be an object")
        if config.get("data_dir"):
            config["data_dir"] = str((file.parent / config["data_dir"]).resolve())
    return Store(
        Path(args.data_dir or os.environ.get("SSP_DATA_DIR") or config.get("data_dir", "data")),
        config,
    )


def main(argv=None):
    try:
        tokens = list(argv if argv is not None else sys.argv[1:])
        leading = []
        rest = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in {"--config", "--data-dir"}:
                leading.extend(tokens[i : i + 2])
                i += 2
            elif token in {"--json", "--quiet", "--version"} or token.startswith(
                ("--config=", "--data-dir=")
            ):
                leading.append(token)
                i += 1
            else:
                rest.append(token)
                i += 1
        args = parser().parse_args(leading + rest)
        store = get_store(args)
        cmd = args.command
        if cmd == "exam":
            result = (
                store.exams()
                if args.action == "list"
                else store.use_exam(args.id)
                if args.action == "use"
                else store.add_exam(args.name, args.date, args.id)
            )
        elif cmd == "subject":
            result = (
                store.subjects(args.exam)
                if args.action == "list"
                else store.add_subject(args.name, args.weight, args.exam)
            )
        elif cmd == "availability":
            result = store.availability(args.key, args.minutes)
        elif cmd == "import":
            result = (
                import_library(store, args.file)
                if args.library
                else store.import_items(args.file, args.subject, args.exam, args.at)
            )
        elif cmd == "plan":
            result = plan(store, args.exam, args.at)
        elif cmd == "today":
            result = today_plan(store, args.exam, args.at)
        elif cmd == "stats":
            result = stats(store, args.days, args.exam, args.at)
        elif cmd == "forecast":
            result = plan(store, args.exam, args.at, days=args.days, persist=False)
        elif cmd == "review":
            result = store.next_items(
                args.limit, args.subject, args.mode, args.tag, args.exam, args.at
            )
        elif cmd == "answer":
            result = store.answer(
                args.item_id,
                args.answer,
                args.rating,
                args.elapsed_s,
                args.request_id,
                args.expected_revision,
                args.at,
                args.session,
            )
        elif cmd in {"mock", "micro"}:
            result = store.start_session(
                cmd, args.n, args.minutes, args.subject, args.exam, args.at
            )
        elif cmd == "finish":
            result = store.finish_session(args.session_id, args.at)
        elif cmd == "export":
            result = export_library(store, args.out, args.format, args.yes)
        elif cmd == "backup":
            result = backup(store, args.out)
        elif cmd == "mcp":
            from .mcp_server import make_server

            make_server(store).run()
            return 0
        elif cmd == "serve":
            if args.host not in {"localhost", "127.0.0.1", "::1"}:
                raise StudyError("Only loopback serving is supported")
            import uvicorn

            from .web.app import create_app

            uvicorn.run(create_app(store), host=args.host, port=args.port, log_level="warning")
            return 0
        if not args.quiet:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "data": result,
                        "warnings": result.get("warnings", []) if isinstance(result, dict) else [],
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    except (
        StudyError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        sqlite3.Error,
        yaml.YAMLError,
    ) as exc:
        code = (
            exc.code
            if isinstance(exc, StudyError)
            else "IO_ERROR"
            if isinstance(exc, (OSError, sqlite3.Error))
            else "INVALID_INPUT"
        )
        status = exc.exit_code if isinstance(exc, StudyError) else 1 if code == "IO_ERROR" else 2
        print(
            json.dumps(
                {"ok": False, "error": {"code": code, "message": str(exc)}}, ensure_ascii=False
            )
        )
        return status


if __name__ == "__main__":
    raise SystemExit(main())

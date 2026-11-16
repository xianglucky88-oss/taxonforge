import argparse
import json
from .service import TaxonomyService


def _text_chunks(path, chunk_codepoints):
    text = open(path, encoding="utf-8").read()
    if chunk_codepoints <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_codepoints):
        yield text[i:i + chunk_codepoints]


def main():
    parser = argparse.ArgumentParser(description="Versioned taxonomy classification")
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register")
    register.add_argument("input")
    classify = sub.add_parser("classify")
    classify.add_argument("input")
    classify.add_argument("--version")
    activate = sub.add_parser("activate")
    activate.add_argument("version")
    impact = sub.add_parser("impact")
    impact.add_argument("input")
    impact.add_argument("before")
    impact.add_argument("after")
    replay = sub.add_parser("replay")
    replay.add_argument("run")
    replay.add_argument("--version")
    annotate = sub.add_parser("annotate", help="annotate term mentions (read-only)")
    annotate.add_argument("input", help="UTF-8 text file")
    annotate.add_argument("--version")
    annotate.add_argument("--chunk-codepoints", type=int, default=0,
                          help="stream the file in chunks of this many codepoints")
    annotate.add_argument("--strategy", choices=("aho", "scan"), default="aho")
    args = parser.parse_args()
    service = TaxonomyService(args.db)
    try:
        data = None
        if hasattr(args, "input") and args.command != "annotate":
            with open(args.input, encoding="utf-8") as stream: data = json.load(stream)
        if args.command == "register": result = {"version": service.register(data)}
        elif args.command == "classify": result = service.classify(data, args.version)
        elif args.command == "activate":
            service.activate(args.version)
            result = {"active": args.version}
        elif args.command == "replay": result = service.replay(args.run, args.version)
        elif args.command == "annotate":
            result = service.annotate_mentions(
                _text_chunks(args.input, args.chunk_codepoints),
                version=args.version, strategy=args.strategy)
        else: result = service.impact(data, args.before, args.after)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")
    finally:
        service.close()


if __name__ == "__main__": main()

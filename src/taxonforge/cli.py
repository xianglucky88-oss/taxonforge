import argparse
import json
from .service import TaxonomyService


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
    args = parser.parse_args()
    service = TaxonomyService(args.db)
    try:
        data = None
        if hasattr(args, "input"):
            with open(args.input, encoding="utf-8") as stream: data = json.load(stream)
        if args.command == "register": result = {"version": service.register(data)}
        elif args.command == "classify": result = service.classify(data, args.version)
        elif args.command == "activate":
            service.activate(args.version)
            result = {"active": args.version}
        elif args.command == "replay": result = service.replay(args.run, args.version)
        else: result = service.impact(data, args.before, args.after)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")
    finally:
        service.close()


if __name__ == "__main__": main()

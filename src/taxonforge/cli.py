import argparse
import json
from .service import TaxonomyService


def _expected(value):
    return None if value == "none" else value


def main():
    parser = argparse.ArgumentParser(description="Versioned taxonomy classification with reviewed artifacts")
    parser.add_argument("--db", required=True)
    parser.add_argument("--identity", default=None, help="caller identity provided by the trusted environment")
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register", help="normalize a draft into an artifact; activation passes the review gate")
    register.add_argument("input")
    register.add_argument("--no-activate", action="store_true")
    register.add_argument("--author")
    register.add_argument("--expected", type=_expected, default=argparse.SUPPRESS,
                          help="expected active digest, or 'none'")
    for action in ("approve", "withdraw", "reject", "retract"):
        review = sub.add_parser(action)
        review.add_argument("artifact")
        review.add_argument("--by", dest="reviewer", default=None)
        review.add_argument("--comment")
    sub.choices["approve"].add_argument("--ttl", type=int, default=None, help="approval validity in seconds")
    sub.choices["approve"].add_argument("--expires-at", default=None)
    status = sub.add_parser("status")
    status.add_argument("artifact")
    classify = sub.add_parser("classify")
    classify.add_argument("input")
    classify.add_argument("--version")
    activate = sub.add_parser("activate")
    activate.add_argument("artifact")
    activate.add_argument("--expected", type=_expected, default=argparse.SUPPRESS,
                          help="expected active digest, or 'none'; defaults to the current active")
    impact = sub.add_parser("impact")
    impact.add_argument("input")
    impact.add_argument("before")
    impact.add_argument("after")
    replay = sub.add_parser("replay")
    replay.add_argument("run")
    replay.add_argument("--version")
    args = parser.parse_args()
    service = TaxonomyService(args.db, identity=args.identity)
    try:
        data = None
        if hasattr(args, "input"):
            with open(args.input, encoding="utf-8") as stream: data = json.load(stream)
        if args.command == "register":
            kwargs = {"activate": not args.no_activate, "author": args.author}
            if hasattr(args, "expected"): kwargs["expected"] = args.expected
            result = {"version": service.register(data, **kwargs)}
        elif args.command == "approve":
            validity = {}
            if args.ttl is not None: validity["ttl_seconds"] = args.ttl
            if args.expires_at is not None: validity["expires_at"] = args.expires_at
            result = service.approve(args.artifact, args.reviewer or service.identity,
                                     comment=args.comment, **validity)
        elif args.command == "withdraw":
            result = service.withdraw_approval(args.artifact, args.reviewer or service.identity, comment=args.comment)
        elif args.command == "reject":
            result = service.reject(args.artifact, args.reviewer or service.identity, comment=args.comment)
        elif args.command == "retract":
            result = service.retract(args.artifact, author=args.reviewer, comment=args.comment)
        elif args.command == "status":
            result = service.review_state(args.artifact)
        elif args.command == "classify": result = service.classify(data, args.version)
        elif args.command == "activate":
            kwargs = {"expected": args.expected} if hasattr(args, "expected") else {}
            service.activate(args.artifact, **kwargs)
            result = {"active": args.artifact}
        elif args.command == "replay": result = service.replay(args.run, args.version)
        else: result = service.impact(data, args.before, args.after)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")
    finally:
        service.close()


if __name__ == "__main__": main()

"""Command-line entry point: run one plain-English test case in a browser."""

import argparse
import sys

import tester


def main():
    parser = argparse.ArgumentParser(
        description="Run a plain-English test case in a real browser and report PASS/FAIL."
    )
    parser.add_argument("test", help="the test case in plain English (quote it)")
    parser.add_argument("url", help="starting URL to open")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument(
        "--max-steps", type=int, default=tester.MAX_STEPS_DEFAULT,
        help="maximum actions before verifying (default 10)",
    )
    args = parser.parse_args()

    try:
        config = tester.load_config()
        client = tester.build_client(config)
        result = tester.run_test(
            args.test, args.url, client, config["model"],
            headless=not args.headed, max_steps=args.max_steps,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        sys.exit(2)

    print()
    print(f"STATUS: {result['status']}")
    print(f"STEPS:  {result['steps']}")
    print(f"REASON: {result['reason']}")
    if result.get("verified"):
        print("EVIDENCE (checked against the live page):")
        for item in result["verified"]:
            mark = "OK     " if item["found"] else "MISSING"
            print(f"  [{mark}] {item['type']}={item['value']!r}")

    sys.exit({"PASS": 0, "FAIL": 1, "ERROR": 2}[result["status"]])


if __name__ == "__main__":
    main()

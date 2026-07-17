"""Application entry — calls hub."""

from .hub import hub_entry, HubService, downstream_one


def main(argv=None) -> int:
    n = 3
    if argv and len(argv) > 1:
        n = int(argv[1])
    result = hub_entry(n)
    print(downstream_one(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""`claude-code-ptt` command line: dispatches to the daemon or the installer."""
import sys


def main() -> None:
    args = sys.argv[1:]
    if args == ["install"]:
        from . import installer
        sys.exit(installer.main())
    if args:
        print("usage: claude-code-ptt [install]\n"
              "  (no argument)  run the PTT daemon in the foreground\n"
              "  install        register MCP server + hooks for this user")
        sys.exit(2)
    from . import daemon
    daemon.main()


if __name__ == "__main__":
    main()

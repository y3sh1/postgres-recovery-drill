import argparse
import os

from . import init_database, make_server


def main():
    parser = argparse.ArgumentParser(description="Run the order service or initialize its source database.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init-db", action="store_true", help="create the orders table and exit")
    mode.add_argument("--read-only", action="store_true", help="serve restored orders without opening a journal")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        parser.error("Set DATABASE_URL")
    if args.init_db:
        init_database(dsn)
        return
    path = os.environ.get("JOURNAL_PATH")
    if not path and not args.read_only:
        parser.error("Set JOURNAL_PATH to the SQLite file on the independent journal volume")
    with make_server(dsn, path, args.host, args.port, args.read_only) as server:
        print(f"Listening at http://{server.server_address[0]}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

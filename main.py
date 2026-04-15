from modules.database import init_db
from scanner.scanner_scheduler import run_scanner


if __name__ == "__main__":
    init_db()
    run_scanner()

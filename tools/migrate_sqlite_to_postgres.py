"""Source-checkout wrapper; installed packages expose news2douyin-migrate-sqlite."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from news2douyin.storage.migrate import main
if __name__=='__main__':raise SystemExit(main())

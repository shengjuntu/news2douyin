"""Compatibility entry point; the implementation also ships in wheels."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from news2douyin.assets.prepare import main

if __name__ == '__main__':
    main()

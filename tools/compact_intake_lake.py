"""Make a verified offline compacted lake copy; never replace the active DB.

Stop the receiver and projection service first. Example::
    python tools/compact_intake_lake.py --storage-root runs_v7 \
        --destination /backup/records-compact.duckdb --source-stopped

The immutable ZIP evidence must be accessible. The original records.duckdb is
retained. Reported destination_bytes is measured after CHECKPOINT; logical
column lengths and generic zlib ratios are not promised physical savings.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from news2douyin.intake.lake_migrate import compact_lake


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage-root',required=True)
    parser.add_argument('--destination',required=True,type=Path)
    parser.add_argument('--source-stopped',action='store_true')
    parser.add_argument('--chunk-size',type=int,default=128)
    args=parser.parse_args()
    result=compact_lake(args.storage_root,args.destination,source_stopped=args.source_stopped,chunk_size=args.chunk_size,
        progress=lambda item:print(json.dumps(dict(progress=item)),flush=True))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

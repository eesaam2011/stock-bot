"""Validate a local config and exit; never start a service or replay."""
import argparse,json
from pathlib import Path
from .schema import validate
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('config');a=p.parse_args();validate(json.loads(Path(a.config).read_text()));print('VALID_OFFLINE_REVIEW_ONLY; no service started')

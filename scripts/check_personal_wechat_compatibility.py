from __future__ import annotations

import json

from scripts.personal_wechat_crypto import decrypted_dataset, discover_dataset
from scripts.personal_wechat_keys import load_key
from scripts.personal_wechat_sync import compatibility_summary


def main() -> int:
    dataset = discover_dataset()
    with decrypted_dataset(dataset, load_key) as clear:
        summary = compatibility_summary(clear)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

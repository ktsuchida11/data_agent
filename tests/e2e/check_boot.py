"""本番の起動（chainlit run src/data_agent/ui/app.py）と同じ読み方で、画面のファイルを読み込めるかを確かめる。

ほかの確認（fake_app.py で起動するもの）は、ui/app.py を data_agent.ui.app として読むので、
本番とは読み方が違う。Chainlit は、起動するファイルを実行し終えてから sys.modules に登録するため、
実行の途中で自分のモジュールを探す書き方（@dataclass など）は、本番の起動でだけ止まる（2026-11-15 に起きた）。
ui/app.py を変えたら、これも通す。API キーもネットワークも要らない。

    docker run --rm --network none -v "$PWD/src:/app/src:ro" -v "$PWD/config:/app/config:ro" \
      -v "$PWD/tests:/app/tests:ro" data-agent:local python tests/e2e/check_boot.py
"""

from __future__ import annotations

import sys
import traceback

from chainlit.config import load_module

TARGET = "src/data_agent/ui/app.py"  # Dockerfile の CMD と同じ


def main() -> int:
    try:
        load_module(TARGET)  # chainlit run が使うものと同じ関数
    except Exception:
        traceback.print_exc()
        print(f"NG: {TARGET} を、本番の起動と同じ読み方で読み込めなかった")
        return 1
    module = sys.modules.get(TARGET)
    if module is None or not hasattr(module, "_drive"):
        print(f"NG: {TARGET} を読み込んだが、中身が見つからない")
        return 1
    print(f"OK: {TARGET} を、本番の起動と同じ読み方で読み込めた")
    return 0


if __name__ == "__main__":
    sys.exit(main())

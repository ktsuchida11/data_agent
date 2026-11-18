"""コード実行専用のコンテナ（sandbox/）を、単体で確かめる。

sandbox と同じネットワークにつないだ別のコンテナの中で実行する。標準ライブラリだけで動く。

    SANDBOX_URL=http://sandbox:8080 SANDBOX_TOKEN=... python check_sandbox.py

sandbox は、外へ出られないネットワーク（internal）につなぎ、SANDBOX_TOKEN と
メモリの上限（512MB 程度）を付けて起動しておくこと。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

URL = os.environ.get("SANDBOX_URL", "http://sandbox:8080")
TOKEN = os.environ.get("SANDBOX_TOKEN", "")


def check(label: str, ok: bool, detail: object = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {label}" + (f": {str(detail)[:300]}" if detail != "" else ""))
    if not ok:
        raise SystemExit(1)


def post(body: dict, token: str | None = None, timeout: int = 120) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is None:
        token = TOKEN
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{URL}/run", data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def run(code: str, files: dict[str, bytes] | None = None, timeout: int = 60) -> dict:
    payload = [{"name": n, "content_b64": base64.b64encode(c).decode()} for n, c in (files or {}).items()]
    status, result = post({"code": code, "timeout": timeout, "files": payload})
    check("実行のリクエストが通る", status == 200, (status, result)) if status != 200 else None
    result["by_name"] = {f["name"]: base64.b64decode(f["content_b64"]) for f in result["files"]}
    return result


def tiny_pdf(text: str) -> bytes:
    """文字が1行だけ入った、最小の PDF を作る。"""
    stream = f"BT /F1 18 Tf 40 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def main() -> None:
    with urllib.request.urlopen(f"{URL}/health", timeout=10) as res:
        check("起動している", json.loads(res.read()) == {"ok": True})

    # ---------- 基本 ----------
    r = run("import os, sys\nprint('こんにちは', 1 + 1)\nprint(os.getuid(), sys.version_info[:2])")
    check("コードが動き、標準出力が返る", r["exit_code"] == 0 and "こんにちは 2" in r["output"], r)
    check("一般ユーザーで動いている", "10001 (3, 12)" in r["output"], r["output"])
    r = run("raise ValueError('わざと失敗')")
    check("失敗すると、終了コードとエラーの内容が返る", r["exit_code"] == 1 and "わざと失敗" in r["output"], r)

    # ---------- グラフ・Excel・PowerPoint・PDF ----------
    r = run(
        "import warnings\nwarnings.simplefilter('error')\n"  # 文字が描けないときの警告を、エラーにする
        "import matplotlib.pyplot as plt\nfrom matplotlib import font_manager\n"
        "fig, ax = plt.subplots()\nax.bar(['東京', '大阪', '福岡'], [120, 80, -45])\n"
        "ax.set_title('地域別の売上（万円）')\nfig.savefig('chart.png')\n"
        "print(font_manager.findfont(plt.rcParams['font.sans-serif'][0]))\n"
    )
    png = r["by_name"].get("chart.png", b"")
    check(
        "日本語のグラフを画像にできる", r["exit_code"] == 0 and png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000, r["output"]
    )
    check("日本語のフォントが使われている", "ipaex" in r["output"].lower(), r["output"])

    csv = "地域,売上\n東京,120\n大阪,80\n福岡,45\n".encode()
    r = run(
        "import pandas as pd\ndf = pd.read_csv('sales.csv')\nprint(df['売上'].sum())\nprint(df.to_markdown(index=False))\n"
        "with pd.ExcelWriter('report.xlsx') as w:\n    df.to_excel(w, sheet_name='売上', index=False)\n"
        "from openpyxl import load_workbook\nprint(load_workbook('report.xlsx')['売上']['B2'].value)\n",
        {"sales.csv": csv},
    )
    check("渡したファイルを読んで集計できる", r["exit_code"] == 0 and "245" in r["output"], r["output"])
    check("表（Markdown）にできる", "| 東京" in r["output"], r["output"])
    check("Excel のファイルを作れる", r["by_name"].get("report.xlsx", b"")[:2] == b"PK" and "120" in r["output"], r["output"])
    check("渡したままのファイルは、出力として返らない", "sales.csv" not in r["by_name"], list(r["by_name"]))

    r = run(
        "from pptx import Presentation\nfrom pptx.util import Inches\nimport matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3], [2, 4, 8]); plt.title('推移'); plt.savefig('c.png')\n"
        "prs = Presentation()\ns = prs.slides.add_slide(prs.slide_layouts[5])\ns.shapes.title.text = '売上の報告'\n"
        "s.shapes.add_picture('c.png', Inches(1), Inches(1.5), width=Inches(6))\nprs.save('deck.pptx')\n"
        "print(len(Presentation('deck.pptx').slides), Presentation('deck.pptx').slides[0].shapes.title.text)\n"
    )
    check(
        "PowerPoint のファイルを作れる",
        r["by_name"].get("deck.pptx", b"")[:2] == b"PK" and "1 売上の報告" in r["output"],
        r["output"],
    )

    r = run(
        "from pypdf import PdfReader\nimport pdfplumber\nprint('pypdf:', PdfReader('doc.pdf').pages[0].extract_text())\n"
        "with pdfplumber.open('doc.pdf') as pdf:\n    print('plumber:', pdf.pages[0].extract_text())\n",
        {"doc.pdf": tiny_pdf("Hello PDF 2026")},
    )
    check("PDF から文字を取り出せる", r["output"].count("Hello PDF 2026") == 2, r["output"])

    r = run("open('sales.csv', 'a').write('札幌,30\\n')", {"sales.csv": csv})
    check("書き換えたファイルは、出力として返る", "札幌".encode() in r["by_name"].get("sales.csv", b""), list(r["by_name"]))

    # ---------- 隔離 ----------
    r = run(
        "import socket\n"
        "for label, f in (('ip', lambda: socket.create_connection(('1.1.1.1', 53), timeout=3)),\n"
        "                 ('dns', lambda: socket.getaddrinfo('example.com', 80))):\n"
        "    try:\n        f(); print(label, 'REACHED')\n    except OSError as e:\n        print(label, 'blocked', type(e).__name__)\n"
    )
    check("インターネットに出られない", r["output"].count("blocked") == 2 and "REACHED" not in r["output"], r["output"])

    r = run(
        "import os\nfor path in ('/usr/x', '/app/x', '/etc/x', '/home/x'):\n"
        "    try:\n        open(path, 'w'); print('WROTE', path)\n    except OSError as e:\n        print('ro', path)\n"
    )
    check("決まった場所のほかには書き込めない", r["output"].count("ro ") == 4 and "WROTE" not in r["output"], r["output"])

    r = run(
        "import glob, os\nfound = []\n"
        "for path in glob.glob('/proc/[0-9]*/environ'):\n"
        "    try:\n        found.append(open(path, 'rb').read().decode(errors='replace'))\n    except OSError:\n        pass\n"
        "print('SANDBOX_TOKEN' in os.environ, any('SANDBOX_TOKEN' in t for t in found), len(found))\n"
    )
    check("サーバーの環境変数（トークン）を、コードから読めない", r["output"].startswith("False False"), r["output"])

    r = run(
        "import os, subprocess\n"
        "subprocess.Popen(['sleep', '300'], start_new_session=True)\n"
        "for path in ('/work/left.txt', '/tmp/left.txt', '/dev/shm/left.txt', os.path.expanduser('~/left.txt')):\n"
        "    try:\n        open(path, 'w').write('x'); print('wrote', path)\n    except OSError:\n        print('cannot', path)\n"
    )
    wrote = r["output"].count("wrote")
    r = run(
        "import glob, os\n"
        "alive = [p for p in glob.glob('/proc/[0-9]*/cmdline') if b'sleep' in open(p, 'rb').read()]\n"
        "left = [p for p in ('/work/left.txt', '/tmp/left.txt', '/dev/shm/left.txt') if os.path.exists(p)]\n"
        "print('alive', len(alive), 'left', left, 'runs', len(os.listdir('/work')))\n"
    )
    check(
        f"裏で起動したプロセスと、置いていったファイル（{wrote} か所）は、次の実行に残らない",
        "alive 0 left [] runs 1" in r["output"],
        r["output"],
    )

    r = run("while True:\n    pass\n", timeout=2)
    check("時間切れで止まる", r["timed_out"] and r["exit_code"] is None and r["duration_ms"] < 8000, r)

    r = run("x = bytearray(2 * 1024 ** 3)\nfor i in range(0, len(x), 4096):\n    x[i] = 1\nprint('ALLOCATED')\n")
    check(
        "メモリを使いすぎたコードは止められる",
        r["exit_code"] != 0 and "ALLOCATED" not in r["output"],
        (r["exit_code"], r["output"][-200:]),
    )
    with urllib.request.urlopen(f"{URL}/health", timeout=10) as res:
        check("そのあとも、サーバーは動いている", res.status == 200)

    # ---------- 上限 ----------
    r = run("for i in range(25):\n    open(f'f{i:02d}.txt', 'w').write('x')\n")
    reasons = {s["reason"] for s in r["skipped"]}
    check(
        "返すファイルの数に上限がある",
        len(r["files"]) == 20 and len(r["skipped"]) == 5 and reasons == {"too_many_files"},
        r["skipped"],
    )
    r = run("open('big.bin', 'wb').write(b'0' * (30 * 1024 * 1024))\nopen('small.txt', 'w').write('ok')\n")
    check(
        "大きすぎるファイルは返さない",
        list(r["by_name"]) == ["small.txt"] and r["skipped"][0]["reason"] == "too_large",
        r["skipped"],
    )
    r = run("print('あ' * 200000)\nprint('END-OF-OUTPUT')\n")
    check(
        "長い出力は、先頭と末尾だけが返る",
        r["truncated"] and len(r["output"]) < 21000 and "END-OF-OUTPUT" in r["output"],
        len(r["output"]),
    )
    r = run("import os\nos.symlink('/etc/passwd', 'link.txt')\n")
    check("リンクは返さない", r["files"] == [] and r["skipped"][0]["reason"] == "not_a_file", r["skipped"])

    # ---------- リクエストの検査 ----------
    status, body = post({"code": "print(1)"}, token="wrong") if TOKEN else (401, {})
    check("トークンが違うと実行できない", status == 401, (status, body))
    status, body = post({"timeout": 5})
    check("コードがないリクエストは断る", status == 400, (status, body))
    status, body = post({"code": "print(1)", "files": [{"name": "../escape.txt", "content_b64": ""}]})
    check("フォルダの外を指すファイル名は断る", status == 400, (status, body))
    status, body = post({"code": "print(1)", "timeout": 100000})
    check("長すぎる timeout は、上限に丸められて動く", status == 200 and body["exit_code"] == 0, (status, body))
    print("DONE")


if __name__ == "__main__":
    main()

# 5. 自分のツールを Python で足す

社内の API は、MCP でつなぎます（[4](04-mcp.md)）。Python でツールを書くのは、計算や形の変換のように、**毎回同じ答えにしたい処理**です（ADR-0032）。モデルに暗算させるより確かです。
書くのは、関数と、その「扱い」だけです。確認・回数の上限・送る前の確かめ・返事の照合・確認の画面の名前は、扱いから自動で決まります。

## 見本

見本のパックの `packs/internal_api_example/tools.py` です。税抜きの金額から、消費税と税込みの金額を出します。モデルは、受注の詳細（MCP のツール）で金額を見てから、このツールで計算します。

```python
from data_agent.tools.registry import agent_tool

TAX_RATE_PERCENT = 10  # 消費税の率（見本なので 10% で決め打ち）


@agent_tool(label="税込みの金額", grounding=True)
def price_with_tax(amount: int) -> str:
    """税抜きの金額から、消費税（10%）と税込みの金額を出す。1 円未満は切り捨てる。

    Args:
        amount: 税抜きの金額（円。受注の amount）
    """
    if amount < 0:
        return "金額は 0 以上にしてください。"
    tax = amount * TAX_RATE_PERCENT // 100
    total = amount + tax
    return f"税抜き {amount:,} 円の消費税は {tax:,} 円、税込みは {total:,} 円です（{TAX_RATE_PERCENT}%）。"
```

- **関数の名前**（`price_with_tax`）が、ツールの名前になります。英字で始め、英数字と `_` で付けます。
- **docstring**（`"""…"""`）が、モデルへの説明になります。1 行目に「何をするか」、`Args:` に引数の説明を書きます。モデルは、これを読んで、いつ・どう使うかを決めます。
- **引数には型を書きます**（`amount: int`）。モデルが渡す値の形になります。
- **戻り値は文字列にします。** モデルが読みやすい、短い文にします。
- **`grounding=True`** にすると、返事の数の照合で、このツールの結果を根拠にします。返事に書いた「1,716,000 円」が、ツールの結果にある数だと確かめられます。
- 時間のかかる処理は、`async def` でも書けます。

## `@agent_tool` に書く扱い

| 項目 | 意味 | 既定 |
|---|---|---|
| `label` | 確認の画面や、定時実行の説明に出す名前（必ず書く） | なし |
| `require_approval` | 実行の前に確かめるか。`True`（毎回）・`"auto"`（送る内容が怪しいときだけ）・`False` | `False` |
| `check_arguments` | 引数に鍵や個人情報があれば、送らずに止めるか | `True` |
| `max_calls_per_run` | 1 回の依頼で呼べる回数 | なし |
| `grounding` | 返事の数の照合で、結果を根拠にするか | `False` |
| `strict` | 画面で「毎回確認」を選んだチャットで、確かめるか | `True` |

**書き換える・消す・送るツールは、`require_approval=True` にします。**

## 手順

1. パックのフォルダに `tools.py` を作り、上の見本のように関数を書きます。
2. パックの `pack.yaml` に、ファイルを書きます。
   ```yaml
   python_tools: [tools.py]
   ```
3. パックの中身を確かめます。書いたツールと、その扱いが出れば、読み込めています。
   ```bash
   uv run python -m data_agent.tools.registry
   ```
4. 作り直して、画面で試します。
   ```bash
   docker compose up --build -d
   ```

## エラーが起きたとき

- 関数の中でエラーが起きても、アプリは止まりません。モデルには「税込みの金額でエラーが起きました（ValueError）」のように、**エラーの種類だけ**を返します。エラーの文には、値や接続先が入ることがあるためです。
- モデルに理由を伝えたいときは、`ToolException` を投げます。
  ```python
  from langchain_core.tools import ToolException

  raise ToolException("金額は 0 以上にしてください")
  ```

## テストを書く

`tests/test_<パックの名前>.py` に書きます。

```python
import asyncio

from data_agent.settings import load_pack
from data_agent.tools import registry


def test_price_with_tax():
    pack = load_pack("internal_api_example")
    tools = {t.name: t.tool for t in registry.python_tools(pack)}
    result = asyncio.run(tools["price_with_tax"].ainvoke({"amount": 1000}))
    assert "税込みは 1,100 円" in result
```

```bash
uv run pytest -q tests/test_internal_api_example.py
```

## 気をつけること

- **社内の API は、Python のツールから呼ばずに、MCP でつなぎます（[4](04-mcp.md)）。** MCP なら、使うツールの名指し、鍵の扱い、社内の認証局の確かめが、設定だけで効きます。Python のツールから API を直接呼ぶ部品は、用意していません（ADR-0033）。
- **Python のツールは、アプリと同じ権限で動きます。** 鍵（環境変数）にもネットワークにも届きます。アプリのコードと同じように、中身を確かめてから足します。
- **ツールの名前は、元からあるツール（`read_file`・`write_file`・`task` など）と同じにできません。** ほかのツールと重なっても、起動の前に止まります。
- **ファイルを読み書きするツールは作りません。** ファイルは、エージェントの元からのツール（作業フォルダの中だけ）で扱います。

次は [6. 確かめる](06-verify.md)。

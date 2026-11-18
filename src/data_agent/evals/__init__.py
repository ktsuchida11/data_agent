"""点検（測るためのもの。ふだんのチャットでは動かない）。本物のモデルを呼ぶと料金がかかる。

- redteam/: 指示の乗っ取り（間接注入）への強さを測る。`python -m data_agent.evals.redteam`
- factcheck/: 作り話の多さを測る。`python -m data_agent.evals.factcheck`

どちらも `--selftest` を付けると、偽のモデルで、点検の仕組みそのものを確かめる（料金なし）。
"""

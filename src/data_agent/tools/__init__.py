"""エージェントの道具（Deep Agents の標準のファイル操作のほかに足したもの）。

- code_execution.py: コードの実行（run_python）。コードは sandbox のコンテナで動く
- database.py: データベースを読む（list_database_tables、query_database）。読み取り専用で、結果は CSV に保存する
- web_search.py: Web 検索（web_search）。Claude の検索機能を、チャットとは別の呼び出しで使う
- outputs.py: 道具が作ったファイルを、作業フォルダの /outputs/ に保存する共通の処理
- registry.py: ツールと、その扱い（確認・回数の上限・送る前の確かめ・照合）の一覧。用途パックの Python のツール（@agent_tool）もここで読む（ADR-0032）
- mcp.py: 用途パックの MCP サーバー（HTTPS の社内 API など）のツール（ADR-0033）
- internal_http.py: MCP サーバーに HTTPS でつなぐ部品（鍵は .env、社内の認証局は ca_file）
"""

"""保存と記録。

- persistence.py: 会話の状態（LangGraph のチェックポイント）の保存先。PostgreSQL か、メモリ上
- chat_history.py: 左の一覧に出るチャットの保存（Chainlit のデータ層。再開・削除・アーカイブ・グループ分け）
- tracing.py: Langfuse へのトレースの送信（送る前にマスクする。ADR-0015）
"""

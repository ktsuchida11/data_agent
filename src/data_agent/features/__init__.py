"""便利な機能。

- schedules.py: 定時実行の予定（cron 式の解釈と、データベースへの保存）
- schedule_runner.py: 定時実行の本体（時刻が来た予定を、画面なしで実行する）。
  外から起動するときは `python -m data_agent.features.schedule_runner`
- skill_mining.py: 手順書（スキル）の下書きを作る。繰り返している作業から（スキル提案）と、チャットで頼まれて（スキル登録）
"""

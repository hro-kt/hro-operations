"""hro-operations: 開催日オーケストレーション。

各サーバーで所定のコマンドを起動すれば、その開催日の前向きペーパー運用が回る:
  - Windows(JVLink機): 速報オッズ常駐 (`hro-synchronizer poll-odds`) ← liveオッズ供給
  - Linux(計算機):      当日ランナー (`hro-ops run-day`)            ← T-30s に発注判断→paper購入

詳細は README.md を参照。
"""

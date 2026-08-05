"""台電「今日用電曲線」中繼爬蟲。

模組分工：
  parser     純函式，CSV bytes → Point。不碰網路與資料庫，用 fixture 完整測試。
  fetch      抓三支 CSV，驗內容不是 HTML。
  config     讀 config/config.yml。
  db         upsert 曲線 + append monitor_fetch_run。不建表、不 migration。
  telemetry  推 Pushgateway。

進入點是 scripts/run_once.py。
"""

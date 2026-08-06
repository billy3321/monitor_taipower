# 部署到 Mac

## 0. 前提確認（先做這個，不通就不用往下）

```bash
curl -s -o /dev/null -w "%{http_code}\n" --max-time 20 \
  -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36' \
  https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/loadareas.csv
```

要拿到 **200**。拿到 403 表示這台機器的出口 IP 也被 CloudFront 歸類成雲端／
資料中心網段，換一台或換網路——**這個專案的全部前提就是這一行**。

## 1. Cloud SQL 授權

這台 Mac 的**對外 IP** 要加進 Cloud SQL `billy3321-db` 的 authorized networks。

```bash
curl -s https://api.ipify.org; echo     # 查對外 IP
```

★ 家用／辦公室 IP 若會浮動，換 IP 後爬蟲會連不上資料庫（不是抓不到台電）。
兩種失敗長得不一樣，排查時先分清楚是哪一種。

## 2. 憑證

從既有爬蟲機複製三個檔（跟 `monitor_strait_info` 同一組）：

```
config/ssl/server-ca.pem
config/ssl/client-cert.pem
config/ssl/client-key.pem
```

```bash
chmod 600 config/ssl/client-key.pem
```

## 3. 安裝

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config/config.yml.example config/config.yml   # 填密碼與 pushgateway
./venv/bin/python scripts/preflight.py           # 前置檢查（此時就能跑）
```

`preflight.py` 只驗「這台機器抓不抓得到」，**不寫資料庫也不需要 config**——
所以它能乾淨地把「抓不到台電」跟「寫不進資料庫」分開。三個檔都 ✓ 才有後面的事。

正式進入點是 `scripts/run_once.py`。手動跑一次要看到：抓到 4 支檔、原文已歸檔、
**兩支曲線總和吻合**、**即時用電與能源別合計吻合**、寫入筆數、fetch_run 已記錄。

```bash
./venv/bin/pytest -q                      # 73 條
./venv/bin/python scripts/verify_fixtures.py
./venv/bin/python scripts/run_once.py
```

## 4. 排程（launchd）

macOS 用 launchd，不是 cron。把 plist 放到使用者的 LaunchAgents：

```bash
mkdir -p ~/Library/LaunchAgents
cp deployment/tw.nics.taipower-curve.plist ~/Library/LaunchAgents/
# ★ 編輯 plist，把路徑換成實際的專案路徑
launchctl load ~/Library/LaunchAgents/tw.nics.taipower-curve.plist
launchctl list | grep taipower
```

查日誌：

```bash
tail -f /tmp/taipower-curve.log
```

## 5. Mac 特有的坑

- **睡眠**：Mac 睡著時 launchd 不會跑。錯過的排程在喚醒後**只補跑一次**，
  睡了 8 小時不會補 8 次。
  → 這台機器要設成不睡（系統設定 → 電池／電源 → 防止自動進入睡眠），
    或接受夜間有缺口。**缺口不是「那時候沒用電」，圖上要看得出是缺資料。**
  → ★ 夜間睡著最貴：`23:55`／`23:59` 那兩次補不回來（檔案 00:00 換日重置）。
    白天漏掉幾次則不損失曲線——當日累積檔會補齊。詳見 README 的健康度一節。
- **App Nap**：長時間背景執行可能被降頻。用 launchd 定時啟動短命程序
  （每次跑完就結束）比常駐程序安全。
- **憑證權限**：`client-key.pem` 不是 600 的話 psycopg2 會拒絕連線。

## 6. 確認它真的在做事

```sql
SELECT kind, count(*), count(DISTINCT observed_at) AS 時點數,
       max(observed_at AT TIME ZONE 'Asia/Taipei')::time AS 最新
FROM monitor_power_load_curve
WHERE (observed_at AT TIME ZONE 'Asia/Taipei')::date
      = (now() AT TIME ZONE 'Asia/Taipei')::date
GROUP BY 1 ORDER BY 1;
```

正常應該是 `fuel` 12 × N 點、`area` 4 × N 點，N 隨當日時間增長到 144；
`area_gen`／`area_load`／`capacity` 則是每小時一點（那三者不累積）。

更完整的對帳 SQL（斷點偵測、每次執行的結果）與四種失敗的分辨方式，
見 README 的「確認它真的在做事」與「抓取健康度」兩節。

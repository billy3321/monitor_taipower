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
./venv/bin/pytest -q                      # 120 條
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

## 7. 備援機（Windows）

★ 主端是那台 Mac。這一節講的是**第二台**——`config.yml` 設 `mode: backup`，
  主端活著就待命、死了才接手。判斷邏輯與那三個綁在一起的數字見 CLAUDE.md
  的「備援模式」。這裡只講 Windows 這台怎麼裝。

```powershell
py -3.13 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy config\config.yml.example config\config.yml   # 填密碼、pushgateway
# 憑證三個檔放進 config\ssl\（server-ca.pem / client-cert.pem / client-key.pem）
$env:PYTHONUTF8=1; .\venv\Scripts\python.exe scripts\preflight.py
$env:PYTHONUTF8=1; .\venv\Scripts\python.exe -m pytest -q
powershell -ExecutionPolicy Bypass -File deployment\register_backup_task.ps1
```

`config.yml` 要改兩個地方：`mode: backup`，以及 **`instance_id` 換成跟主端
不同的字串**（這台是 `win-relay-02`）。後者不改的話兩台共用同一個 grouping
key，備援的 `last_success` 會蓋掉主端的——**主端死掉會隱形**。

排程是兩個工作：`TaipowerCurveBackup`（每小時 `:56`）與
`TaipowerCurveBackupMidnight`（`23:59`）。移除用
`register_backup_task.ps1 -Unregister`。日誌在 `data\backup-run.log`。

### ★ Windows 特有的四個坑（四個都踩過）

1. **`tzdata`**。Windows 沒有系統 tz 資料庫，`ZoneInfo('Asia/Taipei')` 直接丟
   `ZoneInfoNotFoundError`，整組測試連 collect 都過不了。已經列進
   `requirements.txt`。

2. **`PYTHONUTF8=1`**。這台主控台是 cp932，Python 的 stdout 跟著用 cp932，
   log 裡第一個中文字就 `UnicodeEncodeError` 把整次執行打掉——看起來像
   「爬蟲壞了」，其實只是印不出字。`deployment\run_backup.cmd` 已經設好。

3. **`.cmd` 必須純 ASCII（註解也是）**。cmd.exe 把批次檔當成 OEM 代碼頁的
   原始位元組讀，UTF-8 中文在 `rem` 行會解成把換行吃掉的位元組對，
   cmd 於是去執行半句註解，整個腳本死在
   `is not recognized as an internal or external command`。

4. **`.ps1` 必須存成 UTF-8 with BOM**。Windows PowerShell 5.1 沒看到 BOM
   就當系統 ANSI 讀，中文變亂碼後在某個位元組上把引號吃掉，報的是
   `The string is missing the terminator`——看起來像語法錯，其實是編碼。

### ★ 時間：先校時，而且時區必須是台北

備援的判斷是**拿本機時間去跟資料庫的 `fetched_at` 比**，時鐘偏掉就會判錯
（偏快→以為主端死了而搶著抓；偏慢→以為主端還活著而不接手）。

```powershell
Get-TimeZone                 # 要是 Taipei Standard Time
w32tm /resync /force
```

★ 工作排程器跟著**系統時區**跑。時區錯掉的話 `23:59` 那一次會跑在錯的時刻，
  而檔案 00:00 換日重置，當天最後那幾個點就永久遺失——圖上看起來只像
  「那時候沒用電」。`register_backup_task.ps1` 註冊前會擋下非台北時區。

### ★ 睡眠與登入：跟 Mac 那節同一個問題

工作已經設了 `-WakeToRun -StartWhenAvailable`，但機器關機時排程不會跑，
開機後也只補跑一次。夜間睡著最貴（`23:56`／`23:59` 補不回來），
白天漏掉則由當日累積檔補齊。

★ 工作是用 `-LogonType Interactive` 註冊的，所以**只有使用者登入時才會跑**。
  重開機後沒登入就一次都不會跑，而且工作排程器上看起來一切正常（狀態
  還是 Ready）。要讓它在沒登入時也跑，得改成 `-LogonType Password` 並輸入
  密碼（或 S4U，需要權限）——那是另一個決定，不要順手改。

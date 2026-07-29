# 双浏览器启动（BOSS + 智联，各用独立 profile）
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"

# BOSS
& $chrome --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="$env:TEMP\chrome_boss" "https://www.zhipin.com"

# 智联
& $chrome --remote-debugging-port=9223 --remote-allow-origins=* --user-data-dir="$env:TEMP\chrome_zhilian" "https://www.zhaopin.com"

# activate venv
D:\findjob\findjob_new\v2\venv\Scripts\Activate.ps1

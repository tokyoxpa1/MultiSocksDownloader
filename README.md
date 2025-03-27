# MultiSocksDownloader - 多SOCKS5代理下載器

MultiSocksDownloader是一個基於Python開發的多代理下載工具，可以同時使用多個SOCKS5代理伺服器進行檔案下載，大幅提高下載速度和穩定性。這個工具特別適合下載大檔案和需要分流或繞過網路限制的場景。

![版本](https://img.shields.io/badge/版本-1.0-blue.svg)
![Python](https://img.shields.io/badge/Python-3.6+-green.svg)
![平台](https://img.shields.io/badge/平台-Windows/Linux/MacOS-lightgrey.svg)

## 功能特性

- **多代理伺服器支持**：能夠同時使用多個SOCKS5代理伺服器進行下載
- **多線程下載**：單個檔案可分割成多個部分並行下載，顯著提高下載速度
- **斷點續傳**：支持暫停和恢復下載，不需要從頭開始
- **下載進度持久化**：程式關閉後可以自動恢復未完成的下載任務
- **代理可用性測試**：內建對SOCKS5代理的測試功能
- **友好的圖形界面**：使用PyQt5實現的簡潔、直觀的用戶界面
- **Chrome瀏覽器整合**：通過Chrome擴充功能可以直接攔截瀏覽器下載並轉交給下載器處理
- **支持特殊URL**：對某些需要特殊處理的URL能夠自動檢測檔案名稱並進行正確下載

## 系統需求

- Python 3.6 或更高版本
- Windows、Linux或MacOS
- 依賴套件：
  - PyQt5：圖形界面
  - requests：HTTP請求
  - PySocks：SOCKS代理支持

## 安裝方法

### 方法一：使用執行檔（Windows用戶）

1. 從release頁面下載 `MultiSocksDownloader.exe`
2. 雙擊執行程式

### 方法二：從原始碼執行

1. 克隆或下載本專案
2. 安裝依賴套件：

```bash
pip install -r requirements.txt
```

3. 執行主程式：

```bash
python MultiSocksDownloader.py
```

### Chrome擴充功能安裝（可選）

1. 安裝Chrome瀏覽器擴充功能：
   - 開啟Chrome擴充功能頁面：`chrome://extensions/`
   - 開啟右上角的「開發者模式」
   - 點擊「載入未封裝項目」，選擇 `chrome_extension` 目錄

2. 安裝Native Messaging主機：
   - 確保已安裝Python 3.6或更高版本
   - 進入 `chrome_extension` 目錄
   - 執行安裝腳本，替換 `YOUR_EXTENSION_ID` 為實際的擴充功能ID：

   ```
   python install_host.py --extension-id YOUR_EXTENSION_ID
   ```

## 使用說明

### 添加和管理代理伺服器

1. 點擊頂部標籤中的「代理設置」標籤
2. 點擊「添加代理」按鈕
3. 輸入代理名稱、主機地址和端口
4. 點擊「測試」按鈕檢查代理是否可用
5. 管理已添加的代理：右鍵點擊代理列表中的項目可以刪除或測試

### 下載文件

1. 切換到「下載管理」標籤
2. 在「URL」欄位中輸入下載連結
3. 檢查或修改保存目錄
4. 選擇性地設定檔案名稱（留空時會自動從URL中提取）
5. 調整線程數（建議根據代理數量和網路情況調整）
6. 勾選「使用SOCKS5代理」選項使用已設定的代理
7. 點擊「添加下載」開始任務

### 管理下載任務

- 查看下方的下載列表可以監控所有下載任務的進度
- 右鍵點擊任務可以執行以下操作：
  - 暫停/恢復：暫時停止或繼續下載
  - 取消：徹底刪除下載任務
  - 打開所在資料夾：檔案下載完成後，可快速打開所在目錄

### 通過Chrome擴充功能使用

安裝Chrome擴充功能後：
1. 確保MultiSocksDownloader程式正在運行
2. 在Chrome中右鍵點擊任何下載連結
3. 選擇「使用多代理下載器下載」選項
4. 下載將自動添加到MultiSocksDownloader並開始處理

## 技術架構

MultiSocksDownloader由以下幾個主要模組組成：

- `downloader.py`：下載引擎核心，實現多線程下載、斷點續傳、代理管理等功能
- `ui.py`：基於PyQt5的圖形用戶界面
- `http_server.py`：內建HTTP伺服器，用於接收Chrome擴充功能的下載請求
- `MultiSocksDownloader.py`：程式入口，協調各模組
- `chrome_extension/`：Chrome瀏覽器擴充功能，攔截瀏覽器下載並傳遞給下載器

### 下載機制

程式採用多線程分片下載機制：
1. 獲取檔案總大小
2. 根據線程數將檔案分割為多個部分
3. 每個線程負責下載一個部分
4. 如果有多個代理，線程將被分配到不同代理上
5. 所有部分下載完成後合併為完整檔案

## 常見問題

### Q: 為什麼有些代理測試失敗？
A: 可能是代理伺服器已失效、連接超時或者格式錯誤。確保代理伺服器地址和端口正確。

### Q: 下載速度很慢怎麼辦？
A: 可以嘗試以下方法：
- 增加可用的SOCKS5代理數量
- 調整線程數（通常5-8個比較合適）
- 檢查代理伺服器的網路品質
- 對於支持的檔案，嘗試使用單線程模式下載

### Q: 程式關閉後下載任務會遺失嗎？
A: 不會。程式會自動保存下載進度，重新啟動後可以繼續未完成的任務。

## 授權協議

本專案採用MIT授權協議。詳細信息請參閱LICENSE文件。

## 致謝

本專案的部分設計參考了以下開源項目：
- [Motrix](https://github.com/agalwood/Motrix)：多線程下載管理器
- [PyIDM](https://github.com/pyidm/PyIDM)：Python實現的下載管理器 
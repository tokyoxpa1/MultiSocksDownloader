#!/usr/bin/env python3
"""
多線程下載器 - 支持斷點續傳的下載工具
"""

import sys
from ui import QApplication, MainWindow
from http_server import HttpServer
from downloader import DownloadManager

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # 創建下載管理器實例（以後將通過共享單例模式優化）
    download_manager = DownloadManager()
    
    # 啟動 HTTP 伺服器
    http_server = HttpServer(download_manager)
    server_started = http_server.start()
    
    # 創建主窗口，傳入已有的下載管理器
    window = MainWindow(download_manager)
    
    # 不再更新 UI 上的伺服器狀態
    
    # 註冊回調函數，讓 HTTP 伺服器可以通知 UI 有新任務添加
    if server_started:
        http_server.add_task_added_callback(window.on_task_added)
    
    window.show()
    
    # 應用結束時關閉 HTTP 伺服器
    app.aboutToQuit.connect(http_server.stop)
    
    sys.exit(app.exec_()) 
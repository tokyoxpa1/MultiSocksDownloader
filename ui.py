import sys
import os
import time
import threading
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QLineEdit, QPushButton, QSpinBox, QFileDialog, 
    QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView, 
    QMessageBox, QAbstractItemView, QMenu, QTabWidget, QCheckBox
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread, QSize, QEvent
from PyQt5.QtGui import QIcon, QFont, QColor

from downloader import DownloadManager

# 格式化文件大小顯示
def format_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while size_bytes >= 1024 and i < len(size_name) - 1:
        size_bytes /= 1024
        i += 1
    return f"{size_bytes:.2f} {size_name[i]}"

# 格式化時間顯示
def format_time(seconds):
    if seconds < 1:
        return "0秒"
        
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    if hours > 0:
        return f"{hours}時{minutes}分{seconds}秒"
    elif minutes > 0:
        return f"{minutes}分{seconds}秒"
    else:
        return f"{seconds}秒"

# SOCKS5代理測試線程
class ProxyTester(QThread):
    """SOCKS5代理測試線程"""
    test_finished = pyqtSignal(str)  # 信號：測試完成，參數為代理ID
    
    def __init__(self, download_manager, proxy_id):
        super().__init__()
        self.download_manager = download_manager
        self.proxy_id = proxy_id
        self.is_canceled = False
        
    def run(self):
        """執行測試"""
        print(f"開始測試代理 {self.proxy_id}")
        try:
            # 檢查是否被取消
            if self.is_canceled:
                print(f"代理 {self.proxy_id} 測試已被取消")
                return
                
            # 調用下載管理器的測試方法
            result = self.download_manager.test_socks_proxy(self.proxy_id)
            success, message = result
            print(f"測試結果: success={success}, message={message}")
            
            # 檢查是否被取消
            if self.is_canceled:
                print(f"代理 {self.proxy_id} 測試已被取消")
                return
                
            # 測試完成後發送信號
            self.test_finished.emit(self.proxy_id)
        except Exception as e:
            print(f"測試代理時出錯: {e}")
            # 即使出錯也發送信號，確保UI更新
            if not self.is_canceled:
                self.test_finished.emit(self.proxy_id)
                
    def cancel(self):
        """取消測試"""
        self.is_canceled = True
        print(f"代理 {self.proxy_id} 測試被標記為取消")

# 監控任務進度的線程
class MonitorThread(QThread):
    progress_update = pyqtSignal(dict)
    
    def __init__(self, download_manager):
        super().__init__()
        self.download_manager = download_manager
        self.running = True
        # 保存已知任務 ID，用於檢測新任務
        self.known_task_ids = set()
        
    def run(self):
        while self.running:
            tasks = self.download_manager.get_all_tasks()
            
            # 檢查是否有新任務
            current_task_ids = set(task['id'] for task in tasks)
            new_task_ids = current_task_ids - self.known_task_ids
            
            if new_task_ids:
                print(f"監控線程檢測到新任務: {new_task_ids}")
            
            # 更新所有任務
            for task in tasks:
                self.progress_update.emit(task)
            
            # 更新已知任務 ID 集合
            self.known_task_ids = current_task_ids
            
            # 休眠1秒鐘，讓總耗時每秒更新一次
            time.sleep(1.0)
            
    def stop(self):
        self.running = False

# 主窗口
class MainWindow(QMainWindow):
    def __init__(self, download_manager=None):
        super().__init__()
        
        # 使用傳入的 download_manager 或創建新的
        self.download_manager = download_manager if download_manager is not None else DownloadManager()
        self.task_table = None  # 初始化為 None
        
        # 存儲正在運行的代理測試線程，避免被過早釋放
        self.proxy_testers = {}
        
        self.setup_ui()  # 首先設置 UI，確保 task_table 被初始化
        
        # 更新保存目錄顯示
        self.dir_input.setText(self.download_manager.save_dir)
        
        self.monitor_thread = MonitorThread(self.download_manager)
        self.monitor_thread.progress_update.connect(self.update_task_progress)
        self.monitor_thread.start()
        
        # 恢復未完成的任務
        count = self.download_manager.scan_unfinished_tasks()
        if count > 0:
            # 不再顯示確認對話框，直接恢復
            print(f"已自動恢復 {count} 個未完成的下載任務")
            # 將恢復的任務添加到任務列表
            self.display_restored_tasks()
            
        # 載入已保存的SOCKS5代理
        self.load_socks_proxies()
        
    def setup_ui(self):
        self.setWindowTitle("多socks5代理下載器")
        self.setMinimumSize(800, 600)
        
        # 主佈局
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)
        
        # 創建標籤頁
        self.tab_widget = QTabWidget()
        
        # === 下載標籤頁 ===
        download_tab = QWidget()
        download_layout = QVBoxLayout(download_tab)
        
        # URL輸入區域
        url_layout = QHBoxLayout()
        url_label = QLabel("URL:")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("輸入下載連結...")
        url_layout.addWidget(url_label)
        url_layout.addWidget(self.url_input)
        
        # 保存目錄選擇
        dir_layout = QHBoxLayout()
        dir_label = QLabel("保存目錄:")
        self.dir_input = QLineEdit()
        # 不在此處設置目錄，將在 __init__ 中設置
        self.dir_input.setReadOnly(True)
        dir_button = QPushButton("瀏覽...")
        dir_button.clicked.connect(self.select_save_dir)
        dir_layout.addWidget(dir_label)
        dir_layout.addWidget(self.dir_input)
        dir_layout.addWidget(dir_button)
        
        # 檔案名稱和線程數
        file_layout = QHBoxLayout()
        file_label = QLabel("檔案名稱:")
        self.file_input = QLineEdit()
        self.file_input.setPlaceholderText("留空自動從URL提取")
        
        # 分成兩行布局
        file_layout.addWidget(file_label)
        file_layout.addWidget(self.file_input)
        
        # 添加第二行布局，包含進階設置
        advanced_layout = QHBoxLayout()
        
        # 每個代理線程數配置
        proxy_threads_label = QLabel("每代理線程:")
        self.proxy_threads_spinbox = QSpinBox()
        self.proxy_threads_spinbox.setRange(1, 10)
        self.proxy_threads_spinbox.setValue(3)
        self.proxy_threads_spinbox.setToolTip("每個SOCKS5代理同時運行的線程數 (預設3個)")
        self.proxy_threads_spinbox.setFixedWidth(60)  # 設置固定寬度
        advanced_layout.addWidget(proxy_threads_label)
        advanced_layout.addWidget(self.proxy_threads_spinbox)
        
        # 添加一些固定間距
        spacer = QWidget()
        spacer.setFixedWidth(30)
        advanced_layout.addWidget(spacer)
        
        # 添加使用代理的選項
        self.use_proxy_checkbox = QCheckBox("使用SOCKS5代理")
        self.use_proxy_checkbox.setChecked(True)
        advanced_layout.addWidget(self.use_proxy_checkbox)
        advanced_layout.addStretch(1)  # 添加彈性空間，使控件靠左
        
        # 下載按鈕
        button_layout = QHBoxLayout()
        download_button = QPushButton("新增下載")
        download_button.clicked.connect(self.add_download)
        button_layout.addStretch()
        button_layout.addWidget(download_button)
        
        # 下載列表
        self.task_table = QTableWidget(0, 8)
        self.task_table.setHorizontalHeaderLabels(["檔案名", "大小", "進度", "狀態", "即時速度", "平均速度", "剩餘時間", "總耗時"])
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.task_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.task_table.customContextMenuRequested.connect(self.show_context_menu)
        
        # 將所有元素添加到下載標籤頁佈局
        download_layout.addLayout(url_layout)
        download_layout.addLayout(dir_layout)
        download_layout.addLayout(file_layout)
        download_layout.addLayout(advanced_layout)
        download_layout.addLayout(button_layout)
        download_layout.addWidget(self.task_table)
        
        # === SOCKS5 代理管理標籤頁 ===
        socks_tab = QWidget()
        socks_layout = QVBoxLayout(socks_tab)
        
        # SOCKS5 伺服器列表
        self.socks_table = QTableWidget(0, 5)
        self.socks_table.setHorizontalHeaderLabels(["名稱", "主機", "埠", "狀態", "操作"])
        self.socks_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.socks_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        # 設置狀態列有更大的寬度以顯示詳細信息
        self.socks_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        # 調整操作列寬度
        self.socks_table.setColumnWidth(4, 80)
        self.socks_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.socks_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.socks_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.socks_table.customContextMenuRequested.connect(self.show_socks_context_menu)
        
        # SOCKS5 伺服器添加區域
        socks_form_layout = QHBoxLayout()
        
        # 伺服器名稱輸入
        socks_name_label = QLabel("名稱:")
        self.socks_name_input = QLineEdit()
        self.socks_name_input.setPlaceholderText("為此代理起個名字...")
        socks_form_layout.addWidget(socks_name_label)
        socks_form_layout.addWidget(self.socks_name_input)
        
        # 伺服器主機輸入
        socks_host_label = QLabel("主機:")
        self.socks_host_input = QLineEdit()
        self.socks_host_input.setPlaceholderText("127.0.0.1")
        socks_form_layout.addWidget(socks_host_label)
        socks_form_layout.addWidget(self.socks_host_input)
        
        # 伺服器埠輸入
        socks_port_label = QLabel("埠:")
        self.socks_port_input = QSpinBox()
        self.socks_port_input.setRange(1, 65535)
        self.socks_port_input.setValue(1080)
        socks_form_layout.addWidget(socks_port_label)
        socks_form_layout.addWidget(self.socks_port_input)
        
        # 添加按鈕
        socks_add_button = QPushButton("添加代理")
        socks_add_button.clicked.connect(self.add_socks_proxy)
        socks_form_layout.addWidget(socks_add_button)
        
        # 說明文字
        socks_info_label = QLabel("添加SOCKS5代理伺服器後，單個下載任務將同時使用所有可用的代理伺服器，每個線程使用不同的代理，提高下載速度和穩定性。")
        socks_info_label.setWordWrap(True)
        
        # 將所有元素添加到SOCKS5標籤頁佈局
        socks_layout.addLayout(socks_form_layout)
        socks_layout.addWidget(self.socks_table)
        socks_layout.addWidget(socks_info_label)
        
        # 將標籤頁添加到標籤頁小部件
        self.tab_widget.addTab(download_tab, "下載管理")
        self.tab_widget.addTab(socks_tab, "SOCKS5 代理")
        
        # 將標籤頁小部件添加到主佈局
        main_layout.addWidget(self.tab_widget)
        
        self.setCentralWidget(main_widget)
        
    def select_save_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "選擇保存目錄", self.dir_input.text())
        if dir_path:
            if self.download_manager.set_save_dir(dir_path):
                self.dir_input.setText(dir_path)
            else:
                QMessageBox.warning(self, "錯誤", "無法設置保存目錄，請確保目錄存在且有寫入權限")
                
    def add_download(self):
        """添加新的下載任務"""
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "錯誤", "請輸入下載連結！")
            return
            
        filename = self.file_input.text().strip() or None
        thread_count = 10  # 使用默認值10
        save_dir = self.dir_input.text().strip()
        use_proxy = self.use_proxy_checkbox.isChecked()
        
        # 獲取每個代理的線程數
        threads_per_proxy = self.proxy_threads_spinbox.value()
        
        try:
            # 檢查URL格式
            if not url.startswith(('http://', 'https://', 'ftp://')):
                QMessageBox.warning(self, "錯誤", "無效的URL格式，請確保URL以http://、https://或ftp://開頭")
                return
            
            # 顯示處理中的提示
            QApplication.setOverrideCursor(Qt.WaitCursor)
            
            # 檢查是否選擇使用代理但沒有可用代理
            if use_proxy and not self.download_manager.get_all_proxies():
                reply = QMessageBox.question(self, "無可用代理", 
                                          "目前沒有設置任何SOCKS5代理，是否繼續下載？",
                                          QMessageBox.Yes | QMessageBox.No,
                                          QMessageBox.No)
                if reply == QMessageBox.No:
                    QApplication.restoreOverrideCursor()
                    return
                use_proxy = False
            
            # 添加任務，使用新的參數
            task_id = self.download_manager.add_task(
                url, 
                filename, 
                thread_count, 
                save_dir, 
                use_proxy,
                chunks_per_part=10,  # 使用默認值10
                threads_per_proxy=threads_per_proxy
            )
            
            # 啟動任務
            if not self.download_manager.start_task(task_id):
                QMessageBox.warning(self, "錯誤", f"無法開始下載任務，URL: {url}")
                QApplication.restoreOverrideCursor()
                return
            
            # 清空輸入欄位
            self.url_input.clear()
            self.file_input.clear()
            
            # 更新任務列表
            self.add_task_to_table(task_id, self.download_manager.task_ids[task_id])
            
            QApplication.restoreOverrideCursor()
            
        except Exception as e:
            QApplication.restoreOverrideCursor()
            error_message = str(e)
            print(f"下載任務添加失敗: {error_message}")
            QMessageBox.critical(self, "錯誤", f"下載任務添加失敗: {error_message}")
            
    def add_task_to_table(self, task_id, task):
        row = self.task_table.rowCount()
        self.task_table.insertRow(row)
        
        # 存儲任務ID
        self.task_table.setItem(row, 0, QTableWidgetItem(task.filename))
        self.task_table.item(row, 0).setData(Qt.UserRole, task_id)
        
        # 進度條
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 100)
        progress_bar.setValue(0)
        self.task_table.setCellWidget(row, 2, progress_bar)
        
        # 設置其他列
        self.task_table.setItem(row, 1, QTableWidgetItem("計算中..."))
        self.task_table.setItem(row, 3, QTableWidgetItem(task.status))
        self.task_table.setItem(row, 4, QTableWidgetItem("0 B/s"))
        self.task_table.setItem(row, 5, QTableWidgetItem("0 B/s"))
        self.task_table.setItem(row, 6, QTableWidgetItem("計算中..."))
        self.task_table.setItem(row, 7, QTableWidgetItem("0秒"))  # 初始化總耗時為0秒
        
    def update_task_progress(self, task_data):
        # 確保 task_table 已經初始化
        if self.task_table is None:
            return
            
        task_id = task_data['id']
        progress = task_data['progress']
        
        # 檢查任務是否已顯示在表格中，如果不在則添加
        found = False
        for row in range(self.task_table.rowCount()):
            item = self.task_table.item(row, 0)
            if item and item.data(Qt.UserRole) == task_id:
                found = True
                break
        
        # 如果任務不在表格中並且任務存在於下載管理器中，則添加到表格
        if not found and task_id in self.download_manager.task_ids:
            task = self.download_manager.task_ids[task_id]
            print(f"檢測到新任務 (可能來自 HTTP 伺服器): {task.filename}，添加到 UI 表格")
            self.add_task_to_table(task_id, task)
            
        # 查找對應的行（可能是剛添加的）
        for row in range(self.task_table.rowCount()):
            item = self.task_table.item(row, 0)
            if item and item.data(Qt.UserRole) == task_id:
                # 更新大小
                if progress['total_size'] > 0:
                    size_text = f"{format_size(progress['downloaded_size'])}/{format_size(progress['total_size'])}"
                else:
                    size_text = format_size(progress['downloaded_size'])
                self.task_table.setItem(row, 1, QTableWidgetItem(size_text))
                
                # 更新進度條
                progress_bar = self.task_table.cellWidget(row, 2)
                progress_bar.setValue(int(progress['percentage']))
                
                # 更新狀態
                status = progress['status']
                self.task_table.setItem(row, 3, QTableWidgetItem(self.get_status_text(status)))
                
                # 更新速度
                if status in ['paused', 'error', 'completed', 'canceled']:
                    # 暫停、錯誤或完成狀態下顯示 0 速度
                    speed_text = "0 B/s"
                    avg_speed_text = "0 B/s"
                else:
                    speed_text = f"{format_size(progress['speed'])}/s"
                    avg_speed_text = f"{format_size(progress['average_speed'])}/s"
                self.task_table.setItem(row, 4, QTableWidgetItem(speed_text))
                self.task_table.setItem(row, 5, QTableWidgetItem(avg_speed_text))
                
                # 更新剩餘時間
                if status in ['paused', 'error', 'completed', 'canceled']:
                    # 暫停、錯誤或完成狀態下沒有剩餘時間
                    if status == 'completed':
                        time_text = "已完成"
                    elif status == 'paused':
                        time_text = "已暫停"
                    elif status == 'error':
                        time_text = "出錯"
                    else:
                        time_text = "--"
                elif progress['speed'] > 0 and progress['total_size'] > 0:
                    remaining_bytes = progress['total_size'] - progress['downloaded_size']
                    remaining_time = remaining_bytes / progress['speed']
                    time_text = format_time(remaining_time)
                else:
                    time_text = "計算中..."
                self.task_table.setItem(row, 6, QTableWidgetItem(time_text))
                
                # 設置字體顏色
                if status == 'completed':
                    self.task_table.item(row, 3).setForeground(Qt.green)
                elif status == 'error':
                    self.task_table.item(row, 3).setForeground(Qt.red)
                elif status == 'paused':
                    self.task_table.item(row, 3).setForeground(Qt.blue)
                
                # 更新總耗時
                total_time = progress['total_time']
                self.task_table.setItem(row, 7, QTableWidgetItem(format_time(total_time)))
                
                break
                
    def get_status_text(self, status):
        status_map = {
            'initialized': '初始化',
            'downloading': '下載中',
            'paused': '已暫停',
            'completed': '已完成',
            'error': '錯誤',
            'canceled': '已取消'
        }
        return status_map.get(status, status)
                
    def show_context_menu(self, position):
        row = self.task_table.rowAt(position.y())
        if row < 0:
            return
            
        item = self.task_table.item(row, 0)
        if not item:
            return
            
        task_id = item.data(Qt.UserRole)
        if not task_id:
            return
            
        task = self.download_manager.task_ids.get(task_id)
        if not task:
            return
            
        menu = QMenu(self)
        
        # 添加複製下載連結選項
        copy_url_action = menu.addAction("複製下載連結")
        copy_url_action.triggered.connect(lambda: self.copy_download_url(task.url))
        
        # 添加分隔線
        menu.addSeparator()
        
        # 根據任務狀態顯示不同的菜單項
        if task.status == 'downloading':
            pause_action = menu.addAction("暫停")
            pause_action.triggered.connect(lambda: self.pause_task(task_id))
        elif task.status == 'paused':
            resume_action = menu.addAction("恢復")
            resume_action.triggered.connect(lambda: self.resume_task(task_id))
            
        cancel_action = menu.addAction("刪除")
        cancel_action.triggered.connect(lambda: self.cancel_task(task_id))
        
        if task.status == 'completed':
            open_folder_action = menu.addAction("打開所在資料夾")
            open_folder_action.triggered.connect(lambda: self.open_folder(task.filepath))
            
        menu.exec_(self.task_table.mapToGlobal(position))
        
    def pause_task(self, task_id):
        if self.download_manager.pause_task(task_id):
            # 更新會自動透過監控線程完成
            pass
        else:
            QMessageBox.warning(self, "錯誤", "無法暫停下載任務")
            
    def resume_task(self, task_id):
        if self.download_manager.resume_task(task_id):
            # 更新會自動透過監控線程完成
            pass
        else:
            QMessageBox.warning(self, "錯誤", "無法恢復下載任務")
            
    def cancel_task(self, task_id):
        # 移除確認對話框，直接取消任務
        if self.download_manager.cancel_task(task_id):
            # 從表格中移除任務
            for row in range(self.task_table.rowCount()):
                item = self.task_table.item(row, 0)
                if item and item.data(Qt.UserRole) == task_id:
                    self.task_table.removeRow(row)
                    break
        else:
            QMessageBox.warning(self, "錯誤", "無法取消下載任務")
            
    def open_folder(self, filepath):
        import subprocess
        import platform
        
        folder_path = os.path.dirname(filepath)
        
        if platform.system() == "Windows":
            os.startfile(folder_path)
        elif platform.system() == "Darwin":  # macOS
            subprocess.call(["open", folder_path])
        else:  # Linux
            subprocess.call(["xdg-open", folder_path])
            
    def closeEvent(self, event):
        # 不再詢問用戶是否關閉，直接保存進度
        
        # 停止監控線程
        self.monitor_thread.stop()
        self.monitor_thread.wait()
        
        # 先嘗試優雅地取消所有測試線程
        for proxy_id, tester in list(self.proxy_testers.items()):
            print(f"嘗試取消代理 {proxy_id} 的測試...")
            tester.cancel()
            
        # 然後等待它們完成
        for proxy_id, tester in list(self.proxy_testers.items()):
            print(f"等待代理 {proxy_id} 的測試線程完成...")
            if not tester.wait(2000):  # 最多等待2秒
                print(f"代理 {proxy_id} 的測試線程無法在2秒內完成，將被強制終止")
                try:
                    # 斷開連接信號以避免在對象被銷毀後調用
                    tester.test_finished.disconnect()
                except Exception as e:
                    print(f"斷開信號連接時出錯: {e}")
        
        # 暫停所有仍在下載的任務，確保進度保存
        for task_id, task in self.download_manager.task_ids.items():
            if task.status == 'downloading':
                print(f"關閉應用程式時自動暫停下載任務: {task.filename}")
                self.download_manager.pause_task(task_id)
        
        # 保存配置文件
        self.download_manager.save_config()
                
        event.accept()

    def display_restored_tasks(self):
        """將恢復的未完成任務顯示到任務列表中"""
        print("添加恢復的任務到列表中...")
        tasks = self.download_manager.get_all_tasks()
        for task_info in tasks:
            task_id = task_info['id']
            task = self.download_manager.task_ids.get(task_id)
            if task:
                print(f"添加恢復的任務到列表: {task.filename}")
                self.add_task_to_table(task_id, task)
                # 如果任務狀態是暫停的，保持暫停狀態
                # 如果是下載中或初始化狀態的，則自動開始下載
                if task.status in ['downloading', 'initialized']:
                    print(f"自動開始恢復的任務: {task.filename}")
                    self.download_manager.start_task(task_id)

    def update_server_status(self, url=None, is_running=False):
        """更新 HTTP 伺服器狀態，但不再顯示URL和狀態"""
        # 這個方法保留但不再顯示狀態，以避免修改調用者的代碼結構
        pass

    def copy_server_url(self):
        """複製伺服器 URL 到剪貼簿的方法已不再需要"""
        pass

    def copy_download_url(self, url):
        """複製下載URL到剪貼板"""
        clipboard = QApplication.clipboard()
        clipboard.setText(url)
        
    def on_task_added(self, task_id, task):
        """HTTP伺服器通知新增了任務時的回調"""
        # 在 UI 線程中執行添加操作
        QApplication.instance().postEvent(self, QEvent(QEvent.User))

    def event(self, event):
        """處理事件，主要用於在應用激活時更新下載列表"""
        if event.type() == QEvent.WindowActivate:
            print("窗口激活，刷新任務列表")
            tasks = self.download_manager.get_all_tasks()
            for task in tasks:
                self.update_task_progress(task)
        elif event.type() == QEvent.User:
            # 刷新任務列表
            print("處理自定義事件：刷新任務列表")
            tasks = self.download_manager.get_all_tasks()
            for task_info in tasks:
                task_id = task_info['id']
                if task_id in self.download_manager.task_ids:
                    task = self.download_manager.task_ids[task_id]
                    # 檢查任務是否已在表格中
                    found = False
                    for row in range(self.task_table.rowCount()):
                        item = self.task_table.item(row, 0)
                        if item and item.data(Qt.UserRole) == task_id:
                            found = True
                            break
                    
                    # 如果任務不在表格中，添加它
                    if not found:
                        print(f"添加新任務到表格: ID={task_id}, 檔案名={task.filename}")
                        self.add_task_to_table(task_id, task)
            return True
            
        return super().event(event)
        
    # === SOCKS5 代理管理相關方法 ===
    
    def add_socks_proxy(self):
        """添加新的SOCKS5代理服務器"""
        name = self.socks_name_input.text().strip()
        host = self.socks_host_input.text().strip()
        port = self.socks_port_input.value()
        
        if not name:
            QMessageBox.warning(self, "錯誤", "請輸入代理名稱")
            return
            
        if not host:
            QMessageBox.warning(self, "錯誤", "請輸入代理主機地址")
            return
            
        # 添加到下載管理器
        proxy_id = self.download_manager.add_socks_proxy(name, host, port)
        if proxy_id:
            # 添加到表格
            self.add_proxy_to_table(proxy_id, {"name": name, "host": host, "port": port, "status": "未測試"})
            
            # 清空輸入框
            self.socks_name_input.clear()
            self.socks_host_input.clear()
            self.socks_port_input.setValue(1080)
        else:
            QMessageBox.warning(self, "錯誤", "添加代理失敗，可能存在同名代理")
            
    def add_proxy_to_table(self, proxy_id, proxy):
        """將代理添加到表格中"""
        row = self.socks_table.rowCount()
        self.socks_table.insertRow(row)
        
        # 存儲代理ID
        self.socks_table.setItem(row, 0, QTableWidgetItem(proxy["name"]))
        self.socks_table.item(row, 0).setData(Qt.UserRole, proxy_id)
        
        # 設置其他列
        self.socks_table.setItem(row, 1, QTableWidgetItem(proxy["host"]))
        self.socks_table.setItem(row, 2, QTableWidgetItem(str(proxy["port"])))
        self.socks_table.setItem(row, 3, QTableWidgetItem(proxy["status"]))
        
        # 添加測試按鈕
        test_button = QPushButton("測試")
        test_button.clicked.connect(lambda: self.test_socks_proxy(proxy_id))
        self.socks_table.setCellWidget(row, 4, test_button)
        
    def update_proxy_status(self, proxy_id, status):
        """更新代理狀態"""
        # 查找對應的行
        for row in range(self.socks_table.rowCount()):
            item = self.socks_table.item(row, 0)
            if item and item.data(Qt.UserRole) == proxy_id:
                status_item = QTableWidgetItem(status)
                
                # 根據狀態設置顏色
                if status.startswith("可用"):
                    status_item.setForeground(Qt.green)
                elif status.startswith("有限可用"):
                    # 有限可用使用黃色
                    status_item.setForeground(QColor(255, 165, 0))  # 橙色
                elif status.startswith("不可用"):
                    status_item.setForeground(Qt.red)
                elif status == "測試中...":
                    status_item.setForeground(Qt.blue)
                
                self.socks_table.setItem(row, 3, status_item)
                break
                
    def test_socks_proxy(self, proxy_id):
        """測試SOCKS5代理連接"""
        # 檢查是否已有測試線程在運行
        if proxy_id in self.proxy_testers and self.proxy_testers[proxy_id].isRunning():
            print(f"代理 {proxy_id} 測試已在進行中，忽略請求")
            return
            
        # 先標記為測試中狀態
        self.update_proxy_status(proxy_id, "測試中...")
        
        # 禁用測試按鈕，避免重複點擊
        for row in range(self.socks_table.rowCount()):
            item = self.socks_table.item(row, 0)
            if item and item.data(Qt.UserRole) == proxy_id:
                test_button = self.socks_table.cellWidget(row, 4)
                if test_button:
                    test_button.setEnabled(False)
                    test_button.setText("測試中...")
                break
        
        # 在單獨的線程中運行測試
        proxy_tester = ProxyTester(self.download_manager, proxy_id)
        proxy_tester.test_finished.connect(self.on_proxy_test_finished)
        
        # 保存測試線程的引用，避免被過早釋放
        self.proxy_testers[proxy_id] = proxy_tester
        proxy_tester.start()
    
    def on_proxy_test_finished(self, proxy_id):
        """代理測試完成的回調"""
        print(f"代理 {proxy_id} 測試完成，刷新UI顯示")
        # 直接從下載管理器獲取最新狀態
        self.refresh_proxy_status(proxy_id)
        
        # 從字典中移除測試線程的引用，允許線程正常結束
        if proxy_id in self.proxy_testers:
            # 確保線程完全結束
            self.proxy_testers[proxy_id].wait()
            # 移除線程引用
            self.proxy_testers.pop(proxy_id, None)
            print(f"代理 {proxy_id} 的測試線程已安全結束")
    
    def refresh_proxy_status(self, proxy_id):
        """從下載管理器刷新代理狀態"""
        # 獲取最新狀態
        if proxy_id in self.download_manager.socks_proxies:
            status = self.download_manager.socks_proxies[proxy_id]['status']
            print(f"從下載管理器獲取到代理 {proxy_id} 的最新狀態: {status}")
            
            # 更新UI顯示
            self.update_proxy_status(proxy_id, status)
            
            # 恢復測試按鈕
            for row in range(self.socks_table.rowCount()):
                item = self.socks_table.item(row, 0)
                if item and item.data(Qt.UserRole) == proxy_id:
                    test_button = self.socks_table.cellWidget(row, 4)
                    if test_button:
                        test_button.setEnabled(True)
                        test_button.setText("測試")
                        print(f"測試按鈕已恢復")
                    break
        else:
            print(f"代理 {proxy_id} 不存在於下載管理器中")

    def show_socks_context_menu(self, position):
        """顯示SOCKS5代理右鍵功能表"""
        menu = QMenu()
        
        # 獲取選中的行
        indexes = self.socks_table.selectedIndexes()
        if indexes:
            row = indexes[0].row()
            proxy_id = self.socks_table.item(row, 0).data(Qt.UserRole)
            
            # 添加功能表項
            test_action = menu.addAction("測試")
            delete_action = menu.addAction("刪除")
            
            # 顯示功能表
            action = menu.exec_(self.socks_table.viewport().mapToGlobal(position))
            
            # 處理功能表選擇
            if action == test_action:
                self.test_socks_proxy(proxy_id)
            elif action == delete_action:
                self.delete_socks_proxy(proxy_id)
                
    def delete_socks_proxy(self, proxy_id):
        """刪除SOCKS5代理"""
        # 詢問用戶是否確定要刪除
        reply = QMessageBox.question(self, "確認刪除", 
                                    "確定要刪除這個代理嗎？",
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            # 從下載管理器中刪除代理
            if self.download_manager.delete_socks_proxy(proxy_id):
                # 從表格中刪除代理
                for row in range(self.socks_table.rowCount()):
                    item = self.socks_table.item(row, 0)
                    if item and item.data(Qt.UserRole) == proxy_id:
                        self.socks_table.removeRow(row)
                        break
            else:
                QMessageBox.warning(self, "錯誤", "刪除代理失敗")
                
    def load_socks_proxies(self):
        """載入所有已保存的SOCKS5代理到表格"""
        # 清空表格
        self.socks_table.setRowCount(0)
        
        # 獲取所有代理
        proxies = self.download_manager.get_all_proxies()
        
        # 添加到表格
        for proxy_id, proxy in proxies.items():
            self.add_proxy_to_table(proxy_id, proxy)

# 主程序入口
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_()) 
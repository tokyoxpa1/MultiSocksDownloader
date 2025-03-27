#!/usr/bin/env python3
"""
HTTP 伺服器 - 接收來自 Chrome 擴展程式的下載請求
"""

import os
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import socket
import logging

# 配置日誌記錄
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('http_server')

# 任務添加事件回調函數
task_added_callbacks = []

class DownloadRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, download_manager, *args, **kwargs):
        self.download_manager = download_manager
        super().__init__(*args, **kwargs)
    
    def _set_response(self, status_code=200, content_type='application/json'):
        self.send_response(status_code)
        self.send_header('Content-type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')  # 允許來自任何域的請求
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_OPTIONS(self):
        """處理 CORS 預檢請求"""
        self._set_response()
        
    def do_GET(self):
        """處理 GET 請求"""
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        
        # 處理 /ping 請求 (連接檢查)
        if path == '/ping':
            self._set_response()
            response = {'status': 'ok', 'message': 'Server is running'}
            self.wfile.write(json.dumps(response).encode())
            return
            
        self._set_response(404)
        response = {'status': 'error', 'message': 'Not found'}
        self.wfile.write(json.dumps(response).encode())
    
    def do_POST(self):
        """處理 POST 請求"""
        content_length = int(self.headers.get('Content-Length', 0))
        
        if content_length > 0:
            # 讀取請求體
            post_data = self.rfile.read(content_length).decode('utf-8')
            logger.info(f"收到POST數據: {post_data}")
            
            try:
                # 解析 JSON 數據
                data = json.loads(post_data)
                url = data.get('url', '')
                
                if not url:
                    self._set_response(400)
                    self.wfile.write(json.dumps({'status': 'error', 'message': 'Missing URL'}).encode())
                    return
                    
                # 獲取可選參數
                filename = data.get('filename', None)
                # 確保檔名不是空字符串或全是空格
                if filename and filename.strip() == '':
                    filename = None
                
                # 記錄檔名資訊
                if filename:
                    logger.info(f"從Chrome擴展收到的檔名: {filename}")
                else:
                    logger.info(f"未從Chrome擴展收到檔名，將由下載器自動偵測")
                
                # 使用默認線程數10
                thread_count = 10
                
                # 使用默認分片數10
                chunks_per_part = 10
                
                # 獲取每個代理的線程數，默認為3
                threads_per_proxy = data.get('threads_per_proxy', 3)
                try:
                    threads_per_proxy = int(threads_per_proxy)
                    if threads_per_proxy < 1:
                        threads_per_proxy = 3
                except:
                    threads_per_proxy = 3
                
                logger.info(f"從HTTP請求獲取下載參數: URL={url}, 檔案名={filename or '(將自動偵測)'}, 線程數={thread_count}, 分片數={chunks_per_part}, 每代理線程數={threads_per_proxy}")
                
                # 添加下載任務 - 使用當前下載管理器的保存目錄
                task_id = self.download_manager.add_task(
                    url, 
                    filename, 
                    thread_count, 
                    self.download_manager.save_dir,
                    True,  # 使用代理
                    chunks_per_part,
                    threads_per_proxy
                )
                logger.info(f"HTTP 請求添加了任務 ID: {task_id}, URL: {url}, 檔案名: {filename or '(將自動偵測)'}")
                
                # 啟動任務
                if self.download_manager.start_task(task_id):
                    logger.info(f"成功添加下載任務: {url}")
                    # 檢查任務是否在下載管理器中
                    if task_id in self.download_manager.task_ids:
                        task = self.download_manager.task_ids[task_id]
                        final_filename = task.filename
                        logger.info(f"任務已成功添加到下載管理器，最終檔案名: {final_filename}")
                        
                        # 調用任務添加回調函數
                        for callback in task_added_callbacks:
                            try:
                                callback(task_id, task)
                            except Exception as e:
                                logger.error(f"調用任務添加回調函數時出錯: {str(e)}")
                    else:
                        logger.warning(f"任務 ID {task_id} 不在下載管理器中，可能沒有正確添加")
                    
                    self._set_response()
                    response = {
                        'status': 'success', 
                        'message': '下載任務已添加', 
                        'task_id': task_id,
                        'filename': task.filename if task_id in self.download_manager.task_ids else (filename if filename else '(自動偵測)')
                    }
                else:
                    logger.error(f"無法啟動下載任務: {url}")
                    self._set_response(500)
                    response = {'status': 'error', 'message': 'Failed to start download task'}
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析錯誤: {e}")
                self._set_response(400)
                response = {'status': 'error', 'message': f'Invalid JSON: {str(e)}'}
            except Exception as e:
                logger.error(f"處理請求時出錯: {e}")
                self._set_response(500)
                response = {'status': 'error', 'message': f'Server error: {str(e)}'}
        else:
            logger.warning("收到空的POST請求")
            self._set_response(400)
            response = {'status': 'error', 'message': 'Empty request'}
            
        self.wfile.write(json.dumps(response).encode())


def create_handler_class(download_manager):
    """創建一個包含下載管理器引用的處理程序類"""
    def handler(*args, **kwargs):
        return DownloadRequestHandler(download_manager, *args, **kwargs)
    return type('CustomHandler', (DownloadRequestHandler,), {'__init__': lambda self, *args, **kwargs: DownloadRequestHandler.__init__(self, download_manager, *args, **kwargs)})


class HttpServer:
    def __init__(self, download_manager, host='0.0.0.0', port=8765):
        self.download_manager = download_manager
        self.host = host  # 使用 0.0.0.0 監聽所有網卡
        self.port = port
        self.server = None
        self.thread = None
        self.is_running = False
    
    def add_task_added_callback(self, callback):
        """添加任務添加回調函數"""
        if callback not in task_added_callbacks:
            task_added_callbacks.append(callback)
            logger.info(f"已添加任務添加回調函數")
        
    def remove_task_added_callback(self, callback):
        """移除任務添加回調函數"""
        if callback in task_added_callbacks:
            task_added_callbacks.remove(callback)
            logger.info(f"已移除任務添加回調函數")
    
    def start(self):
        """啟動 HTTP 伺服器"""
        if self.is_running:
            logger.warning("HTTP 伺服器已在運行中")
            return False
        
        try:
            # 創建伺服器
            handler_class = create_handler_class(self.download_manager)
            self.server = HTTPServer((self.host, self.port), handler_class)
            
            # 啟動伺服器線程
            self.thread = threading.Thread(target=self.server.serve_forever)
            self.thread.daemon = True  # 設置為守護線程，主程序結束時自動退出
            self.thread.start()
            
            self.is_running = True
            logger.info(f"HTTP 伺服器啟動成功，監聽於 {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"HTTP 伺服器啟動失敗: {str(e)}")
            return False
    
    def get_local_ip(self):
        """獲取本機 IP 地址"""
        try:
            # 創建臨時 socket 連接來獲取本機 IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # 連接到公共 DNS 伺服器（不需要真正發送數據）
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception as e:
            logger.error(f"獲取本機 IP 出錯: {str(e)}")
            return "localhost"  # 如果無法獲取，返回 localhost
    
    def stop(self):
        """停止 HTTP 伺服器"""
        if not self.is_running:
            logger.warning("HTTP 伺服器未運行")
            return
        
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            self.is_running = False
            logger.info(f"HTTP 伺服器已停止")
        except Exception as e:
            logger.error(f"HTTP 伺服器停止失敗: {str(e)}")
    
    def get_server_url(self):
        """獲取伺服器 URL"""
        if not self.is_running:
            return None
        
        # 返回所有可用的 URL
        local_ip = self.get_local_ip()
        urls = {
            "localhost": f"http://localhost:{self.port}",
            "local_ip": f"http://{local_ip}:{self.port}"
        }
        return urls


# 測試代碼
if __name__ == "__main__":
    # 模擬下載管理器
    class MockDownloadManager:
        def add_task(self, url, filename=None, thread_count=5):
            print(f"添加下載任務: {url}, 文件名: {filename}, 線程數: {thread_count}")
            return "task-1234"
        
        def start_task(self, task_id):
            print(f"啟動任務: {task_id}")
            return True
    
    # 創建並啟動伺服器
    mock_dm = MockDownloadManager()
    server = HttpServer(mock_dm)
    if server.start():
        print(f"伺服器已啟動，URL: {server.get_server_url()}")
        print("按 Ctrl+C 停止伺服器...")
        try:
            # 保持主線程運行
            while True:
                pass
        except KeyboardInterrupt:
            server.stop()
            print("伺服器已停止") 
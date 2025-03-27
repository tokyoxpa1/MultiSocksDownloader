import os
import time
import json
import threading
import requests
from urllib.parse import urlparse, unquote, parse_qs
import collections
import re

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

class DownloadTask:
    def __init__(self, url, save_dir, filename=None, thread_count=10, proxies=None, chunks_per_part=100, threads_per_proxy=3):
        """初始化下載任務
        
        Args:
            url: 下載檔案的URL
            save_dir: 保存檔案的目錄
            filename: 保存的檔案名，如果為None則自動從URL中提取
            thread_count: 用於下載的線程數量
            proxies: SOCKS5代理配置列表，格式為 [{'host': '127.0.0.1', 'port': 1080}, ...]
            chunks_per_part: 默認分片數量
            threads_per_proxy: 每個代理同時使用的線程數
        """
        self.url = url
        self.save_dir = save_dir
        
        # 根據代理數量動態調整線程數
        self.proxies = proxies or []
        if self.proxies:
            # 如果有代理，根據代理數量和每個代理的線程數來設置總線程數
            self.thread_count = min(len(self.proxies) * threads_per_proxy, 32)  # 仍然限制最大為32
        else:
            # 沒有代理時使用提供的線程數
            self.thread_count = max(1, min(thread_count, 32))  # 限制線程數在1-32之間
            
        # 保存每個代理的線程數設置和分片數設置
        self.threads_per_proxy = threads_per_proxy
        self.chunks_per_part = chunks_per_part
        
        # 初始設置臨時檔案名，後續在準備下載時可能會更新
        self.filename = filename
        
        # 從URL中提取檔案名（初始嘗試）
        if self.filename is None:
            parsed_url = urlparse(url)
            path = unquote(parsed_url.path)
            self.filename = os.path.basename(path)
            if not self.filename:
                self.filename = 'download_file'
        
        # 設置檔案路徑
        self.filepath = os.path.join(save_dir, self.filename)
        self.temp_filepath = f"{self.filepath}.downloading"
        self.progress_filepath = f"{self.filepath}.progress"
        
        # 任務狀態
        self.total_size = 0
        self.downloaded_size = 0
        self.status = 'initialized'  # initialized, downloading, paused, completed, error
        self.error_message = ''
        self.start_time = None
        self.end_time = None
        self.threads = []
        self.stop_event = threading.Event()
        self.progress_lock = threading.Lock()
        self.parts = []  # 所有分片的列表
        self.parts_pool = None  # 待分配的分片池
        self.parts_pool_lock = threading.Lock()  # 分片池的鎖
        self.resumed_size = 0  # 記錄恢復下載時的已下載大小，用於正確計算速度
        
        # 記錄總實際下載時間相關的變量
        self.total_active_time = 0  # 累計的實際下載時間
        self.pause_time = None  # 上次暫停時間
        self.last_active_start = None  # 上次開始活動的時間
        
        # 切換到單線程下載模式的標誌
        self.switched_to_single_thread = False
        
        # 用於計算短期下載速度的滑動窗口
        self.speed_window_size = 15  # 增加滑動窗口大小，從10增加到15，使速度計算更平滑
        self.speed_data = collections.deque(maxlen=self.speed_window_size)
        self.last_speed_update = time.time()
        self.last_downloaded_size = 0
        self.min_speed_update_interval = 0.3  # 減少速度更新間隔，從0.5減少到0.3，使速度顯示更及時
        self.last_reported_speed = 0  # 上次報告的速度，用於平滑顯示
        
        # 添加完成回調函數列表
        self.completion_callbacks = []
        
        # 支持特殊HTTP頭的標誌
        self.supports_range = False
        
        # 定義最佳的緩衝區大小（提高從16KB到64KB）
        self.chunk_size = 65536  # 64KB 的緩衝區大小
        
        # 添加切換鎖
        self.switching_lock = threading.Lock()
    
    def get_filename_from_content_disposition(self, response_headers):
        """從 Content-Disposition 響應頭中提取檔案名稱
        
        Args:
            response_headers: HTTP 響應頭字典
            
        Returns:
            str: 提取的檔案名，如果沒有找到則返回 None
        """
        if 'content-disposition' not in response_headers:
            return self.try_extract_filename_from_url()
            
        content_disposition = response_headers['content-disposition']
        print(f"Content-Disposition: {content_disposition}")
        
        # 方法一：直接尋找 filename=
        import re
        
        # 先查找 filename="xxx.yyy" 格式的檔名
        filename_match = re.search(r'filename="([^"]+)"', content_disposition)
        if filename_match:
            filename = filename_match.group(1)
            print(f"從 Content-Disposition 提取到檔案名 (雙引號): {filename}")
            return filename
            
        # 查找 filename=xxx.yyy 格式的檔名
        filename_match = re.search(r'filename=([^;,\s]+)', content_disposition)
        if filename_match:
            filename = filename_match.group(1)
            print(f"從 Content-Disposition 提取到檔案名 (無引號): {filename}")
            return filename
            
        # 查找 filename*=UTF-8''xxx.yyy 格式的檔名 (RFC 5987)
        filename_match = re.search(r"filename\*=UTF-8''([^;,\s]+)", content_disposition)
        if filename_match:
            from urllib.parse import unquote
            filename = unquote(filename_match.group(1))
            print(f"從 Content-Disposition 提取到檔案名 (UTF-8編碼): {filename}")
            return filename
            
        # 如果都沒找到，嘗試從URL提取
        return self.try_extract_filename_from_url()
        
    def try_extract_filename_from_url(self):
        """嘗試從URL中提取檔案名"""
        from urllib.parse import urlparse, unquote, parse_qs
        import re
        
        parsed_url = urlparse(self.url)
        path = unquote(parsed_url.path)
        
        # 先嘗試從路徑中提取基本檔名
        base_filename = os.path.basename(path)
        
        # 檢查是否為HuggingFace CDN URL（它們通常包含一個很長的參數）
        if 'hf.co' in parsed_url.netloc:
            print("檢測到HuggingFace CDN URL，嘗試從請求參數提取檔名")
            query_params = parse_qs(parsed_url.query)
            
            # HuggingFace通常會在response-content-disposition參數中包含檔名
            if 'response-content-disposition' in query_params:
                disposition = query_params['response-content-disposition'][0]
                print(f"解析response-content-disposition參數: {disposition}")
                
                # 優先尋找普通的filename="xxx.yyy"格式 (通常包含更友好的檔名)
                filename_match = re.search(r'filename="([^"]+)"', disposition)
                if filename_match:
                    filename = filename_match.group(1)
                    print(f"從URL參數中提取到檔案名: {filename}")
                    return filename
                    
                # 如果沒找到普通格式，再尋找filename*=UTF-8''xxx.yyy格式
                filename_match = re.search(r"filename\*=UTF-8''([^;,\s]+)", disposition)
                if filename_match:
                    filename = unquote(filename_match.group(1))
                    print(f"從URL參數中提取到UTF-8編碼檔案名: {filename}")
                    return filename
        
        # 如果有擴展名，並且看起來是個有效的檔名，則使用它
        if '.' in base_filename and len(base_filename) < 100:
            print(f"從URL路徑中提取到檔案名: {base_filename}")
            return base_filename
            
        # 如果基本檔名看起來像是一個ID或哈希值，則嘗試從URL參數中提取
        if len(base_filename) > 30 or base_filename.isalnum():
            query_params = parse_qs(parsed_url.query)
            
            # 檢查常見的檔名參數
            filename_params = ['filename', 'name', 'file', 'title', 'download']
            for param in filename_params:
                if param in query_params:
                    candidate = query_params[param][0]
                    if '.' in candidate:
                        print(f"從URL參數 '{param}' 中提取到檔案名: {candidate}")
                        return candidate
        
        # 如果都無法提取有效檔名，則返回原始basename
        print(f"無法從URL提取有效檔名，使用默認basename: {base_filename}")
        return base_filename
    
    def update_speed_data(self):
        """更新短期下載速度數據"""
        current_time = time.time()
        current_size = self.downloaded_size
        
        # 初始化
        if self.last_speed_update is None:
            self.last_speed_update = current_time
            self.last_downloaded_size = current_size
            return 0
            
        # 計算時間間隔和下載量
        time_diff = current_time - self.last_speed_update
        
        # 如果時間間隔太小，不更新速度數據
        if time_diff < self.min_speed_update_interval:
            return 0
            
        size_diff = current_size - self.last_downloaded_size
        
        # 防止除零錯誤和太小的時間間隔
        if time_diff < 0.1:
            return 0
            
        # 計算這個間隔的速度
        speed = size_diff / time_diff
        
        # 添加到滑動窗口
        self.speed_data.append((time_diff, speed))
        
        # 更新最後的數據
        self.last_speed_update = current_time
        self.last_downloaded_size = current_size
        
        return speed
        
    def get_current_speed(self):
        """獲取短期平均下載速度"""
        self.update_speed_data()
        
        if not self.speed_data:
            return 0
            
        # 平滑處理 - 使用簡單平均而不是加權平均，減少波動
        total_time = 0
        total_weighted_speed = 0
        
        for time_diff, speed in self.speed_data:
            total_time += time_diff
            total_weighted_speed += speed * time_diff  # 按時間間隔加權
            
        if total_time == 0:
            return 0
            
        return total_weighted_speed / total_time
    
    def get_average_speed(self):
        """獲取基於總耗時的平均下載速度"""
        # 計算總活動時間
        total_time = 0
        if self.status == 'completed' and self.end_time and self.start_time:
            total_time = self.end_time - self.start_time
        elif self.start_time:
            # 對於未完成的任務，計算實時的總耗時
            if self.status == 'downloading' and self.last_active_start:
                # 正在下載中的任務，累計之前的活動時間和當前的活動時間
                current_active_duration = time.time() - self.last_active_start
                total_time = self.total_active_time + current_active_duration
            else:
                # 暫停的任務，只使用累計的活動時間
                total_time = self.total_active_time
                
        # 防止除以零
        if total_time < 0.1:
            return 0
            
        # 使用實際下載的數據量除以總耗時
        return self.downloaded_size / total_time
    
    def get_progress(self):
        """獲取下載進度和速度
        
        Returns:
            dict: 包含進度信息的字典
        """
        if self.total_size == 0:
            percentage = 0
        else:
            percentage = (self.downloaded_size / self.total_size) * 100
            
        elapsed_time = 0
        if self.start_time:
            if self.end_time:
                elapsed_time = self.end_time - self.start_time
            else:
                elapsed_time = time.time() - self.start_time
        
        # 當任務暫停或出錯時，速度應為0
        speed = 0
        if self.status == 'downloading':
            if elapsed_time > 0:
                # 首先獲取基於總耗時的平均速度
                average_speed = self.get_average_speed()
                
                # 獲取短期平均下載速度
                current_speed = self.get_current_speed()
                
                # 如果短期速度為0或波動異常（可能是剛恢復下載）
                if current_speed == 0 or current_speed > self.total_size / 10:  # 避免速度顯示異常高值
                    # 使用基於總耗時的平均速度
                    speed = average_speed
                else:
                    # 綜合考慮短期速度和平均速度，使顯示更穩定
                    # 短期速度權重0.7，平均速度權重0.3
                    speed = current_speed * 0.7 + average_speed * 0.3
                        
                # 限制速度波動範圍，避免顯示不穩定
                if hasattr(self, 'last_reported_speed') and self.last_reported_speed > 0:
                    # 限制相鄰兩次速度變化不超過20%
                    max_change_ratio = 0.2
                    if speed > self.last_reported_speed * (1 + max_change_ratio):
                        speed = self.last_reported_speed * (1 + max_change_ratio)
                    elif speed < self.last_reported_speed * (1 - max_change_ratio):
                        speed = self.last_reported_speed * (1 - max_change_ratio)
                
                # 存儲本次報告的速度，用於下次比較
                self.last_reported_speed = speed
        
        # 計算總耗時 - 對於已完成的任務，使用end_time，否則使用當前時間
        total_time = 0
        if self.status == 'completed' and self.end_time and self.start_time:
            total_time = self.end_time - self.start_time
        elif self.start_time:
            # 對於未完成的任務，計算實時的總耗時
            if self.status == 'downloading' and self.last_active_start:
                # 正在下載中的任務，累計之前的活動時間和當前的活動時間
                current_active_duration = time.time() - self.last_active_start
                total_time = self.total_active_time + current_active_duration
            else:
                # 暫停的任務，只使用累計的活動時間
                total_time = self.total_active_time
        
        # 計算平均速度
        average_speed = 0
        if total_time > 0:
            average_speed = self.downloaded_size / total_time
        
        return {
            'total_size': self.total_size,
            'downloaded_size': self.downloaded_size,
            'percentage': percentage,
            'speed': speed,
            'average_speed': average_speed,  # 添加平均速度
            'status': self.status,
            'error_message': self.error_message,
            'elapsed_time': elapsed_time,
            'thread_count': self.thread_count,
            'total_time': total_time  # 總耗時字段
        }
    
    def save_progress(self):
        """保存下載進度到檔案，用於恢復下載"""
        progress_data = {
            'url': self.url,
            'total_size': self.total_size,
            'downloaded_size': self.downloaded_size,
            'parts': self.parts,
            'status': self.status,
            'save_dir': self.save_dir,  # 添加保存目錄路徑
            'filename': self.filename,  # 保存當前的檔案名稱
            'proxies': self.proxies,    # 保存代理列表
            'thread_count': self.thread_count,  # 保存線程數量
            'switched_to_single_thread': self.switched_to_single_thread,  # 保存單線程模式標記
            'total_active_time': self.total_active_time  # 保存累計下載時間
        }
        
        with open(self.progress_filepath, 'w') as f:
            json.dump(progress_data, f)
    
    def load_progress(self):
        """從檔案中載入下載進度"""
        if not os.path.exists(self.progress_filepath):
            print(f"進度檔案不存在: {self.progress_filepath}")
            return False
        
        try:
            print(f"載入進度檔案: {self.progress_filepath}")
            with open(self.progress_filepath, 'r') as f:
                progress_data = json.load(f)
                
            # 檢查必要的欄位
            required_fields = ['url', 'total_size', 'downloaded_size', 'status']
            for field in required_fields:
                if field not in progress_data:
                    print(f"進度檔案缺少必要欄位: {field}")
                    return False
                    
            # 檢查URL是否匹配
            if progress_data['url'] != self.url:
                print(f"URL不匹配: 檔案中為 {progress_data['url']}, 當前為 {self.url}")
                return False
                
            self.url = progress_data['url']
            self.total_size = progress_data['total_size']
            
            # 如果進度檔案中有代理信息，載入它
            if 'proxies' in progress_data:
                self.proxies = progress_data['proxies']
            
            # 載入線程數量
            if 'thread_count' in progress_data:
                self.thread_count = progress_data['thread_count']
                
            # 載入單線程模式標記
            if 'switched_to_single_thread' in progress_data:
                self.switched_to_single_thread = progress_data['switched_to_single_thread']
                if self.switched_to_single_thread:
                    print("此任務之前已切換到單線程模式")
            
            # 載入累計下載時間
            if 'total_active_time' in progress_data:
                self.total_active_time = progress_data['total_active_time']
                print(f"載入累計下載時間: {self.total_active_time:.1f}秒")
            else:
                self.total_active_time = 0
            
            # 重要：計算實際下載大小
            if 'parts' in progress_data and progress_data['parts']:
                # 對於多線程下載，從各部分的當前位置計算實際下載大小
                actual_downloaded = 0
                for part in progress_data['parts']:
                    if part['current'] > part['start']:
                        actual_downloaded += (part['current'] - part['start'])
                
                # 檢查下載大小是否異常（大於總大小）
                if actual_downloaded > self.total_size:
                    print(f"警告：下載大小異常 ({actual_downloaded} > {self.total_size})，修正為總大小")
                    actual_downloaded = self.total_size
                
                print(f"從部分下載計算的實際下載大小: {actual_downloaded}")
                self.downloaded_size = actual_downloaded
            else:
                # 對於單線程下載，使用保存的下載大小
                self.downloaded_size = min(progress_data['downloaded_size'], self.total_size)
            
            self.status = progress_data['status']
            
            # 如果進度檔案包含檔案名稱，並且與當前不同，則更新檔案名稱
            if 'filename' in progress_data and progress_data['filename'] != self.filename:
                old_filename = self.filename
                self.filename = progress_data['filename']
                print(f"從進度檔案中更新檔案名稱: {old_filename} -> {self.filename}")
                
                # 更新檔案路徑
                self.filepath = os.path.join(self.save_dir, self.filename)
                self.temp_filepath = f"{self.filepath}.downloading"
                
                # 不更新 progress_filepath，因為我們正在從這個文件讀取
                # 但在完成後需要將新的進度保存到正確的位置
            
            # 如果進度檔案包含保存目錄信息，檢查是否與當前目錄匹配
            if 'save_dir' in progress_data:
                saved_dir = progress_data['save_dir']
                if saved_dir != self.save_dir:
                    print(f"保存目錄不匹配，進度檔案中為: {saved_dir}, 當前為: {self.save_dir}")
                    # 這裡我們保留使用當前的 save_dir，因為路徑已經由 DownloadManager 指定了
                    # 但我們需要更新相關的文件路徑
                    
                    # 檢查原始路徑下的文件是否存在
                    old_temp_filepath = os.path.join(saved_dir, os.path.basename(self.temp_filepath))
                    if os.path.exists(old_temp_filepath) and saved_dir != self.save_dir:
                        print(f"臨時文件存在於原始目錄: {old_temp_filepath}")
                        print(f"但任務現在指向新目錄: {self.temp_filepath}")
                        print(f"保持使用原始目錄中的文件")
                        
                        # 使用原始路徑中的文件
                        self.save_dir = saved_dir
                        self.filepath = os.path.join(saved_dir, self.filename)
                        self.temp_filepath = f"{self.filepath}.downloading"
                        self.progress_filepath = f"{self.filepath}.progress"
            
            # 確保狀態合理
            if self.status == 'completed':
                print("任務已完成，無需恢復")
                return False
                
            if self.status == 'error':
                # 修改狀態為暫停，以便用戶可以重試
                print("任務之前出錯，設為暫停狀態以便重試")
                self.status = 'paused'
                
            if self.status not in ['initialized', 'downloading', 'paused']:
                print(f"無效的任務狀態: {self.status}，設為暫停狀態")
                self.status = 'paused'
                
            # 如果是多線程下載，載入分段信息
            if 'parts' in progress_data and progress_data['parts']:
                self.parts = progress_data['parts']
                print(f"載入 {len(self.parts)} 個下載分段")
                
                # 檢查臨時檔案是否存在
                if not os.path.exists(self.temp_filepath):
                    print(f"臨時檔案不存在: {self.temp_filepath}")
                    return False
                    
                # 檢查檔案大小是否正確
                if self.total_size > 0:
                    temp_size = os.path.getsize(self.temp_filepath)
                    if temp_size < self.total_size:
                        print(f"臨時檔案大小不正確: {temp_size}，應為 {self.total_size}")
                        # 嘗試修復臨時檔案大小
                        try:
                            with open(self.temp_filepath, 'ab') as f:
                                remaining = self.total_size - temp_size
                                if remaining > 0:
                                    f.seek(self.total_size - 1)
                                    f.write(b'\0')
                        except IOError as e:
                            print(f"無法修復臨時檔案: {e}")
                            return False
                            
            print(f"成功載入下載進度: {self.downloaded_size}/{self.total_size} bytes ({self.status})")
            return True
            
        except json.JSONDecodeError as e:
            print(f"解析進度檔案時出錯: {e}")
            return False
        except Exception as e:
            print(f"載入進度檔案時出錯: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def prepare(self):
        """準備下載任務
        
        返回:
            bool: 準備是否成功
        """
        if not os.path.exists(self.save_dir):
            try:
                os.makedirs(self.save_dir)
            except Exception as e:
                self.status = 'error'
                self.error_message = f"無法創建保存目錄: {e}"
                return False
                
        # 檢查文件是否已存在
        if os.path.exists(self.filepath):
            print(f"檔案已存在: {self.filepath}")
            self.status = 'completed'
            self.end_time = time.time()  # 設置一個假的結束時間
            # 獲取文件大小
            self.total_size = os.path.getsize(self.filepath)
            self.downloaded_size = self.total_size
            return True
        
        # 檢查進度檔案是否存在，如果存在則嘗試恢復進度
        if os.path.exists(self.progress_filepath):
            print(f"找到進度檔案: {self.progress_filepath}，嘗試恢復進度")
            if self.load_progress():
                print("成功恢復下載進度")
                # 載入進度後更新了文件名，重新檢查一次最終文件是否存在
                if os.path.exists(self.filepath):
                    print(f"檔案已存在（更新檔名後檢測）: {self.filepath}")
                    self.status = 'completed'
                    self.end_time = time.time()
                    return True
                return True  # 成功載入進度
            else:
                print("無法恢復下載進度，重新開始下載")
                # 刪除舊的進度檔案
                try:
                    os.remove(self.progress_filepath)
                except:
                    pass
        
        try:
            # 嘗試獲取檔案信息（檔案大小、支持的範圍請求等）
            # 使用代理列表嘗試請求
            if self.proxies:
                print(f"將使用 {len(self.proxies)} 個代理輪流獲取檔案信息")
                
                for proxy in self.proxies:
                    try:
                        proxy_url = f"socks5://{proxy['host']}:{proxy['port']}"
                        proxies = {
                            'http': proxy_url,
                            'https': proxy_url
                        }
                        print(f"嘗試使用代理 {proxy_url} 獲取檔案信息")
                        
                        # 使用帶代理的請求
                        session = requests.Session()
                        session.proxies.update(proxies)
                        
                        # 發送HEAD請求獲取檔案信息
                        response = session.head(self.url, timeout=30, allow_redirects=True)
                        
                        # 檢查響應是否成功
                        if response.status_code in [200, 206]:
                            print(f"使用代理 {proxy_url} 成功獲取檔案信息")
                            
                            # 檢查是否支持範圍請求
                            self.supports_range = ('accept-ranges' in response.headers and 
                                                response.headers['accept-ranges'] == 'bytes')
                            
                            if not self.supports_range and response.status_code == 206:
                                # 雖然沒有 accept-ranges 頭，但回應了206
                                self.supports_range = True
                                print("伺服器支持範圍請求 (206狀態碼)")
                            
                            # 獲取檔案大小
                            if 'content-length' in response.headers:
                                self.total_size = int(response.headers['content-length'])
                                print(f"檔案大小: {format_size(self.total_size)}")
                            else:
                                print("警告: 無法獲取檔案大小")
                                
                            # 獲取檔案名（如果尚未指定）
                            if self.filename == 'download_file' or self.filename == '':
                                filename_from_header = self.get_filename_from_content_disposition(response.headers)
                                if filename_from_header:
                                    self.filename = filename_from_header
                                    self.filepath = os.path.join(self.save_dir, self.filename)
                                    self.temp_filepath = f"{self.filepath}.downloading"
                                    self.progress_filepath = f"{self.filepath}.progress"
                                # 如果從頭獲取失敗，但URL是HuggingFace的，則直接從URL參數提取
                                elif 'hf.co' in self.url:
                                    print("從HTTP頭獲取檔名失敗，直接從HuggingFace URL提取")
                                    filename_from_url = self.try_extract_filename_from_url()
                                    if filename_from_url and filename_from_url != 'download_file' and filename_from_url != '':
                                        self.filename = filename_from_url
                                        self.filepath = os.path.join(self.save_dir, self.filename)
                                        self.temp_filepath = f"{self.filepath}.downloading"
                                        self.progress_filepath = f"{self.filepath}.progress"
                                        print(f"從URL成功提取檔名: {self.filename}")
                            
                            # 如果支持範圍請求，判斷是否需要多線程
                            if self.supports_range:
                                print("伺服器支持範圍請求，將使用多線程下載")
                                
                                # 如果文件太小（例如小於 1MB），則不使用多線程
                                if self.total_size < 1024 * 1024:
                                    print(f"檔案太小 ({format_size(self.total_size)})，使用單線程下載")
                                    self.thread_count = 1
                                    self.parts = []
                                else:
                                    # 動態調整分片大小，根據檔案大小調整每個分片的大小
                                    self._adjust_chunk_size()
                                    print(f"使用 {self.thread_count} 線程下載，每個線程處理 {self.chunks_per_part} 個分片")
                                    
                                    # 計算每個線程的下載範圍
                                    if not self.parts:  # 只有在沒有現有進度時才重新分片
                                        self._init_parts()
                            else:
                                # 不支持範圍請求，使用單線程
                                print("伺服器不支持範圍請求，使用單線程下載")
                                self.thread_count = 1
                                self.parts = []
                            
                            # 使用第一個成功的代理就跳出循環
                            break
                            
                        else:
                            print(f"使用代理 {proxy_url} 獲取檔案信息失敗，狀態碼: {response.status_code}")
                            
                    except Exception as proxy_error:
                        print(f"使用代理 {proxy_url} 請求頭信息失敗: {proxy_error}")
                
                # 如果嘗試了所有代理後仍然無法獲取文件信息，嘗試不使用代理
                if self.total_size == 0:
                    print("所有代理都無法獲取檔案信息，嘗試不使用代理")
                    
                    try:
                        # 不使用代理的請求
                        response = requests.head(self.url, timeout=30, allow_redirects=True)
                        
                        # 檢查響應是否成功
                        if response.status_code in [200, 206]:
                            print("成功獲取檔案信息（無代理）")
                            
                            # 檢查是否支持範圍請求
                            self.supports_range = ('accept-ranges' in response.headers and 
                                                response.headers['accept-ranges'] == 'bytes')
                            
                            # 獲取檔案大小
                            if 'content-length' in response.headers:
                                self.total_size = int(response.headers['content-length'])
                                print(f"檔案大小: {format_size(self.total_size)}")
                                
                            # 獲取檔案名（如果尚未指定）
                            if self.filename == 'download_file' or self.filename == '':
                                filename_from_header = self.get_filename_from_content_disposition(response.headers)
                                if filename_from_header:
                                    self.filename = filename_from_header
                                    self.filepath = os.path.join(self.save_dir, self.filename)
                                    self.temp_filepath = f"{self.filepath}.downloading"
                                    self.progress_filepath = f"{self.filepath}.progress"
                                # 同樣，如果從頭獲取失敗，嘗試從URL直接提取
                                elif 'hf.co' in self.url:
                                    print("從HTTP頭獲取檔名失敗，嘗試從HuggingFace URL提取")
                                    filename_from_url = self.try_extract_filename_from_url()
                                    if filename_from_url and filename_from_url != 'download_file' and filename_from_url != '':
                                        self.filename = filename_from_url
                                        self.filepath = os.path.join(self.save_dir, self.filename)
                                        self.temp_filepath = f"{self.filepath}.downloading"
                                        self.progress_filepath = f"{self.filepath}.progress"
                    except Exception as no_proxy_error:
                        print(f"無代理模式獲取檔案信息失敗: {no_proxy_error}")
            else:
                # 直接不使用代理
                try:
                    response = requests.head(self.url, timeout=30, allow_redirects=True)
                    
                    # 檢查響應是否成功
                    if response.status_code in [200, 206]:
                        print("成功獲取檔案信息")
                        
                        # 檢查是否支持範圍請求
                        self.supports_range = ('accept-ranges' in response.headers and 
                                            response.headers['accept-ranges'] == 'bytes')
                        
                        if not self.supports_range and response.status_code == 206:
                            self.supports_range = True
                            print("伺服器支持範圍請求 (206狀態碼)")
                        
                        # 獲取檔案大小
                        if 'content-length' in response.headers:
                            self.total_size = int(response.headers['content-length'])
                            print(f"檔案大小: {format_size(self.total_size)}")
                            
                        # 獲取檔案名（如果尚未指定）
                        if self.filename == 'download_file' or self.filename == '':
                            filename_from_header = self.get_filename_from_content_disposition(response.headers)
                            if filename_from_header:
                                self.filename = filename_from_header
                                self.filepath = os.path.join(self.save_dir, self.filename)
                                self.temp_filepath = f"{self.filepath}.downloading"
                                self.progress_filepath = f"{self.filepath}.progress"
                            # 同樣嘗試從URL直接提取
                            elif 'hf.co' in self.url:
                                print("從HTTP頭獲取檔名失敗，嘗試從HuggingFace URL提取")
                                filename_from_url = self.try_extract_filename_from_url()
                                if filename_from_url and filename_from_url != 'download_file' and filename_from_url != '':
                                    self.filename = filename_from_url
                                    self.filepath = os.path.join(self.save_dir, self.filename)
                                    self.temp_filepath = f"{self.filepath}.downloading"
                                    self.progress_filepath = f"{self.filepath}.progress"
                except Exception as e:
                    print(f"獲取檔案信息失敗: {e}")
                
            # 如果嘗試了所有方法仍然無法獲取文件信息，假設單線程下載
            if self.total_size == 0:
                print("無法獲取檔案信息，將使用單線程下載嘗試")
                self.thread_count = 1
                self.parts = []
            
            # 如果是恢復下載但進度檔案不存在，重新初始化分片
            if self.status == 'paused' and (not os.path.exists(self.progress_filepath) or not self.parts):
                if self.supports_range and self.total_size > 1024 * 1024:
                    self._adjust_chunk_size()
                    self._init_parts()
                else:
                    self.thread_count = 1
                    self.parts = []
                    
            # 確保檔名解析成功 - 最後一次嘗試從URL獲取檔名
            if self.filename == 'download_file' or self.filename == '':
                print("所有標準方法都無法獲取檔名，嘗試直接從URL解析")
                filename_from_url = self.try_extract_filename_from_url()
                if filename_from_url and filename_from_url != 'download_file':
                    self.filename = filename_from_url
                    self.filepath = os.path.join(self.save_dir, self.filename)
                    self.temp_filepath = f"{self.filepath}.downloading"
                    self.progress_filepath = f"{self.filepath}.progress"
                    print(f"最終從URL成功提取檔名: {self.filename}")
            
            # 如果使用多線程下載，創建臨時文件和初始化分片池
            if self.thread_count > 1 and self.parts:
                # 確保臨時文件存在並且大小正確
                if not os.path.exists(self.temp_filepath):
                    # 創建空洞文件 (sparse file)
                    try:
                        with open(self.temp_filepath, 'wb') as f:
                            # 在文件末尾寫入一個字節，創建空洞文件節省空間
                            if self.total_size > 0:
                                f.seek(self.total_size - 1)
                                f.write(b'\0')
                    except Exception as e:
                        print(f"創建臨時文件時出錯: {e}")
                        # 使用傳統方式創建臨時文件
                        with open(self.temp_filepath, 'wb') as f:
                            pass
                
                # 初始化分片池
                self._init_parts_pool()
            
            # 下載準備完成
            self.status = 'initialized'
            return True
            
        except Exception as e:
            self.status = 'error'
            self.error_message = f"準備下載任務時出錯: {e}"
            print(f"準備下載任務時出錯: {e}")
            return False
    
    def _adjust_chunk_size(self):
        """根據文件大小動態調整分片大小和線程數量"""
        # 首先根據檔案大小調整chunks_per_part，大檔案使用較大的分片
        if self.total_size > 0:
            # 基於檔案大小動態調整分片大小（默認為每個部分100個分片）
            if self.total_size > 10 * 1024 * 1024 * 1024:  # 大於10GB
                self.chunks_per_part = 800
            elif self.total_size > 5 * 1024 * 1024 * 1024:  # 大於5GB
                self.chunks_per_part = 500
            elif self.total_size > 1 * 1024 * 1024 * 1024:  # 大於1GB
                self.chunks_per_part = 300
            elif self.total_size > 500 * 1024 * 1024:       # 大於500MB
                self.chunks_per_part = 200
            elif self.total_size > 100 * 1024 * 1024:       # 大於100MB
                self.chunks_per_part = 150
            else:
                self.chunks_per_part = 10  # 默認值
            
            # 同時針對不同大小的檔案調整緩衝區大小
            if self.total_size > 1 * 1024 * 1024 * 1024:  # 大於1GB
                self.chunk_size = 131072  # 128KB
            elif self.total_size > 100 * 1024 * 1024:     # 大於100MB
                self.chunk_size = 65536   # 64KB
            else:
                self.chunk_size = 32768   # 32KB
            
            # 還可以根據檔案大小調整線程數
            if self.total_size < 10 * 1024 * 1024:  # 小於10MB
                self.thread_count = min(self.thread_count, 5)
            elif self.total_size < 100 * 1024 * 1024:  # 小於100MB
                self.thread_count = min(self.thread_count, 10)
            # 大於100MB時保持原有的線程數設置
    
    def _init_parts(self):
        """初始化下載分片"""
        # 如果沒有文件大小信息，無法分片
        if self.total_size <= 0:
            self.thread_count = 1
            self.parts = []
            return
            
        # 計算每個分片的大小
        # 使用動態分片大小，基於檔案大小和線程數
        parts_count = self.thread_count * self.chunks_per_part
        chunk_size = max(1024 * 1024, self.total_size // parts_count)  # 最小1MB，防止過小分片
        
        # 創建分片列表
        self.parts = []
        for i in range(parts_count):
            start = i * chunk_size
            end = min(start + chunk_size - 1, self.total_size - 1)
            
            # 如果是最後一個分片，確保能覆蓋到文件末尾
            if i == parts_count - 1:
                end = self.total_size - 1
                
            # 如果這個分片的起始位置已經超過文件大小，跳過
            if start >= self.total_size:
                break
                
            # 添加分片信息
            self.parts.append({
                'index': i,
                'start': start,
                'end': end,
                'current': start,  # 當前下載位置，初始等於起始位置
                'completed': False,  # 是否已完成
                'progress': 0  # 進度百分比
            })
            
        print(f"創建了 {len(self.parts)} 個分片")
    
    def _init_parts_pool(self):
        """初始化待下載分片池"""
        with self.parts_pool_lock:
            # 創建一個新的隊列，包含所有未完成的分片
            self.parts_pool = [p for p in self.parts if not p['completed']]
            print(f"初始化分片池，共有 {len(self.parts_pool)} 個未完成分片")
    
    def get_next_part(self):
        """從分片池中獲取下一個要下載的分片
        
        Returns:
            dict: 下一個分片信息，如果沒有可用分片則返回None
        """
        with self.parts_pool_lock:
            if not self.parts_pool:
                return None
            return self.parts_pool.pop(0)
    
    def download_thread(self, thread_id, proxy=None):
        """線程持續從分片池獲取分片並下載
        
        Args:
            thread_id: 線程ID
            proxy: 使用的代理配置
        """
        print(f"線程 {thread_id} 開始運行" + (f"，使用代理 {proxy['host']}:{proxy['port']}" if proxy else ""))
        
        # 重用連接池以提高性能
        session = None
        manager = None
        
        try:
            # 如果使用代理，創建一個支持SOCKS5代理的連接池
            if proxy:
                import urllib3
                import urllib3.contrib.socks
                
                proxy_url = f"socks5://{proxy['host']}:{proxy['port']}"
                
                # 創建使用SOCKS代理的連接池
                manager = urllib3.contrib.socks.SOCKSProxyManager(
                    proxy_url, 
                    timeout=urllib3.Timeout(connect=10.0, read=30.0),
                    retries=0,
                    maxsize=5  # 每個線程維護最多5個連接
                )
            else:
                # 創建標準會話
                import requests
                session = requests.Session()
        except Exception as e:
            print(f"線程 {thread_id} 創建連接池失敗: {e}")
        
        while not self.stop_event.is_set():
            # 從池中獲取下一個分片
            part = self.get_next_part()
            if part is None:
                print(f"線程 {thread_id} 沒有更多分片可下載，退出")
                break
                
            # 下載分片
            print(f"線程 {thread_id} 開始下載分片 {part['index']}")
            self.download_part(part, proxy, manager=manager, session=session)
            
            # 檢查所有分片是否已完成
            if all(p['completed'] for p in self.parts):
                print(f"線程 {thread_id} 檢測到所有分片已完成")
                break
                
        # 關閉連接池或會話
        if manager:
            try:
                manager.clear()
            except:
                pass
        if session:
            try:
                session.close()
            except:
                pass
                
        print(f"線程 {thread_id} 結束運行")
    
    def download_part(self, part, proxy=None, manager=None, session=None):
        """下載檔案的一部分
        
        Args:
            part: 包含開始和結束位置的字典
            proxy: 指定的代理配置，如果為None則使用根據part index分配的代理
            manager: urllib3連接池管理器，用於重用連接
            session: requests會話，用於重用連接
        """
        max_retries = 3
        retry_count = 0
        retry_delay = 1  # 初始重試延遲為1秒
        
        # 如果沒有指定代理，但有可用代理，則根據分片索引選擇代理
        if proxy is None and self.proxies and len(self.proxies) > 0:
            proxy_index = part['index'] % len(self.proxies)
            proxy = self.proxies[proxy_index]
            print(f"分片 {part['index']} 自動分配SOCKS5代理 #{proxy_index+1}: {proxy['host']}:{proxy['port']}")
            
        while retry_count < max_retries:
            try:
                # 檢查是否已達到或超過結束位置
                if part['current'] >= part['end'] + 1:
                    print(f"部分 {part['index']} 已完成 (當前位置: {part['current']}, 結束位置: {part['end']})")
                    part['completed'] = True
                    part['current'] = part['end'] + 1
                    self.save_progress()
                    
                    # 檢查整個任務是否已完成
                    if all(p['completed'] for p in self.parts):
                        print("所有部分已完成，將任務標記為完成")
                        self.complete_download()
                    return
                
                headers = {
                    'User-Agent': 'Multi-Socks-Downloader/1.0',
                    'Range': f"bytes={part['current']}-{part['end']}",
                    'Connection': 'keep-alive'
                }
                
                print(f"下載部分 {part['index']}: bytes={part['current']}-{part['end']}")
                
                # 首先嘗試使用urllib3的方式下載
                download_success = False
                http_416_error = False
                
                # 方法1: 使用urllib3+SOCKS (優先使用傳入的連接池)
                if proxy and (manager or urllib3):
                    try:
                        import urllib3
                        import urllib3.contrib.socks
                        
                        # 使用傳入的管理器或創建新的
                        current_manager = manager
                        if not current_manager:
                            proxy_url = f"socks5://{proxy['host']}:{proxy['port']}"
                            current_manager = urllib3.contrib.socks.SOCKSProxyManager(
                                proxy_url, 
                                timeout=urllib3.Timeout(connect=10.0, read=30.0),
                                retries=0
                            )
                        
                        # 發送請求
                        response = current_manager.request(
                            'GET',
                            self.url,
                            headers=headers,
                            preload_content=False
                        )
                        
                        # 檢查響應狀態碼
                        if response.status not in [200, 206]:
                            print(f"urllib3下載部分 {part['index']} 出錯: HTTP錯誤 {response.status}")
                            response.release_conn()
                            if response.status == 416:
                                http_416_error = True
                                raise Exception(f"HTTP錯誤: {response.status}, 伺服器不支持範圍請求")
                            else:
                                raise Exception(f"HTTP錯誤: {response.status}")
                        
                        # 寫入文件
                        with open(self.temp_filepath, 'rb+') as f:
                            f.seek(part['current'])
                            
                            for chunk in response.stream(self.chunk_size):  # 使用更大的緩衝區 (64KB)
                                if self.stop_event.is_set():
                                    # 保存當前進度
                                    part['current'] = f.tell()
                                    print(f"部分 {part['index']} 下載暫停於位置 {part['current']}")
                                    response.release_conn()
                                    return
                                
                                # 檢查是否會超過該部分的結束位置
                                current_pos = f.tell()
                                if current_pos + len(chunk) > part['end'] + 1:
                                    # 只寫入到結束位置
                                    bytes_to_write = part['end'] + 1 - current_pos
                                    if bytes_to_write > 0:
                                        f.write(chunk[:bytes_to_write])
                                        
                                        with self.progress_lock:
                                            self.downloaded_size += bytes_to_write
                                            part['current'] = f.tell()
                                    
                                    print(f"部分 {part['index']} 到達結束位置: {part['end']}")
                                    part['completed'] = True
                                    part['current'] = part['end'] + 1
                                    break
                                    
                                if chunk:
                                    f.write(chunk)
                                    
                                    with self.progress_lock:
                                        self.downloaded_size += len(chunk)
                                        part['current'] = f.tell()
                                        
                                    # 減少保存進度頻率，從每MB保存一次改為每5MB保存一次
                                    if self.downloaded_size % (5 * 1024 * 1024) == 0:
                                        self.save_progress()
                        
                        # 釋放連接
                        response.release_conn()
                        download_success = True
                        
                    except Exception as e:
                        print(f"urllib3 SOCKS下載失敗: {e}")
                        if "416" in str(e):
                            http_416_error = True
                
                # 方法2: 使用原始socket方式下載
                if not download_success and proxy:
                    try:
                        import socket
                        import socks
                        from urllib.parse import urlparse
                        
                        parsed_url = urlparse(self.url)
                        host = parsed_url.netloc
                        path = parsed_url.path
                        if not path:
                            path = "/"
                        
                        # 設置要連接的端口
                        port = 443 if parsed_url.scheme == 'https' else 80
                        
                        # 創建SOCKS代理socket
                        sock = socks.socksocket()
                        sock.set_proxy(socks.SOCKS5, proxy['host'], proxy['port'])
                        sock.settimeout(30)
                        
                        print(f"使用raw socket連接到 {host}:{port}")
                        sock.connect((host, port))
                        
                        # 對於HTTPS，需要包裝SSL
                        if parsed_url.scheme == 'https':
                            import ssl
                            context = ssl.create_default_context()
                            sock = context.wrap_socket(sock, server_hostname=host)
                        
                        # 構建HTTP請求
                        request = f"GET {path} HTTP/1.1\r\n"
                        request += f"Host: {host}\r\n"
                        request += "User-Agent: Multi-Socks-Downloader/1.0\r\n"
                        request += f"Range: bytes={part['current']}-{part['end']}\r\n"
                        request += "Connection: close\r\n\r\n"
                        
                        # 發送請求
                        sock.sendall(request.encode())
                        
                        # 接收並解析HTTP頭
                        response_data = b""
                        content_started = False
                        header_data = b""
                        buffer_size = self.chunk_size  # 增加緩衝區大小
                        
                        with open(self.temp_filepath, 'rb+') as f:
                            f.seek(part['current'])
                            
                            while True:
                                if self.stop_event.is_set():
                                    # 保存當前進度
                                    part['current'] = f.tell()
                                    print(f"部分 {part['index']} 下載暫停於位置 {part['current']}")
                                    sock.close()
                                    return
                                
                                chunk = sock.recv(buffer_size)
                                if not chunk:
                                    break
                                
                                if not content_started:
                                    response_data += chunk
                                    if b"\r\n\r\n" in response_data:
                                        # 分離頭部和內容
                                        parts = response_data.split(b"\r\n\r\n", 1)
                                        header_data = parts[0]
                                        
                                        # 檢查HTTP狀態碼
                                        header_lines = header_data.split(b"\r\n")
                                        status_line = header_lines[0].decode('ascii', errors='ignore')
                                        
                                        if "200" not in status_line and "206" not in status_line:
                                            print(f"HTTP錯誤: {status_line}")
                                            sock.close()
                                            if "416" in status_line:
                                                http_416_error = True
                                                raise Exception(f"HTTP錯誤: {status_line}, 伺服器不支持範圍請求")
                                            else:
                                                raise Exception(f"HTTP錯誤: {status_line}")
                                        
                                        # 如果有內容，寫入文件
                                        if len(parts) > 1 and parts[1]:
                                            content_data = parts[1]
                                            f.write(content_data)
                                            with self.progress_lock:
                                                self.downloaded_size += len(content_data)
                                                part['current'] = f.tell()
                                        
                                        content_started = True
                                else:
                                    # 直接寫入內容
                                    current_pos = f.tell()
                                    bytes_remaining = part['end'] + 1 - current_pos
                                    
                                    if len(chunk) > bytes_remaining:
                                        # 只寫入需要的部分
                                        f.write(chunk[:bytes_remaining])
                                        with self.progress_lock:
                                            self.downloaded_size += bytes_remaining
                                            part['current'] = f.tell()
                                        print(f"部分 {part['index']} 到達結束位置: {part['end']}")
                                        break
                                    else:
                                        f.write(chunk)
                                        with self.progress_lock:
                                            self.downloaded_size += len(chunk)
                                            part['current'] = f.tell()
                                
                                # 減少保存進度頻率，改為每5MB保存一次
                                if self.downloaded_size % (5 * 1024 * 1024) == 0:
                                    self.save_progress()
                        
                        sock.close()
                        download_success = True
                        
                    except Exception as e:
                        print(f"原始socket下載失敗: {e}")
                        if "416" in str(e):
                            http_416_error = True
                
                # 方法3：使用標準的requests庫
                if not download_success:
                    # 構建代理字典
                    proxies = None
                    if proxy:
                        proxy_url = f"socks5://{proxy['host']}:{proxy['port']}"
                        proxies = {
                            'http': proxy_url,
                            'https': proxy_url
                        }
                    
                    # 使用requests下載 (優先使用傳入的會話)
                    import requests
                    
                    # 使用傳入的會話或創建新請求
                    if session:
                        response = session.get(
                            self.url, 
                            headers=headers, 
                            stream=True, 
                            timeout=30,
                            proxies=proxies
                        )
                    else:
                        response = requests.get(
                            self.url, 
                            headers=headers, 
                            stream=True, 
                            timeout=30,
                            proxies=proxies
                        )
                    
                    if response.status_code not in [200, 206]:
                        print(f"requests下載部分 {part['index']} 出錯: HTTP錯誤 {response.status_code}")
                        if response.status_code == 416:
                            http_416_error = True
                            raise Exception(f"HTTP錯誤: {response.status_code}, 伺服器不支持範圍請求")
                        else:
                            raise Exception(f"HTTP錯誤: {response.status_code}")
                        
                    with open(self.temp_filepath, 'rb+') as f:
                        f.seek(part['current'])
                        
                        for chunk in response.iter_content(chunk_size=self.chunk_size):  # 增加緩衝區大小至64KB
                            if self.stop_event.is_set():
                                # 保存當前進度
                                part['current'] = f.tell()
                                print(f"部分 {part['index']} 下載暫停於位置 {part['current']}")
                                return
                            
                            # 檢查是否會超過該部分的結束位置
                            current_pos = f.tell()
                            if current_pos + len(chunk) > part['end'] + 1:
                                # 只寫入到結束位置
                                bytes_to_write = part['end'] + 1 - current_pos
                                if bytes_to_write > 0:
                                    f.write(chunk[:bytes_to_write])
                                    
                                    with self.progress_lock:
                                        self.downloaded_size += bytes_to_write
                                        part['current'] = f.tell()
                                
                                print(f"部分 {part['index']} 到達結束位置: {part['end']}")
                                part['completed'] = True
                                part['current'] = part['end'] + 1
                                break
                                
                            if chunk:
                                f.write(chunk)
                                
                                with self.progress_lock:
                                    self.downloaded_size += len(chunk)
                                    part['current'] = f.tell()
                                    
                                # 減少保存進度頻率，改為每5MB保存一次
                                if self.downloaded_size % (5 * 1024 * 1024) == 0:
                                    self.save_progress()
                    
                    download_success = True
                
                # 檢查是否是HTTP 416錯誤，如果是，轉為單線程下載
                if http_416_error:
                    # 在第一次遇到416錯誤時就立即轉為單線程
                    print(f"檢測到伺服器不支持範圍請求 (HTTP 416)，轉為單線程下載")
                    
                    # 使用鎖確保只有一個線程執行切換操作
                    with self.switching_lock:
                        # 檢查是否已經有其他線程執行了切換
                        if not self.switched_to_single_thread:
                            self.switched_to_single_thread = True
                            
                            # 停止所有下載線程
                            self.stop_event.set()
                            time.sleep(0.5)  # 給其他線程一些時間來停止
                            
                            # 標記為需要單線程下載
                            self.thread_count = 1
                            self.parts = []
                            
                            # 重置下載進度
                            self.downloaded_size = 0
                            
                            # 清空臨時文件
                            try:
                                with open(self.temp_filepath, 'wb') as f:
                                    pass
                            except:
                                pass
                                
                            # 重置停止事件，允許新的單線程下載開始
                            self.stop_event.clear()
                            
                            # 啟動單線程下載
                            thread = threading.Thread(target=self.download_single)
                            self.threads = [thread]
                            thread.start()
                            
                            print("已啟動單線程下載模式")
                        else:
                            print("另一個線程已經啟動了單線程下載模式")
                    
                    return
                
                # 標記此部分已完成
                part['completed'] = True
                part['current'] = part['end'] + 1
                self.save_progress()
                print(f"下載部分 {part['index']} 完成")
                
                # 檢查整個任務是否已完成
                if all(p['completed'] for p in self.parts):
                    print("所有部分已完成，將任務標記為完成")
                    self.complete_download()
                return
                
            except Exception as e:
                retry_count += 1
                print(f"下載部分 {part['index']} 出錯 (嘗試 {retry_count}/{max_retries}): {e}")
                
                # 檢查是否是HTTP 416錯誤
                if "416" in str(e):
                    # 在第一次遇到416錯誤時就立即轉為單線程下載
                    print(f"檢測到伺服器不支持範圍請求 (HTTP 416)，轉為單線程下載")
                    
                    # 使用鎖確保只有一個線程執行切換操作
                    with self.switching_lock:
                        # 檢查是否已經有其他線程執行了切換
                        if not self.switched_to_single_thread:
                            self.switched_to_single_thread = True
                            
                            # 停止所有下載線程
                            self.stop_event.set()
                            time.sleep(0.5)  # 給其他線程一些時間來停止
                            
                            # 標記為需要單線程下載
                            self.thread_count = 1
                            self.parts = []
                            
                            # 重置下載進度
                            self.downloaded_size = 0
                            
                            # 清空臨時文件
                            try:
                                with open(self.temp_filepath, 'wb') as f:
                                    pass
                            except:
                                pass
                                
                            # 重置停止事件，允許新的單線程下載開始
                            self.stop_event.clear()
                            
                            # 啟動單線程下載
                            thread = threading.Thread(target=self.download_single)
                            self.threads = [thread]
                            thread.start()
                            
                            print("已啟動單線程下載模式")
                        else:
                            print("另一個線程已經啟動了單線程下載模式")
                    
                    return
                
                # 如果已達到最大重試次數或用戶取消，退出重試
                if retry_count >= max_retries or self.stop_event.is_set():
                    break
                # 短暫延遲後重試
                time.sleep(2)
                
        # 達到最大重試次數仍然失敗
        print(f"下載部分 {part['index']} 失敗，達到最大重試次數")
        # 保存當前進度，以便後續恢復
        self.save_progress()
        
        # 如果是因為暫停導致的，則不視為錯誤
        if self.stop_event.is_set():
            self.status = 'paused'
        else:
            self.status = 'error'
            self.error_message = f"下載部分 {part['index']} 失敗: 達到最大重試次數"
    
    def start(self):
        """開始或恢復下載任務"""
        if not self.prepare():
            return False
            
        self.stop_event.clear()
        
        # 檢查是否為恢復下載
        is_resume = (self.status == 'paused')
        previous_status = self.status
        
        self.status = 'downloading'
        
        # 只有在新開始下載時才重置開始時間
        # 如果是從暫停狀態恢復，則在 resume 方法中已調整開始時間
        if not is_resume or not self.start_time:
            self.start_time = time.time()
            self.last_active_start = time.time()  # 記錄活動開始時間
            self.total_active_time = 0  # 新下載任務的累計活動時間為0
            self.resumed_size = 0  # 新下載任務，已恢復大小為0
            print(f"新下載任務開始: {self.filename}")
        else:
            # 恢復下載時，記錄已下載的大小
            self.resumed_size = self.downloaded_size
            self.last_active_start = time.time()  # 記錄本次活動開始時間
            print(f"恢復下載任務: {self.filename}, 已下載: {format_size(self.downloaded_size)}")
            
            # 重置速度計算相關數據
            self.speed_data.clear()
            self.last_downloaded_size = self.downloaded_size
            self.last_speed_update = time.time()
            self.last_reported_speed = 0  # 重置上次報告的速度
        
        # 創建並啟動下載線程
        self.threads = []
        
        # 如果之前因為 HTTP 416 錯誤而切換到單線程，或者本來就是單線程下載模式
        if self.switched_to_single_thread or (self.thread_count == 1 and self.total_size == 0):
            # 單線程下載整個檔案（不支持斷點續傳）
            print("使用單線程模式下載")
            thread = threading.Thread(target=self.download_single)
            self.threads.append(thread)
            thread.start()
        else:
            # 多線程下載 - 使用分片池和代理分配的新模式
            print(f"使用 {self.thread_count} 線程並行下載")
            
            if self.proxies:
                # 使用代理時，為每個代理分配固定數量的線程
                thread_id = 0
                for proxy_index, proxy in enumerate(self.proxies):
                    # 每個代理分配固定數量的線程
                    for i in range(self.threads_per_proxy):
                        thread = threading.Thread(
                            target=self.download_thread, 
                            args=(thread_id, proxy)
                        )
                        self.threads.append(thread)
                        thread.start()
                        thread_id += 1
            else:
                # 沒有代理時，所有線程直接從分片池中獲取任務
                for i in range(self.thread_count):
                    thread = threading.Thread(
                        target=self.download_thread, 
                        args=(i, None)
                    )
                    self.threads.append(thread)
                    thread.start()
            
            # 啟動一個線程定期檢查任務是否完成
            completion_check_thread = threading.Thread(target=self.check_completion_loop)
            completion_check_thread.daemon = True
            self.threads.append(completion_check_thread)
            completion_check_thread.start()
        
        return True
    
    def check_completion_loop(self):
        """定期檢查任務是否完成的循環"""
        last_downloaded_size = self.downloaded_size
        no_progress_count = 0
        
        while not self.stop_event.is_set() and self.status == 'downloading':
            # 休眠一段時間
            time.sleep(1)
            
            # 檢查下載是否有進度
            current_downloaded = self.downloaded_size
            if current_downloaded == last_downloaded_size:
                no_progress_count += 1
            else:
                no_progress_count = 0
                last_downloaded_size = current_downloaded
            
            # 檢查所有下載線程是否都已完成
            if self.thread_count > 1:
                active_threads = 0
                for thread in self.threads:
                    if thread != threading.current_thread() and thread.is_alive():
                        active_threads += 1
                
                # 如果沒有活動線程或者長時間無進度，檢查任務狀態
                if active_threads == 0 or no_progress_count > 5:
                    # 檢查是否所有部分都已完成
                    if all(part['completed'] for part in self.parts):
                        print("檢測到所有部分已完成，將任務標記為完成")
                        self.complete_download()
                        break
                    
                    # 檢查是否已下載完整個檔案
                    # 允許誤差範圍為 1KB
                    if self.total_size > 0 and abs(self.downloaded_size - self.total_size) <= 1024:
                        print(f"檢測到下載進度接近 100%，將任務標記為完成")
                        print(f"下載大小: {self.downloaded_size}，總大小: {self.total_size}，誤差: {self.downloaded_size - self.total_size}")
                        self.complete_download()
                        break
                    
                    # 如果長時間無進度但任務未完成，檢查是否需要重啟線程
                    if no_progress_count > 10:
                        incomplete_parts = [p for p in self.parts if not p['completed']]
                        if incomplete_parts:
                            print(f"檢測到下載長時間無進度，還有 {len(incomplete_parts)} 個部分未完成")
                            # 這裡可以添加重啟未完成部分的邏輯
            
            # 檢查是否已下載完整個檔案
            if self.total_size > 0:
                # 允許一個小的誤差範圍
                if self.downloaded_size >= self.total_size or abs(self.downloaded_size - self.total_size) < 1024:
                    print(f"檢測到下載進度已達到或接近 100%，將任務標記為完成")
                    print(f"下載大小: {self.downloaded_size}，總大小: {self.total_size}")
                    if self.downloaded_size > self.total_size:
                        print(f"下載大小超過總大小，修正為總大小")
                        self.downloaded_size = self.total_size
                    self.complete_download()
                    break
    
    def download_single(self):
        """單線程下載整個檔案（不支持斷點續傳）"""
        try:
            headers = {
                'User-Agent': 'Multi-Socks-Downloader/1.0',
                'Connection': 'keep-alive'
            }
            
            print(f"使用單線程下載整個檔案: {self.url}")
            
            # 設置代理
            proxies = None
            if self.proxies and len(self.proxies) > 0:
                # 使用第一個代理
                proxy = self.proxies[0]
                proxy_url = f"socks5://{proxy['host']}:{proxy['port']}"
                proxies = {
                    'http': proxy_url,
                    'https': proxy_url
                }
                print(f"單線程下載使用SOCKS5代理: {proxy_url}")
            
            response = requests.get(
                self.url, 
                headers=headers, 
                stream=True, 
                timeout=30,
                proxies=proxies
            )
            
            response.raise_for_status()
            
            # 獲取檔案大小
            self.total_size = int(response.headers.get('content-length', 0))
            
            # 獲取檔案名稱（如果尚未指定）
            if self.filename in ['download_file', '']:
                filename_from_header = self.get_filename_from_content_disposition(response.headers)
                if filename_from_header:
                    self.filename = filename_from_header
                    self.filepath = os.path.join(self.save_dir, self.filename)
                    self.temp_filepath = f"{self.filepath}.downloading"
                    self.progress_filepath = f"{self.filepath}.progress"
                    print(f"從響應頭獲取檔案名稱: {self.filename}")
                    
            # 打開臨時檔案寫入數據
            with open(self.temp_filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=self.chunk_size):  # 增加緩衝區大小
                    if self.stop_event.is_set():
                        # 暫停下載
                        self.status = 'paused'
                        print(f"單線程下載暫停: {self.filename}")
                        return
                        
                    if chunk:
                        f.write(chunk)
                        
                        with self.progress_lock:
                            self.downloaded_size += len(chunk)
                            
                        # 減少保存進度頻率，改為每5MB保存一次
                        if self.downloaded_size % (5 * 1024 * 1024) == 0:
                            # 更新下載進度
                            self.update_speed_data()
                            # 讓UI更容易感知更新
                            time.sleep(0.01)
            
            print("\n單線程下載完成")
            
            # 將臨時檔案重命名為目標檔案
            self.complete_download()
            
        except Exception as e:
            print(f"單線程下載出錯: {e}")
            self.status = 'error'
            self.error_message = str(e)
            
            # 如果是暫停導致的異常，則不視為錯誤
            if self.stop_event.is_set():
                self.status = 'paused'
    
    def pause(self):
        """暫停下載任務"""
        if self.status == 'downloading':
            current_time = time.time()
            print(f"暫停下載任務: {self.filename}")
            # 記錄暫停時間和已下載大小
            self.pause_time = current_time
            self.downloaded_before_pause = self.downloaded_size
            
            # 更新累計下載時間
            if self.last_active_start:
                active_duration = current_time - self.last_active_start
                self.total_active_time += active_duration
                print(f"本次活動時長: {active_duration:.1f}秒, 累計活動時間: {self.total_active_time:.1f}秒")
            
            self.stop_event.set()
            self.status = 'paused'
            self.save_progress()
            
            # 重置進度相關變量，為下次恢復做準備
            self.resumed_size = 0  # 將在恢復時重新設置
            self.speed_data.clear()  # 清空速度數據
            self.last_reported_speed = 0  # 重置上次報告的速度
            
            return True
        return False
    
    def resume(self):
        """恢復暫停的下載任務"""
        if self.status == 'paused':
            print(f"恢復下載任務: {self.filename}")
            # 記錄當前已下載的大小，用於準確計算恢復後的下載速度
            self.resumed_size = self.downloaded_size
            # 不重置開始時間，而是重新設置活動開始時間
            self.last_active_start = time.time()
            
            # 重置速度計算數據
            self.speed_data.clear()
            self.last_downloaded_size = self.downloaded_size
            self.last_speed_update = None  # 將在恢復下載後的第一次更新中設置
            
            return self.start()
        return False
    
    def cancel(self):
        """取消下載任務並刪除臨時檔案"""
        self.stop_event.set()
        self.status = 'canceled'
        
        # 等待所有線程結束
        for thread in self.threads:
            if thread.is_alive():
                thread.join(1)
                
        # 刪除臨時檔案
        if os.path.exists(self.temp_filepath):
            try:
                os.remove(self.temp_filepath)
            except:
                pass
                
        # 刪除進度檔案
        if os.path.exists(self.progress_filepath):
            try:
                os.remove(self.progress_filepath)
            except:
                pass
                
        return True
    
    def is_running(self):
        """檢查任務是否正在運行"""
        return any(thread.is_alive() for thread in self.threads)
    
    def is_completed(self):
        """檢查任務是否已完成"""
        # 先檢查狀態
        if self.status == 'completed':
            return True
            
        # 檢查所有部分是否已完成
        if self.thread_count > 1 and self.parts:
            if all(part['completed'] for part in self.parts):
                if self.status != 'completed':
                    print("檢測到所有部分已完成，任務標記為完成")
                    self.complete_download()
                return True
        
        # 檢查是否已下載完整個檔案
        if self.total_size > 0:
            if self.downloaded_size >= self.total_size:
                # 如果下載大小超過總大小，修正為總大小
                if self.downloaded_size > self.total_size:
                    print(f"修正下載大小: {self.downloaded_size} -> {self.total_size}")
                    self.downloaded_size = self.total_size
                
                if self.status != 'completed':
                    print(f"檢測到下載進度達到 100%，任務標記為完成")
                    self.complete_download()
                return True
        
        return False
    
    def complete_download(self):
        """完成下載，將臨時檔案重命名為最終檔案名"""
        # 使用鎖確保只執行一次
        with self.progress_lock:
            # 如果已經完成，直接返回
            if self.status == 'completed':
                return True
                
            print(f"完成下載任務: {self.filename}")
            self.end_time = time.time()
            
            # 計算最終的總下載時間
            if self.last_active_start:
                active_duration = self.end_time - self.last_active_start
                self.total_active_time += active_duration
                print(f"最後一次活動時長: {active_duration:.1f}秒, 總下載時間: {self.total_active_time:.1f}秒")
            
            self.status = 'completed'
            
            # 將臨時檔案重命名為最終檔案名
            try:
                # 確保下載大小不會超過總大小
                if self.total_size > 0 and self.downloaded_size > self.total_size:
                    self.downloaded_size = self.total_size
                
                # 檢查臨時文件是否存在
                temp_file_found = False
                temp_file_to_use = None
                
                # 首先檢查標準的臨時文件(.downloading)是否存在
                if os.path.exists(self.temp_filepath):
                    temp_file_found = True
                    temp_file_to_use = self.temp_filepath
                    print(f"找到臨時檔案: {self.temp_filepath}")
                else:
                    print(f"標準臨時檔案不存在: {self.temp_filepath}")
                    
                    # 檢查是否存在沒有副檔名的臨時檔案
                    # 有時候臨時文件可能已經被重命名，但沒有加上正確的檔案名
                    base_temp_path = os.path.join(self.save_dir, os.path.basename(self.filepath).split('.')[0])
                    if os.path.exists(base_temp_path):
                        temp_file_found = True
                        temp_file_to_use = base_temp_path
                        print(f"找到基本臨時檔案: {base_temp_path}")
                    else:
                        # 嘗試查找以哈希值命名的臨時文件
                        hash_part = os.path.basename(self.filepath).split('-')[0]
                        if len(hash_part) > 30:  # 可能是哈希值
                            hash_temp_path = os.path.join(self.save_dir, hash_part)
                            if os.path.exists(hash_temp_path):
                                temp_file_found = True
                                temp_file_to_use = hash_temp_path
                                print(f"找到哈希命名的臨時檔案: {hash_temp_path}")
                
                if temp_file_found:
                    # 檢查目標文件是否已存在
                    if os.path.exists(self.filepath):
                        try:
                            os.remove(self.filepath)
                            print(f"已刪除已存在的文件: {self.filepath}")
                        except Exception as e:
                            print(f"無法刪除已存在的文件: {e}")
                            self.status = 'error'
                            self.error_message = f"無法刪除已存在的文件: {e}"
                            return False
                    
                    # 重命名臨時文件
                    os.rename(temp_file_to_use, self.filepath)
                    print(f"臨時檔案 {temp_file_to_use} 已重命名為: {self.filepath}")
                    
                    # 刪除進度檔案
                    if os.path.exists(self.progress_filepath):
                        try:
                            os.remove(self.progress_filepath)
                            print(f"已刪除進度檔案: {self.progress_filepath}")
                        except Exception as e:
                            print(f"刪除進度檔案時出錯 (非致命): {e}")
                else:
                    print(f"錯誤：未找到任何臨時文件")
                    self.status = 'error'
                    self.error_message = "臨時文件不存在"
                    return False
            except Exception as e:
                self.status = 'error'
                self.error_message = f"完成下載時出錯: {e}"
                print(f"完成下載時出錯: {e}")
                return False
                
            return True


class DownloadManager:
    def __init__(self):
        # 從配置檔案中載入設置
        self.tasks = {}  # URL -> DownloadTask
        self.task_ids = {}  # task_id -> DownloadTask
        self.next_id = 1
        self.save_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self.download_dirs = set([self.save_dir])
        self.socks_proxies = {}  # 存儲SOCKS5代理配置 id -> {name, host, port, status}
        self.next_proxy_id = 1
        
        # 默認設置
        self.default_thread_count = 10
        self.default_chunks_per_part = 10  # 默認分片數
        self.default_threads_per_proxy = 3  # 每個代理的默認線程數
        
        # 網絡性能優化參數
        self.connection_timeout = 10  # 連接超時時間（秒）
        self.read_timeout = 30  # 讀取超時時間（秒）
        self.max_retry_count = 3  # 最大重試次數
        self.retry_backoff_factor = 2  # 重試間隔增長因子
        self.keep_alive_enabled = True  # 是否啟用 HTTP Keep-Alive
        self.auto_adjust_chunk_size = True  # 根據文件大小自動調整分片大小
        self.auto_adjust_threads = True  # 根據網絡條件自動調整線程數
        self.minimum_speed_threshold = 5 * 1024  # 最低速度閾值（5KB/s），低於此速度增加重試
        
        # 載入配置檔案
        self.config_dir = os.path.join(os.path.expanduser("~"), ".multi_socks_downloader")
        self.config_file = os.path.join(self.config_dir, "config.json")
        os.makedirs(self.config_dir, exist_ok=True)
        
        self.load_config()
    
    def load_config(self):
        """載入程式配置"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                
                # 載入保存目錄
                if 'save_dir' in config and os.path.exists(config['save_dir']):
                    self.save_dir = config['save_dir']
                    
                # 載入曾經使用過的下載目錄列表
                if 'download_dirs' in config:
                    for directory in config['download_dirs']:
                        if os.path.exists(directory):
                            self.download_dirs.add(directory)
                
                # 載入SOCKS5代理列表
                if 'socks_proxies' in config:
                    self.socks_proxies = config['socks_proxies']
                    # 找出最大的代理ID
                    if self.socks_proxies:
                        self.next_proxy_id = max(int(proxy_id) for proxy_id in self.socks_proxies.keys()) + 1
                
                # 載入下載設置
                if 'default_thread_count' in config:
                    self.default_thread_count = config['default_thread_count']
                if 'default_chunks_per_part' in config:
                    self.default_chunks_per_part = config['default_chunks_per_part']
                if 'default_threads_per_proxy' in config:
                    self.default_threads_per_proxy = config['default_threads_per_proxy']
                    
                # 載入網絡性能優化參數
                if 'connection_timeout' in config:
                    self.connection_timeout = config['connection_timeout']
                if 'read_timeout' in config:
                    self.read_timeout = config['read_timeout']
                if 'max_retry_count' in config:
                    self.max_retry_count = config['max_retry_count']
                if 'retry_backoff_factor' in config:
                    self.retry_backoff_factor = config['retry_backoff_factor']
                if 'keep_alive_enabled' in config:
                    self.keep_alive_enabled = config['keep_alive_enabled']
                if 'auto_adjust_chunk_size' in config:
                    self.auto_adjust_chunk_size = config['auto_adjust_chunk_size']
                if 'auto_adjust_threads' in config:
                    self.auto_adjust_threads = config['auto_adjust_threads']
                if 'minimum_speed_threshold' in config:
                    self.minimum_speed_threshold = config['minimum_speed_threshold']
                    
                print(f"成功載入配置檔案: {self.config_file}")
                
        except Exception as e:
            print(f"載入配置檔案時出錯: {e}")
            # 使用默認設置
            
    def save_config(self):
        """保存程式配置"""
        try:
            config = {
                'save_dir': self.save_dir,
                'download_dirs': list(self.download_dirs),
                'socks_proxies': self.socks_proxies,
                'default_thread_count': self.default_thread_count,
                'default_chunks_per_part': self.default_chunks_per_part,
                'default_threads_per_proxy': self.default_threads_per_proxy,
                # 保存網絡性能優化參數
                'connection_timeout': self.connection_timeout,
                'read_timeout': self.read_timeout,
                'max_retry_count': self.max_retry_count,
                'retry_backoff_factor': self.retry_backoff_factor,
                'keep_alive_enabled': self.keep_alive_enabled,
                'auto_adjust_chunk_size': self.auto_adjust_chunk_size,
                'auto_adjust_threads': self.auto_adjust_threads,
                'minimum_speed_threshold': self.minimum_speed_threshold
            }
            
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=4)
                
            print(f"配置已保存到: {self.config_file}")
            return True
            
        except Exception as e:
            print(f"保存配置檔案時出錯: {e}")
            return False
            
    # ===== SOCKS5代理管理方法 =====
    
    def add_socks_proxy(self, name, host, port):
        """添加SOCKS5代理
        
        Args:
            name: 代理名稱
            host: 代理主機地址
            port: 代理埠
            
        Returns:
            str: 代理ID，如果添加失敗則返回None
        """
        # 檢查是否存在同名代理
        for proxy in self.socks_proxies.values():
            if proxy['name'] == name:
                return None
                
        proxy_id = str(self.next_proxy_id)
        self.next_proxy_id += 1
        
        self.socks_proxies[proxy_id] = {
            'name': name,
            'host': host,
            'port': port,
            'status': '未測試'
        }
        
        # 保存配置
        self.save_config()
        
        return proxy_id
        
    def delete_socks_proxy(self, proxy_id):
        """刪除SOCKS5代理
        
        Args:
            proxy_id: 代理ID
            
        Returns:
            bool: 是否成功刪除代理
        """
        if proxy_id not in self.socks_proxies:
            return False
            
        del self.socks_proxies[proxy_id]
        
        # 保存配置
        self.save_config()
        
        return True
        
    def test_socks_proxy(self, proxy_id):
        """測試SOCKS5代理連接
        
        Args:
            proxy_id: 代理ID
            
        Returns:
            tuple: (bool, str) 表示是否成功和錯誤信息
        """
        if proxy_id not in self.socks_proxies:
            print(f"錯誤: 代理 {proxy_id} 不存在")
            return (False, "代理不存在")
            
        proxy = self.socks_proxies[proxy_id]
        host = proxy['host']
        port = proxy['port']
        
        # 更新代理狀態為「測試中」
        self.socks_proxies[proxy_id]['status'] = '測試中...'
        # 立即保存配置確保狀態被持久化
        self.save_config()
        print(f"已將代理 {proxy_id} ({host}:{port}) 狀態設置為「測試中...」")
        
        try:
            # 引入必要的庫
            import socket
            import socks
            import time
            
            # 記錄測試開始時間
            start_time = time.time()
            
            # 1. 首先測試基本的socket連接能力
            print(f"測試代理 {host}:{port} - 正在測試Socket連接...")
            # 創建一個SOCKS5代理socket
            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, host, port)
            s.settimeout(10)  # 設置10秒超時
            
            # 嘗試連接到多個備選目標，提高成功率
            test_targets = [
                ("www.google.com", 80),
                ("www.cloudflare.com", 80),
                ("www.microsoft.com", 80),
                ("1.1.1.1", 80),
                ("8.8.8.8", 53)
            ]
            
            socket_success = False
            socket_error = "所有目標連接都失敗"
            connected_target = None
            
            for target, target_port in test_targets:
                try:
                    print(f"嘗試連接到 {target}:{target_port}...")
                    s.connect((target, target_port))
                    socket_success = True
                    print(f"成功連接到 {target}:{target_port}")
                    socket_error = ""
                    connected_target = (target, target_port)
                    break
                except Exception as e:
                    print(f"連接到 {target}:{target_port} 失敗: {e}")
                    socket_error = str(e)
                    continue
            
            if not socket_success:
                raise Exception(f"Socket連接測試失敗: {socket_error}")
            
            # 2. 使用socket直接發送HTTP請求測試，而不是使用requests庫
            print("Socket連接成功，進行HTTP測試...")
            try:
                # 重新建立一個socket連接
                test_socket = socks.socksocket()
                test_socket.set_proxy(socks.SOCKS5, host, port)
                test_socket.settimeout(10)
                
                # 連接到目標網站
                print("連接到httpbin.org...")
                test_socket.connect(("httpbin.org", 80))
                
                # 構建基本的HTTP請求
                http_request = (
                    "GET /ip HTTP/1.1\r\n"
                    "Host: httpbin.org\r\n"
                    "User-Agent: Multi-Socks-Downloader/1.0\r\n"
                    "Connection: close\r\n\r\n"
                )
                
                # 發送HTTP請求
                print("發送HTTP請求...")
                test_socket.sendall(http_request.encode())
                
                # 接收並解析HTTP頭
                print("接收響應...")
                response = b""
                while True:
                    data = test_socket.recv(4096)
                    if not data:
                        break
                    response += data
                
                # 分析響應
                response_text = response.decode('utf-8', errors='ignore')
                print(f"收到響應: {response_text[:100]}...")
                
                # 檢查是否成功
                if "HTTP/1.1 200" in response_text:
                    http_success = True
                    # 嘗試從響應中提取IP地址
                    import re
                    ip_match = re.search(r'"origin":\s*"([^"]+)"', response_text)
                    response_ip = ip_match.group(1) if ip_match else "IP未知"
                    print(f"從響應中提取到IP: {response_ip}")
                else:
                    http_success = False
                    response_ip = None
                    print(f"HTTP響應不成功，狀態碼不是200")
                
                test_socket.close()
                
            except Exception as e:
                print(f"使用原始socket的HTTP測試失敗: {e}")
                http_success = False
                response_ip = None
            
            # 計算測試完成的總時間
            end_time = time.time()
            test_time = end_time - start_time
            
            # 基於結果更新代理狀態
            if http_success:
                # HTTP測試成功
                status_info = f"可用 ({test_time:.1f}秒) - IP: {response_ip}"
                print(f"代理測試成功，設置狀態為: {status_info}")
                self.socks_proxies[proxy_id]['status'] = status_info
                self.save_config()
                return (True, f"延遲: {test_time:.1f}秒，IP: {response_ip}")
            else:
                # 如果Socket測試成功但HTTP測試失敗，仍然將代理標記為有限可用
                print("HTTP測試失敗，但Socket連接成功，將代理標記為有限可用")
                status_info = f"有限可用 ({test_time:.1f}秒) - 僅支持TCP連接"
                print(f"更新代理狀態為: {status_info}")
                self.socks_proxies[proxy_id]['status'] = status_info
                self.save_config()
                print(f"保存配置後，代理狀態為: {self.socks_proxies[proxy_id]['status']}")
                return (True, f"僅TCP連接可用，延遲: {test_time:.1f}秒")
            
        except ImportError as e:
            error_msg = f"缺少必要庫: {e}"
            print(f"錯誤: {error_msg}")
            # 更新代理狀態
            self.socks_proxies[proxy_id]['status'] = f'不可用: {error_msg}'
            self.save_config()
            return (False, error_msg)
        except Exception as e:
            error_msg = str(e)
            print(f"代理測試失敗: {error_msg}")
            
            # 更新代理狀態
            self.socks_proxies[proxy_id]['status'] = f'不可用: {error_msg}'
            self.save_config()
            
            return (False, error_msg)
            
    def get_all_proxies(self):
        """獲取所有SOCKS5代理
        
        Returns:
            dict: 代理字典，以ID為key
        """
        return self.socks_proxies
        
    def get_available_proxies(self):
        """獲取所有可用的SOCKS5代理列表
        
        Returns:
            list: 代理配置列表，如果沒有可用代理則返回空列表
        """
        available_proxies = [
            {'host': proxy['host'], 'port': proxy['port']} 
            for proxy_id, proxy in self.socks_proxies.items() 
            if proxy['status'].startswith('可用') or proxy['status'].startswith('有限可用')
        ]
        
        return available_proxies
    
    def add_task(self, url, filename=None, thread_count=None, save_dir=None, use_proxy=True, chunks_per_part=None, threads_per_proxy=None):
        """添加下載任務
        
        Args:
            url: 下載檔案的URL
            filename: 保存的檔案名，如果為None則自動從URL中提取
            thread_count: 用於下載的線程數量，若未指定則使用配置的默認值
            save_dir: 保存檔案的目錄，若未指定則使用配置的默認目錄
            use_proxy: 是否使用SOCKS5代理
            chunks_per_part: 每個線程處理的分片數量，若未指定則使用配置的默認值
            threads_per_proxy: 每個代理的線程數，若未指定則使用配置的默認值
            
        Returns:
            str: 任務ID
        """
        # 使用默認值（如果未指定）
        if thread_count is None:
            thread_count = self.default_thread_count
        if save_dir is None:
            save_dir = self.save_dir
        if chunks_per_part is None:
            chunks_per_part = self.default_chunks_per_part
        if threads_per_proxy is None:
            threads_per_proxy = self.default_threads_per_proxy
            
        # 確保保存目錄存在
        if not os.path.exists(save_dir):
            try:
                os.makedirs(save_dir)
            except Exception as e:
                print(f"無法創建保存目錄: {e}")
                return None
                
        # 獲取可用的代理（如果需要使用代理）
        proxies = None
        if use_proxy:
            available_proxies = self.get_available_proxies()
            if available_proxies:
                proxies = available_proxies
                # 根據代理自動優化線程數
                if self.auto_adjust_threads:
                    max_threads = min(len(proxies) * threads_per_proxy, 32)
                    thread_count = max_threads
        
        # 使用優化參數創建下載任務        
        task = DownloadTask(
            url=url,
            save_dir=save_dir,
            filename=filename,
            thread_count=thread_count,
            proxies=proxies,
            chunks_per_part=chunks_per_part,
            threads_per_proxy=threads_per_proxy
        )
        
        # 將任務添加到任務列表
        self.tasks[url] = task
        
        # 生成任務ID並關聯到任務
        task_id = str(self.next_id)
        self.next_id += 1
        self.task_ids[task_id] = task
        
        # 記錄保存目錄
        self.download_dirs.add(save_dir)
        self.save_config()
        
        print(f"已添加下載任務 #{task_id}: {url}")
        return task_id
    
    def start_task(self, task_id):
        """開始下載任務
        
        Args:
            task_id: 任務ID
            
        Returns:
            bool: 是否成功開始任務
        """
        if task_id not in self.task_ids:
            return False
            
        task = self.task_ids[task_id]
        return task.start()
    
    def pause_task(self, task_id):
        """暫停下載任務
        
        Args:
            task_id: 任務ID
            
        Returns:
            bool: 是否成功暫停任務
        """
        if task_id not in self.task_ids:
            return False
            
        task = self.task_ids[task_id]
        return task.pause()
    
    def resume_task(self, task_id):
        """恢復下載任務
        
        Args:
            task_id: 任務ID
            
        Returns:
            bool: 是否成功恢復任務
        """
        if task_id not in self.task_ids:
            return False
            
        task = self.task_ids[task_id]
        return task.resume()
    
    def cancel_task(self, task_id):
        """取消下載任務
        
        Args:
            task_id: 任務ID
            
        Returns:
            bool: 是否成功取消任務
        """
        if task_id not in self.task_ids:
            return False
            
        task = self.task_ids[task_id]
        result = task.cancel()
        
        if result:
            url = task.url
            del self.tasks[url]
            del self.task_ids[task_id]
            
        return result
    
    def get_task_progress(self, task_id):
        """獲取任務進度
        
        Args:
            task_id: 任務ID
            
        Returns:
            dict: 包含進度信息的字典，如果任務不存在則返回None
        """
        if task_id not in self.task_ids:
            return None
            
        task = self.task_ids[task_id]
        return task.get_progress()
    
    def get_all_tasks(self):
        """獲取所有任務的ID和基本信息
        
        Returns:
            list: 包含所有任務基本信息的列表
        """
        result = []
        for task_id, task in self.task_ids.items():
            result.append({
                'id': task_id,
                'url': task.url,
                'filename': task.filename,
                'status': task.status,
                'progress': task.get_progress()
            })
        return result
    
    def set_save_dir(self, directory):
        """設置下載檔案的保存目錄
        
        Args:
            directory: 保存目錄的路徑
            
        Returns:
            bool: 是否成功設置保存目錄
        """
        print(f"嘗試設置保存目錄: {directory}")
        
        # 檢查路徑是否有效
        if not directory or not isinstance(directory, str):
            print("無效的目錄路徑")
            return False
        
        # 如果目錄不存在，嘗試創建
        if not os.path.exists(directory):
            try:
                print(f"目錄不存在，嘗試創建: {directory}")
                os.makedirs(directory, exist_ok=True)
            except (OSError, IOError) as e:
                print(f"創建目錄失敗: {e}")
                return False
                
        # 確認是目錄而非文件
        if not os.path.isdir(directory):
            print(f"路徑不是目錄: {directory}")
            return False
        
        # 檢查寫入權限
        try:
            print(f"檢查目錄寫入權限: {directory}")
            test_file = os.path.join(directory, '.download_test')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
        except (OSError, IOError) as e:
            print(f"目錄無寫入權限: {e}")
            return False
            
        print(f"成功設置保存目錄: {directory}")
        self.save_dir = directory
        
        # 添加到下載目錄集合中
        self.download_dirs.add(directory)
        
        # 保存配置
        self.save_config()
        
        return True
    
    def scan_unfinished_tasks(self):
        """掃描未完成的下載任務，從進度檔案中恢復
        
        Returns:
            int: 恢復的任務數量
        """
        count = 0
        print(f"掃描未完成的下載任務")
        print(f"當前保存目錄: {self.save_dir}")
        print(f"將掃描的目錄列表: {self.download_dirs}")
        
        # 掃描所有曾經使用過的下載目錄
        for directory in self.download_dirs:
            # 確保目錄存在
            if not os.path.exists(directory) or not os.path.isdir(directory):
                print(f"保存目錄不存在或不是目錄: {directory}")
                continue
            
            print(f"正在掃描目錄: {directory}")
            # 查找所有進度檔案
            progress_files = []
            try:
                for filename in os.listdir(directory):
                    if filename.endswith('.progress'):
                        progress_files.append(os.path.join(directory, filename))
            
                print(f"在目錄 {directory} 中找到 {len(progress_files)} 個進度檔案")
            except Exception as e:
                print(f"掃描目錄時出錯: {directory}, 錯誤: {e}")
                continue
            
            for progress_file in progress_files:
                try:
                    print(f"嘗試載入進度檔案: {progress_file}")
                    with open(progress_file, 'r') as f:
                        progress_data = json.load(f)
                        
                    url = progress_data['url']
                    if url in self.tasks:
                        print(f"URL已存在於任務列表中: {url}")
                        continue
                        
                    # 從進度文件中獲取保存目錄，如果不存在則使用當前掃描的目錄
                    task_save_dir = progress_data.get('save_dir', directory)
                    
                    # 確保任務的保存目錄存在
                    if not os.path.exists(task_save_dir):
                        print(f"任務的保存目錄不存在: {task_save_dir}，使用當前目錄: {directory}")
                        task_save_dir = directory
                    
                    # 將該目錄添加到下載目錄集合中
                    if os.path.exists(task_save_dir) and os.path.isdir(task_save_dir):
                        self.download_dirs.add(task_save_dir)
                    
                    # 從進度檔案獲取檔案名稱
                    basename = os.path.basename(progress_file)
                    default_filename = basename[:-9]  # 移除.progress後綴
                    
                    # 優先使用保存在進度檔案中的檔案名稱
                    filename = progress_data.get('filename', default_filename)
                    
                    print(f"創建下載任務: {filename}, URL: {url}, 保存目錄: {task_save_dir}")
                    task = DownloadTask(url, task_save_dir, filename)
                    if task.load_progress():
                        task_id = self.next_id
                        self.next_id += 1
                        
                        self.tasks[url] = task
                        self.task_ids[task_id] = task
                        count += 1
                        print(f"成功恢復任務 {count}: {filename} (目錄: {task_save_dir})")
                        
                        # 記錄任務狀態
                        status = task.status
                        progress = task.get_progress()
                        print(f"  任務狀態: {status}")
                        print(f"  下載進度: {progress['downloaded_size']}/{progress['total_size']} "
                              f"({progress['percentage']:.1f}%)")
                    else:
                        print(f"載入進度失敗: {filename}")
                        try:
                            os.remove(progress_file)
                            print(f"刪除無效進度檔案: {progress_file}")
                        except:
                            print(f"無法刪除無效進度檔案: {progress_file}")
                except Exception as e:
                    print(f"處理進度檔案時出錯: {progress_file}, 錯誤: {e}")
                    continue
        
        # 在完成掃描後保存配置，確保所有發現的目錄都被記錄
        self.save_config()
            
        print(f"總共恢復了 {count} 個未完成的下載任務")
        return count 
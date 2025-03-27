// 擴展程式啟動時初始化
chrome.runtime.onInstalled.addListener(() => {
  // 初始化存儲設置
  chrome.storage.sync.get(['enabled', 'serverUrl', 'cancelOriginalDownload'], (result) => {
    if (result.enabled === undefined) {
      chrome.storage.sync.set({ enabled: true });
    }
    if (result.serverUrl === undefined) {
      chrome.storage.sync.set({ serverUrl: 'http://localhost:8765' });
    }
    if (result.cancelOriginalDownload === undefined) {
      chrome.storage.sync.set({ cancelOriginalDownload: true });
    }
  });

  // 創建右鍵選單
  chrome.contextMenus.create({
    id: 'download-with-multisocks',
    title: '使用多代理下載器下載',
    contexts: ['link']
  });
});

// 處理右鍵選單點擊事件
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'download-with-multisocks') {
    sendDownloadRequest(info.linkUrl);
  }
});

// 追蹤已處理的下載，避免重複處理
const processedDownloads = new Set();
const pendingDownloads = new Map(); // 用於追蹤等待確定文件名的下載

// 監聽下載開始事件
chrome.downloads.onCreated.addListener(function(downloadItem) {
  console.log("檢測到下載開始:", downloadItem);
  
  // 檢查是否已處理過此下載
  if (processedDownloads.has(downloadItem.url)) {
    console.log("此下載已處理過，跳過:", downloadItem.url);
    return;
  }
  
  // 標記為已處理
  processedDownloads.add(downloadItem.url);
  
  // 將下載項目添加到待處理列表，等待 onDeterminingFilename 事件
  pendingDownloads.set(downloadItem.id, downloadItem);
  
  // 如果不是通過右鍵選單觸發的下載，可以直接發送請求
  // 但我們優先等待 onDeterminingFilename 事件獲取最終 URL
  setTimeout(() => {
    // 如果 5 秒後仍未通過 onDeterminingFilename 處理，則使用原始 URL
    if (pendingDownloads.has(downloadItem.id)) {
      console.log("等待超時，使用原始 URL:", downloadItem.url);
      sendDownloadRequest(downloadItem.url, downloadItem.id);
      pendingDownloads.delete(downloadItem.id);
    }
  }, 5000);
  
  // 清理已處理的下載記錄（防止內存泄漏）
  setTimeout(() => {
    processedDownloads.delete(downloadItem.url);
  }, 60000); // 1分鐘後清理
});

// 監聽下載文件名確定事件（此時可以獲取最終 URL）
chrome.downloads.onDeterminingFilename.addListener(function(downloadItem, suggest) {
  console.log("確定下載文件名:", downloadItem);
  
  // 檢查是否在待處理列表中
  if (pendingDownloads.has(downloadItem.id)) {
    console.log("獲取到最終下載 URL:", downloadItem.finalUrl || downloadItem.url);
    console.log("獲取到檔案名:", downloadItem.filename);
    
    // 使用最終 URL 和檔案名發送下載請求
    const finalUrl = downloadItem.finalUrl || downloadItem.url;
    sendDownloadRequest(finalUrl, downloadItem.id, downloadItem.filename);
    
    // 從待處理列表中移除
    pendingDownloads.delete(downloadItem.id);
  }
  
  // 繼續正常的文件名確定流程
  suggest();
});

// 監聽下載狀態變化
chrome.downloads.onChanged.addListener(function(downloadDelta) {
  if (downloadDelta.state) {
    console.log(`下載 ID ${downloadDelta.id} 狀態變更為: ${downloadDelta.state.current}`);
    
    // 如果下載完成，可以在這裡執行額外操作
    if (downloadDelta.state.current === 'complete') {
      console.log(`下載 ID ${downloadDelta.id} 已完成`);
    }
    
    // 如果下載失敗，可以在這裡處理錯誤
    if (downloadDelta.state.current === 'interrupted') {
      console.log(`下載 ID ${downloadDelta.id} 已中斷，原因: ${downloadDelta.error?.current || '未知'}`);
    }
  }
});

// 發送下載請求到本地應用
function sendDownloadRequest(url, downloadId = null, filename = null) {
  console.log("發送下載請求:", url, "檔案名:", filename);
  
  // 檢查是否需要取消原始下載
  chrome.storage.sync.get(['cancelOriginalDownload', 'serverUrl'], (result) => {
    const serverUrl = result.serverUrl || 'http://localhost:8765';
    
    fetch(`${serverUrl}/ping`, {
      method: 'GET'
    })
    .then(response => {
      if (!response.ok) {
        throw new Error('伺服器連接失敗');
      }
      return response.json();
    })
    .then(pingData => {
      console.log('伺服器連接成功:', pingData);
      
      // 發送下載請求
      return fetch(`${serverUrl}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ 
          url: url,
          downloadId: downloadId,
          filename: filename // 加入檔案名
        })
      });
    })
    .then(response => response.json())
    .then(data => {
      console.log('下載請求已發送:', data);
      
      // 如果設置為取消原始下載且有下載 ID
      if (result.cancelOriginalDownload && downloadId !== null) {
        chrome.downloads.cancel(downloadId, function() {
          console.log(`已取消原始下載 ID: ${downloadId}`);
        });
      }
    })
    .catch(error => {
      console.error('發送下載請求時出錯:', error);
    });
  });
} 
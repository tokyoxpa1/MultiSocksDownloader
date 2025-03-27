// 當彈出視窗載入時
document.addEventListener('DOMContentLoaded', () => {
  const enableToggle = document.getElementById('enableToggle');
  const cancelOriginalToggle = document.getElementById('cancelOriginalToggle');
  const serverUrlInput = document.getElementById('serverUrl');
  const connectionStatus = document.getElementById('connectionStatus');
  const testConnectionBtn = document.getElementById('testConnection');
  const saveSettingsBtn = document.getElementById('saveSettings');
  
  // 載入儲存的設置
  chrome.storage.sync.get(['enabled', 'serverUrl', 'cancelOriginalDownload'], (result) => {
    enableToggle.checked = result.enabled !== undefined ? result.enabled : true;
    cancelOriginalToggle.checked = result.cancelOriginalDownload !== undefined ? result.cancelOriginalDownload : true;
    serverUrlInput.value = result.serverUrl || 'http://localhost:8765';
    
    // 初始檢查連接狀態
    checkConnection(serverUrlInput.value);
  });
  
  // 切換啟用/禁用狀態
  enableToggle.addEventListener('change', () => {
    chrome.storage.sync.set({ enabled: enableToggle.checked });
    updateStatus(`下載攔截已${enableToggle.checked ? '啟用' : '禁用'}`);
  });
  
  // 切換取消原始下載狀態
  cancelOriginalToggle.addEventListener('change', () => {
    chrome.storage.sync.set({ cancelOriginalDownload: cancelOriginalToggle.checked });
    updateStatus(`取消 Chrome 原始下載已${cancelOriginalToggle.checked ? '啟用' : '禁用'}`);
  });
  
  // 測試連接按鈕
  testConnectionBtn.addEventListener('click', () => {
    const serverUrl = serverUrlInput.value.trim();
    if (!serverUrl) {
      updateStatus('請輸入有效的伺服器地址', 'error');
      return;
    }
    
    checkConnection(serverUrl);
  });
  
  // 儲存設置按鈕
  saveSettingsBtn.addEventListener('click', () => {
    const serverUrl = serverUrlInput.value.trim();
    if (!serverUrl) {
      updateStatus('請輸入有效的伺服器地址', 'error');
      return;
    }
    
    chrome.storage.sync.set({ 
      serverUrl: serverUrl,
      enabled: enableToggle.checked,
      cancelOriginalDownload: cancelOriginalToggle.checked
    }, () => {
      updateStatus('設置已儲存');
    });
  });
  
  // 檢查與應用程式的連接
  function checkConnection(serverUrl) {
    updateStatus('正在檢查連接...', 'checking');
    
    // 嘗試通過 Native Messaging 連接
    try {
      chrome.runtime.sendNativeMessage(
        'com.multisocks.downloader',
        { action: 'ping' },
        (response) => {
          if (chrome.runtime.lastError) {
            console.log('Native messaging 失敗，嘗試 HTTP 請求');
            checkHttpConnection(serverUrl);
          } else {
            updateStatus('已通過 Native Messaging 連接到應用程式', 'success');
          }
        }
      );
    } catch (error) {
      console.log('Native messaging 錯誤，嘗試 HTTP 請求');
      checkHttpConnection(serverUrl);
    }
  }
  
  // 通過 HTTP 檢查連接
  function checkHttpConnection(serverUrl) {
    fetch(`${serverUrl}/ping`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json'
      }
    })
    .then(response => {
      if (!response.ok) {
        throw new Error('HTTP 請求失敗');
      }
      return response.json();
    })
    .then(data => {
      updateStatus('已通過 HTTP 連接到應用程式', 'success');
    })
    .catch(error => {
      updateStatus('無法連接到多代理下載器應用程式', 'error');
      console.error('連接檢查失敗:', error);
    });
  }
  
  // 更新狀態顯示
  function updateStatus(message, type = 'info') {
    connectionStatus.textContent = message;
    
    // 根據狀態類型設置樣式
    connectionStatus.style.backgroundColor = {
      'info': '#f0f0f0',
      'success': '#d4edda',
      'error': '#f8d7da',
      'checking': '#fff3cd'
    }[type] || '#f0f0f0';
    
    connectionStatus.style.color = {
      'info': '#000',
      'success': '#155724',
      'error': '#721c24',
      'checking': '#856404'
    }[type] || '#000';
  }
}); 
#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import json
import shutil
import platform
import argparse
import winreg

# 擴展程式 ID，需要在擴展程式安裝後更新
DEFAULT_EXTENSION_ID = "EXTENSION_ID_PLACEHOLDER"

def get_chrome_version():
    """獲取 Chrome 版本"""
    try:
        if platform.system() == 'Windows':
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Google\Chrome\BLBeacon') as key:
                version = winreg.QueryValueEx(key, 'version')[0]
                return version
        return None
    except:
        return None

def get_host_path():
    """獲取主機程式的絕對路徑"""
    # 獲取當前腳本所在目錄的上一級目錄
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    host_path = os.path.join(parent_dir, 'native_messaging_host.py')
    
    # 確保路徑存在
    if not os.path.exists(host_path):
        print(f"錯誤: 找不到主機程式 {host_path}")
        sys.exit(1)
        
    return host_path

def get_host_manifest_path():
    """獲取主機清單文件的路徑"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'com.multisocks.downloader.json')

def get_native_messaging_dir():
    """獲取 Native Messaging 主機目錄"""
    if platform.system() == 'Windows':
        return os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Google', 'Chrome', 'User Data', 'NativeMessagingHosts')
    elif platform.system() == 'Darwin':  # macOS
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'Google', 'Chrome', 'NativeMessagingHosts')
    else:  # Linux
        return os.path.join(os.path.expanduser('~'), '.config', 'google-chrome', 'NativeMessagingHosts')

def update_manifest(extension_id):
    """更新主機清單文件"""
    manifest_path = get_host_manifest_path()
    host_path = get_host_path()
    
    # 讀取清單文件
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    
    # 更新路徑和擴展程式 ID
    manifest['path'] = host_path
    manifest['allowed_origins'] = [f"chrome-extension://{extension_id}/"]
    
    # 寫入更新後的清單文件
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)
        
    return manifest_path

def install_host(extension_id):
    """安裝 Native Messaging 主機"""
    # 更新清單文件
    manifest_path = update_manifest(extension_id)
    
    # 創建目標目錄
    target_dir = get_native_messaging_dir()
    os.makedirs(target_dir, exist_ok=True)
    
    # 複製清單文件到目標目錄
    target_path = os.path.join(target_dir, os.path.basename(manifest_path))
    shutil.copy2(manifest_path, target_path)
    
    print(f"已安裝 Native Messaging 主機到 {target_path}")
    print(f"主機程式路徑: {get_host_path()}")
    print(f"擴展程式 ID: {extension_id}")
    
    # 檢查 Chrome 版本
    chrome_version = get_chrome_version()
    if chrome_version:
        print(f"Chrome 版本: {chrome_version}")
    
    return True

def uninstall_host():
    """卸載 Native Messaging 主機"""
    target_dir = get_native_messaging_dir()
    target_path = os.path.join(target_dir, 'com.multisocks.downloader.json')
    
    if os.path.exists(target_path):
        os.remove(target_path)
        print(f"已卸載 Native Messaging 主機: {target_path}")
        return True
    else:
        print("Native Messaging 主機未安裝")
        return False

def main():
    parser = argparse.ArgumentParser(description='安裝或卸載多代理下載器 Native Messaging 主機')
    parser.add_argument('--uninstall', action='store_true', help='卸載主機')
    parser.add_argument('--extension-id', help='Chrome 擴展程式 ID')
    args = parser.parse_args()
    
    if args.uninstall:
        uninstall_host()
    else:
        extension_id = args.extension_id or DEFAULT_EXTENSION_ID
        if extension_id == "EXTENSION_ID_PLACEHOLDER":
            print("警告: 使用預設的擴展程式 ID，這可能無法正常工作")
            print("請在安裝擴展程式後，使用 --extension-id 參數指定正確的擴展程式 ID")
        install_host(extension_id)

if __name__ == '__main__':
    main() 
import pyautogui

def list_all_windows():
    """列出所有窗口的详细信息"""
    windows = pyautogui.getWindowsWithTitle('')
    
    print(f"共有 {len(windows)} 个窗口:\n")
    for i, window in enumerate(windows, 1):
        print(f"{i}. 标题: {window.title}")
        # print(f"   位置: ({window.left}, {window.top})")
        # print(f"   大小: {window.width} x {window.height}")
        # print(f"   可见: {window.visible}")
        # print(f"   活动: {window.isActive}")
        print("-" * 50)

list_all_windows()
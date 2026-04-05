import random
import pyautogui
import cv2
import numpy as np
import win32api
import time
import math
import os
from PIL import Image
import matplotlib.pyplot as plt


def capture_window_screenshot(window_title):
    """
    捕获指定窗口的截图
    :param window_title: 窗口标题
    :return: 截图的 PIL.Image 对象
    """
    # 获取窗口
    window = pyautogui.getWindowsWithTitle(window_title)
    if not window:
        raise Exception(f"未找到窗口: {window_title}")
    window = window[0]
    # window.activate()

    # 获取窗口位置和大小
    left, top, width, height = window.left, window.top, window.width, window.height

    # 截取窗口截图
    screenshot = pyautogui.screenshot(region=(left, top, width, height))
    return screenshot, left, top, width, height


def resize_template_to_fit_screenshot(template, screenshot_width, screenshot_height):
    # 获取模板图片的原始尺寸
    h, w = template.shape[:2]

    # 计算缩放比例
    scale_w = screenshot_width / w
    scale_h = screenshot_height / h
    scale = min(scale_w, scale_h, 1)  # 确保不放大

    # 计算新尺寸
    new_w = int(w * scale)
    new_h = int(h * scale)

    # 调整大小
    resized_template = cv2.resize(
        template, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return resized_template


def find_best_match(screenshot, class_folders, class_labels, threshold=0.75):
    """
    在窗口截图中查找三个类别中匹配度最高的区域，并返回匹配的类别名和模板尺寸
    :param screenshot: 窗口截图 (PIL.Image 对象)
    :param class_folders: 每个类别对应的文件夹路径列表
    :param class_labels: 每个类别的名称列表
    :param threshold: 匹配的最低阈值 (0~1)
    :return: 最佳匹配结果 (class_name, match_center, max_val, template_width, template_height)，未找到返回 None
    """
    # 转换截图为 OpenCV 格式
    screenshot_cv = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

    best_match = None
    max_similarity = 0
    candidate_matches = []
    # 遍历每个类别文件夹
    for folder, class_name in zip(class_folders, class_labels):
        # 遍历当前类别文件夹中的所有图片
        template_files = [f for f in os.listdir(
            folder) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp'))]

        for file_name in template_files:
            template_path = os.path.join(folder, file_name)

            # 读取模板图片
            template = cv2.imread(template_path, cv2.IMREAD_COLOR)
            # cv2.imshow('Template', template)
            if template is None:
                print(f"无法读取模板图片: {template_path}")
                continue

            original_h, original_w = template.shape[:2]
            if original_h > screenshot.height or original_w > screenshot.width:
                error_reason = f"原始尺寸过大 (原尺寸: {original_w}x{original_h}, 截图尺寸: {screenshot.width}x{screenshot.height})"
                print(error_reason)
                print(f"{class_name}/{file_name}", error_reason)

            # template = resize_template_to_fit_screenshot(template, screenshot.width, screenshot.height)

            result = cv2.matchTemplate(
                screenshot_cv, template, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

            # # 如果匹配度高于阈值并且是当前最佳匹配
            # if max_val >= threshold and max_val > max_similarity:
            #     max_similarity = max_val
            #     template_height, template_width = template.shape[:2]
            #     match_center = (
            #         max_loc[0] + template_width // 2,
            #         max_loc[1] + template_height // 2
            #     )
            #     best_match = (class_name, match_center, max_val, template_width, template_height)
            
            if max_val >= threshold:
                template_height, template_width = template.shape[:2]
                match_center = (
                    max_loc[0] + template_width // 2,
                    max_loc[1] + template_height // 2
                )
                candidate_matches.append(
                    (class_name, match_center, max_val, template_width, template_height))

                # 随机选择一个候选结果作为最佳匹配
        if candidate_matches:
            best_match = random.choice(candidate_matches)
                    
    return best_match



def bezier_curve(points, n=100):
    """
    生成贝塞尔曲线的路径点
    :param points: 控制点列表 [(x1, y1), (x2, y2), ...]
    :param n: 路径点数量
    :return: 路径点列表
    """
    points = np.array(points)
    t_values = np.linspace(0, 1, n)
    path = []

    for t in t_values:
        temp_points = points.copy()
        while len(temp_points) > 1:
            temp_points = [temp_points[i] * (1 - t) + temp_points[i + 1] * t for i in range(len(temp_points) - 1)]
        path.append(temp_points[0])
    return path

def move_mouse_human_like(end, duration):
    """
    模拟人类操作的鼠标移动
    :param start: 起点 (x, y)
    :param end: 终点 (x, y)
    :param duration: 总时长 (秒)
    """
    
    start = pyautogui.position()
    # 生成随机控制点，保证路径有一定弧度
    control1 = (start[0] + random.randint(-100, 100), start[1] + random.randint(-100, 100))
    control2 = (end[0] + random.randint(-100, 100), end[1] + random.randint(-100, 100))

    # 贝塞尔曲线路径
    path = bezier_curve([start, control1, control2, end], n=100)

    # 每个点的移动时间
    
    distance = np.linalg.norm(np.array(end) - np.array(start))
    duration = distance / 2000  # 速度为 500 像素/秒
    delay = duration / len(path)

    for point in path:
        win32api.SetCursorPos((int(point[0]), int(point[1])))
    # 模拟移动
    for point in path:
        # 添加微小随机偏移，模拟手的抖动
        jitter_x = random.uniform(-1, 1)
        jitter_y = random.uniform(-1, 1)
        win32api.SetCursorPos((int(point[0] + jitter_x), int(point[1] + jitter_y)))
        #pyautogui.moveTo(point[0] + jitter_x, point[1] + jitter_y, duration=0)
        time.sleep(max(delay + random.uniform(-0.001, 0.001),0))  # 随机微调停顿时间

def generate_random_point_in_circle(radius):
    """
    在圆心为原点，半径为指定值的圆内生成随机点
    :param radius: 圆的半径，默认为 10
    :return: (x, y) 圆内点的坐标
    """
    # 随机生成角度 θ，范围 [0, 2π]
    theta = random.uniform(0, 2 * math.pi)

    # 随机生成半径 r，范围 [0, radius]，使用平方根分布确保均匀分布
    r = random.uniform(0, 1) * radius

    # 转换为直角坐标
    x = r * math.cos(theta)
    y = r * math.sin(theta)
    return x, y


def class_process(class_name, width, hight, time_of_one):
    '''
    begin是开始按钮
    mvp是结算时候的那个经验界面
    end是掉东西的页面
    
    '''
    if class_name == "begin":
        x_shift, y_shift = generate_random_point_in_circle(radius=width/2)
        delay = np.random.uniform(time_of_one, time_of_one+3)
    elif class_name == "mvp":
        
        x_shift = np.random.uniform(-(width/2-width/20), width/2-width/20)# 窗口宽度的一半少一点
        y_shift = np.random.uniform(-(hight/2-hight/20), hight/2-hight/20)
        delay = np.random.uniform(1, 2)
    else:
        x_shift = np.random.uniform(-(width/2-width/20), width/2-width/20)# 窗口宽度的一半少一点
        y_shift = np.random.uniform(-(hight/2-hight/20), hight/2-hight/20)
        delay = np.random.uniform(2, 3)
    return x_shift, y_shift, delay


def go_random(x, y, xmin=0, xmax=1920, ymin=0, ymax=1080):
    dx = random.randint(-300, 300)
    dy = random.randint(-300, 300)

    new_x = max(xmin, min(x + dx, xmax))
    new_y = max(ymin, min(y + dy, ymax))

    return new_x, new_y


def move_mouse_and_click(window_title, myclass):
    """
    在指定窗口中查找模板图片并点击其中心点
    :param window_title: 窗口标题
    :param template_path: 模板图片路径
    """
    try:
        # 截取窗口截图
        screenshot, left, top, width, height = capture_window_screenshot(window_title)

        # 查找模板图片
        class_folders, class_labels, time_of_one = myclass
        match_center = find_best_match(screenshot, class_folders, class_labels, threshold=0.75)
        
        if match_center:
            #template_name, match_center, max_val = match_center
            class_name, match_center, max_val, template_width, template_height = match_center
            print(f"最佳匹配类别: {class_name}")
            print(f"匹配中心点坐标: {match_center}")
            print(f"匹配度: {max_val}")
            
            # 计算点击坐标
            x_shift, y_shift, delay = class_process(class_name, template_width, template_height, time_of_one)
            
            # 移动鼠标并点击
            # x_shift, y_shift = generate_random_point_in_circle(radius=width/20)
            x = match_center[0]+left+x_shift
            y = match_center[1]+top+y_shift
            global point_begin, point_mvp, point_end
            global list0, list1
            
            if window_title == mumu_title:
                if class_name == "begin":
                    list0["begin"].append((x, -y))
                elif class_name == "mvp":
                    list0["mvp"].append((x, -y))
                else:
                    list0["end"].append((x, -y))
            elif window_title == mumu_title_xb:
                if class_name == "begin":
                    list1["begin"].append((x, -y))
                elif class_name == "mvp":
                    list1["mvp"].append((x, -y))
                else:
                    list1["end"].append((x, -y))
                    
            # if class_name == "begin":
            #     point_begin.append((x, -y))
            # elif class_name == "mvp":
            #     point_mvp.append((x, -y))
            # else:
            #     point_end.append((x, -y))
            
            move_mouse_human_like((x, y), duration=0.1)
            
            time.sleep(0.05)
            pyautogui.click(x, y, button='left')
            time.sleep(0.05)
            print("点击坐标：", x, y)
            x_move, y_move = go_random(x, y)
            move_mouse_human_like((x_move, y_move), duration=0.1)
            # move_mouse_human_like((random.randint(0,1920), random.randint(0,1080)), duration=0.1)
            print("点击完成！")
            global count_yangyang, count_xiaobai, begin_count, end_count
            if class_name == "begin": #在这在这
                begin_count += 1
                end_count = 0
                if window_title == mumu_title:
                    count_yangyang += 1
                elif window_title == mumu_title_xb:
                    count_xiaobai += 1
                time.sleep(0.5)
            elif class_name == "end":
                end_count += 1
                begin_count = 0
            global err_count
            err_count = 0    
            
        else:
            print("未找到匹配区域！")
            global err
            err += 1
            time.sleep(0.5)
    except Exception as e:
        print(f"操作失败: {e}")
        time.sleep(1)


mumu_title = "MuMu模拟器枯条"  
mumu_title_xb = "小白"  
desk_title = "阴阳师-网易游戏"  

# 每个类别对应的文件夹，接一张对应的图片放到对应文件夹里，前面的路径改成当前文件夹
chi_folders = ["yys_auto/chi/begin", "yys_auto/chi/mvp", "yys_auto/chi/end"]  
chi_labels = ["begin", "mvp", "end"]  # 每个类别的名称
chi = (chi_folders, chi_labels, 21)

yuling_folders = ["yys_auto/yuling/begin", "yys_auto/yuling/mvp", "yys_auto/yuling/end"]  
yuling_labels = ["begin", "mvp", "end"]  # 每个类别的名称
yuling = (yuling_folders, yuling_labels, 17)

huntu_folders = ["yys_auto/huntu/begin", "yys_auto/huntu/mvp", "yys_auto/huntu/end"]  
huntu_labels = ["begin", "mvp", "end"]  # 每个类别的名称
huntu = (huntu_folders, huntu_labels, 15)

mult_play_folders = ["yys_auto/mult_play/begin", "yys_auto/mult_play/mvp", "yys_auto/mult_play/end"]  
mult_play_labels = ["begin", "mvp", "end"]  # 每个类别的名称
mult_play = (mult_play_folders, mult_play_labels, 15)

shit_folders = ["yys_auto/shit/begin",  "yys_auto/shit/end"]  
shit_labels = ["begin", "end"]  # 每个类别的名称
shit = (shit_folders, shit_labels, 10)

shit_fight_folders = ["yys_auto/shit_fight/begin",  "yys_auto/shit_fight/end"]  
shit_fight_labels = ["begin", "end"]  # 每个类别的名称
shit_fight = (shit_fight_folders, shit_fight_labels, 10)

huodong_folders = ["yys_auto/huodong/begin",  "yys_auto/huodong/end"]  
huodong_labels = ["begin", "end"]  # 每个类别的名称
huodong = (huodong_folders, huodong_labels, 10)

hunwang_folders = ["yys_auto/hunwang/begin",  "yys_auto/hunwang/end"]  
hunwang_labels = ["begin", "end"]  # 每个类别的名称
hunwang = (hunwang_folders, hunwang_labels, 10)

daozhang_folders = ["yys_auto/daozhang/begin",  "yys_auto/daozhang/end"]  
daozhang_labels = ["begin", "end"]  # 每个类别的名称
daozhang = (daozhang_folders, daozhang_labels, 10)

jiu_folders = ["yys_auto/jiu/begin",  "yys_auto/jiu/end"]  
jiu_labels = ["begin", "end"]  # 每个类别的名称
jiu = (jiu_folders, jiu_labels, 10)
jiu_xiao_folders = ["yys_auto/jiu_xiao/begin",  "yys_auto/jiu_xiao/end"]  
jiu_xiao_labels = ["begin", "end"]  # 每个类别的名称
jiu_xiao = (jiu_xiao_folders, jiu_xiao_labels, 10)

count_yangyang = 0
count_xiaobai = 0
err = 0
err_count = 0
begin_count = 0
end_count = 0
rest = random.randint(-10, 10) + 60
point_begin = []
point_mvp = []
point_end = []


list0 = {
    "begin": [],
    "end": [],
    "mvp": []
}

list1 = {
    "begin": [],
    "end": [],
    "mvp": []
}


limit_yangyang = 60
limit_xiaobai = 600

for i in range(100000):
    # if count_yangyang < limit_yangyang:
    #     move_mouse_and_click(mumu_title, shit_fight)
    # time.sleep(0.7)
    if count_xiaobai < limit_xiaobai:
        move_mouse_and_click(mumu_title_xb, huodong)
    time.sleep(0.3)
     
    if err > 20:
        move_mouse_human_like((random.randint(10,1910), random.randint(10,1070)), duration=0.1)
        err = 0
        err_count += 1
        if err_count > 3:
            print("错误次数过多，退出")
            break
    if begin_count > 10 or end_count > 10:
        print("卡住了，结束")
        break
        
    # print("阳阳已经打了", count_yangyang, "次")
    print("小白已经打了", count_xiaobai, "次")
    if count_xiaobai == rest:
        print("休息一会")
        for i in range(3):
            move_mouse_human_like((random.randint(0,1920), random.randint(0,1080)), duration=0.1)
            time.sleep(random.uniform(5, 10))
        rest = random.randint(45, 65)
    # if count_yangyang == limit_yangyang and count_xiaobai == limit_xiaobai:
    #     print("结束")
    #     break
    if count_xiaobai == limit_xiaobai:
        print("结束")
        break
    if count_yangyang == limit_yangyang:
        print("结束")
        break
    



styles = {
    "begin": {"color": "red",    "marker": "o"},
    "end":   {"color": "blue",   "marker": "s"},
    "mvp":   {"color": "green",  "marker": "D"}
}

# 绘制 list0 的散点图
plt.figure(figsize=(8, 6))
for key, points in list0.items():
    # 分离 x 和 y 坐标
    x_values = [p[0] for p in points]
    y_values = [p[1] for p in points]
    # 绘制散点
    plt.scatter(x_values, y_values, 
                color=styles[key]["color"],
                marker=styles[key]["marker"],
                label=key,
                s=10)
plt.title("list0")
plt.xlabel("X")
plt.ylabel("Y")
plt.legend()
plt.grid(True)
plt.show()

# 绘制 list1 的散点图
plt.figure(figsize=(8, 6))
for key, points in list1.items():
    x_values = [p[0] for p in points]
    y_values = [p[1] for p in points]
    plt.scatter(x_values, y_values,
                color=styles[key]["color"],
                marker=styles[key]["marker"],
                label=key,
                s=10)
plt.title("list1")
plt.xlabel("X")
plt.ylabel("Y")
plt.legend()
plt.grid(True)
plt.show()



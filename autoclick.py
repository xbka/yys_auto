import random
import pyautogui
import cv2
import numpy as np
import win32api
import time
import math
import os
import matplotlib.pyplot as plt
import threading
import queue
import keyboard

# ==========================================
# 全局控制区
# ==========================================
# 全局紧急停止信号灯。多线程环境下，通过设置此事件(Event)来安全、迅速地中断所有线程的循环。
global_stop_event = threading.Event()

# ==========================================
# 模块一：鼠标控制模块 (MouseController)
# 功能描述：处理所有和鼠标轨迹相关的纯数学与系统底层调用，提供高度拟人化的鼠标移动和坐标偏移。
# ==========================================
class MouseController:
    @staticmethod
    def bezier_curve(points, n=100):
        """
        生成贝塞尔曲线的路径点。
        :param points: 控制点列表，包含起点、控制点和终点。
        :param n: 生成的路径点数量。数量越多，轨迹越平滑。
        :return: 包含路径坐标的列表。
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

    @staticmethod
    def move_human_like(end_pos, duration=None):
        """
        模拟人类操作的鼠标移动，强制执行慢速拟人化。
        :param end_pos: 目标坐标 (x, y)。
        :param duration: 占位参数，实际已被内部算法接管以防止外部误传导致瞬移。
        """
        start_pos = pyautogui.position()
        
        # 随机生成两个控制点，限制在 1920x1080 屏幕范围内，使鼠标轨迹呈现自然的弧度
        ctrl1_x = max(0, min(1920, start_pos[0] + random.randint(-100, 100)))
        ctrl1_y = max(0, min(1080, start_pos[1] + random.randint(-100, 100)))
        ctrl2_x = max(0, min(1920, end_pos[0] + random.randint(-100, 100)))
        ctrl2_y = max(0, min(1080, end_pos[1] + random.randint(-100, 100)))

        # 生成 50 个节点的贝塞尔曲线路径
        path = MouseController.bezier_curve([start_pos, (ctrl1_x, ctrl1_y), (ctrl2_x, ctrl2_y), end_pos], n=50)
        
        # 计算起点到终点的直线欧几里得距离
        distance = np.linalg.norm(np.array(end_pos) - np.array(start_pos))
        
        # 核心防检测逻辑：基于像素距离动态计算移动时间
        # 设定人类拖动鼠标的速度范围 (每秒划过 1800 到 2000 像素)
        pixels_per_second = random.uniform(2000, 2500) 
        
        # 设定人类看到按钮后的神经反应停顿时间 (0.1 到 0.2 秒)
        reaction_time = random.uniform(0.1, 0.2)
        
        # 真实总耗时 = 移动距离 / 移动速度 + 反应时间
        actual_duration = (distance / pixels_per_second) + reaction_time
            
        # 计算每个路径点之间需要停顿的时间
        delay = actual_duration / len(path)

        for point in path:
            # 引入微小的像素级随机抖动 (Jitter)
            jitter_x = random.uniform(-1, 1)
            jitter_y = random.uniform(-1, 1)
            # 使用 win32api 进行底层鼠标位移，规避部分上层检测
            win32api.SetCursorPos((int(point[0] + jitter_x), int(point[1] + jitter_y)))
            # 保证延时时间不为负数
            time.sleep(max(delay + random.uniform(-0.003, 0.003), 0))

    @staticmethod
    def generate_random_point_in_circle(radius):
        """在圆心为原点，指定半径的圆内均匀生成随机坐标点。"""
        theta = random.uniform(0, 2 * math.pi)
        r = math.sqrt(random.uniform(0, 1)) * radius 
        return r * math.cos(theta), r * math.sin(theta)

    @staticmethod
    def go_random(x, y, xmin=0, xmax=1920, ymin=0, ymax=1080):
        """生成随机偏移的新坐标，并确保不会超出屏幕边界。"""
        dx = random.randint(-300, 300)
        dy = random.randint(-300, 300)
        return max(xmin, min(x + dx, xmax)), max(ymin, min(y + dy, ymax))


# ==========================================
# 模块二：鼠标执行单线程 (MouseWorker)
# 功能描述：作为多线程架构中唯一的消费者(Consumer)。从任务队列中获取坐标并执行物理鼠标操作，
#           彻底解决多线程同时抢夺鼠标控制权导致的指针乱跳问题。
# ==========================================
class MouseWorker(threading.Thread):
    def __init__(self, task_queue):
        super().__init__()
        self.task_queue = task_queue
        self.daemon = True # 设置为守护线程，主程序结束时自动销毁

    def run(self):
        print("[鼠标线程] 启动成功，正在监听点击队列...")
        while not global_stop_event.is_set():
            try:
                # 阻塞最多 0.5 秒获取任务。
                # 设定 timeout 的目的是防止死锁，使线程有机会在抛出 queue.Empty 异常时
                # 进入下一次循环，从而检查 global_stop_event 是否被触发。
                task = self.task_queue.get(timeout=0.5)
            except queue.Empty:
                continue
                
            if task is None: 
                # 接收到主线程发送的毒药丸(None)，安全退出循环
                self.task_queue.task_done()
                break
                
            try:
                task_type = task.get("type")
                
                if task_type == "click":
                    bot = task["bot"]
                    x, y = int(task["x"]), int(task["y"])
                    x_move, y_move = int(task["x_move"]), int(task["y_move"])
                    class_name = task["class_name"]

                    # 移动到目标并点击
                    MouseController.move_human_like((x, y))
                    time.sleep(0.05)
                    pyautogui.click(x, y, button='left')
                    print(f"[{bot.window_title}] [队列执行] 点击坐标: ({x}, {y})")

                    # 点击完成后随机将鼠标移开，防止遮挡下一帧的图像识别
                    MouseController.move_human_like((x_move, y_move))
                    
                    # 触发回调函数，通知发出任务的 Bot 更新其计次状态
                    bot.on_click_success(class_name, x, y) 
                    
                elif task_type == "move_only":
                    # 仅移动任务，常用于防卡死的随机活动
                    x, y = int(task["x"]), int(task["y"])
                    MouseController.move_human_like((x, y))

            except Exception as e:
                print(f"[鼠标线程] 执行任务出错: {e}")
            finally:
                # 无论任务成功与否，必须通知队列该任务已处理完毕
                self.task_queue.task_done()
                
        print("[鼠标线程] 收到全局停止信号或结束指令，安全退出。")


# ==========================================
# 模块三：模板匹配模块 (TemplateManager)
# 功能描述：负责在初始化时一次性将模板图片加载至内存，并在运行时支持根据模拟器窗口大小动态缩放比对。
# ==========================================
class TemplateManager:
    def __init__(self, task_config):
        self.folders, self.labels, self.time_of_one = task_config
        self.templates = {}  
        self._load_templates_to_memory()

    def _load_templates_to_memory(self):
        """
        初始化加载逻辑。将硬盘中的图片转为 numpy 数组读入内存。
        使用 cv2.imdecode 代替 cv2.imread 以完美支持包含中文的路径。
        """
        total_loaded = 0
        for folder, class_name in zip(self.folders, self.labels):
            self.templates[class_name] = []
            abs_folder = os.path.abspath(folder)
            if not os.path.exists(abs_folder):
                print(f"[警告] 找不到文件夹: {abs_folder}")
                continue
                
            for file_name in os.listdir(abs_folder):
                if file_name.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp')):
                    template_path = os.path.join(abs_folder, file_name)
                    try:
                        template_img = cv2.imdecode(np.fromfile(template_path, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if template_img is not None:
                            self.templates[class_name].append((file_name, template_img))
                            total_loaded += 1
                    except Exception as e:
                        print(f"[异常] 加载图片失败: {e}")
                        pass

    def find_best_match(self, screenshot_cv, scale=1.0, threshold=0.75):
        """
        在截图中寻找匹配度最高的特征。
        :param screenshot_cv: 当前屏幕的 OpenCV 格式截图。
        :param scale: 动态缩放比。计算公式为: 当前窗口宽度 / 基准截图宽度。
        :param threshold: 匹配阈值 (0.0 到 1.0)。
        """
        candidate_matches = []
        for class_name, img_list in self.templates.items():
            for file_name, template_img in img_list:
                # 核心逻辑：如果当前窗口被拉伸，则等比例缩放内存中的模板图片后再进行匹配
                if scale != 1.0:
                    new_w = int(template_img.shape[1] * scale)
                    new_h = int(template_img.shape[0] * scale)
                    if new_w == 0 or new_h == 0:
                        continue
                    # 缩小图片用 INTER_AREA 算法，放大图片用 INTER_LINEAR 算法，以保证图像质量
                    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                    current_template = cv2.resize(template_img, (new_w, new_h), interpolation=interpolation)
                else:
                    current_template = template_img

                # 容错：防止因缩放导致模板尺寸大于截图尺寸而引发 OpenCV 报错
                if current_template.shape[0] > screenshot_cv.shape[0] or current_template.shape[1] > screenshot_cv.shape[1]:
                    continue

                result = cv2.matchTemplate(screenshot_cv, current_template, cv2.TM_CCOEFF_NORMED)
                min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

                if max_val >= threshold:
                    template_height, template_width = current_template.shape[:2]
                    match_center = (
                        max_loc[0] + template_width // 2,
                        max_loc[1] + template_height // 2
                    )
                    candidate_matches.append((class_name, match_center, max_val, template_width, template_height))

        # 若存在多个符合阈值的匹配项，随机选择一个以增加不可预测性
        if candidate_matches:
            return random.choice(candidate_matches)
        return None


# ==========================================
# 模块四：自动点击机器人 (AutoClickerBot)
# 功能描述：作为多线程架构中的生产者(Producer)。每个实例负责独立监控一个游戏窗口，
#           发现目标后仅将坐标任务打包送入共享队列，自身绝不直接控制鼠标。
# ==========================================
class AutoClickerBot:
    def __init__(self, window_title, task_config, limit, baseline_width, shared_queue): 
        self.window_title = window_title
        self.limit = limit
        self.baseline_width = baseline_width 
        self.shared_queue = shared_queue 
        
        self.template_manager = TemplateManager(task_config)
        
        # 状态机计数器
        self.action_count = 0
        self.err = 0
        self.err_count = 0
        self.begin_count = 0
        self.end_count = 0
        
        # 设定下一次进行防封随机休息的阈值
        self.next_rest_count = random.randint(-10, 10) + 60
        
        # 用于图表绘制的历史坐标记录
        self.click_history = {"begin": [], "mvp": [], "end": []}

    def capture_window(self):
        """定位目标窗口并进行屏幕截图。"""
        windows = pyautogui.getWindowsWithTitle(self.window_title)
        if not windows:
            raise Exception(f"未找到窗口: {self.window_title}")
        window = windows[0]
        
        # 如果窗口被最小化，则强制恢复并置顶激活，确保能截取到实际画面
        if window.isMinimized:
            window.restore()
        window.activate()
        
        left, top, width, height = window.left, window.top, window.width, window.height
        screenshot = pyautogui.screenshot(region=(left, top, width, height))
        return screenshot, left, top, width, height

    def calculate_shift(self, class_name, width, height):
        """根据识别到的按钮类别，采用不同的算法计算拟人化的点击偏移量。"""
        if class_name == "begin":
            # 对于圆形按钮，在半径范围内生成均匀分布的随机落点
            x_shift, y_shift = MouseController.generate_random_point_in_circle(radius=width/2)
        else: 
            # 对于方形按钮，在中心矩形区域内生成随机落点，避开极度边缘
            x_shift = np.random.uniform(-(width/2-width/20), width/2-width/20)
            y_shift = np.random.uniform(-(height/2-height/20), height/2-height/20)
        return x_shift, y_shift

    def process_match(self, match_result, win_left, win_top):
        """处理匹配成功的逻辑：计算实际屏幕坐标并将任务放入执行队列。"""
        class_name, match_center, max_val, template_width, template_height = match_result
        print(f"[{self.window_title}] [识别成功] 类别: {class_name} | 匹配度: {max_val:.2f} -> 已加入队列")

        x_shift, y_shift = self.calculate_shift(class_name, template_width, template_height)
        x = match_center[0] + win_left + x_shift
        y = match_center[1] + win_top + y_shift
        x_move, y_move = MouseController.go_random(x, y)
        
        # 构造任务载荷并存入全局队列
        task_payload = {
            "type": "click",
            "bot": self, # 将当前实例指针传入，方便鼠标线程回调更新状态
            "class_name": class_name,
            "x": x, "y": y,
            "x_move": x_move, "y_move": y_move
        }
        self.shared_queue.put(task_payload)
        
        # 发现目标后清空错误计数
        self.err = 0 
        self.err_count = 0
        
        # 设置动作冷却时间(Cooldown)，避免在画面过渡期高频触发重复点击
        cooldown_time = 3.0 if class_name == "begin" else 1.0
        
        print(f"[{self.window_title}] [动作冷却] 等待游戏画面过渡，暂停检测 {cooldown_time} 秒...")
        
        # 将冷却时间切分为 0.1 秒的片段，确保能随时响应全局停止按键
        for _ in range(int(cooldown_time * 10)):
            if global_stop_event.is_set():
                break
            time.sleep(0.1)

    def on_click_success(self, class_name, x, y):
        """回调函数：由 MouseWorker 线程在执行完毕后调用，负责更新当前 Bot 的业务状态。"""
        if class_name in self.click_history:
            self.click_history[class_name].append((x, -y))

        if class_name == "begin":
            self.begin_count += 1
            self.end_count = 0
            self.action_count += 1
            print(f"[{self.window_title}] [执行统计] 成功点击！当前已执行: {self.action_count} 次")
        elif class_name == "end":
            self.end_count += 1
            self.begin_count = 0

    def run_step(self):
        """执行单次完整的截图、缩放比例计算及模板匹配流程。"""
        try:
            screenshot, left, top, width, height = self.capture_window()
            screenshot_cv = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            
            # 计算当前窗口缩放比例
            current_scale = width / self.baseline_width
            
            match_result = self.template_manager.find_best_match(
                screenshot_cv, scale=current_scale, threshold=0.75
            )

            if match_result:
                self.process_match(match_result, left, top)
            else:
                self.err += 1

        except Exception as e:
            print(f"[{self.window_title}] 操作异常: {e}")

    def run(self):
        """视觉扫描主循环。"""
        while self.action_count < self.limit and not global_stop_event.is_set():
            self.run_step()
            
            # 基础扫描间隔，使用切片化睡眠响应停止信号
            for _ in range(5):
                if global_stop_event.is_set():
                    break
                time.sleep(0.1)

            # 连续多次未找到目标，触发防卡死移动逻辑
            if self.err > 20:
                self.shared_queue.put({
                    "type": "move_only",
                    "x": random.randint(10, 1910), "y": random.randint(10, 1070)
                })
                self.err = 0
                self.err_count += 1
                if self.err_count > 3:
                    print(f"[{self.window_title}] 连续错误次数过多，线程即将退出。")
                    break
            
            # 异常状态判定
            if self.begin_count > 10 or self.end_count > 10:
                print(f"[{self.window_title}] 画面疑似卡住，线程即将退出。")
                break
                
            # 到达指定阈值，执行防封机制的长时间随机休息
            if self.action_count >= self.next_rest_count:
                print(f"[{self.window_title}] [防封机制] 触发随机休息...")
                for _ in range(3):
                    if global_stop_event.is_set():
                        break
                    self.shared_queue.put({
                        "type": "move_only",
                        "x": random.randint(10, 1910), "y": random.randint(10, 1070)
                    })
                    time.sleep(random.uniform(5, 10))
                # 重新规划下一次休息的执行次数
                self.next_rest_count = self.action_count + random.randint(45, 65)

        print(f"[{self.window_title}] 扫描任务完成或已被手动停止。")

    def plot_history(self):
        """绘制该账号本次运行的点击坐标分布图。"""
        styles = {
            "begin": {"color": "red", "marker": "o"},
            "end": {"color": "blue", "marker": "s"},
            "mvp": {"color": "green", "marker": "D"}
        }
        plt.figure(figsize=(8, 6))
        for key, points in self.click_history.items():
            if not points:
                continue
            x_values = [p[0] for p in points]
            y_values = [p[1] for p in points]
            plt.scatter(x_values, y_values, color=styles[key]["color"], marker=styles[key]["marker"], label=key, s=10)
        
        plt.title(f"Click Distribution: {self.window_title}")
        plt.xlabel("X")
        plt.ylabel("Y (Negative for display)")
        plt.legend()
        plt.grid(True)
        plt.show()

# ==========================================
# 模块五：入口文件 (多线程启动中心)
# 功能描述：初始化队列资源，配置挂机账号参数，统筹并启动所有工作线程。
# ==========================================
if __name__ == "__main__":
    
    # 绑定紧急停止快捷键
    def emergency_stop():
        print("\n" + "="*40)
        print("[警告] 触发紧急停止！正在安全切断所有线程...")
        print("="*40 + "\n")
        global_stop_event.set() 
        
    keyboard.add_hotkey('f12', emergency_stop)
    print(">>> 提示：脚本已启动！随时可按下 F12 键紧急停止。 <<<")
    
    start_time = time.time()
    
    # 1. 创建全局唯一的点击任务共享队列
    master_queue = queue.Queue()
    
    # 2. 启动鼠标专职消费线程
    mouse_thread = MouseWorker(master_queue)
    mouse_thread.start()
    
    # ================= 配置区 =================
    # 请确保相对路径正确无误
    daozhang_folders = ["yys_auto/daozhang/begin", "yys_auto/daozhang/end"]
    daozhang_labels = ["begin", "end"]
    daozhang_config = (daozhang_folders, daozhang_labels, 10)

    # 3. 实例化你的各个游戏号的 Bot
    # 第一开账号设置
    xiaobai_bot = AutoClickerBot(
        window_title="小白", 
        task_config=daozhang_config, 
        limit=500,
        baseline_width=977,
        shared_queue=master_queue
    )
    
    # 第二开账号设置
    yangyang_bot = AutoClickerBot(
        window_title="枯条", 
        task_config=daozhang_config, 
        limit=0,
        baseline_width=977,
        shared_queue=master_queue
    )
    
    # 4. 为每个 Bot 创建并启动它的“眼睛”扫描线程
    t_xiaobai = threading.Thread(target=xiaobai_bot.run)
    t_yangyang = threading.Thread(target=yangyang_bot.run)
    
    t_xiaobai.start()
    t_yangyang.start()
    
    # 5. 等待所有视觉扫描任务完成。只要有一个号还在跑，主线程就继续循环等待，
    #    同时保持对键盘快捷键事件的响应能力。
    while t_xiaobai.is_alive() or t_yangyang.is_alive():
        time.sleep(0.5)
    
    # 6. Bot 任务全部结束，给鼠标消费线程发送毒药丸(None)以安全销毁
    master_queue.put(None)
    mouse_thread.join()
    
    # 7. 记录结束时间，计算总耗时并格式化输出
    end_time = time.time()
    total_seconds = end_time - start_time
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    print("\n" + "="*40)
    print(f"所有脚本执行完毕！")
    print(f"本次总运行时间: {int(hours)} 小时 {int(minutes)} 分钟 {seconds:.2f} 秒")
    print("="*40 + "\n")
    
    print("即将绘制点击分布图...")
    
    # 8. 绘图展示 (关闭前一个图表后，会自动弹出下一个)
    xiaobai_bot.plot_history()
    yangyang_bot.plot_history()
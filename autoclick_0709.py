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
# [核心逻辑] 全局紧急停止信号灯。多线程环境下，通过设置此事件(Event)来安全、迅速地中断所有线程的循环。
global_stop_event = threading.Event()


# ==========================================
# 工具函数：基准图自适应模块  
# ==========================================
def get_baseline_width(parent_folder, default_width=977):
    """
    读取特定任务父目录下的 baseline 全图（支持 png/jpg/jpeg/bmp）,自动获取并返回其宽度。
    若文件不存在或读取失败，则返回默认宽度，保障程序不会因缺图而崩溃。
    """
    abs_folder = os.path.abspath(parent_folder)
    
    # 1. 检查父文件夹是否存在
    if not os.path.exists(abs_folder):
        print(f"[系统] 未找到文件夹 {abs_folder}，将使用默认宽度 {default_width}")
        return default_width

    # 2. 定义支持的图片后缀名字典 (元组)
    valid_extensions = ('.png', '.jpg', '.jpeg', '.bmp')
    baseline_path = None
    
    # 3. 动态搜索匹配格式的 baseline 文件
    for file_name in os.listdir(abs_folder):
        # 统一转小写进行判断，防止出现 Baseline.JPG 这种大小写混用导致漏判
        if file_name.lower().startswith('baseline') and file_name.lower().endswith(valid_extensions):
            baseline_path = os.path.join(abs_folder, file_name)
            break  # 找到第一张符合的就跳出循环
            
    # 4. 如果遍历完都没找到
    if not baseline_path:
        print(f"[系统] 未在 {abs_folder} 中找到 baseline 图片，将使用默认宽度 {default_width}")
        return default_width
        
    try:
        # 使用 cv2.imdecode 而非 cv2.imread，以完美兼容包含中文的绝对路径
        img = cv2.imdecode(np.fromfile(baseline_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            height, width = img.shape[:2]
            print(f"[系统] 成功读取基准截图 {baseline_path}，自动设置 baseline_width = {width} px")
            return width
        else:
            print(f"[警告] 基准截图损坏，将使用默认宽度 {default_width}")
            return default_width
    except Exception as e:
        print(f"[异常] 读取基准截图出错: {e}，将使用默认宽度 {default_width}")
        return default_width


# ==========================================
# 模块一：鼠标控制模块 (MouseController)
# 功能描述：处理所有和鼠标轨迹相关的纯数学与系统底层调用，动态适配屏幕边界。
# ==========================================
class MouseController:
    @staticmethod
    def bezier_curve(points, n=100):
        """
        [核心逻辑] 生成贝塞尔曲线的路径点，用于模拟人类鼠标移动的平滑弧线。
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
    def move_human_like(end_pos, screen_config, duration=None):
        """
        [核心逻辑] 模拟人类操作的鼠标移动，强制执行慢速拟人化，并严格限制在配置的屏幕范围内。
        """
        start_pos = pyautogui.position()
        
        # 动态获取当前分配给该 Bot 的屏幕物理边界
        s_min_x, s_max_x = screen_config["xmin"], screen_config["xmax"]
        s_min_y, s_max_y = screen_config["ymin"], screen_config["ymax"]

        # 随机生成两个控制点，限制在当前屏幕范围内，防止轨迹越界
        ctrl1_x = max(s_min_x, min(s_max_x, start_pos[0] + random.randint(-100, 100)))
        ctrl1_y = max(s_min_y, min(s_max_y, start_pos[1] + random.randint(-100, 100)))
        ctrl2_x = max(s_min_x, min(s_max_x, end_pos[0] + random.randint(-100, 100)))
        ctrl2_y = max(s_min_y, min(s_max_y, end_pos[1] + random.randint(-100, 100)))

        # 生成 50 个节点的平滑路径
        path = MouseController.bezier_curve([start_pos, (ctrl1_x, ctrl1_y), (ctrl2_x, ctrl2_y), end_pos], n=50)
        distance = np.linalg.norm(np.array(end_pos) - np.array(start_pos))
        
        # [配置项] 鼠标移动速度与反应时间微调区
        # pixels_per_second: 设定鼠标每秒划过的像素距离，值越小移动越慢
        pixels_per_second = random.uniform(2000, 2500) 
        # reaction_time: 设定识别目标后，鼠标起步前的停顿时间 (秒)
        reaction_time = random.uniform(0.1, 0.2)
        
        actual_duration = (distance / pixels_per_second) + reaction_time
        delay = actual_duration / len(path)

        for point in path:
            # 引入微小的像素级随机抖动 (Jitter) 增强拟真度
            jitter_x = random.uniform(-1, 1)
            jitter_y = random.uniform(-1, 1)
            # 使用 win32api 底层位移规避上层检测
            win32api.SetCursorPos((int(point[0] + jitter_x), int(point[1] + jitter_y)))
            time.sleep(max(delay + random.uniform(-0.003, 0.003), 0))

    @staticmethod
    def generate_random_point_in_circle(radius):
        """在圆心为原点，指定半径的圆内均匀生成随机坐标点。"""
        theta = random.uniform(0, 2 * math.pi)
        r = math.sqrt(random.uniform(0, 1)) * radius 
        return r * math.cos(theta), r * math.sin(theta)

    @staticmethod
    def go_random(x, y, screen_config):
        """生成随机偏移的新坐标，常用于点击后的鼠标移开动作，确保不超出所在屏幕边界。"""
        xmin, xmax = screen_config["xmin"], screen_config["xmax"]
        ymin, ymax = screen_config["ymin"], screen_config["ymax"]
        
        dx = random.randint(-300, 300)
        dy = random.randint(-300, 300)
        return max(xmin, min(x + dx, xmax)), max(ymin, min(y + dy, ymax))


# ==========================================
# 模块二：鼠标执行单线程 (MouseWorker)
# 功能描述：多线程架构中的唯一消费者，接收队列任务执行点击，防止多端抢夺鼠标。
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
                # 阻塞最多 0.5 秒获取任务。设定 timeout 可防止死锁，
                # 让线程有机会进入下一次循环检查 global_stop_event 是否被触发。
                task = self.task_queue.get(timeout=0.5)
            except queue.Empty:
                continue
                
            if task is None: 
                # 接收到主线程发送的毒药丸(None)，确认所有任务已派发完毕，安全退出
                self.task_queue.task_done()
                break
                
            try:
                task_type = task.get("type")
                screen_config = task.get("screen_config") # 提取该任务对应的屏幕边界限制
                
                if task_type == "click":
                    bot = task["bot"]
                    x, y = int(task["x"]), int(task["y"])
                    x_move, y_move = int(task["x_move"]), int(task["y_move"])
                    class_name = task["class_name"]

                    # 1. 拟人化移动到目标
                    MouseController.move_human_like((x, y), screen_config)
                    time.sleep(0.05) # 点击前的微小停顿
                    
                    # 2. 执行物理点击
                    pyautogui.click(x, y, button='left')
                    print(f"[{bot.window_title}] [队列执行] 点击坐标: ({x}, {y})")

                    # 3. 点击完成后随机将鼠标移开，防止遮挡下一帧的图像识别
                    MouseController.move_human_like((x_move, y_move), screen_config)
                    
                    # 4. 触发回调，通知发出任务的 Bot 更新计数
                    bot.on_click_success(class_name, x, y) 
                    
                elif task_type == "move_only":
                    # 仅移动任务，常用于防封/防卡死的随机活动
                    x, y = int(task["x"]), int(task["y"])
                    MouseController.move_human_like((x, y), screen_config)

            except Exception as e:
                print(f"[鼠标线程] 执行任务出错: {e}")
            finally:
                # 无论任务成功与否，必须通知队列该任务已处理完毕
                self.task_queue.task_done()
                
        print("[鼠标线程] 收到全局停止信号或结束指令，安全退出。")


# ==========================================
# 模块三：模板匹配模块 (TemplateManager)
# 功能描述：内存预加载所有模板图，并支持运行时根据窗口大小动态缩放比对。
# ==========================================
class TemplateManager:
    def __init__(self, task_config):
        self.folders, self.labels = task_config
        self.templates = {}  
        # [新增] 缓存机制：记录已缩放的模板，避免每帧重复计算 resize 耗费 CPU
        self.cached_templates = {}
        self.current_scale = None
        self._load_templates_to_memory()

    def _load_templates_to_memory(self):
        """初始化加载逻辑：将硬盘中的图片一次性转为 numpy 数组读入内存，提升运行期匹配速度。"""
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
                    except Exception:
                        pass

    def find_best_match(self, screenshot_cv, scale=1.0, threshold=0.75):
        """
        [核心逻辑] 在截图中寻找匹配度最高的特征。
        scale 参数允许当游戏窗口被拉伸缩放时，代码动态缩放内存中的模板进行适配。
        """
        # [优化] 判定缩放比例是否变化。若未变化则直接使用缓存的模板列表，极大节省计算资源。
        if scale != self.current_scale:
            self.cached_templates = {}
            for class_name, img_list in self.templates.items():
                self.cached_templates[class_name] = []
                for file_name, template_img in img_list:
                    if scale != 1.0:
                        new_w = int(template_img.shape[1] * scale)
                        new_h = int(template_img.shape[0] * scale)
                        if new_w == 0 or new_h == 0:
                            continue
                        # 缩小图片用 INTER_AREA，放大用 INTER_LINEAR，保证图像质量
                        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                        resized = cv2.resize(template_img, (new_w, new_h), interpolation=interpolation)
                        self.cached_templates[class_name].append(resized)
                    else:
                        self.cached_templates[class_name].append(template_img)
            self.current_scale = scale
            print(f"[系统] 检测到窗口比例调整，已重新生成并缓存模板缓存(当前缩放: {scale:.4f})")

        candidate_matches = []
        for class_name, img_list in self.cached_templates.items():
            for current_template in img_list:
                # 容错：防止因缩放导致模板尺寸大于截图尺寸而引发 OpenCV 崩溃
                if current_template.shape[0] > screenshot_cv.shape[0] or current_template.shape[1] > screenshot_cv.shape[1]:
                    continue

                # 执行模板匹配算法
                result = cv2.matchTemplate(screenshot_cv, current_template, cv2.TM_CCOEFF_NORMED)
                min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

                # [配置项] threshold=0.75 为匹配阈值，若误触高可调大(如0.85)，若漏判高可调小(如0.65)
                if max_val >= threshold:
                    template_height, template_width = current_template.shape[:2]
                    match_center = (
                        max_loc[0] + template_width // 2,
                        max_loc[1] + template_height // 2
                    )
                    candidate_matches.append((class_name, match_center, max_val, template_width, template_height))

        # 若同一画面中存在多个符合阈值的匹配项，随机选择一个以增加不可预测性
        if candidate_matches:
            return random.choice(candidate_matches)
        return None


# ==========================================
# 模块四：自动点击机器人 (AutoClickerBot)
# 功能描述：独立监控指定的模拟器窗口，完成视觉扫描后不直接操作鼠标，而是向队列派发任务。
# ==========================================
class AutoClickerBot:
    def __init__(self, window_title, task_config, limit, baseline_width, shared_queue, screen_config): 
        self.window_title = window_title
        self.limit = limit
        self.baseline_width = baseline_width 
        self.shared_queue = shared_queue 
        
        # 绑定该实例的运行屏幕边界
        self.screen_config = screen_config
        
        self.template_manager = TemplateManager(task_config)

        # [新增] 线程锁：防止 MouseWorker 线程与 Bot 扫描线程同时读写状态属性导致的冲突
        self.lock = threading.Lock()
        
        # 状态机计数器
        self.action_count = 0
        self.err = 0
        self.err_count = 0
        self.begin_count = 0
        self.end_count = 0
        
        # [新增] 运行时间统计属性
        self.last_begin_time = None
        self.durations = []
        
        # [逻辑优化] 增加“初次运行”标记
        self.is_first_run = True
        
        # 防封机制：设定下一次触发长时间休眠的随机执行次数
        self.next_rest_count = random.randint(-10, 10) + 60
        
        # 用于图表绘制的历史坐标记录
        self.click_history = {"begin": [], "mvp": [], "end": []}

    def capture_window(self, force_activate=False):
        """定位目标窗口并进行屏幕截图。增加 force_activate 参数控制是否强制激活窗口。"""
        windows = pyautogui.getWindowsWithTitle(self.window_title)
        if not windows:
            raise Exception(f"未找到窗口: {self.window_title}")
        window = windows[0]
        
        # [核心优化] 仅在满足特定条件（首次或识别失败过多）时执行激活
        if force_activate:
            # 如果窗口被最小化，则强制恢复并置顶激活，确保能够正常截取画面
            if window.isMinimized:
                window.restore()
            try:
                window.activate()
                print(f"[{self.window_title}] 触发按需激活：提升窗口至前台")
            except Exception as e:
                print(f"[{self.window_title}] 激活失败（通常不影响截图）：{e}")
        
        left, top, width, height = window.left, window.top, window.width, window.height
        screenshot = pyautogui.screenshot(region=(left, top, width, height))
        return screenshot, left, top, width, height

    def calculate_shift(self, class_name, width, height):
        """根据识别到的按钮类别，采用不同的算法计算拟人化的点击偏移量。"""
        if class_name == "begin":
            # 针对开始按钮(圆形)，在半径范围内生成均匀分布的随机落点
            x_shift, y_shift = MouseController.generate_random_point_in_circle(radius=width/2)
        else: 
            # 针对结束等方形按钮，在中心矩形区域内生成随机落点，并主动避开极度边缘地带
            x_shift = np.random.uniform(-(width/2-width/20), width/2-width/20)
            y_shift = np.random.uniform(-(height/2-height/20), height/2-height/20)
        return x_shift, y_shift

    def process_match(self, match_result, win_left, win_top):
        """处理匹配成功的逻辑：计算实际屏幕物理坐标，并将任务打包推入执行队列。"""
        class_name, match_center, max_val, template_width, template_height = match_result
        print(f"[{self.window_title}] [识别成功] 类别: {class_name} | 匹配度: {max_val:.2f} -> 已加入队列")

        x_shift, y_shift = self.calculate_shift(class_name, template_width, template_height)
        x = match_center[0] + win_left + x_shift
        y = match_center[1] + win_top + y_shift
        x_move, y_move = MouseController.go_random(x, y, self.screen_config)
        
        # 构造任务载荷
        task_payload = {
            "type": "click",
            "bot": self, # 将当前实例指针传入，方便工作线程回调更新状态
            "class_name": class_name,
            "x": x, "y": y,
            "x_move": x_move, "y_move": y_move,
            "screen_config": self.screen_config
        }
        self.shared_queue.put(task_payload)
        
        # 发现目标后清空连续错误计数
        self.err = 0 
        self.err_count = 0
        
        # [配置项] 动作冷却时间 (Cooldown) 微调区
        # 根据点击的不同类别，设定点击后的休眠扫描时间，避免画面未切换导致的高频重复点击
        cooldown_time = 3.0 if class_name == "begin" else 1.0
        print(f"[{self.window_title}] [动作冷却] 等待游戏画面过渡，暂停检测 {cooldown_time} 秒...")
        
        # 将长段冷却时间切分为 0.1 秒的片段，确保挂机休眠期也能瞬间响应 F12 停止按键
        for _ in range(int(cooldown_time * 10)):
            if global_stop_event.is_set():
                break
            time.sleep(0.1)

    def on_click_success(self, class_name, x, y):
        """回调函数：由 MouseWorker 线程在执行完鼠标操作后调用，用于更新当前 Bot 的业务进度。"""
        # [优化] 修改共享状态属性前加锁，保证线程安全
        with self.lock:
            if class_name in self.click_history:
                self.click_history[class_name].append((x, -y))

            if class_name == "begin":
                # [新增] 记录本轮耗时并更新全局平均耗时
                current_time = time.time()
                if self.last_begin_time is not None:
                    duration = current_time - self.last_begin_time
                    self.durations.append(duration)
                    
                    # [核心优化] 计算已完成所有轮次的平均耗时
                    avg_duration = sum(self.durations) / len(self.durations)
                    
                    # 使用平均时间计算预计剩余完成时间
                    remaining_count = max(0, self.limit - self.action_count - 1)
                    est_time_sec = avg_duration * remaining_count
                    
                    # 格式化预计剩余时间 (总秒数 -> 时, 分, 秒)
                    rem_m, rem_s = divmod(est_time_sec, 60)
                    rem_h, rem_m = divmod(rem_m, 60)
                    
                    print(f"[{self.window_title}] [效率监控] 本轮耗时: {duration:.1f}秒 | "
                          f"平均耗时: {avg_duration:.1f}秒 | "
                          f"预估剩余完成时间: {int(rem_h)}时{int(rem_m)}分{int(rem_s)}秒")
                
                self.last_begin_time = current_time
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
            # [核心优化] 决定本次循环是否需要激活窗口
            # 条件：1. 脚本刚开始运行 2. 识别失败次数正好达到 15 次（预判可能被遮挡）
            should_activate = False
            if self.is_first_run:
                should_activate = True
                self.is_first_run = False
            elif self.err == 15:
                should_activate = True

            screenshot, left, top, width, height = self.capture_window(force_activate=should_activate)
            screenshot_cv = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            
            # 计算当前窗口宽度的缩放比例
            current_scale = width / self.baseline_width
            
            match_result = self.template_manager.find_best_match(
                screenshot_cv, scale=current_scale, threshold=0.75
            )

            if match_result:
                self.process_match(match_result, left, top)
            else:
                self.err += 1
                if self.err % 10 == 0: # 每连续 10 次未找到目标，输出一次警告日志
                    print(f"[{self.window_title}] 扫描中... 未找到匹配目标 (连续失败 {self.err} 次)")

        except Exception as e:
            # [修正] 针对未找到窗口等异常，如果是 limit=0 本就不该跑到这里，逻辑已在 run() 头部拦截
            print(f"[{self.window_title}] 操作异常: {e}")

    def run(self):
        """视觉扫描主循环。"""
        # [新增逻辑] 若执行次数设定为 0，代表本次不需要运行该账号，直接停止并退出线程
        if self.limit == 0:
            print(f"[{self.window_title}] 检测到执行次数为 0，该账号本次任务跳过。")
            return

        # 生成防卡死乱滑时的内缩安全边界，防止触碰屏幕绝对边缘
        safe_xmin = self.screen_config["xmin"] + 10
        safe_xmax = self.screen_config["xmax"] - 10
        safe_ymin = self.screen_config["ymin"] + 10
        safe_ymax = self.screen_config["ymax"] - 10

        while not global_stop_event.is_set():
            # [优化] 读取共享属性前加锁，避免复合操作非原子性导致的逻辑误判
            with self.lock:
                current_action_count = self.action_count
                current_begin_count = self.begin_count
                current_end_count = self.end_count
                current_rest_trigger = self.next_rest_count

            if current_action_count >= self.limit:
                break

            self.run_step()
            
            # 基础扫描间隔，使用切片化睡眠
            for _ in range(5):
                if global_stop_event.is_set():
                    break
                time.sleep(0.1)

            # 异常处理 1：连续多次未找到目标，触发防卡死的纯鼠标乱滑移动逻辑
            if self.err > 20:
                self.shared_queue.put({
                    "type": "move_only",
                    "x": random.randint(safe_xmin, safe_xmax), 
                    "y": random.randint(safe_ymin, safe_ymax),
                    "screen_config": self.screen_config
                })
                self.err = 0
                self.err_count += 1
                # 连续触发卡死乱滑超过 3 次，判定为彻底掉线或迷失，终止当前线程
                if self.err_count > 3:
                    print(f"[{self.window_title}] 连续错误次数过多，线程即将退出。")
                    break
            
            # 异常处理 2：同一个按钮连续识别点击超过 10 次，判定为画面死机或网络断开
            if current_begin_count > 10 or current_end_count > 10:
                print(f"[{self.window_title}] 画面疑似卡住，线程即将退出。")
                break
                
            # 防封处理：执行次数到达设定阈值，强制执行长时间随机休眠模拟真人走神
            if current_action_count >= current_rest_trigger:
                print(f"[{self.window_title}] [防封机制] 触发随机休息...")
                for _ in range(3):
                    if global_stop_event.is_set():
                        break
                    self.shared_queue.put({
                        "type": "move_only",
                        "x": random.randint(safe_xmin, safe_xmax), 
                        "y": random.randint(safe_ymin, safe_ymax),
                        "screen_config": self.screen_config
                    })
                    time.sleep(random.uniform(5, 10))
                # 重新规划下一次休息的触发次数 (当前次数 + 随机偏移)
                with self.lock:
                    self.next_rest_count = self.action_count + random.randint(45, 65)

        print(f"[{self.window_title}] 扫描任务完成或已被手动停止。")

    def plot_history(self):
        """任务结束后，利用 Matplotlib 绘制该账号本次运行的点击落点散点图。"""
        # [优化] 如果没有产生点击历史，不执行绘图
        if not any(self.click_history.values()):
            return

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
# 模块五：主程序入口与统一配置中心
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
    
    # ==========================================
    # [经常修改] 核心控制面板
    # ==========================================
    
    # 1. 屏幕物理边界定义字典
    # 请确保此处的分辨率和多屏相对位置与你的 Windows 系统显示设置一致
    SCREENS = {
        "main": {"xmin": 0, "xmax": 1920, "ymin": 0, "ymax": 1080},         # 主屏
        "left_sub": {"xmin": -1920, "xmax": 0, "ymin": 0, "ymax": 1080},    # 左侧副屏
        "right_sub": {"xmin": 1920, "xmax": 3840, "ymin": 0, "ymax": 1080}  # 右侧副屏
    }
    
    # 2. 挂机任务定义与图片路径配置
    # 结构：(文件夹路径列表, 对应标签列表)
    task1_dir = "yys_auto/chi"
    task1_config = (
        [os.path.join(task1_dir, "begin"), os.path.join(task1_dir, "end")],
        ["begin", "end"]
    )
    # 自动读取该任务目录下的 baseline.png 尺寸作为缩放基准
    task1_baseline_width = get_baseline_width(task1_dir, default_width=977)

    # 3. 游戏账号挂机实例构建
    # 如果增加新的模拟器多开，只需按格式复制粘贴新的实例即可
    
    # 账号1
    xiaobai_bot = AutoClickerBot(
        window_title="小白",                    # [配置项] 必须完全匹配模拟器窗口的标题名称
        task_config=task1_config,             # [配置项] 绑定上方定义的业务任务
        limit=0,                            # [配置项] 执行成功多少次后自动停止
        baseline_width=task1_baseline_width, 
        shared_queue=master_queue,
        screen_config=SCREENS["main"]     # [配置项] 指定该模拟器所在的物理屏幕
    )
    
    # 账号2
    yangyang_bot = AutoClickerBot(
        window_title="枯条", 
        task_config=task1_config, 
        limit=200,                              # [配置项] 执行次数设为 0，则该账号不运行
        baseline_width=task1_baseline_width, 
        shared_queue=master_queue,
        screen_config=SCREENS["main"]     # [配置项] 指定该模拟器所在的物理屏幕
    )
    
    # ================= 控制面板结束 =================
    
    # 4. 为每个处于激活状态的 Bot 创建并启动它的“眼睛”扫描线程
    t_xiaobai = threading.Thread(target=xiaobai_bot.run)
    t_yangyang = threading.Thread(target=yangyang_bot.run)
    
    t_xiaobai.start()
    t_yangyang.start()
    
    # 5. 等待所有视觉扫描任务完成 (持续检测任一线程的存活状态)
    while t_xiaobai.is_alive() or t_yangyang.is_alive():
        time.sleep(0.5)
    
    # 6. Bot 任务全部结束，向工作队列发送终止指令以安全销毁鼠标线程
    master_queue.put(None)
    mouse_thread.join()
    
    # 7. 记录结束时间并格式化输出时长
    end_time = time.time()
    total_seconds = end_time - start_time
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    print("\n" + "="*40)
    print(f"所有脚本执行完毕！")
    print(f"本次总运行时间: {int(hours)} 小时 {int(minutes)} 分钟 {seconds:.2f} 秒")
    print("="*40 + "\n")
    
    print("即将绘制点击分布图...")
    
    # 8. 绘图展示 (注：关闭前一个窗口后才会弹出下一个窗口)
    xiaobai_bot.plot_history()
    yangyang_bot.plot_history()
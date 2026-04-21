# 🚀 Fund-Insight 部署到 PythonAnywhere 超详细教程

## 📋 前置准备
- ✅ 你已经有 PythonAnywhere 账号了
- ✅ 本地的 fund-insight 项目文件完整
- ✅ 已经看完了前面的内容，知道账号密码不能随便给人！

---

## 第一步：上传项目文件到 PythonAnywhere

### 方法一：直接上传（推荐新手）

1. **登录 PythonAnywhere**
   - 打开浏览器访问：https://www.pythonanywhere.com
   - 点击右上角 "Log in" 登录你的账号

2. **打开文件管理界面**
   - 登录后点击顶部的 **"Files"** 标签
   - 你会看到你的主目录（类似 `/home/你的用户名/`）

3. **创建项目文件夹**
   - 在页面左侧的 "Directories" 区域，点击 **"New directory"**
   - 输入名字：`fund-insight`，然后点 "Create"
   - 点击刚创建的 `fund-insight` 文件夹进入

4. **上传文件**
   - 在页面右侧的 "Files" 区域，点击 **"Upload a file"**
   - 逐个上传你本地 `fund-insight` 文件夹里的所有文件（注意：是文件夹里的内容，不是整个文件夹）
   - 必须上传的文件：
     - `app/` 文件夹（整个上传）
     - `scripts/` 文件夹（整个上传）
     - `requirements.txt`
     - `wsgi.py`（刚创建的那个）
     - `README.md`
     - `.env`（如果有的话）

### 方法二：用 Git 上传（如果你项目在 GitHub 上）

1. **打开 Bash 控制台**
   - 点击顶部的 **"Consoles"** 标签
   - 点击 **"Bash"** 创建一个新的控制台

2. **克隆项目**
   ```bash
   cd ~
   git clone https://github.com/你的用户名/你的仓库名.git fund-insight
   cd fund-insight
   ```

---

## 第二步：创建虚拟环境并安装依赖

1. **还是在 Bash 控制台里**（如果刚才关了，就重新开一个）

2. **进入项目目录**
   ```bash
   cd ~/fund-insight
   ```

3. **创建虚拟环境**
   ```bash
   mkvirtualenv fund-insight --python=/usr/bin/python3.10
   ```

4. **激活虚拟环境**
   ```bash
   workon fund-insight
   ```

5. **安装依赖包**
   ```bash
   pip install -r requirements.txt
   ```
   （这一步可能要等几分钟，耐心点！）

---

## 第三步：配置 Web 应用

1. **打开 Web 配置页面**
   - 点击顶部的 **"Web"** 标签

2. **创建新 Web 应用**
   - 点击 **"Add a new web app"**
   - 如果有弹窗提示，点击 "Next"
   - 选择 **"Manual configuration"**（手动配置，很重要！）
   - 选择 **Python 3.10**
   - 点击 "Next" 完成创建

3. **配置 Web 应用**
   现在你在 Web 配置页面，往下滚动，找到以下配置项：

   - **Source code:** 点击输入框，改成：`/home/你的用户名/fund-insight`
   - **Working directory:** 改成：`/home/你的用户名/fund-insight`
   - **Virtualenv:** 改成：`/home/你的用户名/.virtualenvs/fund-insight`

4. **配置 WSGI 文件**
   - 在 "Code" 区域找到 **"WSGI configuration file"**，点击那个链接（类似 `/var/www/你的用户名_pythonanywhere_com_wsgi.py`）
   - 会打开一个文件编辑器
   - **把里面所有内容都删掉**，然后粘贴下面的代码：

   ```python
   import sys
   import os

   # 替换成你的用户名！
   YOUR_USERNAME = "你的用户名"

   path = f'/home/{YOUR_USERNAME}/fund-insight'
   if path not in sys.path:
       sys.path.append(path)

   from a2wsgi import ASGIMiddleware
   from app.main import app

   application = ASGIMiddleware(app)
   ```

   ⚠️ **重要**：把上面的 `你的用户名` 替换成你 PythonAnywhere 的实际用户名！

   - 点击右上角的 **"Save"** 保存文件

---

## 第四步：启动！

1. **回到 Web 配置页面**
   - 点击顶部的 **"Web"** 标签

2. **重载 Web 应用**
   - 点击页面顶部那个大大的绿色按钮 **"Reload 你的用户名.pythonanywhere.com"**

3. **访问你的应用！**
   - 在浏览器里打开：`http://你的用户名.pythonanywhere.com`
   - 应该就能看到 Fund-Insight 的界面了！

---

## 🎉 完成了！

现在你的 Fund-Insight 已经 24 小时在线了！用手机、平板、任何设备都能访问！

---

## 💡 常见问题

### Q: 免费版会不会休眠？
A: 会！免费版如果 24 小时没人访问会休眠。解决办法：
   - 你可以每天访问一次
   - 或者用 UptimeRobot 之类的免费监控服务定时访问

### Q: 数据库会保存吗？
A: 会！SQLite 数据库文件会自动保存在项目目录里，不会丢失

### Q: 怎么更新代码？
A: 重新上传文件，然后在 Web 页面点 "Reload" 就行

### Q: 出错了怎么办？
A: 在 Web 配置页面的 "Log files" 区域看错误日志，有问题随时喊我！

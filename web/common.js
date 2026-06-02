/**
 * Fund Insight 公共脚本
 * 提取自 cleanup-manager.html、article-crawler.html、viewpoint-manager.html
 */

/**
 * HTML 转义函数 - 防止 XSS 注入
 * @param {string} str - 要转义的字符串
 * @returns {string} 转义后的安全字符串
 */
function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

/**
 * 显示消息提示
 * @param {string} text - 消息文本
 * @param {string} type - 消息类型：'success' | 'error' | 'warning'
 */
function showMessage(text, type = 'success') {
    const msg = document.getElementById('message');
    if (!msg) {
        console.warn('未找到 message 元素');
        return;
    }
    msg.textContent = text;
    msg.className = `message ${type}`;
    msg.style.display = 'block';
    setTimeout(() => {
        msg.style.display = 'none';
    }, 3000);
}

/**
 * 切换内容展开/折叠
 * @param {string} id - 内容元素的 ID
 */
function toggleContent(id) {
    const contentDiv = document.getElementById(`content-${id}`);
    const icon = document.getElementById(`icon-${id}`);
    const btnText = document.getElementById(`btn-text-${id}`);

    if (!contentDiv || !icon || !btnText) {
        console.warn(`未找到元素: content-${id}, icon-${id}, btn-text-${id}`);
        return;
    }

    if (contentDiv.classList.contains('collapsed')) {
        contentDiv.classList.remove('collapsed');
        icon.className = 'ri-arrow-up-s-line';
        btnText.textContent = '收起';
    } else {
        contentDiv.classList.add('collapsed');
        icon.className = 'ri-arrow-down-s-line';
        btnText.textContent = '展开全文';
    }
}

/**
 * 格式化日期
 * @param {string} dateStr - 日期字符串
 * @returns {string} 格式化后的日期
 */
function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) return dateStr;
    return date.toLocaleDateString('zh-CN');
}

/**
 * 格式化数字
 * @param {number} num - 数字
 * @param {number} decimals - 小数位数
 * @returns {string} 格式化后的数字
 */
function formatNumber(num, decimals = 0) {
    if (num === null || num === undefined) return '-';
    return Number(num).toFixed(decimals);
}

/**
 * 防抖函数
 * @param {Function} func - 要防抖的函数
 * @param {number} wait - 等待时间（毫秒）
 * @returns {Function} 防抖后的函数
 */
function debounce(func, wait = 300) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * 确认对话框（替代原生 confirm）
 * @param {string} message - 确认消息
 * @returns {Promise<boolean>} 用户选择结果
 */
function confirmDialog(message) {
    return new Promise((resolve) => {
        // 如果已有确认框，不重复创建
        if (document.getElementById('confirm-dialog')) {
            resolve(false);
            return;
        }

        const dialog = document.createElement('div');
        dialog.id = 'confirm-dialog';
        dialog.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.5);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 10000;
        `;

        dialog.innerHTML = `
            <div style="
                background: white;
                padding: 25px;
                border-radius: 12px;
                box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
                max-width: 400px;
                width: 90%;
                text-align: center;
            ">
                <p style="margin-bottom: 20px; font-size: 16px; color: #333;">${message}</p>
                <div style="display: flex; gap: 10px; justify-content: center;">
                    <button id="confirm-cancel" class="btn btn-secondary" style="min-width: 80px;">取消</button>
                    <button id="confirm-ok" class="btn btn-primary" style="min-width: 80px;">确定</button>
                </div>
            </div>
        `;

        document.body.appendChild(dialog);

        const cancelBtn = document.getElementById('confirm-cancel');
        const okBtn = document.getElementById('confirm-ok');

        const cleanup = () => {
            document.body.removeChild(dialog);
        };

        cancelBtn.onclick = () => {
            cleanup();
            resolve(false);
        };

        okBtn.onclick = () => {
            cleanup();
            resolve(true);
        };

        // 点击背景关闭
        dialog.onclick = (e) => {
            if (e.target === dialog) {
                cleanup();
                resolve(false);
            }
        };
    });
}

/**
 * 复制文本到剪贴板
 * @param {string} text - 要复制的文本
 * @returns {Promise<boolean>} 是否成功
 */
async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        showMessage('已复制到剪贴板');
        return true;
    } catch (err) {
        // 降级方案
        const textArea = document.createElement('textarea');
        textArea.value = text;
        textArea.style.position = 'fixed';
        textArea.style.left = '-9999px';
        document.body.appendChild(textArea);
        textArea.select();
        try {
            document.execCommand('copy');
            showMessage('已复制到剪贴板');
            return true;
        } catch (e) {
            showMessage('复制失败', 'error');
            return false;
        } finally {
            document.body.removeChild(textArea);
        }
    }
}

/**
 * 发送带认证的请求
 * @param {string} url - 请求 URL
 * @param {object} options - axios 配置选项
 * @returns {Promise} axios 响应
 */
async function authRequest(url, options = {}) {
    const password = localStorage.getItem('access_password');
    if (password) {
        options.headers = options.headers || {};
        options.headers['X-Access-Password'] = password;
    }
    return axios(url, options);
}

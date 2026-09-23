(function () {
    'use strict';

    window.createViewpointManager = function createViewpointManager(options) {
        const { axios, ref, reactive, computed, localStorage, alert, confirm } = options;
        const viewpoints = options.viewpoints || ref([]);
        const analyzing = options.analyzing || ref(false);
        const onStatsChanged = options.onStatsChanged;
        const viewpointMeta = reactive({ page: 1, page_size: 20, total: 0, pages: 0 });
        const viewpointFilters = reactive({
            keyword: '', source: '', market_direction: '', analysis_status: '',
            date_from: '', date_to: '', viewpoint_type: '', page: 1, page_size: 20,
        });
        const viewpointInsights = ref({ directions: {}, sector_consensus: [], source_quality: {}, pending_summary: [] });
        const viewpointTask = ref(null);
        const viewpointDetail = options.viewpointDetail || ref(null);
        const showViewpointDetail = options.showViewpointDetail || ref(false);
        const summarizing = ref(false);
        const summaryStats = ref(null);
        const showSummaryConfirmModal = ref(false);
        const sourceMenuOpen = ref(false);
        const selectedSources = reactive({ sina_blog: true, stock_guba: true, fund_guba: true });
        // 抓取可选来源（后端 ALLOWED_SOURCES）
        const fetchSourceOptions = [
            { value: 'sina_blog', label: '新浪博客' },
            { value: 'stock_guba', label: '热门股吧' },
            { value: 'fund_guba', label: '热门基金吧' },
        ];
        // 筛选下拉保留历史来源标签，兼容库内旧数据
        const sourceOptions = [
            ...fetchSourceOptions,
            { value: 'eastmoney_blog', label: '东方财富博客' },
            { value: 'eastmoney_guide', label: '东方财富导读' },
            { value: 'eastmoney_news', label: '东方财富快讯' },
            { value: 'sina_finance', label: '新浪财经' },
        ];
        const taskRunning = computed(() => ['pending', 'running'].includes(viewpointTask.value?.status));
        let pollTimer = null;

        // FastAPI 的 422 把 detail 写成数组（[{loc, msg}, ...]），直接插进模板就变成
        // "[object Object]"，等于把唯一的报错线索吞掉（第 15 轮 MINOR-4）。
        const errorMessage = (error) => {
            const d = error.response?.data?.detail;
            if (Array.isArray(d)) return d.map(x => ((x.loc || []).slice(1).join('.') || 'body') + '：' + x.msg).join('；');
            return d || error.response?.data?.message || error.message;
        };
        const selectedSourceList = () => fetchSourceOptions
            .map((item) => item.value)
            .filter((value) => selectedSources[value]);
        const fetchViewpoints = async () => {
            const params = {
                page: viewpointFilters.page,
                page_size: viewpointFilters.page_size,
                keyword: viewpointFilters.keyword.trim() || undefined,
                source: viewpointFilters.source || undefined,
                market_direction: viewpointFilters.market_direction || undefined,
                analysis_status: viewpointFilters.analysis_status || undefined,
                date_from: viewpointFilters.date_from || undefined,
                date_to: viewpointFilters.date_to || undefined,
                viewpoint_type: viewpointFilters.viewpoint_type || undefined,
            };
            // 与 posts/predictions 同一条规矩：筛选与翻页直连这里，不经过 `loadView` 的兜底，
            // 所以三种形状都得自己说一句话（第 34 轮 A-MAJOR-4 / B-MAJOR-4）。
            const report = (msg) => { if (options.onFetchFailure) options.onFetchFailure('viewpoints', msg); };
            try {
                const response = await axios.get('/api/viewpoints', { params });
                if (response.data.success) {
                    viewpoints.value = response.data.data || [];
                    Object.assign(viewpointMeta, response.data.meta || {});
                    report('');
                } else {
                    report('观点列表没取到：' + (response.data.message || '接口未给出原因'));
                }
            } catch (error) {
                report('观点列表拉取失败：' + (options.isServiceDown && options.isServiceDown(error)
                    ? '服务连不上（可能在唤醒）' : '接口报错'));
                throw error;
            }
        };
        const insightsLoaded = ref(false);
        const insightsError = ref('');
        const fetchInsights = async () => {
            // 这个端点单独失败时，四张卡以前全渲染 0（表体却有数据 ⇒ "0 条观点看多"是编的）
            // 失败还要把上一轮的真数一起清掉：红字说"没取到"、卡上却挂着 3/2/0，一样是假话
            const forget = () => { viewpointInsights.value = {}; insightsLoaded.value = false; };
            try {
                const response = await axios.get('/api/viewpoints/insights');
                if (response.data.success && response.data.data) {
                    viewpointInsights.value = response.data.data;
                    insightsLoaded.value = true;
                    insightsError.value = '';
                } else {
                    forget();
                    insightsError.value = '观点洞察没取到：' + (response.data.message || '接口未给出原因');
                }
            } catch (error) {
                forget();
                insightsError.value = '观点洞察拉取失败：' + (options.isServiceDown && options.isServiceDown(error)
                    ? '服务连不上（可能在唤醒）' : '接口报错');
                throw error;
            }
        };
        const fetchLatestTask = async () => {
            const response = await axios.get('/api/viewpoints/tasks/latest');
            viewpointTask.value = response.data.data;
            return viewpointTask.value;
        };
        const loadViewpoints = async () => {
            // 三笔各自报各自的（一起 `Promise.all` 会让"最新任务"失败被说成"观点拉取失败"，主语错）
            await Promise.all([fetchViewpoints(), fetchInsights().catch(() => null),
                               fetchLatestTask().catch(() => null)]);
            if (taskRunning.value) pollTask(viewpointTask.value.task_id);
        };
        const applyViewpointFilters = async () => { viewpointFilters.page = 1; await fetchViewpoints(); };
        const resetViewpointFilters = async () => {
            Object.assign(viewpointFilters, {
                keyword: '', source: '', market_direction: '', analysis_status: '',
                date_from: '', date_to: '', viewpoint_type: '', page: 1,
            });
            await fetchViewpoints();
        };
        // 同 `post-manager.js`：失败就把页码退回去，别拿「第 N 页」配第 N-1 页的数据
        const viewpointPrevPage = async () => {
            if (viewpointFilters.page <= 1) return;
            const back = viewpointFilters.page; viewpointFilters.page -= 1;
            try { await fetchViewpoints(); } catch (error) { viewpointFilters.page = back; }
        };
        const viewpointNextPage = async () => {
            if (viewpointFilters.page >= viewpointMeta.pages) return;
            const back = viewpointFilters.page; viewpointFilters.page += 1;
            try { await fetchViewpoints(); } catch (error) { viewpointFilters.page = back; }
        };
        const clearPoll = () => {
            if (pollTimer) window.clearTimeout(pollTimer);
            pollTimer = null;
            localStorage.removeItem('viewpoint_task_id');
        };
        const MAX_POLL_FAILURES = 90;   // 10 秒一次 ≈ 15 分钟
        const pollTask = async (taskId, attempts = 0) => {
            if (pollTimer) window.clearTimeout(pollTimer);
            try {
                const latest = await fetchLatestTask();
                if (latest && options.onPollRecovered) options.onPollRecovered('观点汇总');
                if (!latest || latest.task_id !== taskId || ['succeeded', 'failed', 'cancelled'].includes(latest.status)) {
                    clearPoll();
                    analyzing.value = false;
                    await Promise.all([fetchViewpoints(), fetchInsights()]);
                    return;
                }
                localStorage.setItem('viewpoint_task_id', String(taskId));
                pollTimer = window.setTimeout(() => pollTask(taskId), 3000);
            } catch (error) {
                // 与 `post-manager.js` 同一条规矩：只有 404（任务确实不在了）才丢句柄；
                // 401/403/500 与唤醒期的网络错留着句柄限次再问，停手也不删。
                if (error.response && error.response.status === 404) {
                    clearPoll();
                    analyzing.value = false;
                } else if (attempts < MAX_POLL_FAILURES) {
                    pollTimer = window.setTimeout(() => pollTask(taskId, attempts + 1), 10000);
                    return;
                } else {
                    // 同 `post-manager.js`：数到上限要放开 `analyzing` 那把全局锁，
                    // 否则 13 个按钮永久灰掉且没有任何解释（第 32 轮 A-M1）。
                    analyzing.value = false;
                    if (options.onPollStalled) options.onPollStalled('观点汇总');
                }
                console.error('观点任务轮询失败', error);
            }
        };
        const restoreViewpointTask = () => {
            const taskId = localStorage.getItem('viewpoint_task_id');
            if (taskId) pollTask(Number(taskId));
        };
        const fetchLatestViewpoints = async () => {
            const sources = selectedSourceList();
            if (!sources.length) {
                alert('请至少选择一个数据源');
                return;
            }
            const sourcesText = fetchSourceOptions
                .filter((item) => sources.includes(item.value))
                .map((item) => `${item.value}: ${item.label}`)
                .join('\n');
            if (!confirm(`确认抓取最新观点？\n\n数据源：\n${sourcesText}\n\n仅抓取当天发布的内容：新浪博客当天全部（≤20条），股吧/基金吧各约15条。`)) return;
            try {
                const response = await axios.post('/api/viewpoints/fetch', {
                    sources,
                    // 后端会对 sina 用 20、其他源按传入 limit 截断；20 作为上限即可。
                    limit_per_source: 20,
                    mode: 'fetch',
                });
                viewpointTask.value = response.data.data;
                sourceMenuOpen.value = false;
                localStorage.setItem('viewpoint_task_id', String(viewpointTask.value.task_id));
                pollTask(viewpointTask.value.task_id);
            } catch (error) { alert('抓取失败: ' + errorMessage(error)); }
        };
        const retryViewpointTask = async () => {
            if (!viewpointTask.value?.task_id) return;
            try {
                const response = await axios.post(`/api/viewpoints/tasks/${viewpointTask.value.task_id}/retry`);
                viewpointTask.value = response.data.data;
                localStorage.setItem('viewpoint_task_id', String(viewpointTask.value.task_id));
                pollTask(viewpointTask.value.task_id);
            } catch (error) { alert('重试失败: ' + errorMessage(error)); }
        };
        const viewViewpointDetail = async (id) => {
            try {
                const response = await axios.get(`/api/viewpoints/${id}`);
                viewpointDetail.value = response.data.data;
                showViewpointDetail.value = true;
            } catch (error) { alert('获取详情失败: ' + errorMessage(error)); }
        };
        const batchAnalyzeViewpoints = async () => {
            analyzing.value = true;
            try {
                const res = await axios.post('/api/viewpoints/batch-analyze', { limit: 30 });
                const data = res.data.data || {};
                const message = res.data.message || '没有需要分析的观点';
                if (data.in_progress && data.task_id) {
                    // 立即启动轮询；补齐 status 字段，避免 taskRunning 误判
                    viewpointTask.value = {
                        ...data,
                        status: data.status || 'pending',
                        total_count: data.total || data.total_count || 0,
                        processed_count: data.analyzed_count || data.processed_count || 0,
                    };
                    localStorage.setItem('viewpoint_task_id', String(data.task_id));
                    pollTask(data.task_id);
                } else {
                    analyzing.value = false;
                }
                // alert 放在轮询启动后，避免阻塞 UI 更新
                alert(message);
                await fetchViewpoints();
                await fetchInsights();
            } catch (error) {
                analyzing.value = false;
                alert('分析失败: ' + errorMessage(error));
            }
        };
        const fetchSummaryStats = async () => {
            try {
                const res = await axios.get('/api/viewpoints/summary/stats');
                if (res.data.success) summaryStats.value = res.data.data;
            } catch (error) { console.error('获取汇总统计失败:', error); }
        };
        const showSummaryModal = async () => {
            await fetchSummaryStats();
            const pending = (summaryStats.value && summaryStats.value.total_pending_viewpoints)
                || (viewpointInsights.value.pending_summary && viewpointInsights.value.pending_summary.length > 0);
            if (!pending) { alert('没有待汇总的观点'); return; }
            showSummaryConfirmModal.value = true;
        };
        const executeSummary = async () => {
            summarizing.value = true;
            showSummaryConfirmModal.value = false;
            try {
                const res = await axios.post('/api/viewpoints/summary/execute', {});
                alert(res.data.message);
                await Promise.all([fetchViewpoints(), fetchInsights(), fetchSummaryStats()]);
                if (onStatsChanged) await onStatsChanged();
            } catch (error) { alert('汇总失败: ' + errorMessage(error)); }
            summarizing.value = false;
        };
        const deleteViewpoint = async (id) => {
            if (!confirm('此操作会永久删除该观点，无法恢复。确定继续？')) return;
            try {
                await axios.delete(`/api/viewpoints/${id}`, { headers: { 'X-Danger-Confirm': 'delete-viewpoint' } });
                await Promise.all([fetchViewpoints(), fetchInsights()]);
                if (options.onStatsChanged) await options.onStatsChanged();
            } catch (error) { alert('删除失败: ' + errorMessage(error)); }
        };
        const sourceLabel = (source) => ({
            eastmoney_blog: '东方财富博客', eastmoney_guide: '东方财富导读',
            eastmoney_news: '东方财富快讯', sina_finance: '新浪财经',
            sina_blog: '新浪博客', stock_guba: '热门股吧', fund_guba: '热门基金吧',
            daily_summary: '每日汇总',
        }[source] || source || '未知');
        const directionLabel = (direction) => ({ bullish: '看多', bearish: '看空', neutral: '中性' }[direction] || '中性');
        const taskStatusLabel = (status) => ({
            pending: '等待执行', running: '抓取分析中', succeeded: '已完成', failed: '部分失败', cancelled: '已取消',
        }[status] || status || '暂无任务');

        return {
            viewpoints, viewpointMeta, viewpointFilters, viewpointInsights, insightsLoaded, insightsError, viewpointTask,
            viewpointDetail, showViewpointDetail, sourceMenuOpen, selectedSources, sourceOptions,
            fetchSourceOptions,
            taskRunning, summarizing, summaryStats, showSummaryConfirmModal,
            fetchViewpoints, loadViewpoints, applyViewpointFilters, resetViewpointFilters,
            viewpointPrevPage, viewpointNextPage, fetchLatestViewpoints, retryViewpointTask,
            viewViewpointDetail, deleteViewpoint, restoreViewpointTask, sourceLabel,
            directionLabel, taskStatusLabel,
            batchAnalyzeViewpoints, fetchSummaryStats, showSummaryModal, executeSummary,
        };
    };
})();

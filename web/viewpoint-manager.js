(function () {
    'use strict';

    window.createViewpointManager = function createViewpointManager(options) {
        const { axios, ref, reactive, computed, localStorage, alert, confirm } = options;
        const viewpoints = options.viewpoints || ref([]);
        const viewpointMeta = reactive({ page: 1, page_size: 20, total: 0, pages: 0 });
        const viewpointFilters = reactive({
            keyword: '', source: '', market_direction: '', analysis_status: '',
            date_from: '', date_to: '', viewpoint_type: '', page: 1, page_size: 20,
        });
        const viewpointInsights = ref({ directions: {}, sector_consensus: [], source_quality: {}, pending_summary: [] });
        const viewpointTask = ref(null);
        const viewpointDetail = options.viewpointDetail || ref(null);
        const showViewpointDetail = options.showViewpointDetail || ref(false);
        const sourceMenuOpen = ref(false);
        const selectedSources = reactive({
            eastmoney_blog: true,
            eastmoney_guide: true,
            sina_finance: true,
            eastmoney_news: false,
        });
        const sourceOptions = [
            { value: 'eastmoney_blog', label: '东方财富博客' },
            { value: 'eastmoney_guide', label: '东方财富导读' },
            { value: 'sina_finance', label: '新浪财经' },
            { value: 'eastmoney_news', label: '东方财富快讯' },
        ];
        const taskRunning = computed(() => ['pending', 'running'].includes(viewpointTask.value?.status));
        let pollTimer = null;

        const errorMessage = (error) => error.response?.data?.detail || error.response?.data?.message || error.message;
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
            const response = await axios.get('/api/viewpoints', { params });
            viewpoints.value = response.data.data || [];
            Object.assign(viewpointMeta, response.data.meta || {});
        };
        const fetchInsights = async () => {
            const response = await axios.get('/api/viewpoints/insights');
            viewpointInsights.value = response.data.data || viewpointInsights.value;
        };
        const fetchLatestTask = async () => {
            const response = await axios.get('/api/viewpoints/tasks/latest');
            viewpointTask.value = response.data.data;
            return viewpointTask.value;
        };
        const loadViewpoints = async () => {
            await Promise.all([fetchViewpoints(), fetchInsights(), fetchLatestTask()]);
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
        const viewpointPrevPage = async () => {
            if (viewpointFilters.page > 1) { viewpointFilters.page -= 1; await fetchViewpoints(); }
        };
        const viewpointNextPage = async () => {
            if (viewpointFilters.page < viewpointMeta.pages) { viewpointFilters.page += 1; await fetchViewpoints(); }
        };
        const clearPoll = () => {
            if (pollTimer) window.clearTimeout(pollTimer);
            pollTimer = null;
            localStorage.removeItem('viewpoint_task_id');
        };
        const pollTask = async (taskId) => {
            if (pollTimer) window.clearTimeout(pollTimer);
            try {
                const latest = await fetchLatestTask();
                if (!latest || latest.task_id !== taskId || ['succeeded', 'failed', 'cancelled'].includes(latest.status)) {
                    clearPoll();
                    await Promise.all([fetchViewpoints(), fetchInsights()]);
                    return;
                }
                localStorage.setItem('viewpoint_task_id', String(taskId));
                pollTimer = window.setTimeout(() => pollTask(taskId), 3000);
            } catch (error) {
                clearPoll();
                console.error('观点任务轮询失败', error);
            }
        };
        const restoreViewpointTask = () => {
            const taskId = localStorage.getItem('viewpoint_task_id');
            if (taskId) pollTask(Number(taskId));
        };
        const fetchLatestViewpoints = async () => {
            const sources = sourceOptions.filter(item => selectedSources[item.value]).map(item => item.value);
            if (!sources.length) { alert('请至少选择一个来源'); return; }
            try {
                const response = await axios.post('/api/viewpoints/fetch', { sources, limit_per_source: 15 });
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
            sina_blog: '新浪历史来源', daily_summary: '每日汇总',
        }[source] || source || '未知');
        const directionLabel = (direction) => ({ bullish: '看多', bearish: '看空', neutral: '中性' }[direction] || '中性');
        const taskStatusLabel = (status) => ({
            pending: '等待执行', running: '抓取分析中', succeeded: '已完成', failed: '部分失败', cancelled: '已取消',
        }[status] || status || '暂无任务');

        return {
            viewpoints, viewpointMeta, viewpointFilters, viewpointInsights, viewpointTask,
            viewpointDetail, showViewpointDetail, sourceMenuOpen, selectedSources, sourceOptions,
            taskRunning, fetchViewpoints, loadViewpoints, applyViewpointFilters, resetViewpointFilters,
            viewpointPrevPage, viewpointNextPage, fetchLatestViewpoints, retryViewpointTask,
            viewViewpointDetail, deleteViewpoint, restoreViewpointTask, sourceLabel,
            directionLabel, taskStatusLabel,
        };
    };
})();

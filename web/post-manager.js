(function () {
    'use strict';

    window.createPostManager = function createPostManager(options) {
        const { axios, ref, reactive, localStorage, alert, confirm, analyzing } = options;
        const posts = ref([]);
        const postMeta = reactive({ total: 0, skip: 0, limit: 20, has_more: false, status_counts: {} });
        const postFilters = reactive({
            keyword: '', blogger_id: '', analysis_status: '', start_date: '', end_date: '', quality: '', page: 1, limit: 20,
        });
        const showAddPost = ref(false);
        const showPostDetail = ref(false);
        const showEditPost = ref(false);
        const postDetail = ref(null);
        const editingPost = reactive({ id: null, title: '', source_url: '' });
        const newPost = reactive({
            blogger_id: null,
            title: '',
            source_url: '',
            content: '',
            post_date: new Date().toISOString().split('T')[0],
        });
        const analysisJob = ref(null);
        const postAnalysisRunning = ref(false);
        let pollTimer = null;
        const resumeRequested = new Set();

        // FastAPI 的 422 把 detail 写成数组（[{loc, msg}, ...]），直接插进模板就变成
        // "[object Object]"，等于把唯一的报错线索吞掉（第 15 轮 MINOR-4）。
        const errorMessage = (error) => {
            const d = error.response?.data?.detail;
            if (Array.isArray(d)) return d.map(x => ((x.loc || []).slice(1).join('.') || 'body') + '：' + x.msg).join('；');
            return d || error.response?.data?.message || error.message;
        };

        const fetchPosts = async () => {
            const params = {
                skip: (postFilters.page - 1) * postFilters.limit,
                limit: postFilters.limit,
                keyword: postFilters.keyword.trim() || undefined,
                blogger_id: postFilters.blogger_id || undefined,
                analysis_status: postFilters.analysis_status || undefined,
                start_date: postFilters.start_date || undefined,
                end_date: postFilters.end_date || undefined,
                quality: postFilters.quality || undefined,
            };
            // 翻页/筛选是直连这里取数的，不经过 `loadView` 的失败登记 ⇒ 三种形状都得说一句话：
            // 成功（把旧的失败说明清掉）、200 + success:false、抛错（原来连 catch 都没有）。
            const report = (msg) => { if (options.onFetchFailure) options.onFetchFailure('posts', msg); };
            try {
                const res = await axios.get('/api/posts', { params });
                if (res.data.success) {
                    posts.value = res.data.data || [];
                    Object.assign(postMeta, res.data.meta || {});
                    report('');
                } else {
                    report('帖子列表没取到：' + (res.data.message || '接口未给出原因'));
                }
            } catch (error) {
                report('帖子列表拉取失败：' + (options.isServiceDown && options.isServiceDown(error)
                    ? '服务连不上（可能在唤醒）' : '接口报错'));
                throw error;
            }
        };

        const applyPostFilters = async () => { postFilters.page = 1; await fetchPosts(); };
        const resetPostFilters = async () => {
            Object.assign(postFilters, { keyword: '', blogger_id: '', analysis_status: '', start_date: '', end_date: '', quality: '', page: 1 });
            await fetchPosts();
        };
        // 翻页失败要把页码退回去：否则「第 4 页」配的是第 3 页的数据（第 34 轮 B-MINOR-15）
        const postPrevPage = async () => {
            if (postFilters.page <= 1) return;
            const back = postFilters.page; postFilters.page -= 1;
            try { await fetchPosts(); } catch (error) { postFilters.page = back; }
        };
        const postNextPage = async () => {
            if (!postMeta.has_more) return;
            const back = postFilters.page; postFilters.page += 1;
            try { await fetchPosts(); } catch (error) { postFilters.page = back; }
        };

        const refreshRelated = async () => {
            await fetchPosts();
            if (options.onStatsChanged) await options.onStatsChanged();
            if (options.onPredictionsChanged) await options.onPredictionsChanged();
        };

        const clearJob = () => {
            if (analysisJob.value?.task_id) resumeRequested.delete(String(analysisJob.value.task_id));
            localStorage.removeItem('post_analysis_task_id');
            postAnalysisRunning.value = false;
            analyzing.value = false;
            if (pollTimer) window.clearTimeout(pollTimer);
            pollTimer = null;
        };

        const MAX_POLL_FAILURES = 90;   // 10 秒一次 ≈ 15 分钟
        const pollAnalysisJob = async (taskId, attempts = 0) => {
            try {
                const res = await axios.get(`/api/posts/analysis-jobs/${taskId}`);
                analysisJob.value = res.data.data;
                if (options.onPollRecovered) options.onPollRecovered('帖子批量分析');
                const status = analysisJob.value?.status;
                const updatedAt = analysisJob.value?.updated_at ? new Date(analysisJob.value.updated_at) : null;
                const staleRunning = status === 'running' && updatedAt && (Date.now() - updatedAt.getTime() > 15 * 60 * 1000);
                if ((status === 'pending' || staleRunning) && !resumeRequested.has(String(taskId))) {
                    resumeRequested.add(String(taskId));
                    await axios.post(`/api/posts/analysis-jobs/${taskId}/resume`);
                }
                if (['succeeded', 'failed', 'cancelled'].includes(status)) {
                    clearJob();
                    await refreshRelated();
                    return;
                }
                postAnalysisRunning.value = true;
                analyzing.value = true;
                pollTimer = window.setTimeout(() => pollAnalysisJob(taskId), 3000);
            } catch (error) {
                // 只有"任务确实不在了"（404）才丢句柄。401/403/500 与唤醒期的网络错都不算：
                // 第 31 轮两份复评共同指出，`ACCESS_PASSWORD` 轮换那天，正在跑的批量分析
                // 会因为登录态没确立就被清掉任务号，老板看不到、很可能再点一次造成重复写入。
                // 其余失败一律留着句柄限次再问（10 秒一次、最多 15 分钟），
                // 停手也不删 —— 下次打开页面还能接着看。
                if (error.response && error.response.status === 404) {
                    clearJob();
                } else if (attempts < MAX_POLL_FAILURES) {
                    pollTimer = window.setTimeout(() => pollAnalysisJob(taskId, attempts + 1), 10000);
                } else {
                    // 数到上限必须把全局锁放开：`analyzing` 跨 5 个视图禁用 13 个按钮，
                    // 第 32 轮 A 实测"耗尽后句柄保住了、但按钮全灰且页面一个字都不说"。
                    postAnalysisRunning.value = false;
                    analyzing.value = false;
                    if (options.onPollStalled) options.onPollStalled('帖子批量分析');
                }
                console.error('恢复帖子分析任务失败', error);
            }
        };

        const rememberJob = (data) => {
            if (!data?.task_id) return;
            analysisJob.value = data;
            postAnalysisRunning.value = ['pending', 'running'].includes(data.status);
            analyzing.value = postAnalysisRunning.value;
            localStorage.setItem('post_analysis_task_id', String(data.task_id));
            pollAnalysisJob(data.task_id);
        };

        const restoreAnalysisJob = () => {
            const taskId = localStorage.getItem('post_analysis_task_id');
            if (taskId) pollAnalysisJob(taskId);
        };

        const cancelAnalysisJob = async () => {
            const taskId = analysisJob.value?.task_id;
            if (!taskId) return;
            try {
                await axios.post(`/api/posts/analysis-jobs/${taskId}/cancel`);
                clearJob();
                await refreshRelated();
            } catch (error) {
                alert('取消失败: ' + errorMessage(error));
            }
        };

        const startAnalysisJob = async (postIds) => {
            const res = await axios.post('/api/posts/analysis-jobs', {
                post_ids: postIds?.length ? postIds : null,
                limit: postIds?.length || 100,
            });
            if (res.data.success) rememberJob(res.data.data);
            return res;
        };

        const addPost = async (enqueue = false) => {
            try {
                const res = await axios.post('/api/posts', { ...newPost, async_mode: true });
                if (res.data.success) {
                    const postId = res.data.data?.id;
                    showAddPost.value = false;
                    Object.assign(newPost, { title: '', source_url: '', content: '' });
                    if (enqueue && postId) {
                        try {
                            await startAnalysisJob([postId]);
                            alert('帖子已保存并加入分析队列');
                        } catch (queueError) {
                            alert('帖子已保存，但暂时无法加入分析队列: ' + errorMessage(queueError));
                        }
                    } else {
                        alert('帖子已保存');
                    }
                    // 同上：帖子已经存下了，刷新失败不许说「添加失败」
                    try { await refreshRelated(); }
                    catch (refreshError) { alert('帖子已保存，只是列表没刷新出来 —— 刷新页面即可'); }
                } else {
                    alert('添加失败: ' + (res.data.message || '未知错误'));
                }
            } catch (error) {
                alert('添加失败: ' + errorMessage(error));
            }
        };

        const analyzePost = async (id) => {
            try {
                const res = await axios.post(`/api/posts/${id}/analyze`);
                rememberJob(res.data.data);
            } catch (error) {
                alert('加入分析队列失败: ' + errorMessage(error));
            }
        };

        const batchAnalyzePosts = async () => {
            analyzing.value = true;
            try {
                const res = await startAnalysisJob();
                // alert 放在 rememberJob 之后，轮询已经启动，避免阻塞 UI 更新
                if (res.data.message) alert(res.data.message);
            } catch (error) {
                postAnalysisRunning.value = false;
                analyzing.value = false;
                alert('分析失败: ' + errorMessage(error));
            }
        };

        const viewPostDetail = async (id) => {
            try {
                const res = await axios.get(`/api/posts/${id}`);
                if (res.data.success) {
                    postDetail.value = res.data.data;
                    showPostDetail.value = true;
                }
            } catch (error) { alert('获取详情失败: ' + errorMessage(error)); }
        };

        const openEditPost = (post) => {
            Object.assign(editingPost, { id: post.id, title: post.title || '', source_url: post.source_url || '' });
            showEditPost.value = true;
        };

        const savePostEdit = async () => {
            try {
                const res = await axios.patch(`/api/posts/${editingPost.id}`, {
                    title: editingPost.title || null,
                    source_url: editingPost.source_url || null,
                });
                if (res.data.success) {
                    showEditPost.value = false;
                    // 刷新失败不能说"保存失败"：帖子已经改好了，那句话会让老板再点一次（第 34 轮 B-MAJOR-5）
                    try { await fetchPosts(); }
                    catch (refreshError) { alert('帖子已保存，只是列表没刷新出来 —— 刷新页面或再点一次就能看见'); }
                } else {
                    alert('这次没保存成功：' + (res.data.message || '接口未给出原因'));
                }
            } catch (error) { alert('保存失败: ' + errorMessage(error)); }
        };

        const deletePost = async (id) => {
            try {
                const previewRes = await axios.get(`/api/posts/${id}/delete-preview`);
                const p = previewRes.data.data || {};
                const message = `此操作不可恢复。将删除帖子及 ${p.prediction_count || 0} 条预测、${p.verification_task_count || 0} 个验证任务、${p.prediction_group_count || 0} 个预测组、${p.analysis_log_count || 0} 条分析日志；${p.viewpoint_detach_count || 0} 条观点仅解除关联。确定继续？`;
                if (!confirm(message)) return;
                await axios.delete(`/api/posts/${id}`, { headers: { 'X-Danger-Confirm': 'delete-post' } });
                // 删除已经落库了，刷新失败不能报"删除失败"（老板会再点一次，而东西早没了）
                try { await refreshRelated(); }
                catch (refreshError) { alert('帖子及关联数据已删除，只是列表没刷新出来 —— 刷新页面即可'); return; }
                alert('帖子及关联运行数据已彻底删除');
            } catch (error) { alert('删除失败: ' + errorMessage(error)); }
        };

        const analysisStatusText = (status) => ({
            pending: '待分析', running: '分析中', succeeded: '已完成', failed: '失败', skipped: '已删除',
        }[status] || '待分析');

        return {
            posts, postMeta, postFilters, newPost, postDetail, editingPost, analysisJob, postAnalysisRunning,
            showAddPost, showPostDetail, showEditPost,
            fetchPosts, applyPostFilters, resetPostFilters, postPrevPage, postNextPage,
            addPost, analyzePost, batchAnalyzePosts, viewPostDetail, openEditPost, savePostEdit,
            deletePost, restoreAnalysisJob, cancelAnalysisJob, startAnalysisJob, analysisStatusText,
        };
    };
})();

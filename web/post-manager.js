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
        let pollTimer = null;
        const resumeRequested = new Set();

        const errorMessage = (error) => error.response?.data?.detail || error.response?.data?.message || error.message;

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
            const res = await axios.get('/api/posts', { params });
            if (res.data.success) {
                posts.value = res.data.data || [];
                Object.assign(postMeta, res.data.meta || {});
            }
        };

        const applyPostFilters = async () => { postFilters.page = 1; await fetchPosts(); };
        const resetPostFilters = async () => {
            Object.assign(postFilters, { keyword: '', blogger_id: '', analysis_status: '', start_date: '', end_date: '', quality: '', page: 1 });
            await fetchPosts();
        };
        const postPrevPage = async () => { if (postFilters.page > 1) { postFilters.page -= 1; await fetchPosts(); } };
        const postNextPage = async () => { if (postMeta.has_more) { postFilters.page += 1; await fetchPosts(); } };

        const refreshRelated = async () => {
            await fetchPosts();
            if (options.onStatsChanged) await options.onStatsChanged();
            if (options.onPredictionsChanged) await options.onPredictionsChanged();
        };

        const clearJob = () => {
            if (analysisJob.value?.task_id) resumeRequested.delete(String(analysisJob.value.task_id));
            localStorage.removeItem('post_analysis_task_id');
            analyzing.value = false;
            if (pollTimer) window.clearTimeout(pollTimer);
            pollTimer = null;
        };

        const pollAnalysisJob = async (taskId) => {
            try {
                const res = await axios.get(`/api/posts/analysis-jobs/${taskId}`);
                analysisJob.value = res.data.data;
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
                analyzing.value = true;
                pollTimer = window.setTimeout(() => pollAnalysisJob(taskId), 3000);
            } catch (error) {
                clearJob();
                console.error('恢复帖子分析任务失败', error);
            }
        };

        const rememberJob = (data) => {
            if (!data?.task_id) return;
            analysisJob.value = data;
            analyzing.value = ['pending', 'running'].includes(data.status);
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
                    await refreshRelated();
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
                alert(res.data.message);
            } catch (error) {
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
                    await fetchPosts();
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
                await refreshRelated();
                alert('帖子及关联运行数据已彻底删除');
            } catch (error) { alert('删除失败: ' + errorMessage(error)); }
        };

        const analysisStatusText = (status) => ({
            pending: '待分析', running: '分析中', succeeded: '已完成', failed: '失败', skipped: '已删除',
        }[status] || '待分析');

        return {
            posts, postMeta, postFilters, newPost, postDetail, editingPost, analysisJob,
            showAddPost, showPostDetail, showEditPost,
            fetchPosts, applyPostFilters, resetPostFilters, postPrevPage, postNextPage,
            addPost, analyzePost, batchAnalyzePosts, viewPostDetail, openEditPost, savePostEdit,
            deletePost, restoreAnalysisJob, cancelAnalysisJob, startAnalysisJob, analysisStatusText,
        };
    };
})();

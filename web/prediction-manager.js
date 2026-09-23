(function () {
    'use strict';

    window.createPredictionManager = function createPredictionManager(options) {
        const { axios, ref, reactive, computed, alert, confirm, analyzing } = options;
        const predictions = options.predictions || ref([]);
        const predictionDetail = options.predictionDetail || ref(null);
        const showPredictionDetail = options.showPredictionDetail || ref(false);
        const showEditPrediction = options.showEditPrediction || ref(false);
        const editingPrediction = options.editingPrediction || ref(null);
        const predictionFilter = ref('all');
        const predictionMeta = reactive({
            page: 1, page_size: 50, total: 0, has_more: false, sort: 'due_first',
            facets: {
                all: 0, pending: 0, verified: 0, correct: 0, wrong: 0, flat: 0, archived: 0,
                due: 0, upcoming: 0, unverifiable: 0,
            },
        });
        const predictionFilters = reactive({
            keyword: '', blogger_id: '', fund_code: '', sector: '', direction: '',
            start_date: '', end_date: '', page: 1, page_size: 50,
            status: '', result: '', archive: 'active', lifecycle: '', sort: 'due_first',
        });
        const verifyTask = ref(null);
        const showPredictionMaintenance = ref(false);
        const maintenancePreview = ref(null);
        const maintenanceError = ref('');
        const filteredPredictions = computed(() => predictions.value);
        let verifyPollTimer = null;

        // FastAPI 的 422 把 detail 写成数组（[{loc, msg}, ...]），直接插进模板就变成
        // "[object Object]"，等于把唯一的报错线索吞掉（第 15 轮 MINOR-4）。
        const errorMessage = (error) => {
            const d = error.response?.data?.detail;
            if (Array.isArray(d)) return d.map(x => ((x.loc || []).slice(1).join('.') || 'body') + '：' + x.msg).join('；');
            return d || error.response?.data?.message || error.message;
        };

        const fetchPredictions = async () => {
            const params = {
                page: predictionFilters.page,
                page_size: predictionFilters.page_size,
                search: predictionFilters.keyword.trim() || undefined,
                blogger_id: predictionFilters.blogger_id || undefined,
                fund_code: predictionFilters.fund_code.trim() || undefined,
                sector: predictionFilters.sector.trim() || undefined,
                prediction_type: predictionFilters.direction || undefined,
                // 观望预测默认隐藏，主动点"观望"筛选（direction=flat）时才显示
                exclude_flat: predictionFilters.direction ? undefined : true,
                status: predictionFilters.status || undefined,
                result: predictionFilters.result || undefined,
                start_date: predictionFilters.start_date || undefined,
                end_date: predictionFilters.end_date || undefined,
                archive: predictionFilters.archive,
                lifecycle: predictionFilters.lifecycle || undefined,
                sort: predictionFilters.sort || undefined,
            };
            // 同 `post-manager.js`：翻页直连，三种形状都要有话说（第 33 轮 A-MAJOR-2）
            const report = (msg) => { if (options.onFetchFailure) options.onFetchFailure('predictions', msg); };
            try {
                const response = await axios.get('/api/predictions', { params });
                if (response.data.success) {
                    predictions.value = response.data.data || [];
                    Object.assign(predictionMeta, response.data.meta || {});
                    report('');
                    return true;
                }
                report('预测列表没取到：' + (response.data.message || '接口未给出原因'));
                return false;
            } catch (error) {
                report('预测列表拉取失败：' + (options.isServiceDown && options.isServiceDown(error)
                    ? '服务连不上（可能在唤醒）' : '接口报错'));
                throw error;
            }
        };

        const applyPredictionFilters = async () => {
            predictionFilters.page = 1;
            predictionFilter.value = 'custom';
            await fetchPredictions();
        };
        const resetPredictionFilters = async () => {
            Object.assign(predictionFilters, {
                keyword: '', blogger_id: '', fund_code: '', sector: '', direction: '',
                start_date: '', end_date: '', page: 1, status: '', result: '', archive: 'active',
                lifecycle: '', sort: 'due_first',
            });
            predictionFilter.value = 'all';
            await fetchPredictions();
        };
        const setPredictionFilter = async (filter) => {
            predictionFilter.value = filter;
            Object.assign(predictionFilters, {
                page: 1, status: '', result: '', direction: '', archive: 'active', lifecycle: '',
            });
            if (filter === 'due') predictionFilters.lifecycle = 'due';
            if (filter === 'upcoming') predictionFilters.lifecycle = 'active';
            if (filter === 'pending') predictionFilters.status = 'pending';
            if (filter === 'verified') predictionFilters.status = 'verified';
            if (filter === 'correct') predictionFilters.result = 'correct';
            if (filter === 'wrong') predictionFilters.result = 'wrong';
            if (filter === 'flat') predictionFilters.direction = 'flat';
            if (filter === 'archived') predictionFilters.archive = 'archived';
            await fetchPredictions();
        };
        const setPredictionSort = async (sort) => {
            predictionFilters.sort = sort;
            predictionFilters.page = 1;
            await fetchPredictions();
        };
        // 翻页失败把页码退回去（同 `post-manager.js`，第 34 轮 B-MINOR-15）
        const predictionPrevPage = async () => {
            if (predictionFilters.page <= 1) return;
            const back = predictionFilters.page; predictionFilters.page -= 1;
            try { if (await fetchPredictions() === false) predictionFilters.page = back; }
            catch (error) { predictionFilters.page = back; }
        };
        const predictionNextPage = async () => {
            if (!predictionMeta.has_more) return;
            const back = predictionFilters.page; predictionFilters.page += 1;
            try { if (await fetchPredictions() === false) predictionFilters.page = back; }
            catch (error) { predictionFilters.page = back; }
        };

        const refreshAfterChange = async () => {
            await fetchPredictions();
            if (options.onStatsChanged) await options.onStatsChanged();
        };
        // 刷新失败 ≠ 动作失败：预测已经移进/移出回收站了，说「失败」会让老板再点一次
        const afterChange = async (doneText) => {
            try { await refreshAfterChange(); }
            catch (refreshError) { alert(doneText + '，只是列表没刷新出来 —— 刷新页面即可'); return; }
        };
        const archivePrediction = async (id) => {
            if (!confirm('将该预测移入回收站？之后可以恢复。')) return;
            try {
                await axios.delete(`/api/predictions/${id}`);
                await afterChange('已移入回收站');
            } catch (error) { alert('归档失败: ' + errorMessage(error)); }
        };
        const restorePrediction = async (id) => {
            try {
                await axios.post(`/api/predictions/${id}/restore`);
                await afterChange('已恢复');
            } catch (error) { alert('恢复失败: ' + errorMessage(error)); }
        };
        const viewPredictionDetail = async (id) => {
            try {
                const response = await axios.get(`/api/predictions/${id}`);
                if (!response.data.success) { alert('这条预测的详情没取到：' + (response.data.message || '接口未给出原因')); return; }
                predictionDetail.value = response.data.data;
                showPredictionDetail.value = true;
            } catch (error) { alert('获取详情失败: ' + errorMessage(error)); }
        };
        const editPrediction = (prediction) => {
            if (prediction.lifecycle_status === 'verified') {
                alert('已验证预测的关键依据不可直接修改');
                return;
            }
            editingPrediction.value = {
                id: prediction.id,
                sector: prediction.sector || '',
                fund_code: prediction.fund_code || '',
                fund_name: prediction.fund_name || '',
                prediction_type: prediction.prediction_type || 'up',
                confidence: prediction.confidence ?? 50,
                prediction_period: prediction.prediction_period || '1周',
            };
            showEditPrediction.value = true;
        };
        const savePrediction = async () => {
            const prediction = editingPrediction.value;
            if (!prediction) return;
            try {
                await axios.put(`/api/predictions/${prediction.id}`, {
                    sector: prediction.sector,
                    fund_code: prediction.fund_code,
                    fund_name: prediction.fund_name,
                    prediction_type: prediction.prediction_type,
                    confidence: prediction.confidence,
                    prediction_period: prediction.prediction_period,
                });
                showEditPrediction.value = false;
                // 预测已经改好了，刷新失败不能说「保存失败」（那会让老板再点一次保存）
                try { await fetchPredictions(); }
                catch (refreshError) { alert('预测已保存，只是列表没刷新出来 —— 刷新页面即可'); }
            } catch (error) { alert('保存失败: ' + errorMessage(error)); }
        };

        const stopVerifyPolling = () => {
            if (verifyPollTimer) window.clearTimeout(verifyPollTimer);
            verifyPollTimer = null;
        };
        const pollVerifyTask = async () => {
            stopVerifyPolling();
            try {
                const response = await axios.get('/api/predictions/verify-all/status');
                verifyTask.value = response.data.data || null;
                analyzing.value = Boolean(verifyTask.value?.in_progress);
                if (verifyTask.value?.in_progress) {
                    verifyPollTimer = window.setTimeout(pollVerifyTask, 4000);
                } else {
                    await refreshAfterChange();
                }
            } catch (error) {
                analyzing.value = false;
                console.error('预测验证状态读取失败', error);
            }
        };
        const batchAnalyzePredictions = async () => {
            analyzing.value = true;
            try {
                const response = await axios.post('/api/predictions/verify-all');
                verifyTask.value = response.data.data || null;
                alert(response.data.message);
                // 验证任务已经起来了，收尾这两腿失败不能说「验证失败」
                try {
                    if (verifyTask.value?.in_progress) await pollVerifyTask();
                    else {
                        analyzing.value = false;
                        await refreshAfterChange();
                    }
                }
                catch (refreshError) { alert('验证已开始，只是列表没刷新出来 —— 刷新页面即可'); }
            } catch (error) {
                analyzing.value = false;
                alert('验证失败: ' + errorMessage(error));
            }
        };
        const restorePredictionVerifyTask = async () => {
            // 首屏第 6 个取数点：实例唤醒期它会和其余几个一起失败，所以走同一个
            // `withWakeRetry`（由 index.html 注入）。判据只在一处，别在这里再抄一遍等待逻辑。
            const wake = options.withWakeRetry || (fn => fn());
            try {
                const response = await wake(() => axios.get('/api/predictions/verify-all/status'));
                verifyTask.value = response.data.data || null;
                if (verifyTask.value?.in_progress) await pollVerifyTask();
                // 非进行中也保留 verifyTask，用于展示上次验证的失败原因汇总
            } catch (error) { console.error('恢复预测验证任务失败', error); }
        };

        const previewPredictionMaintenance = async (type) => {
            analyzing.value = true;
            // 红色「确认执行」活在 `maintenancePreview` 上 ⇒ 每次预览先把它擦掉：
            // 留着上一轮的数就等于让老板按旧清单点真写（AGENTS ⑥ 的推论）
            maintenancePreview.value = null;
            maintenanceError.value = '';
            try {
                let response;
                if (type === 'duplicates') response = await axios.post('/api/predictions/merge-similar');
                if (type === 'mapping') response = await axios.post('/api/predictions/sync-sector-mapping', null, { params: { dry_run: true } });
                if (type === 'rollback') response = await axios.post('/api/predictions/rollback-invalid', null, { params: { dry_run: true } });
                // 后端很多"被护栏按住"走的是 200 + success:false，不看它就等于把拒绝当成预览成功
                if (response.data && response.data.success === false) {
                    maintenanceError.value = '预览被拒绝：' + (response.data.message || '接口未给出原因');
                } else {
                    maintenancePreview.value = { type, message: response.data.message, data: response.data.data || {} };
                }
            } catch (error) { maintenanceError.value = '预览失败：' + errorMessage(error); }
            analyzing.value = false;
        };
        const executePredictionMaintenance = async () => {
            const type = maintenancePreview.value?.type;
            if (!['duplicates', 'mapping', 'rollback'].includes(type)) return;
            const label = type === 'duplicates' ? '重复预测去重' : type === 'mapping' ? '板块映射同步' : '无效验证回溯';
            // 回溯只允许撤"刚刚预览给用户看过的那几条"：不限定 id 的全库回溯会抹掉
            // 上千条历史结论（多数只是本地镜像缺那段历史），那个口径不给按钮用。
            let ids = null;
            if (type === 'rollback') {
                const rows = (maintenancePreview.value.data || {}).rollback_details || [];
                if (!rows.length) {
                    alert('这次预览没有可回溯的预测（或预览已过期），请重新点"预览"再来一次。');
                    return;
                }
                // 预览是全库扫出来的，不设界就等于把"allow_full_sweep"藏进按钮里：
                // 这里只提交后端允许的一次上限，剩下的明确告诉用户要去脚本处理。
                const cap = 200;
                // 预览里现在混着两类动作：撤结论（无 action 键）与改标并清掉旧结论
                // （would_retag，第 20 轮把"漂移行没人管"补上的）。按钮以前只说"修改资料"，
                // 老板不知道点下去会换标的（第 21 轮 MAJOR-5）。
                // 先按 cap 截断再计数：以前第一句按整份预览报数、却只提交前 200 条，
                // 老板看到的数字和真正发生的动作不是一回事（第 22 轮 MINOR-7）。
                if (rows.length > cap &&
                    !confirm(`预览有 ${rows.length} 条，一次只处理前 ${cap} 条，` +
                             `剩下的请走脚本（scripts/revert_degenerate_verdicts.py）。继续吗？`)) return;
                const batch = rows.slice(0, cap);
                const wouldRetag = batch.filter(r => r.action === 'would_retag').length;
                const willRollback = batch.length - wouldRetag;
                const actionText = wouldRetag
                    ? `其中 ${willRollback} 条撤掉验证结论，${wouldRetag} 条会换成体检可服务的基金标的、并清掉按旧标的判出的结论`
                    : `将撤掉 ${willRollback} 条预测的验证结论`;
                if (!confirm(`即将执行"无效验证回溯"（只处理刚预览过的这些行）：\n\n${actionText}。\n\n确认继续？`)) return;
                ids = batch.map(r => r.prediction_id).filter(Boolean).join(',');
                if (!ids) {
                    alert('预览数据里没有可用的预测 id，请重新点"预览"。');
                    return;
                }
            }
            if (!confirm(`确认执行${label}？系统将按预览清单修改资料。`)) return;
            analyzing.value = true;
            try {
                const endpoint = type === 'duplicates' ? 'dedupe-duplicates' : type === 'mapping' ? 'sync-sector-mapping' : 'rollback-invalid';
                const confirmValue = type === 'duplicates' ? 'dedupe-predictions' : type === 'mapping' ? 'sync-prediction-mapping' : 'rollback-predictions';
                const params = { dry_run: false };
                if (ids) params.ids = ids;
                const response = await axios.post(`/api/predictions/${endpoint}`, null, {
                    params,
                    headers: { 'X-Danger-Confirm': confirmValue },
                });
                // 后端用 200 + success:false 表达"被护栏拦住"，不看这个字段就会把
                // 没执行当成执行成功（第 11 轮 M-F）
                if (response.data && response.data.success === false) {
                    throw new Error(response.data.message || '后端拒绝执行');
                }
                alert(response.data.message);
                maintenancePreview.value = null;
                try { await refreshAfterChange(); }
                catch (refreshError) { alert('已执行' + label + '，只是列表没刷新出来 —— 刷新页面即可'); }
            } catch (error) { alert('执行失败: ' + errorMessage(error)); }
            analyzing.value = false;
        };

        return {
            predictions, predictionDetail, showPredictionDetail, showEditPrediction, editingPrediction,
            predictionFilter, predictionMeta, predictionFilters, filteredPredictions, verifyTask,
            showPredictionMaintenance, maintenancePreview, maintenanceError,
            fetchPredictions, applyPredictionFilters, resetPredictionFilters, setPredictionFilter,
            setPredictionSort,
            predictionPrevPage, predictionNextPage, archivePrediction, restorePrediction,
            viewPredictionDetail, editPrediction, savePrediction, batchAnalyzePredictions,
            restorePredictionVerifyTask, previewPredictionMaintenance, executePredictionMaintenance,
        };
    };
})();

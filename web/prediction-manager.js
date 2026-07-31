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
        const filteredPredictions = computed(() => predictions.value);
        let verifyPollTimer = null;

        const errorMessage = (error) => error.response?.data?.detail || error.response?.data?.message || error.message;

        const fetchPredictions = async () => {
            const params = {
                page: predictionFilters.page,
                page_size: predictionFilters.page_size,
                search: predictionFilters.keyword.trim() || undefined,
                blogger_id: predictionFilters.blogger_id || undefined,
                fund_code: predictionFilters.fund_code.trim() || undefined,
                sector: predictionFilters.sector.trim() || undefined,
                prediction_type: predictionFilters.direction || undefined,
                status: predictionFilters.status || undefined,
                result: predictionFilters.result || undefined,
                start_date: predictionFilters.start_date || undefined,
                end_date: predictionFilters.end_date || undefined,
                archive: predictionFilters.archive,
                lifecycle: predictionFilters.lifecycle || undefined,
                sort: predictionFilters.sort || undefined,
            };
            const response = await axios.get('/api/predictions', { params });
            if (response.data.success) {
                predictions.value = response.data.data || [];
                Object.assign(predictionMeta, response.data.meta || {});
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
        const predictionPrevPage = async () => {
            if (predictionFilters.page > 1) {
                predictionFilters.page -= 1;
                await fetchPredictions();
            }
        };
        const predictionNextPage = async () => {
            if (predictionMeta.has_more) {
                predictionFilters.page += 1;
                await fetchPredictions();
            }
        };

        const refreshAfterChange = async () => {
            await fetchPredictions();
            if (options.onStatsChanged) await options.onStatsChanged();
        };
        const archivePrediction = async (id) => {
            if (!confirm('将该预测移入回收站？之后可以恢复。')) return;
            try {
                await axios.delete(`/api/predictions/${id}`);
                await refreshAfterChange();
            } catch (error) { alert('归档失败: ' + errorMessage(error)); }
        };
        const restorePrediction = async (id) => {
            try {
                await axios.post(`/api/predictions/${id}/restore`);
                await refreshAfterChange();
            } catch (error) { alert('恢复失败: ' + errorMessage(error)); }
        };
        const viewPredictionDetail = async (id) => {
            try {
                const response = await axios.get(`/api/predictions/${id}`);
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
                await fetchPredictions();
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
                if (verifyTask.value?.in_progress) await pollVerifyTask();
                else {
                    analyzing.value = false;
                    await refreshAfterChange();
                }
            } catch (error) {
                analyzing.value = false;
                alert('验证失败: ' + errorMessage(error));
            }
        };
        const restorePredictionVerifyTask = async () => {
            try {
                const response = await axios.get('/api/predictions/verify-all/status');
                verifyTask.value = response.data.data || null;
                if (verifyTask.value?.in_progress) await pollVerifyTask();
                // 非进行中也保留 verifyTask，用于展示上次验证的失败原因汇总
            } catch (error) { console.error('恢复预测验证任务失败', error); }
        };

        const previewPredictionMaintenance = async (type) => {
            analyzing.value = true;
            try {
                let response;
                if (type === 'duplicates') response = await axios.post('/api/predictions/merge-similar');
                if (type === 'mapping') response = await axios.post('/api/predictions/sync-sector-mapping', null, { params: { dry_run: true } });
                if (type === 'rollback') response = await axios.post('/api/predictions/rollback-invalid', null, { params: { dry_run: true } });
                maintenancePreview.value = { type, message: response.data.message, data: response.data.data || {} };
            } catch (error) { alert('预览失败: ' + errorMessage(error)); }
            analyzing.value = false;
        };
        const executePredictionMaintenance = async () => {
            const type = maintenancePreview.value?.type;
            if (!['duplicates', 'mapping', 'rollback'].includes(type)) return;
            const label = type === 'duplicates' ? '重复预测去重' : type === 'mapping' ? '板块映射同步' : '无效验证回溯';
            if (!confirm(`确认执行${label}？系统将按预览清单修改资料。`)) return;
            analyzing.value = true;
            try {
                const endpoint = type === 'duplicates' ? 'dedupe-duplicates' : type === 'mapping' ? 'sync-sector-mapping' : 'rollback-invalid';
                const confirmValue = type === 'duplicates' ? 'dedupe-predictions' : type === 'mapping' ? 'sync-prediction-mapping' : 'rollback-predictions';
                const response = await axios.post(`/api/predictions/${endpoint}`, null, {
                    params: { dry_run: false },
                    headers: { 'X-Danger-Confirm': confirmValue },
                });
                alert(response.data.message);
                maintenancePreview.value = null;
                await refreshAfterChange();
            } catch (error) { alert('执行失败: ' + errorMessage(error)); }
            analyzing.value = false;
        };

        return {
            predictions, predictionDetail, showPredictionDetail, showEditPrediction, editingPrediction,
            predictionFilter, predictionMeta, predictionFilters, filteredPredictions, verifyTask,
            showPredictionMaintenance, maintenancePreview,
            fetchPredictions, applyPredictionFilters, resetPredictionFilters, setPredictionFilter,
            setPredictionSort,
            predictionPrevPage, predictionNextPage, archivePrediction, restorePrediction,
            viewPredictionDetail, editPrediction, savePrediction, batchAnalyzePredictions,
            restorePredictionVerifyTask, previewPredictionMaintenance, executePredictionMaintenance,
        };
    };
})();

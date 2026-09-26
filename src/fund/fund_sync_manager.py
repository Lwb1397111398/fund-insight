"""
智能基金同步管理器
功能：
1. 检测预测与基金的匹配情况
2. 自动抓取缺失的基金
3. 同类型基金去重（一个板块只保留一个基金）
4. 更新基金信息
5. 根据板块-基金映射同步预测关联
"""
import json
from typing import Dict, List, Optional, Tuple
from datetime import date, datetime
from sqlalchemy.orm import Session

from src.models.database import FundInfo, FundHistory, Prediction, SectorFundMapping, SessionLocal
from src.fund.fund_api import fund_api, is_future_nav
from src.fund.fund_auto_manager import fund_auto_manager


class FundSyncManager:
    """基金同步管理器"""
    
    def __init__(self):
        pass

    @staticmethod
    def _parse_nav_date(value) -> Optional[date]:
        if not value:
            return None
        if isinstance(value, date):
            return value
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def retag_prediction(db: Session, pred, new_code: str, new_name: str, *,
                         source: str = 'fund_sync', run_id: str = None,
                         touched_bloggers: set = None,
                         evidence: tuple = None) -> bool:
        """把预测换到另一个标的上：已有结论的必须同时清掉结论并留痕。

        第 18 轮的实测教训：全库有 53 条结论是按改标**之前**那只基金判出来的
        （存的端点净值只等于旧码当天的值，例：id=1903 现挂 512170、结论却是 512010
        在 07-16 的 0.3788）。原因就在这里 —— 这几条改标路径以前既写变更日志都不做，
        也不清结论，于是"改标（B 功能）"静默把"准确率（A 功能）"的依据换掉了。
        `scripts/audit_verdict_evidence.py` 报的 `verdict_under_other_fund` 就是这个族。

        返回是否清掉过结论（调用方可以用来计数）。**别拿它当"改标成功了没"**：
        已经是这个标的、以及被证据门拒了，返回的都是同一个 False。要判"到底动没动"
        只能看 `pred.fund_code` 现在是什么（`sync_sector_mappings` 就是这么数回执的）。

        `evidence`：批量调用方预先读好的 `(窗口内净值日, 库里末笔净值日)`；
        不传就自己问一次（一次一条的调用方不需要关心）。
        """
        from src.services.prediction_change_log_service import (
            add_prediction_change_log, snapshot_prediction)
        from src.services.prediction_verify_service import (
            clear_verification_fields, has_verdict_trace)

        if pred.fund_code == new_code and pred.fund_name == new_name:
            return False
        # 不许把预测绑到"这段窗口它给不出证据"的标的上。生产实测那 15 条验不了的预测不是
        # 随机来的：台账里 57 行 action=maintenance_sync / source=sector_mapping，全是这条
        # 改标路把 003033（末条净值停在 2020-12-08）、508031（停在 2026-06-30）这类
        # **源端已停更**的产品盖到了活预测身上 ⇒ 到期必然判不出来。
        # 尺子只有一把：`target_cannot_evidence_window`（问的就是验证器
        # `_check_fund_data_availability` 那两件事：窗口内点数、终点年龄）。库里一条净值
        # 都没有 ⇒ 不下结论（新档案刚建、还没同步过是常态），交回正常流程。
        # `evidence` 是批量调用方（「按板块对齐标的」）预先读好的那一份，
        # 不传才自己查一次 —— 预览与实跑因此问的是同一句话、给出同一个数。
        from src.services.prediction_lifecycle import (
            target_cannot_evidence_window, window_evidence)

        if evidence is None:
            evidence = window_evidence(db, new_code, pred.prediction_date,
                                       pred.target_date)
        gap = target_cannot_evidence_window(
            evidence[0], evidence[1], pred.prediction_date, pred.target_date)
        if gap:
            print('[跳过改标] 预测 %s 不绑 %s：%s'
                  % (getattr(pred, 'id', '?'), new_code, gap))
            return False
        before = snapshot_prediction(pred)
        # 判据只有一份（`has_verdict_trace`）：以前这里只看 is_correct，
        # 维护服务那边看 verify_count/status/is_expired ⇒ "改标必清结论"有漏网的一条
        had_verdict = has_verdict_trace(pred)
        pred.fund_code = new_code
        pred.fund_name = new_name
        if had_verdict:
            # 结论退回未验证：由下一次验证按**新标的**重判，而不是留着旧标的的数
            clear_verification_fields(pred)
            # 清结论等于改数据 ⇒ 必须留下能整批还原的句柄。
            # `restore_prediction_batch` 只认带 run_id 的日志，而实测 4883 条日志里
            # 3503 条 run_id 为空 —— 调用方不给就自动生成一个，别让"可还原"落空。
            if not run_id:
                run_id = 'retag-%s-%s' % (source[:18],
                                          datetime.now().strftime('%Y%m%d-%H%M%S'))
            if touched_bloggers is not None and pred.blogger_id:
                touched_bloggers.add(pred.blogger_id)
        add_prediction_change_log(db, pred, action='maintenance_sync', source=source,
                                  before_state=before, run_id=run_id)
        return had_verdict

    def check_prediction_fund_match(self, db: Session) -> Dict:
        """
        检查预测与基金的匹配情况
        
        Returns:
            {
                "total_predictions": 总预测数,
                "matched_predictions": 已匹配预测数,
                "unmatched_predictions": 未匹配预测数,
                "unmatched_list": [未匹配的预测信息],
                "missing_sectors": [缺失的板块],
                "missing_funds": [缺失的基金]
            }
        """
        predictions = db.query(Prediction).filter(Prediction.is_deleted == False).all()
        funds = db.query(FundInfo).all()
        
        # 构建基金查找表
        fund_sectors = {f.sector_type: f for f in funds if f.sector_type}
        fund_codes = {f.fund_code: f for f in funds}
        
        matched = 0
        unmatched = 0
        # 被 retag 清掉结论的博主：统计列是存下来的增量值，改完必须重算
        touched_bloggers = set()
        unmatched_list = []
        missing_sectors = set()
        missing_funds = set()
        
        for pred in predictions:
            has_match = False
            
            # 1. 检查是否有直接关联的基金
            if pred.fund_code and pred.fund_code in fund_codes:
                has_match = True
            # 2. 检查板块是否已有基金
            elif pred.sector_type and pred.sector_type in fund_sectors:
                has_match = True
                # 自动关联已有基金
                if not pred.fund_code:
                    fund = fund_sectors[pred.sector_type]
                    # 走统一入口：改标要留痕、有结论要清（见 retag_prediction）
                    self.retag_prediction(db, pred, fund.fund_code, fund.fund_name,
                                          source='fund_sync_link',
                                          touched_bloggers=touched_bloggers)
            # 3. 检查sector是否已有基金
            elif pred.sector and pred.sector in fund_sectors:
                has_match = True
                # 自动关联已有基金
                if not pred.fund_code:
                    fund = fund_sectors[pred.sector]
                    self.retag_prediction(db, pred, fund.fund_code, fund.fund_name,
                                          source='fund_sync_link',
                                          touched_bloggers=touched_bloggers)
            
            if has_match:
                matched += 1
            else:
                unmatched += 1
                unmatched_list.append({
                    "prediction_id": pred.id,
                    "sector": pred.sector,
                    "sector_type": pred.sector_type,
                    "fund_code": pred.fund_code,
                    "fund_name": pred.fund_name
                })
                
                # 记录缺失的板块或基金
                if pred.sector_type:
                    missing_sectors.add(pred.sector_type)
                elif pred.sector:
                    missing_sectors.add(pred.sector)
                
                if pred.fund_code:
                    missing_funds.add(pred.fund_code)
        
        # 统计列是存下来的增量值：清了结论必须重算，否则页面继续显示一个
        # 表里已不存在的基数（第 18 轮 BLOCKER 的另一半）
        from src.utils.blogger_stats import recalculate_blogger_stats
        for blogger_id in touched_bloggers:
            recalculate_blogger_stats(db, blogger_id, commit=False)
        db.commit()
        
        return {
            "total_predictions": len(predictions),
            "matched_predictions": matched,
            "unmatched_predictions": unmatched,
            "unmatched_list": unmatched_list,
            "missing_sectors": list(missing_sectors),
            "missing_funds": list(missing_funds)
        }
    
    def sync_missing_funds(self, db: Session) -> Dict:
        """
        同步缺失的基金
        
        Returns:
            {
                "checked": 检查的预测数,
                "added": 添加的基金数,
                "linked": 关联的预测数,
                "already_ok": 标的本来就对、这一轮一个字都没写的预测数,
                "skipped": 跳过的（同类型已有）,
                "failed": 失败的,
                "details": [详细操作记录]
            }
        """
        result = {
            "checked": 0,
            "added": 0,
            "linked": 0, "already_ok": 0,
            "skipped": 0,
            "failed": 0,
            "details": []
        }
        
        # 获取所有预测
        predictions = db.query(Prediction).filter(Prediction.is_deleted == False).all()
        
        # 被 retag 清掉结论的博主：统计列是存下来的增量值，改完必须重算
        touched_bloggers = set()
        # 获取现有基金
        existing_funds = db.query(FundInfo).all()
        existing_sectors = {f.sector_type: f for f in existing_funds if f.sector_type}
        existing_fund_codes = {f.fund_code: f for f in existing_funds if f.fund_code}
        
        for pred in predictions:
            result["checked"] += 1
            
            # 确定板块
            sector = pred.sector_type or pred.sector
            if not sector:
                result["details"].append({
                    "prediction_id": pred.id,
                    "action": "跳过",
                    "reason": "预测没有板块信息"
                })
                continue
            
            # 1. 检查该板块是否已有基金（同类型去重）
            if sector in existing_sectors:
                # 已有同类型基金，直接关联 —— 但**只允许填空**，不覆盖已有标的。
                # `existing_sectors` 是 `{f.sector_type: f}`：同一板块登记过两只基金时
                # "最后一条赢"（镜像 38 个 sector_type 背后有 >1 只基金）。第 18 轮实测：
                # 页面按钮 `POST /api/funds/update-all` 会因此把 1110 条已判结论里的
                # 515 条改标、并因 S9 的"改标必清结论"把结论一起清空 —— 而这里既不
                # 重算博主统计也不带 run_id，撤不回来。改标是**人工决定**，只属于
                # 带 run_id + 重算统计的 PredictionMaintenanceService.sync_sector_mappings。
                fund = existing_sectors[sector]
                if not pred.fund_code:
                    self.retag_prediction(db, pred, fund.fund_code, fund.fund_name,
                                          source='fund_sync_link',
                                          touched_bloggers=touched_bloggers)
                    result["linked"] += 1
                    result["details"].append({
                        "prediction_id": pred.id,
                        "action": "关联",
                        "fund_code": fund.fund_code,
                        "fund_name": fund.fund_name,
                        "reason": f"板块 '{sector}' 已有基金，直接关联"
                    })
                continue
            
            # 2. 检查预测是否有指定基金代码
            if pred.fund_code:
                # 检查基金是否已存在
                existing = existing_fund_codes.get(pred.fund_code)
                if existing:
                    # 基金已存在，更新sector_type
                    if not existing.sector_type:
                        existing.sector_type = sector
                    # 这一支对 `predictions` **一个字都没写** —— 标的本来就是它。以前它和
                    # 真走了 `retag_prediction` 那两支一起加进 `linked`，回执于是把"什么都没做"
                    # 报成"关联了 N 个预测"（第 51 轮 B-2：页面上那个按钮的回执今天 131 条
                    # 全部来自这一支，两库实测真关联 0 条）。
                    result["already_ok"] += 1
                    result["details"].append({
                        "prediction_id": pred.id,
                        "action": "关联",
                        "fund_code": existing.fund_code,
                        "fund_name": existing.fund_name,
                        "reason": "基金已存在"
                    })
                    continue
                
                # 基金不存在，尝试抓取
                try:
                    fund_info = fund_api.get_fund_info(pred.fund_code)
                    if fund_info:
                        history = fund_api.get_fund_history(pred.fund_code, days=1)
                        actual_day_growth = None
                        if history:
                            actual_day_growth = history[0].get('growth')
                        
                        day_growth = actual_day_growth if actual_day_growth is not None else fund_info.get('day_growth')
                        
                        new_fund = FundInfo(
                            fund_code=pred.fund_code,
                            fund_name=fund_info.get('fund_name', pred.fund_name or '未知'),
                            fund_type=fund_info.get('fund_type', '未知类型'),
                            sector_type=sector,
                            latest_nav=fund_info.get('nav'),
                            nav_date=self._parse_nav_date(fund_info.get('nav_date')),
                            day_growth=day_growth,
                            can_delete=True
                        )
                        db.add(new_fund)

                        # 获取历史数据（用于AI分析）
                        try:
                            from src.fund.fund_api import fund_data_manager
                            fund_data_manager.update_fund_history(pred.fund_code, days=30, db=db)
                            print(f"[FundSync] 已获取基金 {pred.fund_code} 的历史数据")
                        except Exception as e:
                            print(f"[FundSync] 获取基金 {pred.fund_code} 历史数据失败: {e}")
                        
                        # 更新查找表
                        existing_sectors[sector] = new_fund
                        existing_fund_codes[new_fund.fund_code] = new_fund

                        result["added"] += 1
                        result["details"].append({
                            "prediction_id": pred.id,
                            "action": "添加",
                            "fund_code": new_fund.fund_code,
                            "fund_name": new_fund.fund_name,
                            "reason": f"根据预测指定代码抓取"
                        })
                        continue
                except Exception as e:
                    result["failed"] += 1
                    result["details"].append({
                        "prediction_id": pred.id,
                        "action": "失败",
                        "fund_code": pred.fund_code,
                        "reason": f"抓取失败: {str(e)}"
                    })
                    continue
            
            # 3. 没有指定基金，自动抓取板块对应基金
            try:
                success, message, fund = fund_auto_manager.auto_add_fund_for_prediction(sector, db)
                if success and fund:
                    # 获取历史数据（用于AI分析）
                    try:
                        from src.fund.fund_api import fund_data_manager
                        fund_data_manager.update_fund_history(fund.fund_code, days=30, db=db)
                        print(f"[FundSync] 已获取基金 {fund.fund_code} 的历史数据")
                    except Exception as e:
                        print(f"[FundSync] 获取基金 {fund.fund_code} 历史数据失败: {e}")
                    
                    # 更新查找表
                    existing_sectors[sector] = fund
                    existing_fund_codes[fund.fund_code] = fund

                    # 关联预测（同样走统一入口，别绕过留痕与清结论）
                    self.retag_prediction(db, pred, fund.fund_code, fund.fund_name,
                                          source='fund_sync_new_fund',
                                          touched_bloggers=touched_bloggers)

                    result["added"] += 1
                    result["linked"] += 1
                    result["details"].append({
                        "prediction_id": pred.id,
                        "action": "添加并关联",
                        "fund_code": fund.fund_code,
                        "fund_name": fund.fund_name,
                        "reason": f"自动抓取板块 '{sector}' 的基金"
                    })
                else:
                    result["failed"] += 1
                    result["details"].append({
                        "prediction_id": pred.id,
                        "action": "失败",
                        "sector": sector,
                        "reason": message
                    })
            except Exception as e:
                result["failed"] += 1
                result["details"].append({
                    "prediction_id": pred.id,
                    "action": "失败",
                    "sector": sector,
                    "reason": f"自动抓取失败: {str(e)}"
                })

        # 循环结束后统一提交
        from src.utils.blogger_stats import recalculate_blogger_stats
        for blogger_id in touched_bloggers:
            recalculate_blogger_stats(db, blogger_id, commit=False)
        db.commit()
        return result
    
    def update_all_funds_info(self, db: Session) -> Dict:
        """
        更新所有基金信息（净值、涨跌幅、历史净值）

        Returns:
            {
                "total": 总基金数,
                "updated": 更新成功数,
                "failed": 更新失败数,
                "failed_funds": [失败的基金列表],
                "details": [详细记录]
            }
        """
        funds = db.query(FundInfo).all()
        total = len(funds)

        result = {
            "total": total,
            "updated": 0,
            "failed": 0,
            "failed_funds": [],
            "details": []
        }

        print(f"[FundSync] 开始更新 {total} 只基金...")

        for i, fund in enumerate(funds, 1):
            try:
                print(f"[FundSync] 更新基金 ({i}/{total}): {fund.fund_code} {fund.fund_name}")
                fund_info = fund_api.get_fund_info(fund.fund_code)
                if fund_info:
                    fund.latest_nav = fund_info.get('nav', fund.latest_nav)
                    fund.updated_at = datetime.now()
                    fund.day_growth = fund_info.get('day_growth', fund.day_growth)

                    # 使用实际净值日期（jzrq），而不是 date.today()
                    # 但也**不能是还没到的那天**：2026-09-25 那次同步里 000725（大成添利宝货币B）
                    # 的上游 jzrq 直接给 09-27 ⇒ 档案头自己声称"我有 09-27 的净值"。
                    # 历史行走取数入口那道门（`usable_history_rows`），档案头是同一件事，
                    # 所以共用同一个谓词，不再写第二份比较。
                    parsed_nav_date = self._parse_nav_date(fund_info.get('nav_date'))
                    if parsed_nav_date and is_future_nav(parsed_nav_date):
                        print(f"[FundSync] 基金 {fund.fund_code} 上游给的净值日期 {parsed_nav_date} "
                              f"晚于今天 ⇒ 档案头日期保持不动（现为 {fund.nav_date}）")
                    elif parsed_nav_date:
                        fund.nav_date = parsed_nav_date

                    # 更新历史净值表
                    self._update_fund_history(db, fund.fund_code, fund.fund_name)

                    result["updated"] += 1
                    result["details"].append({
                        "fund_code": fund.fund_code,
                        "fund_name": fund.fund_name,
                        "action": "更新",
                        "nav": fund.latest_nav,
                        "nav_date": str(fund.nav_date),
                        "day_growth": fund.day_growth
                    })
                    print(f"[FundSync] 基金 {fund.fund_code} 更新成功")
                else:
                    result["failed"] += 1
                    result["failed_funds"].append({
                        "fund_code": fund.fund_code,
                        "fund_name": fund.fund_name,
                        "reason": "无法获取基金信息（可能是测试基金或代码无效）"
                    })
                    print(f"[FundSync] 基金 {fund.fund_code} 获取信息失败")
            except Exception as e:
                result["failed"] += 1
                result["failed_funds"].append({
                    "fund_code": fund.fund_code,
                    "fund_name": fund.fund_name,
                    "reason": str(e)
                })
                print(f"[FundSync] 基金 {fund.fund_code} 更新异常: {e}")

        # 循环结束后统一提交
        try:
            db.commit()
            print(f"[FundSync] 数据库提交成功")
        except Exception as e:
            print(f"[FundSync] 数据库提交失败: {e}")
            db.rollback()
            raise

        print(f"[FundSync] 更新完成: 成功 {result['updated']}, 失败 {result['failed']}")
        return result

    def _update_fund_history(self, db: Session, fund_code: str, fund_name: str, days: int = 30):
        """更新单只基金的历史净值"""
        try:
            history = fund_api.get_fund_history(fund_code, days)
            if not history:
                return

            # 批量查询已存在的日期
            existing_dates = set(
                r[0] for r in db.query(FundHistory.nav_date).filter(
                    FundHistory.fund_code == fund_code
                ).all()
            )

            for item in history:
                if item['date'] not in existing_dates:
                    record = FundHistory(
                        fund_code=fund_code,
                        fund_name=fund_name,
                        nav_date=item['date'],
                        nav=item['nav'],
                        day_growth=item['growth']
                    )
                    db.add(record)
        except Exception as e:
            print(f"[FundSync] 更新基金 {fund_code} 历史净值失败: {e}")
    
    def sync_predictions_by_sector_mapping(self, db: Session) -> Dict:
        """
        根据板块-基金映射表同步预测和基金数据

        完整流程：
        1. 刷新 SectorFundService 缓存
        2. 加载板块-基金映射（reviewed=True 优先）
        3. 遍历所有预测，更新基金关联
        4. 确保新基金在 FundInfo 表中（不存在则添加）
        5. 更新 FundInfo 中旧基金的 sector_type（不删除）
        6. 重置已验证预测的状态（基金变了，验证结果可能无效）

        Returns:
            {
                "total_mappings": 映射数,
                "predictions_updated": 预测更新数,
                "predictions_unchanged": 预测未变数,
                "predictions_no_mapping": 预测无映射数,
                "funds_added": 新增基金数,
                "funds_sector_updated": 基金板块更新数,
                "verified_reset": 已验证重置数,
                "details": [详细记录]
            }
        """
        from src.services.sector_fund_service import get_sector_fund_service

        # 被 retag 清掉结论的博主：统计列是存下来的增量值，改完必须重算
        touched_bloggers = set()
        result = {
            "total_mappings": 0,
            "predictions_updated": 0,
            "predictions_unchanged": 0,
            "predictions_no_mapping": 0,
            "funds_added": 0,
            "funds_sector_updated": 0,
            "verified_reset": 0,
            "details": []
        }

        # 1. 刷新缓存
        service = get_sector_fund_service(db)
        service.refresh_cache()

        # 2. 加载板块-基金映射（reviewed=True 优先）
        from src.services.sector_identity_audit import servable_predicate
        mappings = db.query(SectorFundMapping).filter(
            SectorFundMapping.is_active == True,
            servable_predicate(),
        ).all()

        # 构建映射表：sector_name -> {code, name, reviewed}
        sector_map = {}
        for m in mappings:
            if m.sector_name not in sector_map or (m.reviewed and not sector_map[m.sector_name].get('reviewed')):
                sector_map[m.sector_name] = {
                    'code': m.fund_code,
                    'name': m.fund_name,
                    'reviewed': m.reviewed or False
                }

        result["total_mappings"] = len(sector_map)

        if not sector_map:
            result["details"].append({"action": "跳过", "reason": "板块-基金映射表为空"})
            return result

        # 3. 预加载现有基金（按 fund_code 索引，使用 no_autoflush 避免干扰）
        with db.no_autoflush:
            existing_funds_by_code = {f.fund_code: f for f in db.query(FundInfo).all()}

        # 4. 获取所有预测
        predictions = db.query(Prediction).filter(Prediction.is_deleted == False).all()

        for pred in predictions:
            # 确定板块名称
            sector = pred.sector or pred.sector_type
            if not sector:
                result["predictions_no_mapping"] += 1
                continue

            # 查找映射
            mapping = sector_map.get(sector)
            if not mapping:
                result["predictions_no_mapping"] += 1
                continue

            # 检查是否需要更新
            if pred.fund_code == mapping['code']:
                result["predictions_unchanged"] += 1
                continue

            # 记录旧基金信息
            old_code = pred.fund_code
            old_name = pred.fund_name

            # 更新预测的基金关联：走统一入口（留痕 + 清掉旧标的判出的结论）
            cleared_verdict = self.retag_prediction(
                db, pred, mapping['code'], mapping['name'],
                source='fund_sync_sector_map', touched_bloggers=touched_bloggers)
            result["predictions_updated"] += 1

            detail = {
                "prediction_id": pred.id,
                "sector": sector,
                "old_fund": f"{old_name}({old_code})" if old_code else "无",
                "new_fund": f"{mapping['name']}({mapping['code']})"
            }

            # 5.（原"重置已验证预测"分支已删）结论的清退由上面 `retag_prediction` 一处完成。
            #    这里原来还有一份手抄字段清单，而且状态词表用的是 'correct'/'wrong'/'expired'
            #    —— 全仓实际写的是 'success'/'failed'，所以那段条件永不成立（死码），
            #    而它把 verify_score 清成 0 而不是 None，正是第 15 轮"分数与结论打脸"的形状。
            if cleared_verdict:
                result["verified_reset"] += 1
                detail["reset_verified"] = True

            result["details"].append(detail)

        # 6. 确保新基金在 FundInfo 表中
        for sector_name, mapping in sector_map.items():
            fund_code = mapping['code']
            fund_name = mapping['name']

            if fund_code in existing_funds_by_code:
                # 基金已存在，更新 sector_type
                fund = existing_funds_by_code[fund_code]
                if fund.sector_type != sector_name:
                    fund.sector_type = sector_name
                    result["funds_sector_updated"] += 1
                    result["details"].append({
                        "action": "更新基金板块",
                        "fund_code": fund_code,
                        "fund_name": fund_name,
                        "old_sector": fund.sector_type,
                        "new_sector": sector_name
                    })
            else:
                # 基金不存在，添加新基金
                try:
                    fund_info = fund_api.get_fund_info(fund_code)
                    if fund_info:
                        history = fund_api.get_fund_history(fund_code, days=1)
                        actual_day_growth = history[0].get('growth') if history else None
                        day_growth = actual_day_growth if actual_day_growth is not None else fund_info.get('day_growth')

                        new_fund = FundInfo(
                            fund_code=fund_code,
                            fund_name=fund_info.get('fund_name', fund_name),
                            fund_type=fund_info.get('fund_type', '未知类型'),
                            sector_type=sector_name,
                            latest_nav=fund_info.get('nav'),
                            nav_date=self._parse_nav_date(fund_info.get('nav_date')),
                            day_growth=day_growth,
                            can_delete=True
                        )
                        db.add(new_fund)
                        existing_funds_by_code[fund_code] = new_fund

                        # 获取历史数据
                        try:
                            from src.fund.fund_api import fund_data_manager
                            fund_data_manager.update_fund_history(fund_code, days=30, db=db)
                        except Exception as e:
                            print(f"[FundSync] 获取基金 {fund_code} 历史数据失败: {e}")

                        result["funds_added"] += 1
                        result["details"].append({
                            "action": "添加基金",
                            "fund_code": fund_code,
                            "fund_name": fund_name,
                            "sector": sector_name
                        })
                except Exception as e:
                    result["details"].append({
                        "action": "添加基金失败",
                        "fund_code": fund_code,
                        "fund_name": fund_name,
                        "error": str(e)
                    })

        # 统计列是存下来的增量值：清了结论必须重算，否则页面继续显示一个
        # 表里已不存在的基数（第 18 轮 BLOCKER 的另一半）
        from src.utils.blogger_stats import recalculate_blogger_stats
        for blogger_id in touched_bloggers:
            recalculate_blogger_stats(db, blogger_id, commit=False)
        db.commit()
        return result

    def full_sync(self, db: Session = None) -> Dict:
        """
        执行完整的基金同步流程

        1. 检测预测-基金匹配情况
        2. 同步缺失的基金
        3. 更新所有基金信息

        Returns:
            完整的同步报告
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True

        try:
            print("[FundSync] 开始完整基金同步...")

            # 1. 检测匹配情况
            print("[FundSync] 步骤1: 检测预测-基金匹配情况...")
            match_report = self.check_prediction_fund_match(db)
            print(f"[FundSync] 检测完成: 总预测 {match_report['total_predictions']}, "
                  f"已匹配 {match_report['matched_predictions']}, "
                  f"未匹配 {match_report['unmatched_predictions']}")

            # 2. 同步缺失的基金
            print("[FundSync] 步骤2: 同步缺失的基金...")
            sync_report = self.sync_missing_funds(db)
            print(f"[FundSync] 同步完成: 添加 {sync_report['added']}, "
                  f"关联 {sync_report['linked']}, "
                  f"跳过 {sync_report['skipped']}, "
                  f"失败 {sync_report['failed']}")

            # 3. 更新基金信息
            print("[FundSync] 步骤3: 更新基金信息...")
            update_report = self.update_all_funds_info(db)
            print(f"[FundSync] 更新完成: 成功 {update_report['updated']}, "
                  f"失败 {update_report['failed']}")

            # 获取失败基金列表
            failed_funds = update_report.get('failed_funds', [])
            if failed_funds:
                print("[FundSync] 失败详情:")
                for fund in failed_funds:
                    print(f"  - {fund['fund_code']} ({fund['fund_name']}): {fund['reason']}")

            # 构建成功消息
            success_msg = f"同步完成：检测 {match_report['total_predictions']} 个预测，"
            success_msg += f"新增 {sync_report['added']} 个基金，"
            success_msg += f"关联 {sync_report['linked']} 个预测，"
            if sync_report.get("already_ok"):
                # "另有 N 条本来就对"必须单独说：不说的话这一堆会被读成"推进了 N 条"
                success_msg += f"另有 {sync_report['already_ok']} 条标的本来就是它、未做任何改动，"
            success_msg += f"更新 {update_report['updated']} 个基金"

            if failed_funds:
                success_msg += f"\n\n失败 {len(failed_funds)} 个:"
                for fund in failed_funds[:5]:
                    success_msg += f"\n- {fund['fund_code']}({fund['fund_name']}): {fund['reason']}"
                if len(failed_funds) > 5:
                    success_msg += f"\n...等共 {len(failed_funds)} 个"

            failed_count = sync_report.get('failed', 0) + update_report.get('failed', 0)
            return {
                "success": failed_count == 0,
                "match_report": match_report,
                "sync_report": sync_report,
                "update_report": update_report,
                "summary": {
                    "total_predictions": match_report['total_predictions'],
                    "total_funds": update_report['total'],
                    "new_funds_added": sync_report['added'],
                    "predictions_linked": sync_report['linked'],
                    "funds_updated": update_report['updated'],
                    "funds_failed": update_report['failed'],
                    "failed_funds": failed_funds
                },
                "message": success_msg
            }

        except Exception as e:
            print(f"[FundSync] 同步失败: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "error": str(e)
            }
        finally:
            if close_db:
                db.close()


# 全局实例
fund_sync_manager = FundSyncManager()


def get_sync_manager() -> FundSyncManager:
    """获取基金同步管理器实例"""
    return fund_sync_manager

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
from src.fund.fund_api import fund_api
from src.fund.fund_auto_manager import fund_auto_manager


class FundSyncManager:
    """基金同步管理器"""
    
    def __init__(self):
        pass
    
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
                    pred.fund_code = fund.fund_code
                    pred.fund_name = fund.fund_name
            # 3. 检查sector是否已有基金
            elif pred.sector and pred.sector in fund_sectors:
                has_match = True
                # 自动关联已有基金
                if not pred.fund_code:
                    fund = fund_sectors[pred.sector]
                    pred.fund_code = fund.fund_code
                    pred.fund_name = fund.fund_name
            
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
                "skipped": 跳过的（同类型已有）,
                "failed": 失败的,
                "details": [详细操作记录]
            }
        """
        result = {
            "checked": 0,
            "added": 0,
            "linked": 0,
            "skipped": 0,
            "failed": 0,
            "details": []
        }
        
        # 获取所有预测
        predictions = db.query(Prediction).filter(Prediction.is_deleted == False).all()
        
        # 获取现有基金
        existing_funds = db.query(FundInfo).all()
        existing_sectors = {f.sector_type: f for f in existing_funds if f.sector_type}
        
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
                # 已有同类型基金，直接关联
                fund = existing_sectors[sector]
                if pred.fund_code != fund.fund_code:
                    pred.fund_code = fund.fund_code
                    pred.fund_name = fund.fund_name
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
                existing = db.query(FundInfo).filter(FundInfo.fund_code == pred.fund_code).first()
                if existing:
                    # 基金已存在，更新sector_type
                    if not existing.sector_type:
                        existing.sector_type = sector
                    result["linked"] += 1
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
                            nav_date=date.today(),
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

                    # 关联预测
                    pred.fund_code = fund.fund_code
                    pred.fund_name = fund.fund_name

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
                    nav_date_str = fund_info.get('nav_date', '')
                    if nav_date_str:
                        try:
                            fund.nav_date = datetime.strptime(nav_date_str, '%Y-%m-%d').date()
                        except (ValueError, TypeError):
                            fund.nav_date = date.today()
                    else:
                        fund.nav_date = date.today()

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
        mappings = db.query(SectorFundMapping).filter(
            SectorFundMapping.is_active == True
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

            # 更新预测的基金关联
            pred.fund_code = mapping['code']
            pred.fund_name = mapping['name']
            result["predictions_updated"] += 1

            detail = {
                "prediction_id": pred.id,
                "sector": sector,
                "old_fund": f"{old_name}({old_code})" if old_code else "无",
                "new_fund": f"{mapping['name']}({mapping['code']})"
            }

            # 5. 重置已验证预测的状态（基金变了，验证结果可能无效）
            if pred.status in ('correct', 'wrong', 'expired') and pred.verify_count and pred.verify_count > 0:
                pred.status = 'pending'
                pred.is_correct = None
                pred.actual_change = None
                pred.verify_count = 0
                pred.verify_score = 0
                pred.verified_at = None
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
                            nav_date=date.today(),
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
            success_msg += f"更新 {update_report['updated']} 个基金"

            if failed_funds:
                success_msg += f"\n\n失败 {len(failed_funds)} 个:"
                for fund in failed_funds[:5]:
                    success_msg += f"\n- {fund['fund_code']}({fund['fund_name']}): {fund['reason']}"
                if len(failed_funds) > 5:
                    success_msg += f"\n...等共 {len(failed_funds)} 个"

            return {
                "success": True,
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

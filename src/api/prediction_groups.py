"""
预测组 API - 将同一博主对同一基金的多个预测分组管理

功能：
- 自动分组：按预测周期和时间间隔自动分组
- 代表预测：每组选一个代表用于验证
- 合并分析：LLM 生成组内预测的综合分析
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import and_
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import json

from src.api.deps import get_db
from src.models.database import Prediction, PredictionGroup, Blogger, FundInfo
from src.analyzer.llm_analyzer import get_analyzer

router = APIRouter(prefix="/api/prediction-groups", tags=["预测组"])


def _parse_period_days(period: str) -> int:
    """解析预测周期为天数"""
    if not period:
        return 7
    
    period = period.lower()
    if '周' in period or '星期' in period:
        num = int(''.join(filter(str.isdigit, period)) or '1')
        return num * 7
    elif '月' in period:
        num = int(''.join(filter(str.isdigit, period)) or '1')
        return num * 30
    elif '日' in period or '天' in period:
        num = int(''.join(filter(str.isdigit, period)) or '7')
        return num
    else:
        return 7


def _group_predictions(predictions: List[Prediction]) -> List[List[Prediction]]:
    """
    将预测按周期和时间分组
    
    规则：
    1. 相同周期的预测
    2. 相邻预测间隔 ≤ 周期天数
    3. 每组至少 2 个预测
    """
    if not predictions:
        return []
    
    # 按周期分组
    from collections import defaultdict
    period_groups = defaultdict(list)
    
    for pred in predictions:
        if pred.prediction_period and pred.prediction_date:
            period_groups[pred.prediction_period].append(pred)
    
    result = []
    
    for period, preds in period_groups.items():
        # 按日期排序
        preds.sort(key=lambda p: p.prediction_date)
        
        # 计算周期天数窗口
        window_days = _parse_period_days(period)
        
        # 按时间窗口分组
        current_group = [preds[0]]
        
        for i in range(1, len(preds)):
            days_diff = (preds[i].prediction_date - preds[i-1].prediction_date).days
            
            if days_diff <= window_days:
                # 在窗口内，加入当前组
                current_group.append(preds[i])
            else:
                # 超出窗口，保存当前组并开始新组
                if len(current_group) >= 2:
                    result.append(current_group)
                current_group = [preds[i]]
        
        # 保存最后一组
        if len(current_group) >= 2:
            result.append(current_group)
    
    return result


def _analyze_group(blogger_name: str, fund_name: str, predictions: List[Prediction]) -> Dict:
    """使用 LLM 分析预测组"""
    if len(predictions) < 2:
        return {}
    
    analyzer = get_analyzer()
    
    # 准备预测数据
    preds_data = []
    for p in predictions:
        preds_data.append({
            "date": p.prediction_date.isoformat() if p.prediction_date else None,
            "type": p.prediction_type,
            "content": p.prediction_content[:100],
            "confidence": p.confidence
        })
    
    prompt = f"""请分析以下博主对同一基金的多个预测，生成综合观点。

【博主】{blogger_name}
【基金】{fund_name}

【预测记录】
{json.dumps(preds_data, ensure_ascii=False, indent=2)}

请返回 JSON 格式：
{{
    "overall_sentiment": "bullish/bearish/neutral",
    "merged_content": "综合分析内容（200 字以内）",
    "consistency_score": 0-100
}}
"""
    
    try:
        response = analyzer._call_llm(prompt, task_type='core', max_tokens=800, temperature=0.3)
        
        # 解析 JSON
        import re
        json_match = re.search(r'\{[\s\S]+\}', response)
        if json_match:
            json_str = json_match.group()
            json_str = re.sub(r'//.*$', '', json_str, flags=re.MULTILINE)
            json_str = re.sub(r',\s*}', '}', json_str)
            json_str = re.sub(r',\s*]', ']', json_str)
            json_str = json_str.replace("'", '"')
            
            result = json.loads(json_str)
            return result
    except Exception as e:
        print(f"[Prediction Group] LLM 分析失败：{e}")
    
    return {}


@router.get("/list")
def get_prediction_groups(
    blogger_id: Optional[int] = None,
    fund_code: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取预测组列表"""
    try:
        query = db.query(PredictionGroup).filter(PredictionGroup.is_active == True)
        
        if blogger_id:
            query = query.filter(PredictionGroup.blogger_id == blogger_id)
        if fund_code:
            query = query.filter(PredictionGroup.fund_code == fund_code)
        
        groups = query.order_by(PredictionGroup.updated_at.desc()).all()
        
        result = []
        for group in groups:
            blogger = db.query(Blogger).filter(Blogger.id == group.blogger_id).first()
            result.append({
                "id": group.id,
                "blogger_id": group.blogger_id,
                "blogger_name": blogger.name if blogger else "未知",
                "fund_code": group.fund_code,
                "fund_name": group.fund_name,
                "prediction_period": group.prediction_period,
                "prediction_count": group.prediction_count,
                "representative_id": group.representative_id,
                "overall_sentiment": group.overall_sentiment,
                "consistency_score": group.consistency_score,
                "start_date": group.start_date.isoformat() if group.start_date else None,
                "end_date": group.end_date.isoformat() if group.end_date else None,
                "is_verified": group.is_verified,
                "updated_at": group.updated_at.isoformat() if group.updated_at else None
            })
        
        return {"success": True, "data": result}
    
    except Exception as e:
        print(f"[Prediction Group] 获取列表失败：{e}")
        return {"success": False, "message": str(e)}


@router.post("/create")
def create_prediction_group(
    blogger_id: int,
    fund_code: str,
    db: Session = Depends(get_db)
):
    """为指定博主和基金创建预测组"""
    try:
        # 获取所有相关预测
        predictions = db.query(Prediction).filter(
            Prediction.blogger_id == blogger_id,
            Prediction.fund_code == fund_code,
            Prediction.prediction_date != None
        ).order_by(Prediction.prediction_date.asc()).all()
        
        if len(predictions) < 2:
            return {"success": False, "message": "至少需要 2 个预测才能分组"}
        
        # 分组
        groups = _group_predictions(predictions)
        
        if not groups:
            return {"success": False, "message": "没有符合条件的预测组（需要相同周期且时间接近）"}
        
        # 获取博主和基金信息
        blogger = db.query(Blogger).filter(Blogger.id == blogger_id).first()
        fund = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
        
        created_groups = []
        
        for group_preds in groups:
            # 检查是否已存在包含这些预测的组
            existing = db.query(PredictionGroup).filter(
                PredictionGroup.blogger_id == blogger_id,
                PredictionGroup.fund_code == fund_code,
                PredictionGroup.prediction_ids != None
            ).first()
            
            # 简单的存在性检查（可以优化）
            if existing:
                continue
            
            # 选最后一个作为代表（最新的）
            representative = group_preds[-1]
            
            # LLM 分析
            analysis = _analyze_group(
                blogger.name if blogger else "未知",
                fund.fund_name if fund else fund_code,
                group_preds
            )
            
            # 创建组
            group = PredictionGroup(
                blogger_id=blogger_id,
                fund_code=fund_code,
                fund_name=fund.fund_name if fund else None,
                prediction_period=representative.prediction_period,
                prediction_ids=[p.id for p in group_preds],
                representative_id=representative.id,
                prediction_count=len(group_preds),
                start_date=group_preds[0].prediction_date,
                end_date=group_preds[-1].prediction_date,
                overall_sentiment=analysis.get("overall_sentiment", "neutral"),
                merged_content=analysis.get("merged_content", ""),
                consistency_score=analysis.get("consistency_score", 50),
                is_active=True
            )
            
            db.add(group)
            db.flush()
            created_groups.append({
                "id": group.id,
                "prediction_count": len(group_preds)
            })
        
        db.commit()
        
        return {
            "success": True,
            "message": f"创建了 {len(created_groups)} 个预测组",
            "data": created_groups
        }
    
    except Exception as e:
        db.rollback()
        print(f"[Prediction Group] 创建失败：{e}")
        return {"success": False, "message": str(e)}


@router.get("/{group_id}")
def get_prediction_group_detail(group_id: int, db: Session = Depends(get_db)):
    """获取预测组详情"""
    try:
        group = db.query(PredictionGroup).filter(PredictionGroup.id == group_id).first()
        if not group:
            return {"success": False, "message": "预测组不存在"}
        
        # 获取所有成员预测
        predictions = db.query(Prediction).filter(
            Prediction.id.in_(group.prediction_ids)
        ).all()
        
        members = []
        for p in predictions:
            members.append({
                "id": p.id,
                "prediction_date": p.prediction_date.isoformat() if p.prediction_date else None,
                "prediction_type": p.prediction_type,
                "prediction_content": p.prediction_content,
                "prediction_period": p.prediction_period,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "is_representative": p.id == group.representative_id
            })
        
        return {
            "success": True,
            "data": {
                "id": group.id,
                "blogger_id": group.blogger_id,
                "fund_code": group.fund_code,
                "fund_name": group.fund_name,
                "prediction_period": group.prediction_period,
                "prediction_count": group.prediction_count,
                "representative_id": group.representative_id,
                "overall_sentiment": group.overall_sentiment,
                "merged_content": group.merged_content,
                "consistency_score": group.consistency_score,
                "is_verified": group.is_verified,
                "verify_result": group.verify_result,
                "members": members
            }
        }
    
    except Exception as e:
        print(f"[Prediction Group] 获取详情失败：{e}")
        return {"success": False, "message": str(e)}


@router.delete("/{group_id}")
def dissolve_prediction_group(group_id: int, db: Session = Depends(get_db)):
    """解散预测组（不删除原始预测）"""
    try:
        group = db.query(PredictionGroup).filter(PredictionGroup.id == group_id).first()
        if not group:
            return {"success": False, "message": "预测组不存在"}
        
        group.is_active = False
        db.commit()
        
        return {"success": True, "message": "预测组已解散"}
    
    except Exception as e:
        db.rollback()
        print(f"[Prediction Group] 解散失败：{e}")
        return {"success": False, "message": str(e)}

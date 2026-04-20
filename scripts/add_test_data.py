"""
添加测试数据脚本
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
from src.models.database import SessionLocal, Blogger, Post, Prediction, Viewpoint, FundInfo, FundHistory

def add_test_data():
    db = SessionLocal()
    
    try:
        # 获取现有博主
        bloggers = db.query(Blogger).all()
        if not bloggers:
            print("没有博主数据，先创建测试博主...")
            blogger1 = Blogger(name="投资大师张三", platform="eastmoney", grade="S", accuracy_rate=75.5, total_predictions=20, correct_predictions=15)
            blogger2 = Blogger(name="基金达人李四", platform="sina", grade="A", accuracy_rate=68.0, total_predictions=15, correct_predictions=10)
            blogger3 = Blogger(name="理财专家王五", platform="xiaohongshu", grade="B", accuracy_rate=55.0, total_predictions=10, correct_predictions=5)
            db.add_all([blogger1, blogger2, blogger3])
            db.commit()
            bloggers = [blogger1, blogger2, blogger3]
        
        print(f"找到 {len(bloggers)} 个博主")
        
        # 添加帖子数据
        existing_posts = db.query(Post).count()
        if existing_posts == 0:
            print("添加帖子数据...")
            posts_data = []
            for i, blogger in enumerate(bloggers):
                for j in range(3):
                    posts_data.append(Post(
                        blogger_id=blogger.id,
                        title=f"{'看多' if j % 2 == 0 else '看空'}科技板块，{'新能源' if j % 3 == 0 else '半导体'}机会来了",
                        content=f"这是{blogger.name}的第{j+1}篇帖子，主要分析当前市场走势和投资机会。"
                               f"{'看好科技板块的长期发展，建议逢低布局。' if j % 2 == 0 else '短期市场可能调整，建议观望为主。'}"
                               f"重点关注：新能源、半导体、人工智能等板块。",
                        post_date=date.today() - timedelta(days=j*3 + i),
                        analyzed=j % 2 == 0
                    ))
            db.add_all(posts_data)
            db.commit()
            print(f"添加了 {len(posts_data)} 条帖子")
        
        # 添加预测数据
        existing_predictions = db.query(Prediction).count()
        if existing_predictions == 0:
            print("添加预测数据...")
            predictions_data = []
            funds = [
                ("110022", "易方达消费行业"),
                ("000751", "嘉实新兴产业"),
                ("260108", "景顺长城新兴成长"),
                ("161725", "招商中证白酒"),
                ("519674", "银河创新成长")
            ]
            
            for i, blogger in enumerate(bloggers):
                for j, (fund_code, fund_name) in enumerate(funds[:3]):
                    pred_type = "up" if (i + j) % 2 == 0 else "down"
                    target_date = date.today() + timedelta(days=30 - j*5)
                    is_expired = target_date < date.today()
                    
                    predictions_data.append(Prediction(
                        post_id=1,
                        blogger_id=blogger.id,
                        fund_code=fund_code,
                        fund_name=fund_name,
                        sector="消费" if j == 0 else "科技" if j == 1 else "新能源",
                        sector_type="消费" if j == 0 else "科技" if j == 1 else "新能源",
                        prediction_type=pred_type,
                        prediction_content=f"{'看涨' if pred_type == 'up' else '看跌'}{fund_name}，目标涨幅{'5%' if pred_type == 'up' else '-3%'}",
                        confidence=60 + i * 10 + j * 5,
                        prediction_date=date.today() - timedelta(days=30 - j*3),
                        prediction_period="1个月",
                        target_date=target_date,
                        status="expired" if is_expired else "pending",
                        is_expired=is_expired,
                        is_correct=is_expired and (i + j) % 3 == 0
                    ))
            db.add_all(predictions_data)
            db.commit()
            print(f"添加了 {len(predictions_data)} 条预测")
        
        # 添加观点数据
        existing_viewpoints = db.query(Viewpoint).count()
        if existing_viewpoints == 0:
            print("添加观点数据...")
            viewpoints_data = []
            authors = ["股海老手", "价值投资者", "趋势跟踪者", "量化交易员", "长线持有者"]
            directions = ["bullish", "bearish", "neutral"]
            
            for i in range(15):
                viewpoints_data.append(Viewpoint(
                    blogger_id=bloggers[i % len(bloggers)].id if i < 9 else None,
                    fund_code=funds[i % 5][0],
                    fund_name=funds[i % 5][1],
                    content=f"关于{funds[i % 5][1]}的观点：{'看好后市表现，建议逢低加仓。' if i % 3 == 0 else '短期可能震荡，建议观望。' if i % 3 == 1 else '风险较大，建议减仓。'}"
                           f"主要理由：{'基本面良好，估值合理。' if i % 2 == 0 else '技术面走弱，等待企稳。'}",
                    author=authors[i % 5] if i >= 9 else bloggers[i % len(bloggers)].name,
                    source="crawler" if i >= 9 else "manual",
                    market_direction=directions[i % 3],
                    confidence=50 + (i % 5) * 10,
                    viewpoint_date=date.today() - timedelta(days=i % 7)
                ))
            db.add_all(viewpoints_data)
            db.commit()
            print(f"添加了 {len(viewpoints_data)} 条观点")
        
        # 添加基金数据
        existing_funds = db.query(FundInfo).count()
        if existing_funds == 0:
            print("添加基金数据...")
            funds_info = [
                FundInfo(fund_code="110022", fund_name="易方达消费行业", fund_type="混合型", sector_type="消费",
                        latest_nav=3.2156, nav_date=date.today(), day_growth=1.25, week_growth=2.35, month_growth=5.68),
                FundInfo(fund_code="000751", fund_name="嘉实新兴产业", fund_type="混合型", sector_type="科技",
                        latest_nav=2.8734, nav_date=date.today(), day_growth=-0.85, week_growth=1.52, month_growth=4.21),
                FundInfo(fund_code="260108", fund_name="景顺长城新兴成长", fund_type="混合型", sector_type="成长",
                        latest_nav=2.4521, nav_date=date.today(), day_growth=2.15, week_growth=3.28, month_growth=8.56),
                FundInfo(fund_code="161725", fund_name="招商中证白酒", fund_type="指数型", sector_type="白酒",
                        latest_nav=1.2345, nav_date=date.today(), day_growth=0.65, week_growth=-1.25, month_growth=3.45),
                FundInfo(fund_code="519674", fund_name="银河创新成长", fund_type="混合型", sector_type="科技",
                        latest_nav=5.6789, nav_date=date.today(), day_growth=-1.35, week_growth=2.85, month_growth=6.78),
                FundInfo(fund_code="000961", fund_name="天弘沪深300", fund_type="指数型", sector_type="宽基",
                        latest_nav=1.5678, nav_date=date.today(), day_growth=0.45, week_growth=1.15, month_growth=2.89),
                FundInfo(fund_code="040011", fund_name="华安核心优选", fund_type="混合型", sector_type="核心",
                        latest_nav=2.1234, nav_date=date.today(), day_growth=1.85, week_growth=4.25, month_growth=7.12),
                FundInfo(fund_code="519778", fund_name="交银定期支付双息平衡", fund_type="混合型", sector_type="平衡",
                        latest_nav=1.8765, nav_date=date.today(), day_growth=0.25, week_growth=0.85, month_growth=1.95),
            ]
            db.add_all(funds_info)
            db.commit()
            print(f"添加了 {len(funds_info)} 只基金")
        
        # 添加基金历史净值
        existing_history = db.query(FundHistory).count()
        if existing_history == 0:
            print("添加基金历史净值...")
            history_data = []
            for fund_code, fund_name in funds:
                base_nav = 2.0 + (hash(fund_code) % 100) / 50
                for i in range(7):
                    history_data.append(FundHistory(
                        fund_code=fund_code,
                        fund_name=fund_name,
                        nav_date=date.today() - timedelta(days=6-i),
                        nav=base_nav * (1 + (i - 3) * 0.01),
                        day_growth=(i - 3) * 0.5
                    ))
            db.add_all(history_data)
            db.commit()
            print(f"添加了 {len(history_data)} 条历史净值记录")
        
        print("\n测试数据添加完成！")
        
        # 统计数据
        print(f"\n数据统计:")
        print(f"  博主: {db.query(Blogger).count()}")
        print(f"  帖子: {db.query(Post).count()}")
        print(f"  预测: {db.query(Prediction).count()}")
        print(f"  观点: {db.query(Viewpoint).count()}")
        print(f"  基金: {db.query(FundInfo).count()}")
        print(f"  历史净值: {db.query(FundHistory).count()}")
        
    finally:
        db.close()

if __name__ == "__main__":
    add_test_data()
